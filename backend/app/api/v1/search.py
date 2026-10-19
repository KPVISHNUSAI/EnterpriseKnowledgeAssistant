from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal, get_request_meta, rate_limit_user
from app.providers.embeddings import EmbeddingProvider
from app.providers.registry import get_embedder, get_reranker
from app.providers.rerankers import Reranker
from app.schemas.common import errors
from app.schemas.search import SearchRequest, SearchResponse
from app.services import search as search_service
from app.services.access import Principal
from app.services.audit import RequestMeta

router = APIRouter(tags=["search"])


@router.post(
    "/search",
    response_model=SearchResponse,
    responses=errors(401, 422, 429, 503),
    dependencies=[rate_limit_user("search", "rate_limit_search_per_minute")],
)
def search(
    body: SearchRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    embedder: EmbeddingProvider = Depends(get_embedder),
    reranker: Reranker | None = Depends(get_reranker),
    meta: RequestMeta = Depends(get_request_meta),
) -> SearchResponse:
    """Knowledge-base search (no LLM): hybrid vector + full-text retrieval fused with
    RRF and optionally reranked. Only passages from documents the caller can read are
    returned; filters can narrow but never widen that scope. Each result explains why it
    matched (per-method ranks/scores and matched terms)."""
    return search_service.search(
        db, principal, body, embedder=embedder, reranker=reranker, meta=meta
    )
