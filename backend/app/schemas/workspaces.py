from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import WorkspaceRole
from app.schemas.common import Schema


class WorkspaceCreate(BaseModel):
    # Trimmed, so "General " cannot sit next to "General" (names are unique per org).
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class WorkspaceOut(Schema):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    my_role: WorkspaceRole | None
    member_count: int
    document_count: int
    can_manage: bool
    can_upload: bool


class WorkspaceMemberOut(Schema):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: WorkspaceRole
    added_at: datetime


class WorkspaceMemberAdd(BaseModel):
    user_id: uuid.UUID
    role: WorkspaceRole = WorkspaceRole.VIEWER


class WorkspaceMemberUpdate(BaseModel):
    role: WorkspaceRole
