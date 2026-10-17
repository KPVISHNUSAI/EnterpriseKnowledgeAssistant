from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models import OrgRole, WorkspaceRole
from app.schemas.common import Schema


def _normalize_email(value: str) -> str:
    return value.strip().lower()


class RegisterRequest(BaseModel):
    email: EmailStr
    # Length policy (PASSWORD_MIN_LENGTH) is enforced by the service: 422 weak_password.
    password: str = Field(min_length=1, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    organization_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Required unless invite_token is given (creates a new organization).",
    )
    invite_token: str | None = Field(default=None, max_length=200)

    normalize_email = field_validator("email")(_normalize_email)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    normalize_email = field_validator("email")(_normalize_email)


class InvitationPreview(BaseModel):
    organization_name: str
    email: str
    role: OrgRole
    expires_at: datetime


class AcceptInvitationRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class SwitchOrganizationRequest(BaseModel):
    organization_id: uuid.UUID


class UpdateProfileRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)  # policy: see RegisterRequest


class UserOut(Schema):
    id: uuid.UUID
    email: str
    full_name: str
    created_at: datetime
    last_login_at: datetime | None = None


class OrganizationMembershipOut(Schema):
    id: uuid.UUID
    name: str
    role: OrgRole


class WorkspaceAccessOut(Schema):
    id: uuid.UUID
    name: str
    role: WorkspaceRole | None = Field(
        description="The caller's workspace role; null for org admins without membership."
    )


class Capabilities(BaseModel):
    """UI hints only - every action is re-authorized server-side."""

    can_create_workspace: bool
    can_manage_organization: bool
    can_view_audit_log: bool
    can_upload: bool


class MeResponse(BaseModel):
    user: UserOut
    organization: OrganizationMembershipOut
    organizations: list[OrganizationMembershipOut]
    workspaces: list[WorkspaceAccessOut]
    capabilities: Capabilities
