"""Local embeddings with sentence-transformers (the optional `local-models` extra).

The model loads on first use, once per process. Inference is serialized: the Hugging
Face fast tokenizer is not safe to call from several threads at once ("Already
borrowed"), and the API serves queries from a thread pool.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import Any

from app.core.config import Settings
from app.providers.embeddings.base import EmbeddingError

logger = logging.getLogger(__name__)


def resolve_device(device: str) -> str:
    """'auto' picks CUDA, then Apple MPS, then CPU; anything else is used as given."""
    if device != "auto":
        return device
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class SentenceTransformerEmbedder:
    def __init__(
        self,
        model_name: str,
        *,
        dimensions: int,
        device: str = "auto",
        batch_size: int = 32,
        query_prefix: str = "",
        document_prefix: str = "",
    ) -> None:
        self._model_name = model_name
        self._dimensions = dimensions
        self._device = device
        self._batch_size = batch_size
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._model: Any = None
        self._load_lock = threading.Lock()
        # ponytail: one inference at a time per process; run a model server if query
        # throughput ever needs parallel inference.
        self._encode_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> SentenceTransformerEmbedder:
        return cls(
            settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            device=settings.embedding_device,
            batch_size=settings.embedding_batch_size,
            query_prefix=settings.embedding_query_prefix,
            document_prefix=settings.embedding_document_prefix,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode([self._document_prefix + text for text in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self._query_prefix + text])[0]

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        with self._encode_lock:
            try:
                vectors = model.encode(
                    texts,
                    batch_size=self._batch_size,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
            except Exception as exc:  # device out of memory, driver errors
                raise EmbeddingError("the embedding model failed to encode a batch") from exc
        return [[float(value) for value in row] for row in vectors]

    def _load(self) -> Any:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    self._model = self._create()
        return self._model

    def _create(self) -> Any:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingError(
                "sentence-transformers is not installed (uv sync --extra local-models)",
                retryable=False,
            ) from exc
        device = resolve_device(self._device)
        try:
            model = SentenceTransformer(self._model_name, device=device)
        except Exception as exc:  # download, network or disk problems
            raise EmbeddingError(f"could not load embedding model {self._model_name}") from exc
        dimension = model.get_embedding_dimension()
        if dimension != self._dimensions:
            raise EmbeddingError(
                f"embedding model {self._model_name} produces {dimension}-dimensional vectors "
                f"but EMBEDDING_DIMENSIONS is {self._dimensions}",
                retryable=False,
            )
        logger.info("embeddings.model_loaded", extra={"model": self._model_name, "device": device})
        return model
