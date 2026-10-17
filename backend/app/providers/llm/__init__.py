from __future__ import annotations

from app.core.config import Settings
from app.providers.llm.base import (
    LLMConfigurationError,
    LLMError,
    LLMMessage,
    LLMProvider,
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


def build_llm(settings: Settings) -> LLMProvider | None:
    """None for LLM_PROVIDER=extractive (answers are composed from passages, no LLM)."""
    if settings.llm_provider == "extractive":
        return None
    if settings.llm_provider == "anthropic":
        from app.providers.llm.anthropic import AnthropicProvider

        return AnthropicProvider.from_settings(settings)
    from app.providers.llm.openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider.from_settings(settings)


__all__ = [
    "LLMConfigurationError",
    "LLMError",
    "LLMMessage",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMResponse",
    "LLMStreamEnd",
    "LLMStreamEvent",
    "LLMTextDelta",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "LLMUsage",
    "build_llm",
]
