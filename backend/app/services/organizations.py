"""The active organization, its members and invitations."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.schemas.common import Page, PageParams
from app.schemas.organizations import (
    InvitationCreate,
    InvitationCreated,
    InvitationOut,
    MemberOut,
    OrganizationOut,
    OrganizationUpdate,
    UpdateMemberRoleRequest,
)
from app.services.access import Principal
from app.services.audit import RequestMeta


def get_organization(db: Session, principal: Principal) -> OrganizationOut:
    raise NotImplementedError


def update_organization(
    db: Session, principal: Principal, body: OrganizationUpdate, *, meta: RequestMeta
) -> OrganizationOut:
    raise NotImplementedError


def list_members(
    db: Session, principal: Principal, *, q: str | None, page: PageParams
) -> Page[MemberOut]:
    raise NotImplementedError


def update_member_role(
    db: Session,
    principal: Principal,
    user_id: uuid.UUID,
    body: UpdateMemberRoleRequest,
    *,
    meta: RequestMeta,
) -> MemberOut:
    raise NotImplementedError


def remove_member(
    db: Session, principal: Principal, user_id: uuid.UUID, *, meta: RequestMeta
) -> None:
    raise NotImplementedError


def list_invitations(db: Session, principal: Principal, *, page: PageParams) -> Page[InvitationOut]:
    raise NotImplementedError


def create_invitation(
    db: Session, principal: Principal, body: InvitationCreate, *, meta: RequestMeta
) -> InvitationCreated:
    raise NotImplementedError


def revoke_invitation(
    db: Session, principal: Principal, invitation_id: uuid.UUID, *, meta: RequestMeta
) -> None:
    raise NotImplementedError
