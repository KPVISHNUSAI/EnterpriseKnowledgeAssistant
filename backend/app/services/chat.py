"""Chat orchestration: conversation persistence around the RAG answer pipeline."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

from sqlalchemy.orm import Session

from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMProvider
from app.providers.rerankers import Reranker
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.access import Principal


def chat(
    db: Session,
    principal: Principal,
    body: ChatRequest,
    *,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
    llm: LLMProvider | None,
) -> ChatResponse:
    raise NotImplementedError


def start_chat_stream(
    db: Session,
    principal: Principal,
    body: ChatRequest,
    *,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
    llm: LLMProvider | None,
) -> Callable[[threading.Event], Iterator[bytes]]:
    """Validate and persist the user turn synchronously (errors become HTTP statuses),
    then return the SSE producer. The producer runs in another thread with its own
    database session and must stop and persist a partial answer once `cancel` is set."""
    raise NotImplementedError
