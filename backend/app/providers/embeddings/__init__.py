from __future__ import annotations

from app.core.config import Settings
from app.providers.embeddings.base import EmbeddingError, EmbeddingProvider
from app.providers.embeddings.hashing import HashingEmbedder


def build_embedder(settings: Settings) -> EmbeddingProvider:
    """Construct the configured provider. Models load lazily on first use."""
    if settings.embedding_provider == "hashing":
        return HashingEmbedder(settings.embedding_dimensions)
    if settings.embedding_provider == "sentence_transformers":
        from app.providers.embeddings.sentence_transformers import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder.from_settings(settings)
    from app.providers.embeddings.openai_compatible import OpenAICompatibleEmbedder

    return OpenAICompatibleEmbedder.from_settings(settings)


__all__ = ["EmbeddingError", "EmbeddingProvider", "HashingEmbedder", "build_embedder"]
