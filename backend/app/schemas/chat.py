"""Chat contracts, including the Server-Sent Events emitted by POST /chat/stream:

    event: meta     data: StreamMetaEvent      (first; ids for the new messages)
    event: sources  data: StreamSourcesEvent   (passages the answer will be based on)
    event: token    data: StreamTokenEvent     (answer text deltas, many)
    event: done     data: StreamDoneEvent      (final persisted assistant message)
    event: error    data: StreamErrorEvent     (terminal; partial text is persisted)

Comment lines (": keep-alive") are sent every 15s while the model is thinking.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.filetypes import FileType
from app.models import MessageRole, MessageStatus
from app.rag.types import AnswerStatus


class ChatScope(BaseModel):
    """Restrict retrieval to these workspaces/documents (intersected with access)."""

    workspace_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None
    scope: ChatScope = Field(default_factory=ChatScope)


class SourceRelevance(BaseModel):
    rerank_score: float | None = None
    rrf_score: float | None = None
    vector_similarity: float | None = None
    keyword_score: float | None = None


class SourceRef(BaseModel):
    """A passage supplied to the model, labelled S1..Sn."""

    source_id: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    workspace_id: uuid.UUID
    file_type: FileType | None = None
    page_number: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    excerpt: str | None = Field(
        description="Passage text (null when redacted because access was revoked)."
    )
    relevance: SourceRelevance
    cited: bool = False
    redacted: bool = False


class CitationOut(BaseModel):
    source_id: str
    verified: bool = Field(description="True only if the marker maps to a retrieved source.")
    document_id: uuid.UUID | None = None
    document_title: str | None = None
    page_number: int | None = None
    section_title: str | None = None
    excerpt: str | None = Field(default=None, description="Best supporting sentence.")
    redacted: bool = Field(
        default=False, description="True when the caller can no longer read the document."
    )


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    content: str
    status: MessageStatus
    answer_status: AnswerStatus | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    citations: list[CitationOut] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Retrieval summary: search_query, mode, reranked, timings_ms, usage.",
    )
    created_at: datetime


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    user_message: MessageOut
    assistant_message: MessageOut


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int


class StreamMetaEvent(BaseModel):
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    search_query: str | None = None


class StreamSourcesEvent(BaseModel):
    sources: list[SourceRef]
    search_query: str | None = Field(
        default=None, description="What retrieval searched for (rewritten for follow-ups)."
    )


class StreamTokenEvent(BaseModel):
    text: str


class StreamDoneEvent(BaseModel):
    message: MessageOut


class StreamErrorEvent(BaseModel):
    code: str
    message: str
