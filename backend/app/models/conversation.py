from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, str_enum


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    COMPLETE = "complete"
    STREAMING = "streaming"
    INCOMPLETE = "incomplete"  # client disconnected or generation was cancelled
    ERROR = "error"


class Conversation(UUIDPrimaryKey, Timestamps, Base):
    """Private to its owner. The composite FK to organization_memberships deletes a
    user's conversations in an organization when they leave it."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            ondelete="CASCADE",
        ),
        Index("ix_conversations_org_user_updated", "organization_id", "user_id", "updated_at"),
    )

    organization_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(String(200))


class Message(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
        ),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index(
            "ix_messages_sources",
            "sources",
            postgresql_using="gin",
            postgresql_ops={"sources": "jsonb_path_ops"},
        ),
    )

    conversation_id: Mapped[uuid.UUID]
    organization_id: Mapped[uuid.UUID]
    role: Mapped[MessageRole] = mapped_column(str_enum(MessageRole, "message_role"))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[MessageStatus] = mapped_column(
        str_enum(MessageStatus, "message_status"), default=MessageStatus.COMPLETE
    )
    # Serialized SourceRef/CitationOut payloads (see app/schemas/chat.py).
    sources: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    retrieval_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
