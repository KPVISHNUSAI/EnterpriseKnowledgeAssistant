"""Cross-encoder reranker on sentence-transformers (RERANKER_MODEL, e.g.
cross-encoder/ms-marco-MiniLM-L6-v2).

The model loads lazily on first use, once per process. Raw logits pass through a
sigmoid, so scores fall in 0..1 and RETRIEVAL_MIN_RERANK_SCORE is an interpretable cut.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import Any

from app.core.config import Settings

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    def __init__(
        self, model_name: str, *, device: str = "auto", batch_size: int = 32, max_length: int = 512
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._batch_size = batch_size
        self._max_length = max_length
        self._model: Any = None
        # Guards loading and inference: the API serves requests from a thread pool, and
        # Hugging Face fast tokenizers must not be used by two threads at once.
        # ponytail: one inference at a time per process; a model pool if this bottlenecks.
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> CrossEncoderReranker:
        return cls(
            settings.reranker_model,
            device=settings.reranker_device,
            batch_size=settings.reranker_batch_size,
            max_length=settings.reranker_max_length,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Relevance of each passage to the query, 0..1 (higher is more relevant)."""
        if not passages:
            return []
        import torch

        with self._lock:
            scores = self._load().predict(
                [(query, passage) for passage in passages],
                batch_size=self._batch_size,
                # Explicit: the model's own config may leave logits unnormalized.
                activation_fn=torch.nn.Sigmoid(),
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        return [float(score) for score in scores]

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            device = resolve_device(self._device)
            self._model = CrossEncoder(self._model_name, device=device, max_length=self._max_length)
            logger.info("reranker.loaded", extra={"model": self._model_name, "device": device})
        return self._model


def resolve_device(device: str) -> str:
    """'auto' picks cuda, then Apple mps, then cpu; anything else is used as given."""
    if device != "auto":
        return device
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"
