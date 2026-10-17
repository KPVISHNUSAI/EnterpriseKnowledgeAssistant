"""Anthropic provider: the real SDK driven over a fake HTTP transport (nothing leaves the
machine and no API key is needed). Live Claude/Bedrock calls are deliberately not tested here."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
from prometheus_client import REGISTRY
from pydantic import SecretStr

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
from app.providers.llm.anthropic import FALLBACK_BETA, AnthropicProvider

API_KEY = "sk-ant-test-key-0123456789"
SECRET_PROMPT = "TOP-SECRET-PROMPT-TEXT"


# --- SSE builders (the Messages API streaming wire format) ------------------------------------


def sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def message_start(model: str = "claude-opus-5-5", input_tokens: int = 25) -> tuple[str, dict]:
    return (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        },
    )


def block(index: int, content_block: dict, *deltas: dict) -> list[tuple[str, dict]]:
    return [
        (
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": content_block},
        ),
        *(
            ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": d})
            for d in deltas
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": index}),
    ]


def text_block(index: int, *pieces: str) -> list[tuple[str, dict]]:
    return block(
        index,
        {"type": "text", "text": ""},
        *({"type": "text_delta", "text": p} for p in pieces),
    )


# Adaptive thinking: an (empty, "omitted") thinking block precedes the answer.
THINKING = block(
    0,
    {"type": "thinking", "thinking": "", "signature": ""},
    {"type": "signature_delta", "signature": "sig"},
)


def message_end(stop_reason: str = "end_turn", output_tokens: int = 15) -> list[tuple[str, dict]]:
    return [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


def answer(*pieces: str, stop_reason: str = "end_turn", model: str = "claude-opus-5-5") -> bytes:
    return sse(message_start(model), *THINKING, *text_block(1, *pieces), *message_end(stop_reason))


def stream_response(content: bytes, request_id: str = "req_test_1") -> httpx2.Response:
    headers = {"content-type": "text/event-stream", "request-id": request_id}
    return httpx2.Response(200, headers=headers, content=content)


def error_response(status: int, error_type: str, **headers: str) -> httpx2.Response:
    body = {"type": "error", "error": {"type": error_type, "message": f"{API_KEY} {SECRET_PROMPT}"}}
    return httpx2.Response(status, headers={"request-id": "req_err_1", **headers}, json=body)


# --- Fake API + provider factories ------------------------------------------------------------


class FakeApi:
    """httpx2 transport handler: records requests, answers with `respond(request)`."""

    def __init__(self, respond: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.respond = respond
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.respond(request)

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)

    @property
    def beta_header(self) -> str | None:
        return self.requests[-1].headers.get("anthropic-beta")


def answering(*pieces: str, **kwargs: Any) -> FakeApi:
    return FakeApi(lambda _: stream_response(answer(*pieces or ("ok",), **kwargs)))


def failing(status: int, error_type: str, **headers: str) -> FakeApi:
    return FakeApi(lambda _: error_response(status, error_type, **headers))


def make_provider(api: FakeApi, *, server_fallbacks: bool = True) -> AnthropicProvider:
    client = anthropic.Anthropic(
        api_key=API_KEY,
        max_retries=0,  # the SDK's own retries (and their sleeps) are not under test
        http_client=httpx2.Client(transport=httpx2.MockTransport(api)),
    )
    return AnthropicProvider(
        client,
        model="claude-opus-5-5",
        max_output_tokens=4000,
        effort="medium",
        server_fallbacks=server_fallbacks,
    )


def request(**kwargs: Any) -> LLMRequest:
    kwargs.setdefault("system", "You answer from the passages.")
    kwargs.setdefault("messages", [LLMMessage("user", SECRET_PROMPT)])
    return LLMRequest(**kwargs)


def settings_with(**updates: Any) -> Settings:
    base = {"llm_provider": "anthropic", "anthropic_api_key": SecretStr(API_KEY)}
    return get_settings().model_copy(update={**base, **updates})


def errors_counted(kind: str) -> float:
    labels = {"provider": "anthropic", "kind": kind}
    return REGISTRY.get_sample_value("eka_llm_errors_total", labels) or 0.0


def logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every log record including its `extra` fields."""
    return "\n".join(str(record.__dict__) for record in caplog.records)


# --- Streaming and responses ------------------------------------------------------------------


def test_stream_yields_text_deltas_then_one_end_with_usage_and_request_id():
    provider = make_provider(answering("Hello", " world"))
    assert isinstance(provider, LLMProvider)

    events = list(provider.stream(request()))

    assert events[:2] == [LLMTextDelta("Hello"), LLMTextDelta(" world")]  # thinking is skipped
    assert len(events) == 3
    end = events[2]
    assert isinstance(end, LLMStreamEnd)
    assert end.response.text == "Hello world"
    assert end.response.model == "claude-opus-5-5"
    assert end.response.stop_reason == "end_turn"
    assert (end.response.usage.input_tokens, end.response.usage.output_tokens) == (25, 15)
    assert end.response.request_id == "req_test_1"


def test_generate_returns_the_final_response():
    response = make_provider(answering("Hello", " world")).generate(request())
    assert response.text == "Hello world"
    assert response.usage.output_tokens == 15


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_stop_reason_is_surfaced(stop_reason):
    provider = make_provider(answering("I can", stop_reason=stop_reason))
    events = list(provider.stream(request()))
    assert isinstance(events[-1], LLMStreamEnd)
    assert events[-1].response.stop_reason == stop_reason
    assert events[-1].response.text == "I can"


def test_refusal_without_any_text_is_an_empty_answer():
    body = sse(message_start(), *message_end("refusal", output_tokens=0))
    response = make_provider(FakeApi(lambda _: stream_response(body))).generate(request())
    assert response.stop_reason == "refusal"
    assert response.text == ""


def test_response_reports_the_model_that_actually_answered():
    """After a server-side refusal fallback the served model differs from the requested one."""
    fallback = block(
        0,
        {
            "type": "fallback",
            "from": {"model": "claude-opus-5-5"},
            "to": {"model": "claude-opus-4-8"},
        },
    )
    body = sse(
        message_start("claude-opus-4-8"), *fallback, *text_block(1, "Served"), *message_end()
    )
    response = make_provider(FakeApi(lambda _: stream_response(body))).generate(request())
    assert response.model == "claude-opus-4-8"
    assert response.text == "Served"


def test_stream_that_ends_before_message_stop_is_an_error_not_a_short_answer():
    body = sse(message_start(), *text_block(0, "half an ans")[:2])  # no stop events
    provider = make_provider(FakeApi(lambda _: stream_response(body)))
    with pytest.raises(LLMUnavailableError) as raised:
        list(provider.stream(request()))
    assert raised.value.retryable


@pytest.mark.parametrize(
    "reply",
    [
        lambda: httpx2.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>login</html>"
        ),
        lambda: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=b""),
    ],
)
def test_a_200_without_any_message_events_is_an_error_not_an_assertion(reply):
    """e.g. a corporate proxy answering with its own page: the SDK itself asserts on this."""
    provider = make_provider(FakeApi(lambda _: reply()))
    with pytest.raises(LLMUnavailableError) as raised:
        list(provider.stream(request()))
    assert raised.value.retryable


# --- Request construction ---------------------------------------------------------------------


def test_request_uses_effort_and_adaptive_thinking_but_no_sampling_parameters():
    api = answering()
    list(make_provider(api, server_fallbacks=False).stream(request(temperature=0.7)))

    assert api.requests[-1].url.path == "/v1/messages"
    assert api.beta_header is None
    body = api.body
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 4000
    assert body["stream"] is True
    assert body["system"] == "You answer from the passages."
    assert body["messages"] == [{"role": "user", "content": SECRET_PROMPT}]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium"}
    for sampling in ("temperature", "top_p", "top_k"):
        assert sampling not in body


def test_request_overrides_model_effort_and_max_tokens():
    api = answering()
    list(
        make_provider(api).stream(
            request(model="claude-haiku-5-5", effort="low", max_output_tokens=300)
        )
    )
    body = api.body
    assert (body["model"], body["max_tokens"]) == ("claude-haiku-5-5", 300)
    assert body["output_config"] == {"effort": "low"}


def test_system_prompt_is_omitted_when_empty():
    api = answering()
    list(make_provider(api).stream(request(system="")))
    assert "system" not in api.body


@pytest.mark.parametrize(
    ("server_fallbacks", "allow_fallbacks", "model", "expect_fallbacks"),
    [
        (True, True, "claude-opus-5-5", True),
        (True, True, "claude-sonnet-5-5", True),
        (True, False, "claude-opus-5-5", False),  # caller opts out (e.g. cheap query rewrites)
        (False, True, "claude-opus-5-5", False),  # ANTHROPIC_FALLBACKS=off, or Bedrock
        (True, True, "claude-haiku-5-5", False),  # Haiku models have no server-side fallback
        (True, True, "anthropic.claude-haiku-5-5", False),
    ],
)
def test_server_side_fallback_is_used_only_when_allowed(
    server_fallbacks, allow_fallbacks, model, expect_fallbacks
):
    api = answering()
    provider = make_provider(api, server_fallbacks=server_fallbacks)
    list(provider.stream(request(model=model, allow_fallbacks=allow_fallbacks)))

    url = api.requests[-1].url
    if expect_fallbacks:
        assert api.beta_header == FALLBACK_BETA == "server-side-fallback-2026-07-01"
        assert api.body["fallbacks"] == "default"
        assert url.params.get("beta") == "true"
    else:
        assert api.beta_header is None
        assert "fallbacks" not in api.body
        assert "beta" not in url.params


# --- Construction from settings ---------------------------------------------------------------


@pytest.fixture
def sdk_transport(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    """Route clients built inside `from_settings` through a fake transport."""
    api = answering("ok")
    real_api, real_bedrock = anthropic.Anthropic, anthropic.AnthropicBedrockMantle
    transport = httpx2.MockTransport(api)

    def claude_api(**kwargs: Any) -> anthropic.Anthropic:
        return real_api(**kwargs, http_client=httpx2.Client(transport=transport))

    def bedrock(**kwargs: Any) -> anthropic.AnthropicBedrockMantle:
        # skip_auth: no AWS credentials are resolved or signed with in tests.
        return real_bedrock(
            **kwargs, skip_auth=True, http_client=httpx2.Client(transport=transport)
        )

    monkeypatch.setattr(anthropic, "Anthropic", claude_api)
    monkeypatch.setattr(anthropic, "AnthropicBedrockMantle", bedrock)
    return api


def test_from_settings_builds_the_claude_api_provider(sdk_transport):
    settings = settings_with(
        llm_model="claude-opus-5-5", anthropic_effort="high", llm_max_output_tokens=777
    )
    provider = AnthropicProvider.from_settings(settings)

    assert provider.provider_name == "anthropic"
    assert provider.model_name == "claude-opus-5-5"
    list(provider.stream(request()))
    assert sdk_transport.requests[-1].url.host == "api.anthropic.com"
    assert sdk_transport.requests[-1].headers["x-api-key"] == API_KEY
    assert sdk_transport.body["max_tokens"] == 777
    assert sdk_transport.body["output_config"] == {"effort": "high"}
    assert sdk_transport.beta_header == FALLBACK_BETA  # anthropic_fallbacks defaults to "default"


def test_from_settings_with_fallbacks_off_never_sends_the_beta(sdk_transport):
    provider = AnthropicProvider.from_settings(settings_with(anthropic_fallbacks="off"))
    list(provider.stream(request()))
    assert sdk_transport.beta_header is None
    assert "fallbacks" not in sdk_transport.body


def test_from_settings_wires_timeout_and_retries_into_the_sdk_client(monkeypatch):
    seen: dict[str, Any] = {}
    real = anthropic.Anthropic

    def spy(**kwargs: Any) -> anthropic.Anthropic:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", spy)
    AnthropicProvider.from_settings(settings_with(llm_timeout_seconds=33.0, llm_max_retries=4))
    assert seen["timeout"].read == 33.0  # reading the answer gets the configured timeout ...
    assert seen["timeout"].connect == 5.0  # ... connecting fails fast
    assert seen["max_retries"] == 4
    assert seen["api_key"] == API_KEY

    AnthropicProvider.from_settings(settings_with(llm_timeout_seconds=2.0))
    assert seen["timeout"].connect == 2.0  # never longer than the overall timeout


def test_bedrock_uses_the_mantle_client_in_the_configured_region(sdk_transport):
    settings = settings_with(
        anthropic_platform="bedrock",
        aws_region="eu-west-1",
        llm_model="anthropic.claude-opus-5-5",
        anthropic_api_key=None,  # Bedrock never needs (or reads) the Claude API key
    )
    provider = AnthropicProvider.from_settings(settings)

    # allow_fallbacks stays True: Bedrock has no server-side fallback, so none may be sent.
    list(provider.stream(request(allow_fallbacks=True)))
    sent = sdk_transport.requests[-1]
    assert sent.url.host == "bedrock-mantle.eu-west-1.api.aws"
    assert sent.url.path == "/anthropic/v1/messages"
    assert sdk_transport.body["model"] == "anthropic.claude-opus-5-5"
    assert sdk_transport.beta_header is None
    assert "fallbacks" not in sdk_transport.body


def test_bedrock_client_selection_makes_no_aws_calls():
    provider = AnthropicProvider.from_settings(
        settings_with(
            anthropic_platform="bedrock",
            aws_region="us-east-1",
            anthropic_api_key=None,
            llm_model="anthropic.claude-opus-5-5",
        )
    )
    assert isinstance(provider._client, anthropic.AnthropicBedrockMantle)
    assert provider.provider_name == "anthropic"


@pytest.mark.parametrize("key", [None, SecretStr(""), SecretStr("   ")])
def test_missing_api_key_is_a_configuration_error_at_construction(key):
    before = errors_counted("configuration")
    with pytest.raises(LLMConfigurationError, match="ANTHROPIC_API_KEY") as raised:
        AnthropicProvider.from_settings(settings_with(anthropic_api_key=key))
    assert not raised.value.retryable
    assert errors_counted("configuration") == before + 1


def test_bedrock_without_a_region_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("ANTHROPIC_BEDROCK_MANTLE_BASE_URL", raising=False)
    with pytest.raises(LLMConfigurationError, match="region"):
        AnthropicProvider.from_settings(
            settings_with(
                anthropic_platform="bedrock",
                aws_region=None,
                llm_model="anthropic.claude-opus-5-5",
            )
        )


@pytest.mark.parametrize(
    "models",
    [
        {"llm_model": "claude-opus-5-5"},  # the default, left unchanged when switching platform
        {"llm_model": "anthropic.claude-opus-5-5", "llm_query_rewrite_model": "claude-haiku-5-5"},
    ],
)
def test_bedrock_rejects_claude_api_model_ids_with_the_fix_in_the_message(models):
    settings = settings_with(anthropic_platform="bedrock", aws_region="us-east-1", **models)
    with pytest.raises(LLMConfigurationError, match=r"anthropic\.claude-") as raised:
        AnthropicProvider.from_settings(settings)
    assert not raised.value.retryable


@pytest.mark.parametrize(
    "model",
    ["anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5-5", "global.anthropic.claude-x"],
)
def test_bedrock_accepts_prefixed_model_ids_and_inference_profiles(model):
    settings = settings_with(
        anthropic_platform="bedrock",
        aws_region="us-east-1",
        llm_model=model,
        anthropic_api_key=None,
    )
    assert AnthropicProvider.from_settings(settings).model_name == model


def test_build_llm_selects_the_anthropic_provider():
    provider = build_llm(settings_with())
    assert isinstance(provider, AnthropicProvider)
    assert build_llm(settings_with(llm_provider="extractive")) is None


# --- Error mapping ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type", "expected", "retryable", "kind"),
    [
        (429, "rate_limit_error", LLMRateLimitError, True, "rate_limited"),
        (500, "api_error", LLMUnavailableError, True, "unavailable"),
        (529, "overloaded_error", LLMUnavailableError, True, "unavailable"),
        (401, "authentication_error", LLMConfigurationError, False, "configuration"),
        (403, "permission_error", LLMConfigurationError, False, "configuration"),
        (404, "not_found_error", LLMConfigurationError, False, "configuration"),
        (400, "invalid_request_error", LLMError, False, "error"),
        (402, "billing_error", LLMError, False, "error"),
        (413, "request_too_large", LLMError, False, "error"),
    ],
)
def test_http_errors_map_onto_the_llm_error_hierarchy(
    status, error_type, expected, retryable, kind
):
    before = errors_counted(kind)
    provider = make_provider(failing(status, error_type))

    with pytest.raises(LLMError) as raised:
        list(provider.stream(request()))

    assert type(raised.value) is expected
    assert raised.value.retryable is retryable
    assert raised.value.kind == kind
    assert errors_counted(kind) == before + 1


@pytest.mark.parametrize(
    "platform",
    [
        {},
        {
            "anthropic_platform": "bedrock",
            "aws_region": "us-east-1",
            "llm_model": "anthropic.claude-opus-5-5",
            "anthropic_api_key": None,
        },
    ],
    ids=["claude-api", "bedrock"],
)
def test_a_long_retry_after_fails_fast_instead_of_sleeping_through_it(
    platform, sdk_transport, monkeypatch
):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    sdk_transport.respond = lambda _: error_response(
        429, "rate_limit_error", **{"retry-after": "600"}
    )
    provider = AnthropicProvider.from_settings(settings_with(llm_max_retries=2, **platform))

    with pytest.raises(LLMRateLimitError) as raised:
        provider.generate(request())

    assert raised.value.retry_after == 600.0  # the caller learns how long to back off
    assert len(sdk_transport.requests) == 1  # ... but no worker thread waited for it
    assert sleeps == []


def test_a_short_retry_after_is_waited_out_by_the_sdk(sdk_transport, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    replies = iter(
        [
            error_response(429, "rate_limit_error", **{"retry-after": "2"}),
            stream_response(answer("ok")),
        ]
    )
    sdk_transport.respond = lambda _: next(replies)
    provider = AnthropicProvider.from_settings(settings_with(llm_max_retries=2))

    assert provider.generate(request()).text == "ok"
    assert len(sdk_transport.requests) == 2
    assert sleeps == [2.0]


class RaisingClient:
    """An SDK client whose stream() fails the way the SDK does before any network call."""

    def __init__(self, failure: BaseException) -> None:
        self.messages = self
        self.failure = failure

    def stream(self, **_: Any) -> Any:
        raise self.failure


def raising_provider(failure: BaseException) -> AnthropicProvider:
    return AnthropicProvider(
        RaisingClient(failure),  # type: ignore[arg-type]
        model="anthropic.claude-opus-5-5",
        max_output_tokens=100,
        effort="low",
        server_fallbacks=False,
    )


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("Could not resolve AWS credentials from session"),  # the SDK's own error
        NoCredentialsError(),
        ProfileNotFound(profile="missing"),
        ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "AssumeRole"),
    ],
    ids=["sdk-runtime-error", "no-credentials", "profile-not-found", "sts-denied"],
)
def test_unusable_aws_credentials_are_a_configuration_error(failure):
    before = errors_counted("configuration")
    with pytest.raises(LLMConfigurationError, match="AWS credentials") as raised:
        list(raising_provider(failure).stream(request()))
    assert not raised.value.retryable
    assert errors_counted("configuration") == before + 1


def test_bedrock_without_aws_credentials_fails_cleanly_before_any_request(monkeypatch):
    """The real SDK signs requests lazily; with an empty credential chain it raises a bare
    RuntimeError, which must reach callers as a configuration error."""
    for name in list(os.environ):
        if name.startswith(("AWS_", "ANTHROPIC_")):  # also the bearer-token variables
            monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")  # no instance-metadata probing
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)

    def unreachable(_: httpx2.Request) -> httpx2.Response:
        raise AssertionError("a request was sent although signing is impossible")

    client = anthropic.AnthropicBedrockMantle(
        aws_region="us-east-1",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(unreachable)),
    )
    provider = AnthropicProvider(
        client,
        model="anthropic.claude-opus-5-5",
        max_output_tokens=100,
        effort="low",
        server_fallbacks=False,
    )
    with pytest.raises(LLMConfigurationError, match="AWS credentials"):
        provider.generate(request())


@pytest.mark.parametrize(
    "bug", [ZeroDivisionError("bug"), RuntimeError("some other runtime failure")]
)
def test_unexpected_exceptions_are_not_disguised_as_provider_failures(bug):
    with pytest.raises(type(bug)):
        list(raising_provider(bug).stream(request()))


def test_failures_are_logged_with_status_error_type_and_request_id(caplog):
    provider = make_provider(failing(429, "rate_limit_error"))
    with pytest.raises(LLMRateLimitError):
        provider.generate(request(purpose="query_rewrite"))

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.error"]
    assert (record.provider, record.kind) == ("anthropic", "rate_limited")
    assert (record.status, record.error_type) == (429, "rate_limit_error")
    assert record.provider_request_id == "req_err_1"
    assert (record.model, record.purpose) == ("claude-opus-5-5", "query_rewrite")


def test_rate_limit_carries_retry_after():
    provider = make_provider(failing(429, "rate_limit_error", **{"retry-after": "7"}))
    with pytest.raises(LLMRateLimitError) as raised:
        provider.generate(request())
    assert raised.value.retry_after == 7.0


@pytest.mark.parametrize("header", [{}, {"retry-after": "soon"}, {"retry-after": "-3"}])
def test_rate_limit_without_a_usable_retry_after(header):
    provider = make_provider(failing(429, "rate_limit_error", **header))
    with pytest.raises(LLMRateLimitError) as raised:
        provider.generate(request())
    assert raised.value.retry_after is None


def test_timeout_and_connection_failures_are_distinguished():
    def timing_out(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=req)

    def refused(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=req)

    with pytest.raises(LLMTimeoutError) as timeout:
        make_provider(FakeApi(timing_out)).generate(request())
    assert timeout.value.retryable and timeout.value.kind == "timeout"

    with pytest.raises(LLMUnavailableError) as unreachable:
        make_provider(FakeApi(refused)).generate(request())
    assert unreachable.value.retryable and not isinstance(unreachable.value, LLMTimeoutError)


def test_error_event_inside_a_200_stream_is_mapped_by_its_error_type():
    overloaded = ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "x"}})
    body = sse(message_start(), *text_block(0, "partial")[:2], overloaded)
    provider = make_provider(FakeApi(lambda _: stream_response(body)))

    events = []
    with pytest.raises(LLMUnavailableError) as raised:
        for event in provider.stream(request()):
            events.append(event)
    assert events == [LLMTextDelta("partial")]  # what was already delivered stays delivered
    assert raised.value.retryable


def test_rate_limit_event_inside_a_200_stream_is_a_rate_limit_error():
    event = ("error", {"type": "error", "error": {"type": "rate_limit_error", "message": "x"}})
    provider = make_provider(FakeApi(lambda _: stream_response(sse(message_start(), event))))
    with pytest.raises(LLMRateLimitError):
        list(provider.stream(request()))


class BrokenStream(httpx2.SyncByteStream):
    """Delivers some events, then fails like a dropped connection."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.closed = False

    def __iter__(self):
        yield sse(message_start(), *text_block(0, "Hi")[:2])
        raise self.failure

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx2.ReadTimeout("read timed out"), LLMTimeoutError),
        (httpx2.RemoteProtocolError("peer closed connection"), LLMUnavailableError),
    ],
)
def test_network_failure_mid_stream_is_mapped_not_leaked(failure, expected):
    """The SDK does not wrap transport errors raised while reading the body."""
    stream = BrokenStream(failure)
    api = FakeApi(
        lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    )

    events = []
    with pytest.raises(expected) as raised:
        for event in make_provider(api).stream(request()):
            events.append(event)

    assert events == [LLMTextDelta("Hi")]
    assert raised.value.retryable
    assert stream.closed


# --- Early close ------------------------------------------------------------------------------


class TrackedStream(httpx2.SyncByteStream):
    """One SSE event per chunk, remembering whether the response was released."""

    def __init__(self, events: list[tuple[str, dict]]) -> None:
        self.chunks = [sse(event) for event in events]
        self.delivered = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.delivered += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


def test_closing_the_stream_early_releases_the_http_response():
    events = [message_start(), *text_block(0, "one", "two", "three", "four"), *message_end()]
    stream = TrackedStream(events)
    api = FakeApi(
        lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    )

    generator = make_provider(api).stream(request())
    assert next(generator) == LLMTextDelta("one")
    assert not stream.closed
    generator.close()  # what the chat endpoint does when the user disconnects

    assert stream.closed
    assert stream.delivered < len(stream.chunks)  # the rest was never read


def test_a_fully_consumed_stream_is_released_too():
    stream = TrackedStream([message_start(), *text_block(0, "x"), *message_end()])
    api = FakeApi(
        lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    )
    list(make_provider(api).stream(request()))
    assert stream.closed


# --- Secrets stay out of logs and errors ------------------------------------------------------


def test_prompts_answers_and_keys_are_never_logged_or_put_in_errors(caplog):
    caplog.set_level(logging.DEBUG)

    list(make_provider(answering("A-SECRET-ANSWER")).stream(request()))
    with pytest.raises(LLMError) as raised:
        make_provider(failing(401, "authentication_error")).generate(request())

    text = logged(caplog) + str(raised.value)
    assert "llm.error" in text  # the failure was logged ...
    for secret in (SECRET_PROMPT, "A-SECRET-ANSWER", API_KEY):
        assert secret not in text  # ... without any of the sensitive values (the fake API
        # error body echoes both the key and the prompt, so this also proves the provider
        # never copies provider-supplied message text)
