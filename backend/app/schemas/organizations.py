from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models import OrgRole
from app.schemas.common import Schema


class OrganizationOut(Schema):
    id: uuid.UUID
    name: str
    created_at: datetime
    member_count: int


class OrganizationUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class MemberOut(Schema):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: OrgRole
    joined_at: datetime


class UpdateMemberRoleRequest(BaseModel):
    role: OrgRole


class InvitationCreate(BaseModel):
    email: EmailStr
    role: OrgRole = OrgRole.EMPLOYEE

    @field_validator("email")
    @classmethod
    def lower_email(cls, value: str) -> str:
        return value.strip().lower()


class InvitationOut(Schema):
    id: uuid.UUID
    email: str
    role: OrgRole
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    invited_by_email: str | None = None


class InvitationCreated(InvitationOut):
    token: str
    invite_url: str
