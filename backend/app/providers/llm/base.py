"""LLM provider interface. Implementations: anthropic (official SDK) and
openai_compatible (Ollama, vLLM, LM Studio, ...). Answer generation without an LLM
is handled by the extractive generator in app/rag, not by a fake provider."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMRequest:
    system: str
    messages: Sequence[LLMMessage]
    max_output_tokens: int | None = None
    # Provider-specific knobs; each provider ignores what it does not support.
    effort: str | None = None
    temperature: float | None = None
    model: str | None = None  # override the provider's default model (e.g. rewrite model)
    allow_fallbacks: bool = True
    purpose: str = "answer"  # answer | query_rewrite (for logs/metrics)


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    stop_reason: str | None = None  # end_turn | max_tokens | refusal | ...
    request_id: str | None = None


@dataclass(frozen=True)
class LLMTextDelta:
    text: str


@dataclass(frozen=True)
class LLMStreamEnd:
    response: LLMResponse


LLMStreamEvent = LLMTextDelta | LLMStreamEnd


class LLMError(Exception):
    """Base provider error. `retryable` is True for timeouts, 429s and 5xx."""

    kind = "error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMTimeoutError(LLMError):
    kind = "timeout"


class LLMRateLimitError(LLMError):
    kind = "rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, retryable=True)
        self.retry_after = retry_after


class LLMUnavailableError(LLMError):
    kind = "unavailable"


class LLMConfigurationError(LLMError):
    kind = "configuration"


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def generate(self, request: LLMRequest) -> LLMResponse: ...

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        """Yield text deltas then exactly one LLMStreamEnd. Closing the iterator early
        must release the underlying HTTP stream."""
        ...
