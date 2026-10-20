"""Domain types for the RAG pipeline (independent of HTTP and the UI).

Flow: SearchScope + query -> RetrievalResult (Candidates) -> ContextPassages (S1..Sn)
-> AnswerResult (validated Citations). The eval suite drives these types directly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from app.core.config import Settings
from app.services.access import Principal

RetrievalMode = Literal["vector", "keyword", "hybrid"]


@dataclass(frozen=True)
class SearchScope:
    """Who is searching plus optional narrowing filters. Filters are always intersected
    with the principal's access (app/services/access.py), never used to widen it."""

    principal: Principal
    workspace_ids: tuple[uuid.UUID, ...] | None = None
    document_ids: tuple[uuid.UUID, ...] | None = None
    mime_types: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RetrievalOptions:
    mode: RetrievalMode
    rerank: bool
    vector_top_k: int
    keyword_top_k: int
    rrf_k: int
    fused_top_k: int  # candidates kept after fusion (and sent to the reranker)
    final_top_k: int  # candidates returned / considered for context
    min_rerank_score: float | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        mode: RetrievalMode | None = None,
        rerank: bool | None = None,
        final_top_k: int | None = None,
    ) -> RetrievalOptions:
        return cls(
            mode=mode or settings.retrieval_mode,
            rerank=settings.rerank_enabled if rerank is None else rerank,
            vector_top_k=settings.retrieval_vector_top_k,
            keyword_top_k=settings.retrieval_keyword_top_k,
            rrf_k=settings.retrieval_rrf_k,
            fused_top_k=settings.retrieval_fused_top_k,
            final_top_k=final_top_k or settings.retrieval_final_top_k,
            min_rerank_score=settings.retrieval_min_rerank_score,
        )


@dataclass
class Candidate:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    workspace_id: uuid.UUID
    mime_type: str
    chunk_index: int
    content: str
    content_hash: str
    token_count: int
    page_number: int | None
    page_end: int | None
    section_title: str | None
    heading_path: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    # A document manager let this document's flagged text (metadata["flags"]) into answers.
    include_flagged: bool = False
    # Per-method evidence, kept for explainability ("why did this match?").
    vector_rank: int | None = None
    vector_similarity: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    matched_terms: list[str] = field(default_factory=list)
    highlights: list[tuple[str, bool]] | None = None  # (text, is_match) segments


@dataclass
class RetrievalResult:
    query: str  # the user's normalized question
    search_query: str  # what retrieval actually used (rewritten for follow-ups)
    candidates: list[Candidate]  # final order, at most options.final_top_k
    total_candidates: int  # unique candidates found before truncation
    options: RetrievalOptions
    reranked: bool
    timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class ContextPassage:
    source_id: str  # "S1", "S2", ... referenced by citations
    candidate: Candidate  # primary candidate (metadata for citations)
    text: str  # document text shown to the LLM (may merge adjacent chunks; flags removed)
    token_count: int
    chunk_ids: list[uuid.UUID] = field(default_factory=list)


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNVERIFIED = "unverified"  # claims an answer but no citation maps to a retrieved source
    REFUSED = "refused"  # provider safety refusal
    ERROR = "error"


@dataclass
class Citation:
    source_id: str
    verified: bool  # True only if source_id maps to a passage given to the model
    document_id: uuid.UUID | None = None
    document_title: str | None = None
    page_number: int | None = None
    section_title: str | None = None
    excerpt: str | None = None


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class ChatTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass
class AnswerResult:
    question: str
    answer: str  # final text shown to the user (status marker stripped)
    status: AnswerStatus
    retrieval: RetrievalResult
    passages: list[ContextPassage]
    citations: list[Citation]
    invalid_citations: list[str] = field(default_factory=list)
    unverified_quotes: list[str] = field(default_factory=list)
    provider: str = "extractive"
    model: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    rewrite_usage: TokenUsage | None = None
    rewrite_model: str | None = None  # model that produced the rewrite (usage reporting)
    timings_ms: dict[str, float] = field(default_factory=dict)
    # Generation flags: truncated (stop_reason max_tokens), cancelled, error {kind, retryable}.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalCompleted:
    retrieval: RetrievalResult
    passages: list[ContextPassage]


@dataclass
class AnswerDelta:
    text: str


@dataclass
class AnswerCompleted:
    result: AnswerResult


PipelineEvent = RetrievalCompleted | AnswerDelta | AnswerCompleted
