"""OpenAI-compatible chat-completions provider (Ollama, vLLM, LM Studio, ...) over httpx.

No SDK on purpose: these servers implement a small, stable subset of the OpenAI protocol and
httpx is already a dependency. Streaming uses server-sent events; token usage arrives in a
final chunk when the server honours `stream_options.include_usage`.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Iterator
from typing import Any

import httpx

from app.core.config import Settings
from app.core.metrics import LLM_ERRORS
from app.providers.llm.base import (
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMStreamEnd,
    LLMStreamEvent,
    LLMTextDelta,
    LLMTimeoutError,
    LLMUnavailableError,
    LLMUsage,
)

logger = logging.getLogger(__name__)

PROVIDER = "openai_compatible"

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 8.0
# Fail fast when the server is unreachable; only reading the (possibly long) answer gets the
# full LLM_TIMEOUT_SECONDS.
_CONNECT_TIMEOUT_SECONDS = 5.0

# OpenAI finish_reason -> the stop_reason vocabulary shared with the Anthropic provider.
_STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


def _fail(error: LLMError, **context: object) -> LLMError:
    """Count and log a provider error (never prompts, answers or keys), then hand it back."""
    LLM_ERRORS.labels(provider=PROVIDER, kind=error.kind).inc()
    logger.warning(
        "llm.error",
        extra={"provider": PROVIDER, "kind": error.kind, "detail": str(error), **context},
    )
    return error


def _retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header; the HTTP-date form is not worth parsing here."""
    try:
        seconds = float(value or "")
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _status_error(response: httpx.Response) -> LLMError:
    status = response.status_code
    if status == 429:
        return LLMRateLimitError(
            "The LLM server rate limit was exceeded",
            retry_after=_retry_after(response.headers.get("retry-after")),
        )
    if status in (401, 403):
        return LLMConfigurationError(
            f"The LLM server rejected the credentials (HTTP {status}); check LLM_API_KEY"
        )
    if status == 404:
        return LLMConfigurationError(
            "The LLM server returned HTTP 404; check LLM_BASE_URL and that LLM_MODEL exists there"
        )
    if 300 <= status < 400:  # redirects are deliberately not followed (the bearer token)
        return LLMConfigurationError(
            f"The LLM server redirected the request (HTTP {status}); point LLM_BASE_URL at the "
            "final URL"
        )
    if status >= 500:
        return LLMUnavailableError(f"The LLM server is unavailable (HTTP {status})", retryable=True)
    return LLMError(f"The LLM server rejected the request (HTTP {status})")


def _transport_error(exc: httpx.TransportError) -> LLMError:
    if isinstance(exc, httpx.TimeoutException):
        return LLMTimeoutError("The LLM server timed out", retryable=True)
    return LLMUnavailableError("Could not reach the LLM server", retryable=True)


def _estimate_tokens(chars: int) -> int:
    return (chars + 3) // 4


def _usage(raw: Any, request: LLMRequest, text: str) -> LLMUsage:
    """The server's usage block, or a rough chars/4 estimate when it omits one."""
    if isinstance(raw, dict):
        prompt, completion = raw.get("prompt_tokens"), raw.get("completion_tokens")
        if isinstance(prompt, int) and isinstance(completion, int):
            return LLMUsage(input_tokens=prompt, output_tokens=completion)
    logger.info("llm.usage_estimated", extra={"provider": PROVIDER, "usage_estimated": True})
    prompt_chars = len(request.system) + sum(len(m.content) for m in request.messages)
    return LLMUsage(
        input_tokens=_estimate_tokens(prompt_chars), output_tokens=_estimate_tokens(len(text))
    )


def _sse_data(response: httpx.Response) -> Iterator[str]:
    """Payloads of the `data:` lines of a server-sent-events response."""
    for line in response.iter_lines():
        if line.startswith("data:"):
            yield line[5:].strip()


def _parse_chunk(data: str) -> dict[str, Any]:
    try:
        chunk = json.loads(data)
    except ValueError as exc:
        raise _fail(
            LLMUnavailableError("Malformed streaming response from the LLM server", retryable=True)
        ) from exc
    if not isinstance(chunk, dict):
        raise _fail(
            LLMUnavailableError("Malformed streaming response from the LLM server", retryable=True)
        )
    if "error" in chunk:  # servers report a mid-stream failure as an error chunk in a 200
        raise _fail(
            LLMUnavailableError("The LLM server reported an error while streaming", retryable=True)
        )
    return chunk


class OpenAICompatibleProvider:
    provider_name = PROVIDER

    def __init__(
        self,
        client: httpx.Client,
        *,
        url: str,
        model: str,
        max_output_tokens: int,
        temperature: float | None,
        max_retries: int,
    ) -> None:
        self._client = client
        self._url = url
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._max_retries = max_retries

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatibleProvider:
        base_url = (settings.llm_base_url or "").strip().rstrip("/")
        if not base_url:
            raise _fail(
                LLMConfigurationError(
                    "LLM_BASE_URL is required when LLM_PROVIDER=openai_compatible"
                )
            )
        try:
            parsed = httpx.URL(base_url)
        except httpx.InvalidURL as exc:
            raise _fail(LLMConfigurationError("LLM_BASE_URL is not a valid URL")) from exc
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise _fail(
                LLMConfigurationError("LLM_BASE_URL must be an http(s) URL of the server's API")
            )
        headers = {}
        api_key = settings.llm_api_key.get_secret_value().strip() if settings.llm_api_key else ""
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # Redirects stay off (httpx default) so the bearer token never follows one elsewhere.
        timeout = httpx.Timeout(
            settings.llm_timeout_seconds,
            connect=min(_CONNECT_TIMEOUT_SECONDS, settings.llm_timeout_seconds),
        )
        client = httpx.Client(headers=headers, timeout=timeout)
        return cls(
            client,
            url=f"{base_url}/chat/completions",
            model=settings.llm_model,
            max_output_tokens=settings.llm_max_output_tokens,
            temperature=settings.llm_temperature,
            max_retries=settings.llm_max_retries,
        )

    @property
    def model_name(self) -> str:
        return self._model

    def generate(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self._model
        response = self._open(self._body(request, stream=False))
        try:
            response.read()
        except httpx.TransportError as exc:
            raise _fail(_transport_error(exc), model=model, purpose=request.purpose) from exc
        finally:
            response.close()
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            finish = choice.get("finish_reason")
            usage = data.get("usage")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise _fail(
                LLMUnavailableError("Unexpected response from the LLM server", retryable=True),
                model=model,
                purpose=request.purpose,
            ) from exc
        return LLMResponse(
            text=text,
            model=model,
            usage=_usage(usage, request, text),
            stop_reason=_STOP_REASONS.get(finish, finish) if finish else None,
            request_id=response.headers.get("x-request-id"),
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        model = request.model or self._model
        response = self._open(self._body(request, stream=True))
        parts: list[str] = []
        finish: str | None = None
        usage: Any = None
        completed = False
        try:
            for data in _sse_data(response):
                if data == "[DONE]":
                    completed = True
                    break
                chunk = _parse_chunk(data)
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or ():
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:  # reasoning models also send `reasoning_content`; not answer text
                        parts.append(piece)
                        yield LLMTextDelta(piece)
                    finish = choice.get("finish_reason") or finish
        except httpx.TransportError as exc:  # after the first byte nothing is retried
            raise _fail(_transport_error(exc), model=model, purpose=request.purpose) from exc
        finally:
            response.close()  # also runs on generator.close(), releasing the connection
        if not (completed or finish):  # connection closed mid-answer
            raise _fail(
                LLMUnavailableError("The LLM server ended the stream early", retryable=True),
                model=model,
                purpose=request.purpose,
            )
        text = "".join(parts)
        yield LLMStreamEnd(
            LLMResponse(
                text=text,
                model=model,
                usage=_usage(usage, request, text),
                stop_reason=_STOP_REASONS.get(finish, finish) if finish else None,
                request_id=response.headers.get("x-request-id"),
            )
        )

    def _body(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        messages = [{"role": "system", "content": request.system}] if request.system else []
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or self._max_output_tokens,
            "stream": stream,
        }
        temperature = self._temperature if request.temperature is None else request.temperature
        if temperature is not None:
            body["temperature"] = temperature
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _open(self, body: dict[str, Any]) -> httpx.Response:
        """POST the request, retrying 429/5xx/connection failures with exponential backoff
        until a 2xx response arrives. Nothing is retried after that: deltas may be out."""
        attempt = 0
        while True:
            try:
                return self._send(body)
            except LLMError as error:
                if not error.retryable or attempt >= self._max_retries:
                    _fail(error, model=body["model"])
                    raise
                delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * 2**attempt)
                if isinstance(error, LLMRateLimitError) and error.retry_after is not None:
                    if error.retry_after > _BACKOFF_MAX_SECONDS:
                        # Too long to hold a worker; let the caller decide what to do.
                        _fail(error, model=body["model"])
                        raise
                    delay = max(delay, error.retry_after)
                logger.info(
                    "llm.retry",
                    extra={"provider": PROVIDER, "kind": error.kind, "attempt": attempt + 1},
                )
                # ponytail: no jitter; add if many workers retry against one rate-limited gateway
                time.sleep(delay)
                attempt += 1

    def _send(self, body: dict[str, Any]) -> httpx.Response:
        """One attempt: the open response, or the mapped error for a transport failure or a
        non-2xx status."""
        try:
            response = self._client.send(
                self._client.build_request("POST", self._url, json=body), stream=True
            )
        except httpx.TransportError as exc:
            raise _transport_error(exc) from exc
        if not response.is_success:
            response.close()
            raise _status_error(response)
        return response
