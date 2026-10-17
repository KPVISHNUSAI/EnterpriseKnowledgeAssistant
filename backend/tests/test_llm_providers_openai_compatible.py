"""OpenAI-compatible provider (Ollama, vLLM, LM Studio) over a fake HTTP transport, plus an
opt-in live test against a local Ollama server (skipped unless one answers)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from httpx import Client as RealClient
from prometheus_client import REGISTRY
from pydantic import SecretStr

import app.providers.llm.openai_compatible as module
from app.core.config import Settings, get_settings
from app.providers.llm import (
    LLMConfigurationError,
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMRateLimitError,
    LLMRequest,
    LLMStreamEnd,
    LLMTextDelta,
    LLMTimeoutError,
    LLMUnavailableError,
    build_llm,
)
from app.providers.llm.openai_compatible import OpenAICompatibleProvider

URL = "http://llm.test/v1/chat/completions"
API_KEY = "llm-secret-key-0123456789"
SECRET_PROMPT = "TOP-SECRET-PROMPT-TEXT"


# --- Wire builders (OpenAI chat.completion.chunk server-sent events) ---------------------------


def chunk(
    content: str | None = None,
    *,
    finish: str | None = None,
    usage: dict | None = None,
    choices: bool = True,
    delta: dict | None = None,
) -> dict:
    if delta is None:
        delta = {} if content is None else {"content": content}
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "llama3.1:8b",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if choices else [],
        "usage": usage,
    }


def sse(*items: dict | str) -> bytes:
    return "".join(
        f"data: {i if isinstance(i, str) else json.dumps(i)}\n\n" for i in items
    ).encode()


USAGE = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}

HAPPY = sse(
    chunk(delta={"role": "assistant", "content": ""}),
    chunk("Hello"),
    chunk(" world"),
    chunk(finish="stop"),
    chunk(usage=USAGE, choices=False),  # include_usage: a last chunk with no choices
    "[DONE]",
)


def stream_reply(content: bytes = HAPPY) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "x-request-id": "req-42"},
        content=content,
    )


def status_reply(status: int, **headers: str) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(status, headers=headers, json={"error": {"message": API_KEY}})


def completion_reply(
    text: str | None = "Hi there", finish: str | None = "stop", usage: dict | None = USAGE
) -> Callable[[], httpx.Response]:
    body: dict[str, Any] = {
        "id": "chatcmpl-9",
        "model": "llama3.1:8b",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return lambda: httpx.Response(200, headers={"x-request-id": "req-43"}, json=body)


class Script:
    """httpx transport handler playing back replies in order (the last one repeats).

    A reply is a zero-argument factory for a response, or an exception to raise."""

    def __init__(self, *replies: Callable[[], httpx.Response] | Exception) -> None:
        self.replies = replies
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply()

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def make_provider(
    script: Script, *, max_retries: int = 2, temperature: float | None = 0.1
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        RealClient(transport=httpx.MockTransport(script)),
        url=URL,
        model="llama3.1:8b",
        max_output_tokens=500,
        temperature=temperature,
        max_retries=max_retries,
    )


def request(**kwargs: Any) -> LLMRequest:
    kwargs.setdefault("system", "You answer from the passages.")
    kwargs.setdefault("messages", [LLMMessage("user", SECRET_PROMPT)])
    return LLMRequest(**kwargs)


def settings_with(**updates: Any) -> Settings:
    base = {"llm_provider": "openai_compatible", "llm_base_url": "http://llm.test/v1"}
    return get_settings().model_copy(update={**base, **updates})


def errors_counted(kind: str) -> float:
    labels = {"provider": "openai_compatible", "kind": kind}
    return REGISTRY.get_sample_value("eka_llm_errors_total", labels) or 0.0


def logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every log record including its `extra` fields."""
    return "\n".join(str(record.__dict__) for record in caplog.records)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff delays instead of sleeping."""
    delays: list[float] = []
    monkeypatch.setattr(module.time, "sleep", delays.append)
    return delays


# --- Streaming --------------------------------------------------------------------------------


def test_stream_parses_deltas_usage_and_done():
    provider = make_provider(Script(stream_reply()))
    assert isinstance(provider, LLMProvider)

    events = list(provider.stream(request()))

    assert events[:2] == [LLMTextDelta("Hello"), LLMTextDelta(" world")]
    assert len(events) == 3
    end = events[2]
    assert isinstance(end, LLMStreamEnd)
    assert end.response.text == "Hello world"
    assert end.response.model == "llama3.1:8b"
    assert end.response.stop_reason == "end_turn"
    assert (end.response.usage.input_tokens, end.response.usage.output_tokens) == (11, 5)
    assert end.response.request_id == "req-42"


def test_usage_in_the_final_choices_chunk_is_used_too():
    body = sse(chunk("Hi"), chunk(finish="stop", usage=USAGE), "[DONE]")
    events = list(make_provider(Script(stream_reply(body))).stream(request()))
    assert events[-1].response.usage.input_tokens == 11


@pytest.mark.parametrize(
    ("finish", "stop_reason"),
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("content_filter", "refusal"),
        ("other", "other"),
    ],
)
def test_finish_reasons_use_the_shared_stop_reason_vocabulary(finish, stop_reason):
    body = sse(chunk("x"), chunk(finish=finish), chunk(usage=USAGE, choices=False), "[DONE]")
    events = list(make_provider(Script(stream_reply(body))).stream(request()))
    assert events[-1].response.stop_reason == stop_reason


def test_reasoning_text_comments_and_other_event_lines_are_not_answer_text():
    body = b": keep-alive\n\nevent: message\n" + sse(
        chunk(delta={"reasoning_content": "thinking about it"}),
        chunk("Answer"),
        chunk(finish="stop"),
        "[DONE]",
    )
    events = list(make_provider(Script(stream_reply(body))).stream(request()))
    assert [e.text for e in events if isinstance(e, LLMTextDelta)] == ["Answer"]
    assert events[-1].response.text == "Answer"


def test_stream_that_ends_after_finish_reason_without_done_is_complete():
    body = sse(chunk("Hi"), chunk(finish="stop"))
    events = list(make_provider(Script(stream_reply(body))).stream(request()))
    assert events[-1].response.text == "Hi"


def test_missing_usage_is_estimated_and_logged(caplog):
    caplog.set_level(logging.INFO)
    body = sse(chunk("12345678"), chunk(finish="stop"), "[DONE]")  # 8 chars of answer
    provider = make_provider(Script(stream_reply(body)))

    events = list(provider.stream(request(system="a" * 10, messages=[LLMMessage("user", "b" * 6)])))

    usage = events[-1].response.usage
    assert (usage.input_tokens, usage.output_tokens) == (4, 2)  # ceil(16 / 4), ceil(8 / 4)
    estimates = [r for r in caplog.records if r.getMessage() == "llm.usage_estimated"]
    assert len(estimates) == 1
    assert estimates[0].usage_estimated is True


def test_reported_usage_is_not_flagged_as_an_estimate(caplog):
    caplog.set_level(logging.INFO)
    list(make_provider(Script(stream_reply())).stream(request()))
    assert not [r for r in caplog.records if r.getMessage() == "llm.usage_estimated"]


@pytest.mark.parametrize(
    "body",
    [
        sse(chunk("partial")),  # connection closed mid-answer: no finish_reason, no [DONE]
        b"",
    ],
)
def test_truncated_stream_is_an_error_not_a_short_answer(body):
    with pytest.raises(LLMUnavailableError) as raised:
        list(make_provider(Script(stream_reply(body))).stream(request()))
    assert raised.value.retryable


@pytest.mark.parametrize(
    "bad",
    ['{"error": {"message": "CUDA out of memory"}}', "not json", "[1, 2]"],
)
def test_error_or_malformed_chunks_are_unavailable_errors(bad):
    body = sse(chunk("Hi"), bad)
    events = []
    with pytest.raises(LLMUnavailableError):
        for event in make_provider(Script(stream_reply(body))).stream(request()):
            events.append(event)
    assert events == [LLMTextDelta("Hi")]


class TrackedStream(httpx.SyncByteStream):
    """One SSE event per chunk, remembering whether the response was released."""

    def __init__(self, items: list[dict | str], failure: Exception | None = None) -> None:
        self.chunks = [sse(item) for item in items]
        self.failure = failure
        self.delivered = 0
        self.closed = False

    def __iter__(self):
        for data in self.chunks:
            self.delivered += 1
            yield data
        if self.failure:
            raise self.failure

    def close(self) -> None:
        self.closed = True


def tracked_reply(stream: TrackedStream) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


def test_closing_the_stream_early_releases_the_http_response():
    stream = TrackedStream(
        [chunk("one"), chunk("two"), chunk("three"), chunk(finish="stop"), "[DONE]"]
    )
    generator = make_provider(Script(tracked_reply(stream))).stream(request())

    assert next(generator) == LLMTextDelta("one")
    assert not stream.closed
    generator.close()  # what the chat endpoint does when the user disconnects

    assert stream.closed
    assert stream.delivered < len(stream.chunks)


def test_a_fully_consumed_stream_is_released_too():
    stream = TrackedStream([chunk("x"), chunk(finish="stop"), "[DONE]"])
    list(make_provider(Script(tracked_reply(stream))).stream(request()))
    assert stream.closed


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ReadTimeout("read timed out"), LLMTimeoutError),
        (httpx.RemoteProtocolError("peer closed connection"), LLMUnavailableError),
    ],
)
def test_failure_after_streaming_started_is_mapped_and_never_retried(failure, expected, sleeps):
    stream = TrackedStream([chunk("Hi")], failure=failure)
    script = Script(tracked_reply(stream))
    events = []
    with pytest.raises(expected) as raised:
        for event in make_provider(script, max_retries=3).stream(request()):
            events.append(event)

    assert events == [LLMTextDelta("Hi")]
    assert raised.value.retryable
    assert len(script.requests) == 1  # deltas were already delivered: a retry would duplicate them
    assert sleeps == []
    assert stream.closed


# --- Request construction ---------------------------------------------------------------------


def test_stream_request_shape():
    script = Script(stream_reply())
    list(make_provider(script).stream(request()))

    sent = script.requests[-1]
    assert (sent.method, str(sent.url)) == ("POST", URL)
    assert "authorization" not in sent.headers  # no key configured on this client
    assert script.body == {
        "model": "llama3.1:8b",
        "messages": [
            {"role": "system", "content": "You answer from the passages."},
            {"role": "user", "content": SECRET_PROMPT},
        ],
        "max_tokens": 500,
        "temperature": 0.1,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def test_request_overrides_model_and_max_tokens_and_ignores_anthropic_knobs():
    script = Script(stream_reply())
    list(
        make_provider(script).stream(
            request(model="qwen2.5:3b", max_output_tokens=64, effort="max", allow_fallbacks=False)
        )
    )
    assert script.body["model"] == "qwen2.5:3b"
    assert script.body["max_tokens"] == 64
    assert "effort" not in script.body


def test_system_message_is_omitted_when_empty_and_history_keeps_its_order():
    script = Script(stream_reply())
    messages = [LLMMessage("user", "q1"), LLMMessage("assistant", "a1"), LLMMessage("user", "q2")]
    list(make_provider(script).stream(request(system="", messages=messages)))
    assert [m["role"] for m in script.body["messages"]] == ["user", "assistant", "user"]


@pytest.mark.parametrize(
    ("per_request", "default", "expected"),
    [(None, 0.1, 0.1), (0.7, 0.1, 0.7), (0.0, 0.1, 0.0), (None, None, None), (0.3, None, 0.3)],
)
def test_temperature_request_beats_default_and_zero_is_respected(per_request, default, expected):
    script = Script(stream_reply())
    list(make_provider(script, temperature=default).stream(request(temperature=per_request)))
    if expected is None:
        assert "temperature" not in script.body
    else:
        assert script.body["temperature"] == expected


# --- generate() -------------------------------------------------------------------------------


def test_generate_is_a_plain_non_streaming_request():
    script = Script(completion_reply())
    response = make_provider(script).generate(request(purpose="query_rewrite"))

    assert response.text == "Hi there"
    assert response.model == "llama3.1:8b"
    assert response.stop_reason == "end_turn"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (11, 5)
    assert response.request_id == "req-43"
    assert script.body["stream"] is False
    assert "stream_options" not in script.body


def test_generate_estimates_usage_when_the_server_omits_it(caplog):
    caplog.set_level(logging.INFO)
    response = make_provider(Script(completion_reply("abcdefgh", usage=None))).generate(request())
    assert response.usage.output_tokens == 2
    assert [r for r in caplog.records if getattr(r, "usage_estimated", False)]


def test_generate_treats_null_content_as_empty_and_maps_length():
    response = make_provider(Script(completion_reply(None, finish="length"))).generate(request())
    assert response.text == ""
    assert response.stop_reason == "max_tokens"


@pytest.mark.parametrize(
    "reply",
    [
        lambda: httpx.Response(200, content=b"<html>proxy error</html>"),
        lambda: httpx.Response(200, json={"choices": []}),
        lambda: httpx.Response(200, json=["not", "an", "object"]),
    ],
)
def test_generate_rejects_unexpected_response_bodies(reply):
    with pytest.raises(LLMUnavailableError):
        make_provider(Script(reply), max_retries=0).generate(request())


# --- Retries and error mapping ----------------------------------------------------------------


def test_transient_status_is_retried_with_exponential_backoff_until_it_succeeds(sleeps):
    script = Script(status_reply(503), status_reply(502), stream_reply())
    events = list(make_provider(script).stream(request()))

    assert isinstance(events[-1], LLMStreamEnd)
    assert len(script.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_retries_are_bounded_and_the_last_error_is_raised(sleeps):
    script = Script(status_reply(503))
    with pytest.raises(LLMUnavailableError) as raised:
        list(make_provider(script, max_retries=2).stream(request()))
    assert raised.value.retryable
    assert len(script.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_backoff_delay_is_capped(sleeps):
    with pytest.raises(LLMUnavailableError):
        make_provider(Script(status_reply(500)), max_retries=7).generate(request())
    assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_zero_retries_means_one_attempt(sleeps):
    script = Script(status_reply(503))
    with pytest.raises(LLMUnavailableError):
        make_provider(script, max_retries=0).generate(request())
    assert len(script.requests) == 1
    assert sleeps == []


def test_retry_after_is_honoured_when_longer_than_the_backoff(sleeps):
    script = Script(status_reply(429, **{"retry-after": "3"}), stream_reply())
    list(make_provider(script).stream(request()))
    assert sleeps == [3.0]


def test_backoff_wins_over_a_shorter_retry_after(sleeps):
    script = Script(status_reply(429, **{"retry-after": "0"}), stream_reply())
    list(make_provider(script).stream(request()))
    assert sleeps == [0.5]


def test_retry_after_longer_than_we_will_wait_is_raised_immediately(sleeps):
    script = Script(status_reply(429, **{"retry-after": "120"}), stream_reply())
    with pytest.raises(LLMRateLimitError) as raised:
        list(make_provider(script).stream(request()))
    assert raised.value.retry_after == 120.0
    assert len(script.requests) == 1
    assert sleeps == []


def test_rate_limit_without_a_usable_retry_after(sleeps):
    with pytest.raises(LLMRateLimitError) as raised:
        make_provider(Script(status_reply(429, **{"retry-after": "soon"})), max_retries=0).generate(
            request()
        )
    assert raised.value.retry_after is None
    assert raised.value.retryable


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ConnectError("connection refused"), LLMUnavailableError),
        (httpx.ReadTimeout("timed out"), LLMTimeoutError),
    ],
)
def test_connection_failures_are_retried_then_mapped(failure, expected, sleeps):
    script = Script(failure)
    with pytest.raises(expected) as raised:
        list(make_provider(script, max_retries=2).stream(request()))
    assert raised.value.retryable
    assert type(raised.value) is expected
    assert len(script.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_a_dropped_connection_is_retried_and_recovers(sleeps):
    script = Script(httpx.ConnectError("stale keep-alive connection"), stream_reply())
    events = list(make_provider(script).stream(request()))
    assert isinstance(events[-1], LLMStreamEnd)
    assert len(script.requests) == 2


@pytest.mark.parametrize(
    ("status", "expected", "kind"),
    [
        (400, LLMError, "error"),
        (413, LLMError, "error"),
        (401, LLMConfigurationError, "configuration"),
        (403, LLMConfigurationError, "configuration"),
        (404, LLMConfigurationError, "configuration"),
        (301, LLMConfigurationError, "configuration"),  # redirects are not followed
    ],
)
def test_client_errors_are_not_retried(status, expected, kind, sleeps):
    before = errors_counted(kind)
    script = Script(status_reply(status))
    with pytest.raises(LLMError) as raised:
        list(make_provider(script).stream(request()))

    assert type(raised.value) is expected
    assert not raised.value.retryable
    assert raised.value.kind == kind
    assert len(script.requests) == 1
    assert sleeps == []
    assert errors_counted(kind) == before + 1


def test_each_failed_call_is_counted_once_not_once_per_retry(sleeps):
    before = errors_counted("unavailable")
    with pytest.raises(LLMUnavailableError):
        make_provider(Script(status_reply(503)), max_retries=2).generate(request())
    assert errors_counted("unavailable") == before + 1


def test_timeouts_and_rate_limits_are_counted_with_their_kind(sleeps):
    timeouts, limits = errors_counted("timeout"), errors_counted("rate_limited")
    with pytest.raises(LLMTimeoutError):
        make_provider(Script(httpx.ReadTimeout("t")), max_retries=0).generate(request())
    with pytest.raises(LLMRateLimitError):
        make_provider(Script(status_reply(429)), max_retries=0).generate(request())
    assert errors_counted("timeout") == timeouts + 1
    assert errors_counted("rate_limited") == limits + 1


# --- Construction from settings ---------------------------------------------------------------


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Route the client built inside `from_settings` through a fake transport."""
    script = Script(stream_reply())
    seen: dict[str, Any] = {"script": script}

    def client(**kwargs: Any) -> httpx.Client:
        seen.update(kwargs)
        return RealClient(**kwargs, transport=httpx.MockTransport(script))

    monkeypatch.setattr(httpx, "Client", client)
    return seen


def test_from_settings_normalises_the_url_and_wires_timeout_retries_and_temperature(wired):
    settings = settings_with(
        llm_base_url=" http://llm.test:8000/v1/ ",
        llm_model="qwen2.5:3b",
        llm_timeout_seconds=42.0,
        llm_temperature=0.4,
        llm_max_output_tokens=321,
        llm_max_retries=0,
    )
    provider = OpenAICompatibleProvider.from_settings(settings)
    assert (provider.provider_name, provider.model_name) == ("openai_compatible", "qwen2.5:3b")

    list(provider.stream(request()))

    script = wired["script"]
    assert str(script.requests[-1].url) == "http://llm.test:8000/v1/chat/completions"
    assert wired["timeout"].read == 42.0  # reading the answer gets the configured timeout ...
    assert wired["timeout"].connect == 5.0  # ... connecting fails fast
    assert script.body["model"] == "qwen2.5:3b"
    assert script.body["max_tokens"] == 321
    assert script.body["temperature"] == 0.4


def test_bearer_token_is_sent_only_when_an_api_key_is_configured(wired):
    with_key = OpenAICompatibleProvider.from_settings(settings_with(llm_api_key=SecretStr(API_KEY)))
    list(with_key.stream(request()))
    assert wired["script"].requests[-1].headers["authorization"] == f"Bearer {API_KEY}"

    for blank in (None, SecretStr("  ")):
        without = OpenAICompatibleProvider.from_settings(settings_with(llm_api_key=blank))
        list(without.stream(request()))
        assert "authorization" not in wired["script"].requests[-1].headers


@pytest.mark.parametrize(
    "base_url",
    [None, "", "   ", "localhost:11434/v1", "ftp://llm.test/v1", "http://", "not a url"],
)
def test_missing_or_invalid_base_url_is_a_configuration_error(base_url):
    before = errors_counted("configuration")
    with pytest.raises(LLMConfigurationError, match="LLM_BASE_URL") as raised:
        OpenAICompatibleProvider.from_settings(settings_with(llm_base_url=base_url))
    assert not raised.value.retryable
    assert errors_counted("configuration") == before + 1


def test_build_llm_selects_the_openai_compatible_provider():
    assert isinstance(build_llm(settings_with()), OpenAICompatibleProvider)


# --- Secrets stay out of logs and errors ------------------------------------------------------


def test_prompts_answers_and_keys_are_never_logged_or_put_in_errors(caplog, sleeps):
    caplog.set_level(logging.DEBUG)
    answer = sse(chunk("A-SECRET-ANSWER"), chunk(finish="stop"), "[DONE]")

    list(make_provider(Script(stream_reply(answer))).stream(request()))  # no usage -> estimate log
    with pytest.raises(LLMError) as raised:
        make_provider(Script(status_reply(401))).generate(request())
    with pytest.raises(LLMError):
        make_provider(Script(status_reply(503)), max_retries=1).generate(request())  # retry log

    text = logged(caplog) + str(raised.value)
    assert "llm.error" in text and "llm.retry" in text and "llm.usage_estimated" in text
    for secret in (SECRET_PROMPT, "A-SECRET-ANSWER", API_KEY):
        assert secret not in text  # the fake server's error body echoes the key; it is never copied


# --- Live (opt-in): a local Ollama server -----------------------------------------------------

OLLAMA_URL = os.environ.get("EKA_TEST_OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("EKA_TEST_OLLAMA_MODEL", "llama3.1:8b")


@pytest.fixture(scope="module")
def ollama() -> OpenAICompatibleProvider:
    """A provider for the local Ollama server; skips unless the server answers a request.

    The server is often shared and busy (requests queue behind other clients), so a server
    that cannot answer a one-token warm-up within the timeout counts as "not answering"."""
    try:
        listing = httpx.get(f"{OLLAMA_URL}/models", timeout=5.0).json()
        models = {entry["id"] for entry in listing["data"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        pytest.skip(f"no OpenAI-compatible server answering at {OLLAMA_URL}")
    if OLLAMA_MODEL not in models:
        pytest.skip(f"model {OLLAMA_MODEL} is not available on the server (ollama pull it)")
    provider = OpenAICompatibleProvider.from_settings(
        settings_with(
            llm_base_url=OLLAMA_URL,
            llm_model=OLLAMA_MODEL,
            llm_timeout_seconds=90.0,  # the first request may have to load the model
            llm_max_retries=0,
            llm_api_key=None,
        )
    )
    try:
        provider.generate(
            request(system="", messages=[LLMMessage("user", "hi")], max_output_tokens=1)
        )
    except (LLMTimeoutError, LLMUnavailableError) as exc:
        pytest.skip(f"{OLLAMA_URL} did not answer a warm-up request in time: {exc}")
    return provider


@pytest.mark.slow
def test_live_ollama_streams_a_short_completion(ollama):
    live = LLMRequest(
        system="Answer in one short sentence.",
        messages=[LLMMessage("user", "Name one colour.")],
        max_output_tokens=40,
        temperature=0.0,
    )
    events = list(ollama.stream(live))

    deltas = [e.text for e in events if isinstance(e, LLMTextDelta)]
    end = events[-1]
    assert deltas, "expected at least one streamed delta"
    assert isinstance(end, LLMStreamEnd)
    assert end.response.text == "".join(deltas)
    assert end.response.text.strip()
    assert end.response.stop_reason in {"end_turn", "max_tokens"}
    assert end.response.usage.input_tokens > 0
    assert end.response.usage.output_tokens > 0
    assert end.response.model == OLLAMA_MODEL


@pytest.mark.slow
def test_live_ollama_generate_for_query_rewriting(ollama):
    response = ollama.generate(
        LLMRequest(
            system="Rewrite the question so it stands alone. Reply with the question only.",
            messages=[LLMMessage("user", "And what about its price?")],
            max_output_tokens=60,
            temperature=0.0,
            purpose="query_rewrite",
        )
    )
    assert response.text.strip()
    assert response.usage.output_tokens > 0
