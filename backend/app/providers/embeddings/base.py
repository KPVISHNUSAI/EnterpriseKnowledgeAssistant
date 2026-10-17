from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


class EmbeddingError(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into L2-normalized vectors of a fixed dimension.

    `embed_documents` is used at ingestion time (batched by the caller or provider);
    `embed_query` applies any query-side instruction prefix the model expects.
    """

    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...
