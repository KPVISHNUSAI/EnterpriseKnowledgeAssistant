"""Redis cache of document-chunk embeddings.

Key: ``emb:v2:{organization_id}:{model}:{prefix tag}:{content_hash}``. Entries belong to
one organization: a shared namespace would let one tenant's ingestion speed reveal that
another tenant indexed byte-identical text, and one tenant's deletion would drop another
tenant's entries. `content_hash` is DocumentChunk.content_hash, the SHA-256 of the exact
text given to `embed_documents`; the provider prepends EMBEDDING_DOCUMENT_PREFIX itself,
so a short hash of that prefix is part of the key. Values are little-endian float32
vectors (what pgvector stores) that expire after EMBEDDING_CACHE_TTL_SECONDS (0 disables
the cache). An unavailable Redis never fails ingestion: lookups count as misses and
writes are skipped.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import cast

import redis

from app.core.config import get_settings
from app.core.metrics import EMBEDDING_CACHE
from app.core.redis import get_redis
from app.providers.embeddings.base import EmbeddingError, EmbeddingProvider

logger = logging.getLogger(__name__)

_NAMESPACE = "emb:v2"  # v1 keys had no organization; they expire with their TTL
_BATCH = 500  # keys per MGET/DEL round trip


def _keys(organization_id: uuid.UUID, model: str, content_hashes: Sequence[str]) -> list[str]:
    prefix = get_settings().embedding_document_prefix
    tag = hashlib.sha256(prefix.encode()).hexdigest()[:12]
    return [
        f"{_NAMESPACE}:{organization_id}:{model}:{tag}:{content_hash}"
        for content_hash in content_hashes
    ]


def lookup(
    organization_id: uuid.UUID, model: str, content_hashes: Sequence[str], dimensions: int
) -> list[list[float] | None]:
    """Cached vectors in input order; None where missing (or Redis is unavailable)."""
    if get_settings().embedding_cache_ttl_seconds <= 0 or not content_hashes:
        return [None] * len(content_hashes)
    keys = _keys(organization_id, model, content_hashes)
    try:
        client = get_redis()
        raw: list[bytes | None] = []
        for i in range(0, len(keys), _BATCH):
            raw.extend(cast(list[bytes | None], client.mget(keys[i : i + _BATCH])))
    except redis.RedisError:
        logger.warning("embedding_cache.unavailable", extra={"operation": "lookup"})
        EMBEDDING_CACHE.labels("error").inc(len(keys))
        return [None] * len(keys)
    size = 4 * dimensions
    vectors = [
        list(struct.unpack(f"<{dimensions}f", value))
        if value is not None and len(value) == size
        else None
        for value in raw
    ]
    hits = sum(vector is not None for vector in vectors)
    EMBEDDING_CACHE.labels("hit").inc(hits)
    EMBEDDING_CACHE.labels("miss").inc(len(vectors) - hits)
    return vectors


def store(organization_id: uuid.UUID, model: str, vectors: Mapping[str, Sequence[float]]) -> None:
    """Cache vectors by content hash (pipelined SETs with the configured TTL)."""
    ttl = get_settings().embedding_cache_ttl_seconds
    if ttl <= 0 or not vectors:
        return
    try:
        pipe = get_redis().pipeline(transaction=False)
        keys = _keys(organization_id, model, list(vectors))
        for key, vector in zip(keys, vectors.values(), strict=True):
            pipe.set(key, struct.pack(f"<{len(vector)}f", *vector), ex=ttl)
        pipe.execute()
    except redis.RedisError:
        logger.warning("embedding_cache.unavailable", extra={"operation": "store"})


def invalidate(organization_id: uuid.UUID, content_hashes: Iterable[str]) -> None:
    """Drop the organization's cached vectors for these chunk hashes under the configured
    embedding model (called when a document or one of its versions is removed)."""
    from app.providers.registry import get_embedder  # registry imports this package

    hashes = list(dict.fromkeys(content_hashes))
    if not hashes:
        return
    keys = _keys(organization_id, get_embedder().model_name, hashes)
    try:
        client = get_redis()
        for i in range(0, len(keys), _BATCH):
            client.delete(*keys[i : i + _BATCH])
    except redis.RedisError:
        logger.warning("embedding_cache.unavailable", extra={"operation": "invalidate"})


def embed_with_cache(
    organization_id: uuid.UUID,
    embedder: EmbeddingProvider,
    texts: Sequence[str],
    content_hashes: Sequence[str],
    *,
    batch_size: int,
) -> tuple[list[list[float]], int]:
    """Vectors for `texts`, served from the organization's cache where possible; returns
    (vectors, cache hits). Each embedded batch is cached at once, so a retry after a
    failure in a later batch does not pay for the earlier ones again."""
    model = embedder.model_name
    vectors = lookup(organization_id, model, content_hashes, embedder.dimensions)
    missing = [i for i, vector in enumerate(vectors) if vector is None]
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        fresh = embedder.embed_documents([texts[i] for i in batch])
        if len(fresh) != len(batch):
            raise EmbeddingError(
                f"embedding provider returned {len(fresh)} vectors for {len(batch)} texts",
                retryable=False,
            )
        store(
            organization_id,
            model,
            {content_hashes[i]: v for i, v in zip(batch, fresh, strict=True)},
        )
        for i, vector in zip(batch, fresh, strict=True):
            vectors[i] = vector
    return [vector for vector in vectors if vector is not None], len(texts) - len(missing)
