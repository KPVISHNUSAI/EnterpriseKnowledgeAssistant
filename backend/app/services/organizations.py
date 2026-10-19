"""The active organization, its members and invitations.

organizations, organization_memberships, invitations and users are not tenant tables
(row-level security does not cover them), so every query here filters by the caller's
organization explicitly.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import ColumnElement, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.security import hash_token, new_token
from app.db.base import utcnow
from app.db.session import set_tenant
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
        is_active=user.is_active,
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


def _ensure_another_admin(db: Session, organization_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """409 unless an active org admin other than `user_id` remains. Every change that can
    take an admin away locks the organization row first, so they run one at a time and
    each counts (in a new statement, hence a new snapshot) what the previous committed:
    two admins cannot demote, remove or deactivate each other."""
    db.execute(select(Organization.id).where(Organization.id == organization_id).with_for_update())
    others = db.scalar(
        select(func.count())
        .select_from(OrganizationMembership)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.role == OrgRole.ORG_ADMIN,
            OrganizationMembership.user_id != user_id,
            User.is_active,
        )
    )
    if not others:
        raise ConflictError("The organization needs at least one admin", code="last_admin")


def _revoke_invitations_by(db: Session, organization_id: uuid.UUID, user_id: uuid.UUID) -> int:
    """Pending invitations sent by someone who loses access: they must not outlive it."""
    result = db.execute(
        update(Invitation)
        .where(
            Invitation.organization_id == organization_id,
            Invitation.invited_by == user_id,
            *pending_invitations(),
        )
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


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
            _ensure_another_admin(db, principal.organization_id, user_id)
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
        _ensure_another_admin(db, org_id, user_id)
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
    revoked = _revoke_invitations_by(db, org_id, user_id)
    audit.record(
        db,
        AuditAction.ORG_MEMBER_REMOVED,
        organization_id=org_id,
        actor_user_id=principal.user_id,
        resource_type="user",
        resource_id=user_id,
        metadata={"role": membership.role.value, "invitations_revoked": revoked},
        meta=meta,
    )
    db.commit()


def set_member_active(
    db: Session, principal: Principal, user_id: uuid.UUID, active: bool, *, meta: RequestMeta
) -> MemberOut:
    """Deactivate (sign-in refused, sessions revoked) or reactivate a member's account,
    keeping their memberships, grants and conversations. An account is shared by all its
    organizations, so only one that is its sole organization may switch it."""
    access.require(principal.is_org_admin)
    membership = _membership(db, principal, user_id)
    user = db.get_one(User, user_id, with_for_update=True)
    if user.is_active == active:
        return _member_out(membership, user)
    if user_id == principal.user_id:
        raise ConflictError("You cannot deactivate your own account", code="cannot_deactivate_self")
    elsewhere = db.scalar(
        select(func.count()).where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.organization_id != principal.organization_id,
        )
    )
    if elsewhere:
        raise ConflictError(
            "This account also belongs to another organization; remove the member instead",
            code="member_of_other_organizations",
        )
    if not active and membership.role == OrgRole.ORG_ADMIN:
        _ensure_another_admin(db, principal.organization_id, user_id)
    user.is_active = active
    metadata = {}
    if not active:
        revoke_user_sessions(db, user_id)
        metadata["invitations_revoked"] = _revoke_invitations_by(
            db, principal.organization_id, user_id
        )
    audit.record(
        db,
        AuditAction.ORG_MEMBER_DEACTIVATED if not active else AuditAction.ORG_MEMBER_REACTIVATED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="user",
        resource_id=user_id,
        metadata=metadata,
        meta=meta,
    )
    db.commit()
    return _member_out(membership, user)


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
    org_id = principal.organization_id
    member = db.scalar(
        select(OrganizationMembership.id)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(OrganizationMembership.organization_id == org_id, User.email == body.email)
    )
    if member is not None:
        raise ConflictError("This person is already a member", code="already_member")
    return _invite(db, org_id, body, inviter=principal, meta=meta)


def invite_first_admin(
    db: Session, organization_name: str, email: str, *, meta: RequestMeta
) -> InvitationCreated:
    """Create an organization and invite its first admin, from the server
    (scripts/create_organization.py): the link lets them choose their own password. For an
    organization of that name that nobody has joined yet, a new link replaces all earlier ones."""
    org = db.scalar(
        select(Organization)
        .where(Organization.name == organization_name)
        .order_by(Organization.created_at)
        .limit(1)
        .with_for_update()
    )
    if org is None:
        org = Organization(name=organization_name)
        db.add(org)
        db.flush()
    elif db.scalar(select(exists().where(OrganizationMembership.organization_id == org.id))):
        raise ConflictError(
            "This organization already has members; its admins invite people from the admin page",
            code="organization_has_members",
        )
    set_tenant(db, org.id)
    # Only this script invites into an organization without members: the new link replaces
    # every earlier one, also one sent to another (say, mistyped) address.
    db.execute(
        update(Invitation)
        .where(Invitation.organization_id == org.id, *pending_invitations())
        .values(revoked_at=utcnow())
    )
    body = InvitationCreate(email=email, role=OrgRole.ORG_ADMIN)
    return _invite(db, org.id, body, inviter=None, meta=meta)


def _invite(
    db: Session,
    org_id: uuid.UUID,
    body: InvitationCreate,
    *,
    inviter: Principal | None,
    meta: RequestMeta,
) -> InvitationCreated:
    settings = get_settings()
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
        invited_by=inviter.user_id if inviter else None,
        expires_at=utcnow() + timedelta(hours=settings.invitation_ttl_hours),
    )
    db.add(invitation)
    db.flush()
    audit.record(
        db,
        AuditAction.ORG_INVITATION_CREATED,
        organization_id=org_id,
        actor_user_id=inviter.user_id if inviter else None,
        resource_type="invitation",
        resource_id=invitation.id,
        metadata={"email": body.email, "role": body.role.value},
        meta=meta,
    )
    db.commit()
    return InvitationCreated(
        **_invitation_out(invitation, inviter.email if inviter else None).model_dump(),
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
