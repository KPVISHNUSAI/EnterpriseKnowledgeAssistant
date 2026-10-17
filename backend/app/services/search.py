"""Knowledge-base search API on top of the retrieval pipeline (app/rag)."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.filetypes import MIME_TYPES, file_type_for_mime
from app.models import Workspace
from app.providers.embeddings import EmbeddingProvider
from app.providers.rerankers import Reranker
from app.rag.highlight import add_highlights
from app.rag.query import normalize_query
from app.rag.retrieval import retrieve
from app.rag.types import Candidate, RetrievalOptions, SearchScope
from app.schemas.search import (
    HighlightSegment,
    MatchExplanation,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from app.services.access import Principal


def search(
    db: Session,
    principal: Principal,
    body: SearchRequest,
    *,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
) -> SearchResponse:
    """Passages the caller can read, best first, each explaining why it matched.
    A rerank request without a configured reranker returns RRF order (reranked=false)."""
    settings = get_settings()
    query = normalize_query(body.query, max_chars=settings.max_query_chars)
    filters = body.filters
    scope = SearchScope(
        principal=principal,
        # An empty filter list means "no filter", like a multi-select with nothing chosen.
        workspace_ids=tuple(filters.workspace_ids) if filters.workspace_ids else None,
        document_ids=tuple(filters.document_ids) if filters.document_ids else None,
        mime_types=tuple(MIME_TYPES[t] for t in filters.file_types) if filters.file_types else None,
    )
    top_k = min(body.top_k, settings.search_max_results)
    options = RetrievalOptions.from_settings(
        settings, mode=body.mode, rerank=body.rerank, final_top_k=top_k
    )
    # Every stage keeps at least top_k candidates, so the requested count can be met.
    options = replace(
        options,
        vector_top_k=max(options.vector_top_k, top_k),
        keyword_top_k=max(options.keyword_top_k, top_k),
        fused_top_k=max(options.fused_top_k, top_k),
    )
    result = retrieve(db, scope, query, options=options, embedder=embedder, reranker=reranker)

    started = time.perf_counter()
    add_highlights(db, result.candidates, result.search_query)
    timings = {**result.timings_ms, "highlight": round((time.perf_counter() - started) * 1000, 2)}

    workspace_ids = {c.workspace_id for c in result.candidates}
    names = {
        row.id: row.name
        for row in db.execute(
            select(Workspace.id, Workspace.name).where(Workspace.id.in_(workspace_ids))
        )
    }
    return SearchResponse(
        query=result.query,
        mode=options.mode,
        reranked=result.reranked,
        results=[
            _result(candidate, rank, names, reranked=result.reranked)
            for rank, candidate in enumerate(result.candidates, start=1)
        ],
        total_candidates=result.total_candidates,
        timings_ms=timings,
    )


def _result(
    c: Candidate, rank: int, workspace_names: dict[uuid.UUID, str], *, reranked: bool
) -> SearchResult:
    return SearchResult(
        chunk_id=c.chunk_id,
        document_id=c.document_id,
        document_title=c.document_title,
        workspace_id=c.workspace_id,
        workspace_name=workspace_names.get(c.workspace_id, ""),
        file_type=file_type_for_mime(c.mime_type),
        mime_type=c.mime_type,
        page_number=c.page_number,
        page_end=c.page_end,
        section_title=c.section_title,
        heading_path=c.heading_path,
        content=c.content,
        highlights=[
            HighlightSegment(text=segment, match=is_match)
            for segment, is_match in (c.highlights or [(c.content, False)])
        ],
        score=(c.rerank_score if reranked else c.rrf_score) or 0.0,
        explanation=MatchExplanation(
            vector_rank=c.vector_rank,
            vector_similarity=c.vector_similarity,
            keyword_rank=c.keyword_rank,
            keyword_score=c.keyword_score,
            matched_terms=c.matched_terms,
            rrf_score=c.rrf_score,
            rerank_score=c.rerank_score,
            final_rank=rank,
        ),
    )
