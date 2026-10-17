"""Anthropic provider: Claude API and Amazon Bedrock through the official SDK.

Current Claude models reject sampling parameters (temperature, top_p, top_k), so output is
steered with `effort` and adaptive thinking only. Requests always stream: long outputs would
otherwise hit HTTP timeouts, and `generate()` simply drains `stream()`.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Any

import anthropic
import httpx2  # the SDK's HTTP layer; mid-stream network failures escape as its exceptions
from botocore.exceptions import BotoCoreError, ClientError  # raised while signing for Bedrock

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

# The SDK logs whole request bodies (prompts, retrieved document text) at DEBUG and every
# request at INFO. Keep them out of our logs whatever LOG_LEVEL says, even ANTHROPIC_LOG=debug.
for _sdk_logger in ("anthropic", "httpx2"):
    logging.getLogger(_sdk_logger).setLevel(logging.WARNING)

PROVIDER = "anthropic"

# Fail fast when the API is unreachable; only reading the (possibly long) answer gets the full
# LLM_TIMEOUT_SECONDS.
_CONNECT_TIMEOUT_SECONDS = 5.0

# The longest Retry-After the SDK may sleep through. It obeys any value, so a longer one would pin
# a worker thread; instead the call fails at once with LLMRateLimitError(retry_after=...).
_MAX_RETRY_WAIT_SECONDS = 8.0

# Server-side refusal fallbacks: "default" re-runs a declined request on the model Anthropic
# recommends for that refusal category. Claude API only; Amazon Bedrock has no equivalent.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# An `error` event inside an HTTP 200 stream carries an error type but no useful status; these
# are the statuses the same errors use when they are returned before the stream starts.
_STATUS_BY_ERROR_TYPE = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "billing_error": 402,
    "permission_error": 403,
    "not_found_error": 404,
    "rate_limit_error": 429,
    "overloaded_error": 529,
}


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


def _bound_retry_wait(
    request: anthropic.APIRequest, call_next: anthropic.CallNext
) -> anthropic.APIResponse[Any]:
    """SDK middleware: stop the SDK from retrying (and sleeping) past our wait limit."""
    response = call_next(request)
    wait = _retry_after(response.headers.get("retry-after"))
    if wait is not None and wait > _MAX_RETRY_WAIT_SECONDS:
        response.headers["x-should-retry"] = "false"  # the SDK's own do-not-retry switch
    return response


def _final_message(stream: Any) -> Any:
    """The accumulated message, or None when the body held no message events at all."""
    try:
        return stream.get_final_message()
    except AssertionError:  # SDK: no message_start arrived, e.g. a proxy answering 200 with HTML
        return None


def _timeout(settings: Settings) -> anthropic.Timeout:
    return anthropic.Timeout(
        settings.llm_timeout_seconds,
        connect=min(_CONNECT_TIMEOUT_SECONDS, settings.llm_timeout_seconds),
    )


def _map_error(exc: Exception) -> LLMError | None:
    """The LLMError for a provider failure, or None for anything else (a bug: let it surface)."""
    # Subclass order matters: a timeout is also a connection error.
    if isinstance(exc, anthropic.APITimeoutError | httpx2.TimeoutException):
        return LLMTimeoutError("The Anthropic API request timed out", retryable=True)
    if isinstance(exc, anthropic.APIConnectionError | httpx2.TransportError):
        return LLMUnavailableError("Could not reach the Anthropic API", retryable=True)
    if isinstance(exc, anthropic.APIStatusError):
        return _status_error(exc)
    # Signing a Bedrock request fails before any network call when AWS credentials are missing
    # (the SDK raises a bare RuntimeError) or unusable (botocore errors).
    if isinstance(exc, BotoCoreError | ClientError) or (
        isinstance(exc, RuntimeError) and "AWS credentials" in str(exc)
    ):
        return LLMConfigurationError(
            "AWS credentials for Amazon Bedrock are missing or unusable; check the AWS "
            "credential chain (environment, profile, task or instance role)"
        )
    if isinstance(exc, anthropic.APIError):
        return LLMError("Unexpected response from the Anthropic API")
    return None


def _status_error(exc: anthropic.APIStatusError) -> LLMError:
    status = exc.status_code
    if status < 400:
        status = _STATUS_BY_ERROR_TYPE.get(exc.type or "", 500)
    if status == 429:
        return LLMRateLimitError(
            "The Anthropic API rate limit was exceeded",
            retry_after=_retry_after(exc.response.headers.get("retry-after")),
        )
    if status in (401, 403):
        return LLMConfigurationError(
            f"The Anthropic API rejected the credentials (HTTP {status}); check "
            "ANTHROPIC_API_KEY or the AWS credentials and model access"
        )
    if status == 404:
        return LLMConfigurationError(
            "The model was not found or is not available to this account; check LLM_MODEL"
        )
    if status >= 500:  # includes 529 overloaded
        return LLMUnavailableError(
            f"The Anthropic API is unavailable (HTTP {status})", retryable=True
        )
    return LLMError(f"The Anthropic API rejected the request (HTTP {status})")


class AnthropicProvider:
    provider_name = PROVIDER

    def __init__(
        self,
        client: anthropic.Anthropic | anthropic.AnthropicBedrockMantle,
        *,
        model: str,
        max_output_tokens: int,
        effort: str,
        server_fallbacks: bool,
    ) -> None:
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._effort = effort
        self._server_fallbacks = server_fallbacks

    @classmethod
    def from_settings(cls, settings: Settings) -> AnthropicProvider:
        client: anthropic.Anthropic | anthropic.AnthropicBedrockMantle
        if settings.anthropic_platform == "bedrock":
            for name in (settings.llm_model, settings.query_rewrite_model):
                if name.startswith("claude-"):  # a Claude API id, which Bedrock does not accept
                    raise _fail(
                        LLMConfigurationError(
                            f"Model {name!r} is a Claude API id; Amazon Bedrock model ids "
                            f"carry the provider prefix, e.g. 'anthropic.{name}'"
                        )
                    )
            try:
                # Credentials come from the standard AWS chain (env, profile, task or
                # instance role), never from our settings.
                client = anthropic.AnthropicBedrockMantle(
                    aws_region=settings.aws_region,
                    timeout=_timeout(settings),
                    max_retries=settings.llm_max_retries,
                    middleware=[_bound_retry_wait],
                )
            except anthropic.AnthropicError as exc:
                raise _fail(
                    LLMConfigurationError(f"Amazon Bedrock is not configured: {exc}")
                ) from exc
        else:
            key = settings.anthropic_api_key
            if key is None or not key.get_secret_value().strip():
                raise _fail(
                    LLMConfigurationError(
                        "ANTHROPIC_API_KEY is required for LLM_PROVIDER=anthropic "
                        "with ANTHROPIC_PLATFORM=anthropic"
                    )
                )
            client = anthropic.Anthropic(
                api_key=key.get_secret_value(),
                timeout=_timeout(settings),
                max_retries=settings.llm_max_retries,
                middleware=[_bound_retry_wait],
            )
        return cls(
            client,
            model=settings.llm_model,
            max_output_tokens=settings.llm_max_output_tokens,
            effort=settings.anthropic_effort,
            server_fallbacks=(
                settings.anthropic_platform == "anthropic"
                and settings.anthropic_fallbacks == "default"
            ),
        )

    @property
    def model_name(self) -> str:
        return self._model

    def generate(self, request: LLMRequest) -> LLMResponse:
        return next(
            event.response for event in self.stream(request) if isinstance(event, LLMStreamEnd)
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        model = request.model or self._model
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens or self._max_output_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": request.effort or self._effort},
        }
        if request.system:
            params["system"] = request.system
        # Haiku models have no server-side fallback; callers opt out per request (e.g. rewrites).
        use_fallbacks = (
            self._server_fallbacks and request.allow_fallbacks and "haiku" not in model.lower()
        )
        request_id: str | None = None
        try:
            manager: AbstractContextManager[Any]
            if use_fallbacks:
                manager = self._client.beta.messages.stream(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                manager = self._client.messages.stream(**params)
            # Leaving the block (end, error or generator.close()) closes the HTTP response.
            with manager as stream:
                for text in stream.text_stream:  # thinking blocks are not answer text
                    if text:
                        yield LLMTextDelta(text)
                message = _final_message(stream)
                request_id = stream.request_id
        except Exception as exc:
            error = _map_error(exc)
            if error is None:
                raise
            # Status, error type and request id identify the failure without any message text,
            # which could echo request content.
            raise _fail(
                error,
                model=model,
                purpose=request.purpose,
                error_class=type(exc).__name__,
                status=getattr(exc, "status_code", None),
                error_type=getattr(exc, "type", None),
                provider_request_id=getattr(exc, "request_id", None),
            ) from exc
        if message is None or message.stop_reason is None:  # no events, or cut off before the end
            raise _fail(
                LLMUnavailableError(
                    "The Anthropic stream ended early or was malformed", retryable=True
                ),
                model=model,
                purpose=request.purpose,
                provider_request_id=request_id,
            )
        yield LLMStreamEnd(
            LLMResponse(
                text="".join(block.text for block in message.content if block.type == "text"),
                model=message.model or model,  # differs from `model` after a refusal fallback
                # ponytail: top-level usage covers only the attempt that answered; after a refusal
                # fallback, sum message.usage.iterations if the declined attempt's cost matters.
                usage=LLMUsage(
                    input_tokens=message.usage.input_tokens,
                    output_tokens=message.usage.output_tokens,
                ),
                stop_reason=message.stop_reason,
                request_id=request_id,
            )
        )
