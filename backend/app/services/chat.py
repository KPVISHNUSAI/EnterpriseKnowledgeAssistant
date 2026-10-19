"""Chat orchestration: conversation persistence around the RAG answer pipeline.

Conversations are private to their owner within the active organization. Retrieval runs
with a SearchScope built from the request, which can only narrow what the caller may
read. Every assistant turn stores the passages it was shown and the validated citations,
so history can be rendered (and redacted) later without re-running retrieval.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, ValidationFailedError
from app.core.filetypes import file_type_for_mime
from app.core.sse import encode_event
from app.db.base import utcnow
from app.db.session import session_scope
from app.models import Conversation, Message, MessageRole, MessageStatus
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMProvider
from app.providers.rerankers import Reranker
from app.rag.answer import ERROR_MESSAGE, stream_answer
from app.rag.query import normalize_query
from app.rag.types import (
    AnswerCompleted,
    AnswerDelta,
    AnswerResult,
    AnswerStatus,
    ChatTurn,
    ContextPassage,
    RetrievalCompleted,
    RetrievalOptions,
    SearchScope,
)
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    CitationOut,
    SourceRef,
    SourceRelevance,
    StreamDoneEvent,
    StreamErrorEvent,
    StreamMetaEvent,
    StreamSourcesEvent,
    StreamTokenEvent,
)
from app.services import access, audit, usage
from app.services.access import Principal
from app.services.audit import AuditAction, RequestMeta
from app.services.conversations import (
    HISTORY_DOCUMENTS,
    document_ids,
    message_out,
    own_conversation,
)

logger = logging.getLogger(__name__)

_TITLE_CHARS = 80
_FLUSH_SECONDS = 0.05  # token events are coalesced for at most this long


def chat(
    db: Session,
    principal: Principal,
    body: ChatRequest,
    *,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
    llm: LLMProvider | None,
    meta: RequestMeta,
) -> ChatResponse:
    settings = get_settings()
    question = normalize_query(body.message, max_chars=settings.max_query_chars)
    conversation = _conversation(db, principal, body, question)
    history, history_documents = _history(db, principal, conversation)
    user_message = _message(conversation, MessageRole.USER, question)
    db.add(user_message)
    result: AnswerResult | None = None
    for event in stream_answer(
        db,
        _scope(principal, body),
        question,
        history=history,
        options=RetrievalOptions.from_settings(settings),
        embedder=embedder,
        reranker=reranker,
        llm=llm,
    ):
        if isinstance(event, RetrievalCompleted):
            db.commit()  # the question is saved; no connection is held while the model works
        elif isinstance(event, AnswerCompleted):
            result = event.result
    assert result is not None  # noqa: S101 - the pipeline always completes
    assistant = _message(conversation, MessageRole.ASSISTANT, "")
    fill_message(assistant, result, _final_status(result), history_documents)
    db.add(assistant)
    db.flush()  # assigns the id the usage row refers to
    conversation.updated_at = utcnow()
    usage.record(
        db,
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        result=result,
        message_id=assistant.id,
    )
    _audit_answer(db, principal, assistant, history_documents, meta)
    db.commit()
    return ChatResponse(
        conversation_id=conversation.id,
        user_message=message_out(user_message),
        assistant_message=message_out(assistant),
    )


def start_chat_stream(
    db: Session,
    principal: Principal,
    body: ChatRequest,
    *,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
    llm: LLMProvider | None,
    meta: RequestMeta,
) -> Callable[[threading.Event], Iterator[bytes]]:
    """Validate and persist the user turn synchronously (errors become HTTP statuses),
    then return the SSE producer. The producer runs in another thread with its own
    database session and must stop and persist a partial answer once `cancel` is set."""
    settings = get_settings()
    question = normalize_query(body.message, max_chars=settings.max_query_chars)
    conversation = _conversation(db, principal, body, question)
    history, history_documents = _history(db, principal, conversation)
    user_message = _message(conversation, MessageRole.USER, question)
    placeholder = _message(conversation, MessageRole.ASSISTANT, "", status=MessageStatus.STREAMING)
    db.add_all([user_message, placeholder])
    conversation.updated_at = utcnow()
    db.commit()

    ids = StreamMetaEvent(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_message_id=placeholder.id,
    )
    scope = _scope(principal, body)
    options = RetrievalOptions.from_settings(settings)

    def produce(cancel: threading.Event) -> Iterator[bytes]:
        with session_scope(principal.organization_id) as session:
            state = _StreamState()
            try:
                yield encode_event("meta", ids)
                yield from _events(session, state, cancel)
            finally:
                # Also runs when the bridge abandons this generator after the client
                # disconnected, at any yield including the first: the placeholder must
                # never stay in status "streaming".
                final = _persist(session, state, cancel)
            yield _terminal_event(state, final)

    def _events(session: Session, state: _StreamState, cancel: threading.Event) -> Iterator[bytes]:
        pending: list[str] = []
        flushed = time.monotonic()
        try:
            with closing(
                stream_answer(
                    session,
                    scope,
                    question,
                    history=history,
                    options=options,
                    embedder=embedder,
                    reranker=reranker,
                    llm=llm,
                    cancel=cancel,
                )
            ) as events:
                for event in events:
                    if isinstance(event, RetrievalCompleted):
                        state.passages = event.passages
                        state.search_query = event.retrieval.search_query
                        yield encode_event(
                            "sources",
                            StreamSourcesEvent(
                                sources=[source_ref(p, cited=False) for p in state.passages],
                                search_query=state.search_query,
                            ),
                        )
                        session.commit()  # hold no connection while the model generates
                    elif isinstance(event, AnswerDelta):
                        state.text.append(event.text)
                        pending.append(event.text)
                        if time.monotonic() - flushed >= _FLUSH_SECONDS:
                            yield encode_event("token", StreamTokenEvent(text="".join(pending)))
                            pending.clear()
                            flushed = time.monotonic()
                    else:
                        state.result = event.result
            if pending:
                yield encode_event("token", StreamTokenEvent(text="".join(pending)))
        except AppError as exc:
            logger.info("chat.stream_failed", extra={"code": exc.code})
            state.error = (exc.code, exc.message)
        except Exception:
            logger.exception("chat.stream_failed")
            state.error = ("internal_error", "The response failed unexpectedly")

    def _persist(session: Session, state: _StreamState, cancel: threading.Event) -> Message | None:
        try:
            session.rollback()  # a failed statement must not poison the final write
            message = session.get(Message, placeholder.id)
            if message is None:  # the conversation was deleted meanwhile
                return None
            if state.result is not None:
                fill_message(message, state.result, _final_status(state.result), history_documents)
                usage.record(
                    session,
                    organization_id=principal.organization_id,
                    user_id=principal.user_id,
                    result=state.result,
                    message_id=message.id,
                )
            else:  # abandoned or failed before the pipeline finished: keep what arrived
                message.content = "".join(state.text)
                message.status = (
                    MessageStatus.INCOMPLETE if cancel.is_set() else MessageStatus.ERROR
                )
                message.sources = [
                    source_ref(p, cited=False).model_dump(mode="json") for p in state.passages
                ]
                message.retrieval_metadata = {
                    "search_query": state.search_query,
                    **({"error": {"code": state.error[0]}} if state.error else {}),
                    **_history_metadata(history_documents),
                }
            # Also for an abandoned answer: its sources event already reached the client.
            _audit_answer(session, principal, message, history_documents, meta)
            session.commit()
            return message
        except Exception:
            logger.exception("chat.persist_failed", extra={"message_id": str(placeholder.id)})
            return None

    return produce


@dataclass
class _StreamState:
    passages: list[ContextPassage] = field(default_factory=list)
    search_query: str | None = None
    text: list[str] = field(default_factory=list)
    result: AnswerResult | None = None
    error: tuple[str, str] | None = None  # (code, safe message)


def _terminal_event(state: _StreamState, message: Message | None) -> bytes:
    if state.error is not None:
        code, text = state.error
        return encode_event("error", StreamErrorEvent(code=code, message=text))
    result = state.result
    if result is not None and result.status == AnswerStatus.ERROR:
        kind = str(result.metadata.get("error", {}).get("kind", "error"))
        return encode_event("error", StreamErrorEvent(code=f"llm_{kind}", message=ERROR_MESSAGE))
    if message is None:
        return encode_event(
            "error",
            StreamErrorEvent(code="internal_error", message="The answer could not be saved"),
        )
    return encode_event("done", StreamDoneEvent(message=message_out(message)))


# --- Persistence helpers -------------------------------------------------------------------


def fill_message(
    message: Message,
    result: AnswerResult,
    status: MessageStatus,
    history_documents: set[uuid.UUID],
) -> None:
    cited = {citation.source_id for citation in result.citations if citation.verified}
    message.content = result.answer
    message.status = status
    message.sources = [
        source_ref(passage, cited=passage.source_id in cited).model_dump(mode="json")
        for passage in result.passages
    ]
    message.retrieval_metadata = answer_metadata(result) | _history_metadata(history_documents)


def _audit_answer(
    db: Session,
    principal: Principal,
    message: Message,
    history_documents: set[uuid.UUID],
    meta: RequestMeta,
) -> None:
    """Which documents an answer was built from: identifiers only, no text."""
    sources = message.sources or []
    audit.record(
        db,
        AuditAction.CHAT_ANSWERED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="message",
        resource_id=message.id,
        metadata={
            "conversation_id": str(message.conversation_id),
            "status": message.status.value,
            "documents_retrieved": sorted({s["document_id"] for s in sources}),
            "documents_cited": sorted({s["document_id"] for s in sources if s["cited"]}),
            "documents_from_history": sorted(map(str, history_documents)),
        },
        meta=meta,
    )


def _history_metadata(documents: set[uuid.UUID]) -> dict[str, Any]:
    """What the answer inherited through its prompt's history, so it is withheld (and
    erased on deletion) along with the answers it builds on."""
    return {HISTORY_DOCUMENTS: sorted(map(str, documents))} if documents else {}


def source_ref(passage: ContextPassage, *, cited: bool) -> SourceRef:
    c = passage.candidate
    return SourceRef(
        source_id=passage.source_id,
        chunk_id=c.chunk_id,
        document_id=c.document_id,
        document_title=c.document_title,
        workspace_id=c.workspace_id,
        file_type=file_type_for_mime(c.mime_type),
        page_number=c.page_number,
        page_end=c.page_end,
        section_title=c.section_title,
        excerpt=passage.text,
        relevance=SourceRelevance(
            rerank_score=c.rerank_score,
            rrf_score=c.rrf_score,
            vector_similarity=c.vector_similarity,
            keyword_score=c.keyword_score,
        ),
        cited=cited,
    )


def answer_metadata(result: AnswerResult) -> dict[str, Any]:
    """Stored as Message.retrieval_metadata. The documents service scrubs the `citations`
    entries (document_id, excerpt, document_title, section_title) when a document is
    deleted, so those keys must keep their names."""
    return {
        "answer_status": result.status.value,
        "citations": [
            CitationOut(
                source_id=c.source_id,
                verified=c.verified,
                document_id=c.document_id,
                document_title=c.document_title,
                page_number=c.page_number,
                section_title=c.section_title,
                excerpt=c.excerpt,
            ).model_dump(mode="json")
            for c in result.citations
        ],
        "invalid_citations": result.invalid_citations,
        "unverified_quotes": result.unverified_quotes,
        "search_query": result.retrieval.search_query,
        "mode": result.retrieval.options.mode,
        "reranked": result.retrieval.reranked,
        "timings_ms": result.timings_ms,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_usd": result.usage.cost_usd,
        },
        "provider": result.provider,
        "model": result.model,
        **result.metadata,
    }


def _final_status(result: AnswerResult) -> MessageStatus:
    if result.status == AnswerStatus.ERROR:
        return MessageStatus.ERROR
    if result.metadata.get("cancelled"):
        return MessageStatus.INCOMPLETE
    return MessageStatus.COMPLETE


def _conversation(
    db: Session, principal: Principal, body: ChatRequest, question: str
) -> Conversation:
    if body.conversation_id is not None:
        return own_conversation(db, principal, body.conversation_id)
    conversation = Conversation(
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        title=_title(body.message, question),
    )
    db.add(conversation)
    db.flush()  # messages reference it; no relationship() orders the inserts
    return conversation


def _title(message: str, question: str) -> str:
    """First line of the question, whitespace-collapsed, cut at a word boundary."""
    try:
        return normalize_query(message.strip().split("\n", 1)[0], max_chars=_TITLE_CHARS)
    except ValidationFailedError:  # first line was only invisible characters
        return normalize_query(question, max_chars=_TITLE_CHARS)


def _history(
    db: Session, principal: Principal, conversation: Conversation
) -> tuple[list[ChatTurn], set[uuid.UUID]]:
    """The last CHAT_HISTORY_MESSAGES complete turns, oldest first, and the documents the
    answers among them drew on. An answer that drew on a document the caller can no
    longer read is left out, as it is withheld from the conversation: its text must not
    reach a new prompt (see conversations._redact)."""
    rows = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.status == MessageStatus.COMPLETE,
        )
        # Role breaks same-timestamp ties: "assistant" < "user", so reversed = user first.
        .order_by(Message.created_at.desc(), Message.role.asc())
        .limit(get_settings().chat_history_messages)
    ).all()
    used = {row.id: document_ids(row) for row in rows}
    readable = access.readable_document_ids(db, principal, set().union(*used.values()))
    kept = [row for row in reversed(rows) if used[row.id] <= readable]
    turns = [
        ChatTurn(role="user" if row.role == MessageRole.USER else "assistant", content=row.content)
        for row in kept
    ]
    return turns, set().union(*(used[row.id] for row in kept))


def _message(
    conversation: Conversation,
    role: MessageRole,
    content: str,
    *,
    status: MessageStatus = MessageStatus.COMPLETE,
) -> Message:
    return Message(
        conversation_id=conversation.id,
        organization_id=conversation.organization_id,
        role=role,
        content=content,
        status=status,
    )


def _scope(principal: Principal, body: ChatRequest) -> SearchScope:
    """Request filters only narrow retrieval (intersected with access inside the SQL);
    an empty or missing list means no restriction."""
    return SearchScope(
        principal=principal,
        workspace_ids=tuple(body.scope.workspace_ids) if body.scope.workspace_ids else None,
        document_ids=tuple(body.scope.document_ids) if body.scope.document_ids else None,
    )
