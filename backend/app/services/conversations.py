"""Conversation history, private to its owner within the active organization.

Stored sources and citations are rendered from the message's JSON; when the caller can
no longer read a document an answer drew on (access revoked, document deleted), its
excerpts are redacted and the answer itself is withheld, at read time, through the same
access predicate every read path uses."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import Conversation, Document, Message
from app.schemas.chat import (
    CitationOut,
    ClaimOut,
    ConversationOut,
    DocumentChange,
    MessageOut,
    SourceRef,
)
from app.schemas.common import Page, PageParams
from app.services import access, audit
from app.services.access import Principal
from app.services.audit import AuditAction, RequestMeta

# Message.retrieval_metadata key set by app/services/chat.py: the documents behind the
# earlier answers that were part of this answer's prompt as conversation history.
HISTORY_DOCUMENTS = "history_document_ids"
# Metadata that can quote the answer or the history; withheld along with the answer.
_DERIVED = ("search_query", "unverified_quotes", "claims")


def document_ids(message: Message) -> set[uuid.UUID]:
    """Every document an answer may draw on: its own passages and, through the history
    in its prompt, those of earlier answers. Any of them can surface in it, cited or not."""
    refs = [source["document_id"] for source in message.sources or []]
    refs += (message.retrieval_metadata or {}).get(HISTORY_DOCUMENTS, [])
    return {uuid.UUID(ref) for ref in refs}


def without_derived(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key not in _DERIVED}


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
    # Citations only ever point at a message's own sources, so these cover them too.
    used = {row.id: document_ids(row) for row in rows}
    readable = access.readable_document_ids(db, principal, set().union(*used.values()))
    items = [
        _redact(message_out(row), readable, withhold=not used[row.id] <= readable) for row in rows
    ]
    _mark_changed_documents(db, [item for item in items if not item.redacted])
    return Page(
        items=items,
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
    claims = metadata.pop("claims", [])
    metadata.pop(HISTORY_DOCUMENTS, None)  # read by document_ids, not part of the contract
    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
        status=message.status,
        answer_status=answer_status,
        sources=[SourceRef.model_validate(source) for source in message.sources or []],
        citations=[CitationOut.model_validate(citation) for citation in citations],
        claims=[ClaimOut.model_validate(claim) for claim in claims],
        metadata=metadata,
        created_at=message.created_at,
    )


def _redact(message: MessageOut, readable: set[uuid.UUID], *, withhold: bool) -> MessageOut:
    """Blank the text of sources and citations whose document the caller cannot read now
    (section titles are document text too). Titles stay: the caller saw them already.
    `withhold` also hides the answer, which may restate such a document in its own words.
    The stored row is untouched, so restoring access brings everything back."""
    hidden = {"excerpt": None, "section_title": None, "redacted": True}
    update: dict[str, Any] = {
        "sources": [
            s if s.document_id in readable else s.model_copy(update=hidden) for s in message.sources
        ],
        "citations": [
            c if c.document_id in readable else c.model_copy(update=hidden)
            for c in message.citations
        ],
    }
    if withhold:
        update |= {
            "content": "",
            "redacted": True,
            "claims": [],
            "metadata": without_derived(message.metadata),
        }
    return message.model_copy(update=update)


def _mark_changed_documents(db: Session, messages: list[MessageOut]) -> None:
    """Fill `changed_documents`: cited documents indexed at a newer version since the
    answer. Callers pass only messages whose documents the caller can read now. Sources
    stored before versions were recorded have no version and never count as changed."""
    used: dict[uuid.UUID, dict[uuid.UUID, int]] = {}
    for message in messages:
        for source in message.sources:
            if source.cited and source.document_version is not None:
                versions = used.setdefault(message.id, {})
                # The oldest: a new version swapped in between the vector and keyword
                # searches can give one answer passages of both.
                versions[source.document_id] = min(
                    versions.get(source.document_id, source.document_version),
                    source.document_version,
                )
    ids = set().union(*used.values())
    if not ids:
        return
    current = {
        row.id: row
        for row in db.execute(
            select(
                Document.id, Document.title, Document.indexed_version, Document.version_indexed_at
            ).where(Document.id.in_(ids))
        )
    }
    for message in messages:
        for document_id, version in used.get(message.id, {}).items():
            now = current.get(document_id)
            if now is None or now.indexed_version is None or now.indexed_version <= version:
                continue
            message.changed_documents.append(
                DocumentChange(
                    document_id=document_id,
                    document_title=now.title,
                    used_version=version,
                    current_version=now.indexed_version,
                    current_since=now.version_indexed_at,
                )
            )
