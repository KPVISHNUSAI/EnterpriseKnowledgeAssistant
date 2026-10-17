"""Process-wide provider instances (dependency injection point).

API dependencies, the worker and the eval runner obtain providers here; tests swap
them with FastAPI dependency overrides or `reset_providers()` + settings changes.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.providers.embeddings import EmbeddingProvider, build_embedder
from app.providers.llm import LLMProvider, build_llm
from app.providers.rerankers import Reranker, build_reranker


@lru_cache
def get_embedder() -> EmbeddingProvider:
    return build_embedder(get_settings())


@lru_cache
def get_reranker() -> Reranker | None:
    return build_reranker(get_settings())


@lru_cache
def get_llm() -> LLMProvider | None:
    return build_llm(get_settings())


def reset_providers() -> None:
    for factory in (get_embedder, get_reranker, get_llm):
        factory.cache_clear()
