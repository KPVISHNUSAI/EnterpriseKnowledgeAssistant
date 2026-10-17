"""Registration, login, profile and organization switching."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.schemas.auth import (
    ChangePasswordRequest,
    InvitationPreview,
    LoginRequest,
    MeResponse,
    RegisterRequest,
    UpdateProfileRequest,
    UserOut,
)
from app.services.access import Principal
from app.services.audit import RequestMeta


def register(db: Session, body: RegisterRequest, *, meta: RequestMeta) -> tuple[MeResponse, str]:
    """Returns (me, raw session token). 409 if the email is taken."""
    raise NotImplementedError


def login(db: Session, body: LoginRequest, *, meta: RequestMeta) -> tuple[MeResponse, str]:
    raise NotImplementedError


def logout(
    db: Session, session_token: str | None, csrf_token: str | None, *, meta: RequestMeta
) -> None:
    raise NotImplementedError


def get_me(db: Session, principal: Principal) -> MeResponse:
    raise NotImplementedError


def update_profile(
    db: Session, principal: Principal, body: UpdateProfileRequest, *, meta: RequestMeta
) -> UserOut:
    raise NotImplementedError


def change_password(
    db: Session, principal: Principal, body: ChangePasswordRequest, *, meta: RequestMeta
) -> None:
    raise NotImplementedError


def switch_organization(
    db: Session, principal: Principal, organization_id: uuid.UUID, *, meta: RequestMeta
) -> MeResponse:
    raise NotImplementedError


def preview_invitation(db: Session, token: str) -> InvitationPreview:
    raise NotImplementedError


def accept_invitation(
    db: Session, principal: Principal, token: str, *, meta: RequestMeta
) -> MeResponse:
    raise NotImplementedError
