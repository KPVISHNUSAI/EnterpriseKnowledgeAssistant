from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.chat import ConversationOut
from app.schemas.documents import DocumentOut


class ActivityItem(BaseModel):
    id: uuid.UUID
    action: str
    actor_email: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    summary: str
    created_at: datetime


class DashboardResponse(BaseModel):
    """Counts cover only documents the caller can read."""

    documents_total: int
    documents_by_status: dict[str, int]
    workspaces_count: int
    recent_documents: list[DocumentOut]
    recent_conversations: list[ConversationOut]
    recent_activity: list[ActivityItem]


class HealthResponse(BaseModel):
    status: str


class ReadinessCheck(BaseModel):
    ok: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, ReadinessCheck]
