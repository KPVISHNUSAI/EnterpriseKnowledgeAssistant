"""The active organization, its members and invitations.

organizations, organization_memberships, invitations and users are not tenant tables
(row-level security does not cover them), so every query here filters by the caller's
organization explicitly.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import ColumnElement, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.security import hash_token, new_token
from app.db.base import utcnow
from app.models import (
    DocumentPermission,
    Invitation,
    Organization,
    OrganizationMembership,
    OrgRole,
    PrincipalType,
    User,
)
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
from app.services import access, audit
from app.services.access import Principal
from app.services.audit import AuditAction, RequestMeta
from app.services.sessions import revoke_user_sessions


def pending_invitations() -> tuple[ColumnElement[bool], ...]:
    """WHERE conditions for invitations that can still be accepted."""
    return (
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > func.now(),
    )


def _organization_out(db: Session, org: Organization) -> OrganizationOut:
    members = db.scalar(
        select(func.count()).where(OrganizationMembership.organization_id == org.id)
    )
    return OrganizationOut(
        id=org.id, name=org.name, created_at=org.created_at, member_count=members or 0
    )


def get_organization(db: Session, principal: Principal) -> OrganizationOut:
    return _organization_out(db, db.get_one(Organization, principal.organization_id))


def update_organization(
    db: Session, principal: Principal, body: OrganizationUpdate, *, meta: RequestMeta
) -> OrganizationOut:
    access.require(principal.is_org_admin)
    org = db.get_one(Organization, principal.organization_id)
    if body.name != org.name:
        org.name = body.name
        audit.record(
            db,
            AuditAction.ORG_UPDATED,
            organization_id=org.id,
            actor_user_id=principal.user_id,
            resource_type="organization",
            resource_id=org.id,
            metadata={"fields": ["name"]},
            meta=meta,
        )
        db.commit()
    return _organization_out(db, org)


def _member_out(membership: OrganizationMembership, user: User) -> MemberOut:
    return MemberOut(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=membership.role,
        joined_at=membership.created_at,
    )


def list_members(
    db: Session, principal: Principal, *, q: str | None, page: PageParams
) -> Page[MemberOut]:
    stmt = (
        select(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(OrganizationMembership.organization_id == principal.organization_id)
    )
    if q and q.strip():
        term = q.strip()
        stmt = stmt.where(
            or_(
                User.email.icontains(term, autoescape=True),
                User.full_name.icontains(term, autoescape=True),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(User.full_name, User.id).offset(page.offset).limit(page.page_size)
    )
    return Page(
        items=[_member_out(membership, user) for membership, user in rows],
        total=total or 0,
        page=page.page,
        page_size=page.page_size,
    )


def _membership(db: Session, p: Principal, user_id: uuid.UUID) -> OrganizationMembership:
    membership = db.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == p.organization_id,
            OrganizationMembership.user_id == user_id,
        )
    )
    if membership is None:
        raise NotFoundError("Member not found", code="member_not_found")
    return membership


def _ensure_another_admin(db: Session, organization_id: uuid.UUID) -> None:
    """409 unless another org admin remains. Locking the admin rows (in a fixed order)
    serializes concurrent demotions, so two admins cannot remove each other."""
    admins = db.scalars(
        select(OrganizationMembership.id)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.role == OrgRole.ORG_ADMIN,
        )
        .order_by(OrganizationMembership.id)
        .with_for_update()
    ).all()
    if len(admins) < 2:
        raise ConflictError("The organization needs at least one admin", code="last_admin")


def update_member_role(
    db: Session,
    principal: Principal,
    user_id: uuid.UUID,
    body: UpdateMemberRoleRequest,
    *,
    meta: RequestMeta,
) -> MemberOut:
    access.require(principal.is_org_admin)
    membership = _membership(db, principal, user_id)
    previous = membership.role
    if body.role != previous:
        if previous == OrgRole.ORG_ADMIN:
            _ensure_another_admin(db, principal.organization_id)
        membership.role = body.role
        audit.record(
            db,
            AuditAction.ORG_MEMBER_ROLE_CHANGED,
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            resource_type="user",
            resource_id=user_id,
            metadata={"from": previous.value, "to": body.role.value},
            meta=meta,
        )
        db.commit()
    return _member_out(membership, db.get_one(User, user_id))


def remove_member(
    db: Session, principal: Principal, user_id: uuid.UUID, *, meta: RequestMeta
) -> None:
    access.require(principal.is_org_admin)
    org_id = principal.organization_id
    membership = _membership(db, principal, user_id)
    if membership.role == OrgRole.ORG_ADMIN:
        _ensure_another_admin(db, org_id)
    # User grants reference the user id without a foreign key; drop them so a later
    # re-invitation starts without the old document access.
    db.execute(
        delete(DocumentPermission).where(
            DocumentPermission.organization_id == org_id,
            DocumentPermission.principal_type == PrincipalType.USER,
            DocumentPermission.principal_id == user_id,
        )
    )
    # Composite foreign keys cascade workspace memberships and conversations.
    db.execute(delete(OrganizationMembership).where(OrganizationMembership.id == membership.id))
    revoke_user_sessions(db, user_id, active_organization_id=org_id)
    audit.record(
        db,
        AuditAction.ORG_MEMBER_REMOVED,
        organization_id=org_id,
        actor_user_id=principal.user_id,
        resource_type="user",
        resource_id=user_id,
        metadata={"role": membership.role.value},
        meta=meta,
    )
    db.commit()


def _invitation_out(invitation: Invitation, invited_by_email: str | None) -> InvitationOut:
    out = InvitationOut.model_validate(invitation)
    out.invited_by_email = invited_by_email
    return out


def list_invitations(db: Session, principal: Principal, *, page: PageParams) -> Page[InvitationOut]:
    access.require(principal.is_org_admin)
    in_org = Invitation.organization_id == principal.organization_id
    total = db.scalar(select(func.count()).where(in_org))
    rows = db.execute(
        select(Invitation, User.email)
        .outerjoin(User, User.id == Invitation.invited_by)
        .where(in_org)
        .order_by(Invitation.created_at.desc(), Invitation.id.desc())
        .offset(page.offset)
        .limit(page.page_size)
    )
    return Page(
        items=[_invitation_out(invitation, email) for invitation, email in rows],
        total=total or 0,
        page=page.page,
        page_size=page.page_size,
    )


def create_invitation(
    db: Session, principal: Principal, body: InvitationCreate, *, meta: RequestMeta
) -> InvitationCreated:
    access.require(principal.is_org_admin)
    settings = get_settings()
    org_id = principal.organization_id
    member = db.scalar(
        select(OrganizationMembership.id)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(OrganizationMembership.organization_id == org_id, User.email == body.email)
    )
    if member is not None:
        raise ConflictError("This person is already a member", code="already_member")
    # One live invitation per email: a new one supersedes links sent earlier.
    db.execute(
        update(Invitation)
        .where(Invitation.organization_id == org_id, Invitation.email == body.email)
        .where(*pending_invitations())
        .values(revoked_at=utcnow())
    )
    token = new_token()  # returned once; only the hash is stored
    invitation = Invitation(
        organization_id=org_id,
        email=body.email,
        role=body.role,
        token_hash=hash_token(token),
        invited_by=principal.user_id,
        expires_at=utcnow() + timedelta(hours=settings.invitation_ttl_hours),
    )
    db.add(invitation)
    db.flush()
    audit.record(
        db,
        AuditAction.ORG_INVITATION_CREATED,
        organization_id=org_id,
        actor_user_id=principal.user_id,
        resource_type="invitation",
        resource_id=invitation.id,
        metadata={"email": body.email, "role": body.role.value},
        meta=meta,
    )
    db.commit()
    return InvitationCreated(
        **_invitation_out(invitation, principal.email).model_dump(),
        token=token,
        invite_url=f"{settings.public_base_url.rstrip('/')}/invite/{token}",
    )


def revoke_invitation(
    db: Session, principal: Principal, invitation_id: uuid.UUID, *, meta: RequestMeta
) -> None:
    access.require(principal.is_org_admin)
    # Locked so a concurrent acceptance either wins (404 here) or waits for the revoke.
    invitation = db.scalar(
        select(Invitation)
        .where(
            Invitation.id == invitation_id,
            Invitation.organization_id == principal.organization_id,
            *pending_invitations(),
        )
        .with_for_update()
    )
    if invitation is None:
        raise NotFoundError("Invitation not found", code="invitation_not_found")
    invitation.revoked_at = utcnow()
    audit.record(
        db,
        AuditAction.ORG_INVITATION_REVOKED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="invitation",
        resource_id=invitation.id,
        metadata={"email": invitation.email},
        meta=meta,
    )
    db.commit()
