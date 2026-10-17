"""Conversation history, private to its owner within the active organization.

Stored sources and citations are rendered from the message's JSON; when the caller can
no longer read a cited document (access revoked, document deleted), the excerpt is
redacted at read time through the same access predicate every read path uses."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import Conversation, Message
from app.schemas.chat import CitationOut, ConversationOut, MessageOut, SourceRef
from app.schemas.common import Page, PageParams
from app.services import access, audit
from app.services.access import Principal
from app.services.audit import AuditAction, RequestMeta


def own_conversation(db: Session, principal: Principal, conversation_id: uuid.UUID) -> Conversation:
    """The caller's conversation or 404: another user's looks exactly like a missing one."""
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.organization_id == principal.organization_id,
            Conversation.user_id == principal.user_id,
        )
    )
    if conversation is None:
        raise NotFoundError("Conversation not found", code="conversation_not_found")
    return conversation


def list_conversations(
    db: Session, principal: Principal, *, page: PageParams
) -> Page[ConversationOut]:
    mine = and_(
        Conversation.organization_id == principal.organization_id,
        Conversation.user_id == principal.user_id,
    )
    message_count = (
        select(func.count())
        .where(
            Message.conversation_id == Conversation.id,
            Message.organization_id == Conversation.organization_id,
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    rows = db.execute(
        select(Conversation, message_count)
        .where(mine)
        .order_by(Conversation.updated_at.desc(), Conversation.id)
        .offset(page.offset)
        .limit(page.page_size)
    )
    return Page(
        items=[
            ConversationOut(
                id=conversation.id,
                title=conversation.title,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
                message_count=count,
            )
            for conversation, count in rows
        ],
        total=db.scalar(select(func.count()).where(mine)) or 0,
        page=page.page,
        page_size=page.page_size,
    )


def list_messages(
    db: Session, principal: Principal, conversation_id: uuid.UUID, *, page: PageParams
) -> Page[MessageOut]:
    conversation = own_conversation(db, principal, conversation_id)
    in_conversation = Message.conversation_id == conversation.id
    rows = db.scalars(
        select(Message)
        .where(in_conversation)
        # Role breaks same-timestamp ties: "user" sorts after "assistant", so desc = user first.
        .order_by(Message.created_at, Message.role.desc())
        .offset(page.offset)
        .limit(page.page_size)
    ).all()
    messages = [message_out(row) for row in rows]
    cited = {source.document_id for message in messages for source in message.sources} | {
        citation.document_id
        for message in messages
        for citation in message.citations
        if citation.document_id is not None
    }
    readable = access.readable_document_ids(db, principal, cited)
    return Page(
        items=[_redact(message, readable) for message in messages],
        total=db.scalar(select(func.count()).where(in_conversation)) or 0,
        page=page.page,
        page_size=page.page_size,
    )


def delete_conversation(
    db: Session, principal: Principal, conversation_id: uuid.UUID, *, meta: RequestMeta
) -> None:
    conversation = own_conversation(db, principal, conversation_id)
    db.delete(conversation)  # messages cascade
    audit.record(
        db,
        AuditAction.CONVERSATION_DELETED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="conversation",
        resource_id=conversation.id,
        meta=meta,
    )
    db.commit()


def message_out(message: Message) -> MessageOut:
    """MessageOut from the stored row: answer_status and citations live inside
    retrieval_metadata (see app/services/chat.py::answer_metadata)."""
    metadata = dict(message.retrieval_metadata or {})
    answer_status = metadata.pop("answer_status", None)
    citations = metadata.pop("citations", [])
    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
        status=message.status,
        answer_status=answer_status,
        sources=[SourceRef.model_validate(source) for source in message.sources or []],
        citations=[CitationOut.model_validate(citation) for citation in citations],
        metadata=metadata,
        created_at=message.created_at,
    )


def _redact(message: MessageOut, readable: set[uuid.UUID]) -> MessageOut:
    """Blank the text of sources and citations whose document the caller cannot read now
    (section titles are document text too). Titles stay: the caller saw them already."""
    hidden = {"excerpt": None, "section_title": None, "redacted": True}
    return message.model_copy(
        update={
            "sources": [
                s if s.document_id in readable else s.model_copy(update=hidden)
                for s in message.sources
            ],
            "citations": [
                c if c.document_id in readable else c.model_copy(update=hidden)
                for c in message.citations
            ],
        }
    )
