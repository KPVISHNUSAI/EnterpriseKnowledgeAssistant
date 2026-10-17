from __future__ import annotations

from app.core.config import Settings
from app.providers.rerankers.base import Reranker


def build_reranker(settings: Settings) -> Reranker | None:
    """None when reranking is disabled (RERANKER_PROVIDER=none)."""
    if settings.reranker_provider == "none":
        return None
    from app.providers.rerankers.cross_encoder import CrossEncoderReranker

    return CrossEncoderReranker.from_settings(settings)


__all__ = ["Reranker", "build_reranker"]
