"""Redis cache of document-chunk embeddings, keyed by model and DocumentChunk.content_hash
(the SHA-256 of the exact text that was embedded)."""

from __future__ import annotations

from collections.abc import Iterable


def invalidate(content_hashes: Iterable[str]) -> None:
    """Drop cached vectors for these chunk hashes (called when a document is deleted).

    ponytail: placeholder until the ingestion cache lands; there is nothing cached yet.
    """
    del content_hashes
