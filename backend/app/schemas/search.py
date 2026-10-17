from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.core.filetypes import FileType


class SearchFilters(BaseModel):
    """Narrow the search. Always intersected with what the caller may read."""

    workspace_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    file_types: list[FileType] | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    mode: Literal["vector", "keyword", "hybrid"] | None = Field(
        default=None, description="Defaults to RETRIEVAL_MODE."
    )
    rerank: bool | None = Field(default=None, description="Defaults to RERANK_ENABLED.")
    top_k: int = Field(default=10, ge=1, le=50)


class HighlightSegment(BaseModel):
    text: str
    match: bool


class MatchExplanation(BaseModel):
    """Why a passage matched: each retriever's rank/score and the fused/reranked score."""

    vector_rank: int | None = None
    vector_similarity: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    matched_terms: list[str] = Field(default_factory=list)
    rrf_score: float | None = None
    rerank_score: float | None = None
    final_rank: int


class SearchResult(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    workspace_id: uuid.UUID
    workspace_name: str
    file_type: FileType | None
    mime_type: str
    page_number: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    heading_path: str | None = None
    content: str
    highlights: list[HighlightSegment]
    score: float
    explanation: MatchExplanation


class SearchResponse(BaseModel):
    query: str
    mode: Literal["vector", "keyword", "hybrid"]
    reranked: bool
    results: list[SearchResult]
    total_candidates: int
    timings_ms: dict[str, float]
