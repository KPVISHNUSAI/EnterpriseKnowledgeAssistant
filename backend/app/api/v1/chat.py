from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal, get_request_meta, rate_limit_user
from app.core.sse import SSE_HEADERS, stream_from_thread
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMProvider
from app.providers.registry import get_embedder, get_llm, get_reranker
from app.providers.rerankers import Reranker
from app.schemas import chat as chat_schemas
from app.schemas.chat import ChatRequest, ChatResponse, ConversationOut, MessageOut
from app.schemas.common import Page, PageParams, errors
from app.services import chat as chat_service
from app.services import conversations as conversation_service
from app.services.access import Principal
from app.services.audit import RequestMeta

router = APIRouter(tags=["chat"])

_chat_limit = rate_limit_user("chat", "rate_limit_chat_per_minute")


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses=errors(401, 404, 422, 429, 503),
    dependencies=[_chat_limit],
)
def chat(
    body: ChatRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    embedder: EmbeddingProvider = Depends(get_embedder),
    reranker: Reranker | None = Depends(get_reranker),
    llm: LLMProvider | None = Depends(get_llm),
) -> ChatResponse:
    """Ask a question; returns the grounded answer with validated citations.

    Starts a new conversation unless `conversation_id` (owned by the caller) is given.
    Retrieval only sees documents the caller can read, intersected with `scope`.
    `answer_status` is `insufficient_evidence` when the documents do not support an
    answer.
    """
    return chat_service.chat(db, principal, body, embedder=embedder, reranker=reranker, llm=llm)


@router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (chat_schemas.__doc__ or "").strip(),
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **errors(401, 404, 422, 429),
    },
    dependencies=[_chat_limit],
)
def chat_stream(
    body: ChatRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    embedder: EmbeddingProvider = Depends(get_embedder),
    reranker: Reranker | None = Depends(get_reranker),
    llm: LLMProvider | None = Depends(get_llm),
) -> StreamingResponse:
    """Same as POST /chat, streamed as Server-Sent Events (see the 200 response).

    Request errors (unknown conversation, invalid scope) are returned as normal HTTP
    errors before the stream starts. If the client disconnects, generation stops and
    the partial answer is saved with status `incomplete`.
    """
    produce = chat_service.start_chat_stream(
        db, principal, body, embedder=embedder, reranker=reranker, llm=llm
    )
    return StreamingResponse(
        stream_from_thread(produce), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get(
    "/conversations",
    response_model=Page[ConversationOut],
    tags=["conversations"],
    responses=errors(401),
)
def list_conversations(
    page: PageParams = Depends(),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Page[ConversationOut]:
    """The caller's own conversations in the active organization, most recent first."""
    return conversation_service.list_conversations(db, principal, page=page)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=Page[MessageOut],
    tags=["conversations"],
    responses=errors(401, 404),
)
def list_messages(
    conversation_id: uuid.UUID,
    page: PageParams = Depends(),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Page[MessageOut]:
    """Messages in chronological order. Sources from documents the caller can no longer
    read are redacted."""
    return conversation_service.list_messages(db, principal, conversation_id, page=page)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=204,
    tags=["conversations"],
    responses=errors(401, 404),
)
def delete_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    conversation_service.delete_conversation(db, principal, conversation_id, meta=meta)
