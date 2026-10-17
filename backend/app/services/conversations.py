"""Conversation history, private to its owner within the active organization."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.schemas.chat import ConversationOut, MessageOut
from app.schemas.common import Page, PageParams
from app.services.access import Principal
from app.services.audit import RequestMeta


def list_conversations(
    db: Session, principal: Principal, *, page: PageParams
) -> Page[ConversationOut]:
    raise NotImplementedError


def list_messages(
    db: Session, principal: Principal, conversation_id: uuid.UUID, *, page: PageParams
) -> Page[MessageOut]:
    raise NotImplementedError


def delete_conversation(
    db: Session, principal: Principal, conversation_id: uuid.UUID, *, meta: RequestMeta
) -> None:
    raise NotImplementedError
