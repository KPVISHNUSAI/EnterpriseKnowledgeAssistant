"""Permission-aware hybrid retrieval: pgvector similarity + PostgreSQL full-text search,
fused with Reciprocal Rank Fusion and optionally reranked by a cross-encoder.

Both retrievers restrict chunks to the documents the principal may read (narrowed by
the scope's filters) inside the SQL statement, before ranking and LIMIT, so text the
caller cannot read never leaves the database. See docs/RETRIEVAL.md.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    Subquery,
    Text,
    any_,
    cast,
    func,
    literal,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSQUERY
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError
from app.core.metrics import RETRIEVAL_LATENCY
from app.models import Document, DocumentChunk
from app.providers.embeddings import EmbeddingError, EmbeddingProvider
from app.providers.rerankers import Reranker
from app.rag.types import Candidate, RetrievalOptions, RetrievalResult, SearchScope
from app.services.access import readable_documents_clause

logger = logging.getLogger(__name__)

# ts_rank_cd normalization: 1 divides by 1 + log(chunk length), 32 scales into 0..1.
_RANK_NORMALIZATION = 1 | 32

# Transaction-local (is_local=true), so they never leak to other users of the connection.
_HNSW_SETTINGS = text(
    "SELECT set_config('hnsw.ef_search', :ef_search, true), "
    "set_config('hnsw.iterative_scan', 'strict_order', true)"
)
_QUERY_LEXEMES = text(
    "SELECT lexeme FROM unnest(to_tsvector(CAST(:config AS regconfig), :query)) "
    "ORDER BY positions[1], lexeme"
)

_CHUNK_COLUMNS = (
    DocumentChunk.id,
    DocumentChunk.document_id,
    DocumentChunk.chunk_index,
    DocumentChunk.content,
    DocumentChunk.content_hash,
    DocumentChunk.token_count,
    DocumentChunk.page_number,
    DocumentChunk.page_end,
    DocumentChunk.section_title,
    DocumentChunk.heading_path,
    DocumentChunk.chunk_metadata.label("chunk_metadata"),
)


def retrieve(
    db: Session,
    scope: SearchScope,
    query: str,
    *,
    options: RetrievalOptions,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
    search_query: str | None = None,
) -> RetrievalResult:
    """Retrieve passages for a normalized `query` (app/rag/query.py).

    `search_query` is the text actually searched, embedded and reranked against (a
    rewritten follow-up question); it defaults to `query`. Returns at most
    `options.final_top_k` candidates in final order.
    """
    search = search_query or query
    timings: dict[str, float] = {}
    started = time.perf_counter()
    vector_hits: list[Candidate] = []
    keyword_hits: list[Candidate] = []

    if options.mode != "keyword":
        with _timed(timings, "embed"):
            try:
                query_vector = embedder.embed_query(search)
            except EmbeddingError as exc:
                logger.warning("retrieval.embedding_failed", extra={"retryable": exc.retryable})
                raise ServiceUnavailableError(
                    "Search is temporarily unavailable", code="embedding_unavailable"
                ) from exc
        with _timed(timings, "vector"):
            vector_hits = vector_search(
                db, scope, query_vector, model_name=embedder.model_name, limit=options.vector_top_k
            )
    if options.mode != "vector":
        with _timed(timings, "keyword"):
            keyword_hits = keyword_search(db, scope, search, limit=options.keyword_top_k)

    with _timed(timings, "fuse"):
        fused = fuse(vector_hits, keyword_hits, rrf_k=options.rrf_k)
    candidates = fused[: options.fused_top_k]

    reranked = False
    if options.rerank and reranker is not None and candidates:
        with _timed(timings, "rerank"):
            try:
                candidates = _rerank(
                    reranker, search, candidates, min_score=options.min_rerank_score
                )
                reranked = True
            except Exception as exc:  # reranking only refines the order: degrade, don't fail
                logger.warning("retrieval.rerank_failed", extra={"error_type": type(exc).__name__})
    candidates = candidates[: options.final_top_k]

    timings["total"] = _elapsed_ms(started)
    for stage, ms in timings.items():
        RETRIEVAL_LATENCY.labels(stage).observe(ms / 1000)
    logger.info(
        "retrieval.completed",
        extra={
            "mode": options.mode,
            "vector_hits": len(vector_hits),
            "keyword_hits": len(keyword_hits),
            "candidates": len(fused),
            "returned": len(candidates),
            "reranked": reranked,
            "timings_ms": timings,
        },
    )
    return RetrievalResult(
        query=query,
        search_query=search,
        candidates=candidates,
        total_candidates=len(fused),
        options=options,
        reranked=reranked,
        timings_ms=timings,
    )


def vector_search(
    db: Session,
    scope: SearchScope,
    query_vector: Sequence[float],
    *,
    model_name: str,
    limit: int,
) -> list[Candidate]:
    """Nearest readable chunks by cosine distance (HNSW index). Only chunks embedded by
    the query's model are comparable."""
    db.execute(_HNSW_SETTINGS, {"ef_search": str(get_settings().hnsw_ef_search)})
    distance = DocumentChunk.embedding.cosine_distance(query_vector).label("distance")
    # ORDER BY the bare distance operator + LIMIT is the shape the HNSW index can serve
    # (metadata is joined outside the LIMIT); iterative scans keep producing rows until
    # `limit` of them pass the access filter.
    hits = (
        select(*_CHUNK_COLUMNS, distance)
        .where(DocumentChunk.embedding_model == model_name, _in_scope(scope))
        .order_by(distance)
        .limit(limit)
        .subquery("hits")
    )
    rows = db.execute(_with_documents(hits).order_by(hits.c.distance))
    return [
        _candidate(row, vector_rank=rank, vector_similarity=1.0 - row.distance)
        for rank, row in enumerate(rows, start=1)
    ]


def keyword_search(db: Session, scope: SearchScope, query: str, *, limit: int) -> list[Candidate]:
    """Readable chunks matching any query term (OR semantics, so partial matches still
    rank), by ts_rank_cd over the weighted search_vector (heading path A, content B)."""
    lexemes = query_lexemes(db, query)
    if not lexemes:
        return []  # nothing but stop words and punctuation
    tsquery = cast(literal(or_tsquery(lexemes)), TSQUERY)
    score = func.ts_rank_cd(DocumentChunk.search_vector, tsquery, _RANK_NORMALIZATION)
    chunk_terms = func.unnest(DocumentChunk.search_vector).table_valued("lexeme")
    matched = (
        select(func.array_agg(chunk_terms.c.lexeme))
        .where(chunk_terms.c.lexeme == any_(literal(lexemes, ARRAY(Text))))
        .scalar_subquery()
    )
    hits = (
        select(*_CHUNK_COLUMNS, score.label("score"), matched.label("matched"))
        .where(DocumentChunk.search_vector.op("@@")(tsquery), _in_scope(scope))
        # Ties go by content, not by the random chunk id, so re-ingesting the same text
        # gives the same ranking.
        .order_by(score.desc(), DocumentChunk.content_hash, DocumentChunk.id)
        .limit(limit)
        .subquery("hits")
    )
    rows = db.execute(
        _with_documents(hits).order_by(hits.c.score.desc(), hits.c.content_hash, hits.c.id)
    )
    return [
        _candidate(
            row,
            keyword_rank=rank,
            keyword_score=row.score,
            matched_terms=[term for term in lexemes if term in (row.matched or ())],
        )
        for rank, row in enumerate(rows, start=1)
    ]


def query_lexemes(db: Session, query: str) -> list[str]:
    """The query's terms as FTS_CONFIG lexemes (stemmed, stop words removed), in query
    order. The text search configuration comes from settings, never from user input."""
    params = {"config": get_settings().fts_config, "query": query}
    return list(db.scalars(_QUERY_LEXEMES, params))


def or_tsquery(lexemes: Sequence[str]) -> str:
    """tsquery text matching any of `lexemes`. Every lexeme is quoted (quotes and
    backslashes escaped), so no query text is ever parsed as a tsquery operator."""
    return " | ".join("'" + lex.replace("\\", "\\\\").replace("'", "''") + "'" for lex in lexemes)


def fuse(
    vector_hits: Sequence[Candidate], keyword_hits: Sequence[Candidate], *, rrf_k: int
) -> list[Candidate]:
    """Reciprocal Rank Fusion: a chunk scores the sum of 1 / (rrf_k + rank) over the
    lists that found it (1-based ranks). A chunk found by both retrievers keeps the
    evidence of both; ties keep vector-first order."""
    fused: dict[uuid.UUID, Candidate] = {}
    for rank, candidate in enumerate(vector_hits, start=1):
        candidate.rrf_score = 1 / (rrf_k + rank)
        fused[candidate.chunk_id] = candidate
    for rank, candidate in enumerate(keyword_hits, start=1):
        score = 1 / (rrf_k + rank)
        merged = fused.get(candidate.chunk_id)
        if merged is None:
            candidate.rrf_score = score
            fused[candidate.chunk_id] = candidate
        else:
            merged.keyword_rank = candidate.keyword_rank
            merged.keyword_score = candidate.keyword_score
            merged.matched_terms = candidate.matched_terms
            merged.rrf_score = (merged.rrf_score or 0.0) + score
    return sorted(fused.values(), key=lambda c: -(c.rrf_score or 0.0))


def _rerank(
    reranker: Reranker, query: str, candidates: list[Candidate], *, min_score: float | None
) -> list[Candidate]:
    """Order by cross-encoder relevance of heading path + text, dropping candidates
    scored below `min_score`."""
    passages = [
        f"{c.heading_path}\n\n{c.content}" if c.heading_path else c.content for c in candidates
    ]
    scored = list(zip(candidates, map(float, reranker.score(query, passages)), strict=True))
    for candidate, score in scored:
        candidate.rerank_score = score
    scored.sort(key=lambda pair: -pair[1])  # stable: ties keep the fused order
    return [c for c, score in scored if min_score is None or score >= min_score]


def _in_scope(scope: SearchScope) -> ColumnElement[bool]:
    """Chunks of documents the principal may read that also match the scope's filters.
    Filters only narrow: an id the caller cannot read simply matches nothing."""
    documents = select(Document.id).where(readable_documents_clause(scope.principal))
    if scope.workspace_ids is not None:
        documents = documents.where(Document.workspace_id.in_(scope.workspace_ids))
    if scope.document_ids is not None:
        documents = documents.where(Document.id.in_(scope.document_ids))
    if scope.mime_types is not None:
        documents = documents.where(Document.mime_type.in_(scope.mime_types))
    return DocumentChunk.document_id.in_(documents)


def _with_documents(hits: Subquery) -> Select[Any]:
    return select(
        hits,
        Document.title,
        Document.workspace_id,
        Document.mime_type,
        Document.flagged_text_included,
        Document.indexed_version,
    ).join(Document, Document.id == hits.c.document_id)


def _candidate(row: Row[Any], **evidence: Any) -> Candidate:
    return Candidate(
        chunk_id=row.id,
        document_id=row.document_id,
        document_title=row.title,
        workspace_id=row.workspace_id,
        mime_type=row.mime_type,
        chunk_index=row.chunk_index,
        content=row.content,
        content_hash=row.content_hash,
        token_count=row.token_count,
        page_number=row.page_number,
        page_end=row.page_end,
        section_title=row.section_title,
        heading_path=row.heading_path,
        metadata=row.chunk_metadata or {},
        include_flagged=row.flagged_text_included,
        document_version=row.indexed_version,
        **evidence,
    )


@contextmanager
def _timed(timings: dict[str, float], stage: str) -> Iterator[None]:
    started = time.perf_counter()
    yield
    timings[stage] = _elapsed_ms(started)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
