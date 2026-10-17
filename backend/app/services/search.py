"""Knowledge-base search API on top of the retrieval pipeline (app/rag)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.providers.embeddings import EmbeddingProvider
from app.providers.rerankers import Reranker
from app.schemas.search import SearchRequest, SearchResponse
from app.services.access import Principal


def search(
    db: Session,
    principal: Principal,
    body: SearchRequest,
    *,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
) -> SearchResponse:
    raise NotImplementedError
