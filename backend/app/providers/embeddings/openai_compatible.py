"""Embeddings from an OpenAI-compatible `POST {base}/embeddings` endpoint (OpenAI, Ollama,
vLLM, Text Embeddings Inference, LM Studio, ...).

Rate limits (429), server errors and connection failures are retried with bounded
exponential backoff, honouring Retry-After; other 4xx responses are configuration
errors and are not retried. The API key is sent as a bearer token and never logged.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.providers.embeddings.base import EmbeddingError

logger = logging.getLogger(__name__)

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 20.0
_RETRY_AFTER_CAP_SECONDS = 60.0


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        dimensions: int,
        api_key: SecretStr | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        batch_size: int = 32,
        query_prefix: str = "",
        document_prefix: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self._model_name = model_name
        self._dimensions = dimensions
        self._api_key = api_key
        self._max_retries = max_retries
        self._batch_size = batch_size
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._client = client or httpx.Client(timeout=timeout_seconds)

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatibleEmbedder:
        if not settings.embedding_api_base_url:
            raise EmbeddingError(
                "EMBEDDING_API_BASE_URL is required for EMBEDDING_PROVIDER=openai_compatible",
                retryable=False,
            )
        return cls(
            base_url=settings.embedding_api_base_url,
            model_name=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            api_key=settings.embedding_api_key,
            timeout_seconds=settings.embedding_timeout_seconds,
            max_retries=settings.embedding_max_retries,
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
        inputs = [self._document_prefix + text for text in texts]
        size = self._batch_size
        return [v for i in range(0, len(inputs), size) for v in self._request(inputs[i : i + size])]

    def embed_query(self, text: str) -> list[float]:
        return self._request([self._query_prefix + text])[0]

    def _request(self, inputs: list[str]) -> list[list[float]]:
        headers = (
            {"Authorization": f"Bearer {self._api_key.get_secret_value()}"} if self._api_key else {}
        )
        payload = {"model": self._model_name, "input": inputs}
        attempt = 0
        while True:
            retry_after: float | None = None
            cause: Exception | None = None
            try:
                response = self._client.post(self._url, json=payload, headers=headers)
            except httpx.TransportError as exc:  # connect/read timeouts, refused connections
                error, cause = EmbeddingError("the embedding service is unreachable"), exc
            else:
                status = response.status_code
                if status != 429 and status < 500:
                    if response.is_error:
                        raise EmbeddingError(
                            f"the embedding service rejected the request (HTTP {status})",
                            retryable=False,
                        )
                    return self._vectors(response, len(inputs))
                error = EmbeddingError(f"the embedding service returned HTTP {status}")
                retry_after = _retry_after(response)
            if attempt >= self._max_retries:
                raise error from cause
            delay = retry_after if retry_after is not None else _backoff(attempt)
            logger.warning(
                "embeddings.request_retry", extra={"attempt": attempt + 1, "delay_s": delay}
            )
            time.sleep(delay)
            attempt += 1

    def _vectors(self, response: httpx.Response, expected: int) -> list[list[float]]:
        try:
            data: list[dict[str, Any]] = response.json()["data"]
            order = sorted(range(len(data)), key=lambda i: data[i].get("index", i))
            vectors = [[float(value) for value in data[i]["embedding"]] for i in order]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise EmbeddingError(
                "the embedding service returned an unexpected response", retryable=False
            ) from exc
        if len(vectors) != expected:
            raise EmbeddingError(
                f"the embedding service returned {len(vectors)} vectors for {expected} inputs",
                retryable=False,
            )
        return [self._normalized(vector) for vector in vectors]

    def _normalized(self, vector: list[float]) -> list[float]:
        if len(vector) != self._dimensions:
            raise EmbeddingError(
                f"embedding model {self._model_name} returned {len(vector)}-dimensional "
                f"vectors but EMBEDDING_DIMENSIONS is {self._dimensions}",
                retryable=False,
            )
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm == 0:
            raise EmbeddingError(
                "the embedding service returned an invalid vector", retryable=False
            )
        return [value / norm for value in vector]


def _backoff(attempt: int) -> float:
    delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2**attempt)
    return delay + random.uniform(0, delay / 4)


def _retry_after(response: httpx.Response) -> float | None:
    """Retry-After in seconds (delta-seconds or HTTP date), capped."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            return None
    return min(max(seconds, 0.0), _RETRY_AFTER_CAP_SECONDS)
