"""Registration, login, profile and organization switching."""

from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationFailedError,
)
from app.core.rate_limit import hit
from app.core.redis import get_redis
from app.core.security import (
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_csrf,
    verify_password,
)
from app.db.base import utcnow
from app.db.session import session_scope, set_tenant
from app.models import (
    Invitation,
    Organization,
    OrganizationMembership,
    OrgRole,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)
from app.schemas.auth import (
    Capabilities,
    ChangePasswordRequest,
    InvitationPreview,
    LoginRequest,
    MeResponse,
    OrganizationMembershipOut,
    RegisterRequest,
    UpdateProfileRequest,
    UserOut,
    WorkspaceAccessOut,
)
from app.services import access, audit
from app.services.access import Principal
from app.services.audit import AuditAction, RequestMeta
from app.services.organizations import pending_invitations
from app.services.sessions import (
    create_session,
    resolve_session,
    revoke_session,
    revoke_user_sessions,
)
from app.services.workspaces import add_workspace

logger = logging.getLogger(__name__)

STARTER_WORKSPACE = "General"
MAX_PASSWORD_LENGTH = 128


def _invalid_credentials() -> UnauthorizedError:
    return UnauthorizedError("Invalid email or password", code="invalid_credentials")


def _invitation_not_found() -> NotFoundError:
    # One error for unknown, expired, revoked, used and other people's invitations.
    return NotFoundError("Invitation not found or no longer valid", code="invitation_not_found")


def _check_password(password: str, email: str) -> None:
    """PASSWORD_MIN_LENGTH, plus a cheap guessability rule: not built from the email."""
    min_length = get_settings().password_min_length
    if not min_length <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValidationFailedError(
            f"Password must be {min_length} to {MAX_PASSWORD_LENGTH} characters long",
            code="weak_password",
        )
    local_part = email.split("@", 1)[0].lower()
    # ponytail: substring rule only; add a breached-password list if policy requires it.
    if len(local_part) >= 3 and local_part in password.lower():
        raise ValidationFailedError(
            "Password must not contain your email address", code="weak_password"
        )


def _throttle_account(email: str) -> None:
    """Per-account limit shared by sign-in and password re-entry (routes also limit per
    IP), so neither rotating IPs nor a stolen session helps to guess a password. The key
    holds a hash, so Redis never sees the email."""
    settings = get_settings()
    if settings.rate_limit_enabled:
        hit(
            get_redis(),
            "login_email",
            hashlib.sha256(email.encode()).hexdigest(),
            limit=settings.rate_limit_login_per_minute,
            window_seconds=60,
        )


def _set_active_organization(
    db: Session, session_id: uuid.UUID | None, organization_id: uuid.UUID
) -> None:
    db.execute(
        update(UserSession)
        .where(UserSession.id == session_id)
        .values(active_organization_id=organization_id)
    )


def _me_for(
    db: Session, user: User, organization_id: uuid.UUID, session_id: uuid.UUID | None
) -> MeResponse:
    """MeResponse for an organization the request's principal (if any) is not bound to:
    after sign-in, a switch or an accepted invitation. Binds `db` to that tenant."""
    set_tenant(db, organization_id)
    role = db.scalars(
        select(OrganizationMembership.role).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user.id,
        )
    ).one()
    workspace_roles = dict(
        db.execute(
            select(WorkspaceMembership.workspace_id, WorkspaceMembership.role).where(
                WorkspaceMembership.organization_id == organization_id,
                WorkspaceMembership.user_id == user.id,
            )
        ).all()
    )
    principal = Principal(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        organization_id=organization_id,
        org_role=role,
        workspace_roles=workspace_roles,
        session_id=session_id,
    )
    return get_me(db, principal)


def register(db: Session, body: RegisterRequest, *, meta: RequestMeta) -> tuple[MeResponse, str]:
    """Returns (me, raw session token). 409 if the email is taken."""
    settings = get_settings()
    if body.invite_token is None:
        if not settings.allow_public_registration:
            raise ForbiddenError("Registration is by invitation only", code="registration_disabled")
        if not body.organization_name:
            raise ValidationFailedError(
                "organization_name is required", code="organization_name_required"
            )
    _check_password(body.password, body.email)

    invitation = None
    if body.invite_token is not None:
        # Locked, so the same invitation cannot be used twice concurrently.
        invitation = db.scalar(
            select(Invitation)
            .where(
                Invitation.token_hash == hash_token(body.invite_token),
                Invitation.email == body.email,
                *pending_invitations(),
            )
            .with_for_update()
        )
        if invitation is None:
            raise _invitation_not_found()

    user = User(
        email=body.email, password_hash=hash_password(body.password), full_name=body.full_name
    )
    db.add(user)
    try:
        db.flush()  # the unique index also catches concurrent sign-ups
    except IntegrityError:
        db.rollback()
        raise ConflictError(
            "An account with this email already exists", code="email_taken"
        ) from None

    if invitation is not None:
        org_id = invitation.organization_id
        db.add(
            OrganizationMembership(organization_id=org_id, user_id=user.id, role=invitation.role)
        )
        invitation.accepted_at = utcnow()
        invitation.accepted_by = user.id
        set_tenant(db, org_id)
        audit.record(
            db,
            AuditAction.ORG_INVITATION_ACCEPTED,
            organization_id=org_id,
            actor_user_id=user.id,
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"role": invitation.role.value},
            meta=meta,
        )
    else:
        org = Organization(name=body.organization_name)
        db.add(org)
        db.flush()
        org_id = org.id
        db.add(
            OrganizationMembership(organization_id=org_id, user_id=user.id, role=OrgRole.ORG_ADMIN)
        )
        db.flush()  # the workspace membership references it
        set_tenant(db, org_id)
        add_workspace(
            db,
            organization_id=org_id,
            owner_id=user.id,
            name=STARTER_WORKSPACE,
            description=None,
            meta=meta,
        )
    audit.record(
        db,
        AuditAction.AUTH_REGISTER,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_type="user",
        resource_id=user.id,
        metadata={"invited": invitation is not None},
        meta=meta,
    )
    session, token = create_session(db, user, organization_id=org_id, meta=meta)
    db.commit()
    return _me_for(db, user, org_id, session.id), token


def _record_login_failure(user_id: uuid.UUID, reason: str, meta: RequestMeta) -> None:
    """Audit the failure in each organization of the account. Never tenant-less rows:
    retention runs per tenant under row-level security and could not purge them.

    Runs as a background task once the 401 has been sent: these writes (one per
    organization) only happen for existing accounts, so doing them before responding
    would make their failures measurably slower than unknown emails (enumeration)."""
    try:
        with session_scope() as db:
            org_ids = db.scalars(
                select(OrganizationMembership.organization_id).where(
                    OrganizationMembership.user_id == user_id
                )
            ).all()
            for org_id in org_ids:
                set_tenant(db, org_id)  # each row must be written under its own tenant
                audit.record(
                    db,
                    AuditAction.AUTH_LOGIN_FAILED,
                    organization_id=org_id,
                    actor_user_id=user_id,
                    resource_type="user",
                    resource_id=user_id,
                    metadata={"reason": reason},
                    meta=meta,
                )
                db.flush()
            db.commit()
    except SQLAlchemyError as exc:  # the response is already sent; keep the failure visible
        logger.warning("auth.login_failure_audit_failed", extra={"error_type": type(exc).__name__})


def login(db: Session, body: LoginRequest, *, meta: RequestMeta) -> tuple[MeResponse, str]:
    _throttle_account(body.email)
    user = db.scalar(select(User).where(User.email == body.email))
    # Always one hash verification, so unknown emails cost the same as wrong passwords.
    valid = verify_password(user.password_hash if user else None, body.password)
    if user is None or not valid or not user.is_active:
        reason = (
            "unknown_email" if user is None else "invalid_password" if not valid else "inactive"
        )
        logger.info("auth.login_failed", extra={"reason": reason})
        error = _invalid_credentials()
        if user is not None:
            error.background = BackgroundTask(_record_login_failure, user.id, reason, meta)
        raise error

    org_id = db.scalar(
        select(OrganizationMembership.organization_id)
        .where(OrganizationMembership.user_id == user.id)
        .order_by(OrganizationMembership.created_at)
        .limit(1)
    )
    if org_id is None:
        raise ForbiddenError("You are not a member of any organization", code="no_organization")
    if user.password_hash is not None and password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    user.last_login_at = utcnow()
    session, token = create_session(db, user, organization_id=org_id, meta=meta)
    set_tenant(db, org_id)
    audit.record(
        db,
        AuditAction.AUTH_LOGIN,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_type="user",
        resource_id=user.id,
        meta=meta,
    )
    db.commit()
    return _me_for(db, user, org_id, session.id), token


def logout(
    db: Session, session_token: str | None, csrf_token: str | None, *, meta: RequestMeta
) -> None:
    """Revoke the session when the token is live and the CSRF token matches. Never
    raises, so a client can always clear its cookies."""
    if not session_token:
        return
    try:
        resolved = resolve_session(db, session_token)
        if resolved is None or not verify_csrf(session_token, csrf_token):
            return
        session, user = resolved
        revoke_session(db, session.id)
        if session.active_organization_id is not None:
            set_tenant(db, session.active_organization_id)
            audit.record(
                db,
                AuditAction.AUTH_LOGOUT,
                organization_id=session.active_organization_id,
                actor_user_id=user.id,
                resource_type="user",
                resource_id=user.id,
                meta=meta,
            )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        # No exc_info: the statement parameters would include the session token hash.
        logger.warning("auth.logout_failed", extra={"error_type": type(exc).__name__})


def get_me(db: Session, principal: Principal) -> MeResponse:
    user = db.get_one(User, principal.user_id)
    organizations = [
        OrganizationMembershipOut(id=org_id, name=name, role=role)
        for org_id, name, role in db.execute(
            select(Organization.id, Organization.name, OrganizationMembership.role)
            .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
            .where(OrganizationMembership.user_id == principal.user_id)
            .order_by(OrganizationMembership.created_at)
        )
    ]
    active = {org.id: org for org in organizations}[principal.organization_id]

    stmt = (
        select(Workspace.id, Workspace.name)
        .where(Workspace.organization_id == principal.organization_id)
        .order_by(func.lower(Workspace.name))
    )
    if not principal.is_org_admin:
        stmt = stmt.where(Workspace.id.in_(principal.workspace_ids))
    workspaces = [
        WorkspaceAccessOut(id=ws_id, name=name, role=principal.workspace_roles.get(ws_id))
        for ws_id, name in db.execute(stmt)
    ]
    return MeResponse(
        user=UserOut.model_validate(user),
        organization=active,
        organizations=organizations,
        workspaces=workspaces,
        capabilities=Capabilities(
            can_create_workspace=access.can_create_workspace(principal),
            can_manage_organization=principal.is_org_admin,
            can_view_audit_log=principal.is_org_admin,
            can_upload=principal.is_org_admin
            or bool(principal.workspaces_with(WorkspaceRole.OWNER, WorkspaceRole.EDITOR)),
        ),
    )


def update_profile(
    db: Session, principal: Principal, body: UpdateProfileRequest, *, meta: RequestMeta
) -> UserOut:
    user = db.get_one(User, principal.user_id)
    user.full_name = body.full_name
    db.commit()
    return UserOut.model_validate(user)


def change_password(
    db: Session, principal: Principal, body: ChangePasswordRequest, *, meta: RequestMeta
) -> None:
    user = db.get_one(User, principal.user_id)
    _throttle_account(user.email)
    if not verify_password(user.password_hash, body.current_password):
        raise ForbiddenError("The current password is incorrect", code="invalid_password")
    _check_password(body.new_password, user.email)
    user.password_hash = hash_password(body.new_password)
    revoke_user_sessions(db, user.id, except_session_id=principal.session_id)
    audit.record(
        db,
        AuditAction.AUTH_PASSWORD_CHANGED,
        organization_id=principal.organization_id,
        actor_user_id=user.id,
        resource_type="user",
        resource_id=user.id,
        meta=meta,
    )
    db.commit()


def switch_organization(
    db: Session, principal: Principal, organization_id: uuid.UUID, *, meta: RequestMeta
) -> MeResponse:
    is_member = db.scalar(
        select(OrganizationMembership.id).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == principal.user_id,
        )
    )
    if is_member is None:
        raise NotFoundError("Organization not found", code="organization_not_found")
    _set_active_organization(db, principal.session_id, organization_id)
    set_tenant(db, organization_id)
    audit.record(
        db,
        AuditAction.AUTH_ORGANIZATION_SWITCHED,
        organization_id=organization_id,
        actor_user_id=principal.user_id,
        resource_type="organization",
        resource_id=organization_id,
        metadata={"from_organization_id": str(principal.organization_id)},
        meta=meta,
    )
    db.commit()
    return _me_for(db, db.get_one(User, principal.user_id), organization_id, principal.session_id)


def preview_invitation(db: Session, token: str) -> InvitationPreview:
    row = db.execute(
        select(Invitation, Organization.name)
        .join(Organization, Organization.id == Invitation.organization_id)
        .where(Invitation.token_hash == hash_token(token), *pending_invitations())
    ).first()
    if row is None:
        raise _invitation_not_found()
    invitation, organization_name = row
    return InvitationPreview(
        organization_name=organization_name,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
    )


def accept_invitation(
    db: Session, principal: Principal, token: str, *, meta: RequestMeta
) -> MeResponse:
    invitation = db.scalar(
        select(Invitation)
        .where(
            Invitation.token_hash == hash_token(token),
            Invitation.email == principal.email,
            *pending_invitations(),
        )
        .with_for_update()
    )
    if invitation is None:
        raise _invitation_not_found()
    org_id = invitation.organization_id
    already_member = db.scalar(
        select(OrganizationMembership.id).where(
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.user_id == principal.user_id,
        )
    )
    if already_member is not None:
        raise ConflictError("You are already a member of this organization", code="already_member")
    db.add(
        OrganizationMembership(
            organization_id=org_id, user_id=principal.user_id, role=invitation.role
        )
    )
    invitation.accepted_at = utcnow()
    invitation.accepted_by = principal.user_id
    _set_active_organization(db, principal.session_id, org_id)
    set_tenant(db, org_id)
    audit.record(
        db,
        AuditAction.ORG_INVITATION_ACCEPTED,
        organization_id=org_id,
        actor_user_id=principal.user_id,
        resource_type="invitation",
        resource_id=invitation.id,
        metadata={"role": invitation.role.value},
        meta=meta,
    )
    db.commit()
    return _me_for(db, db.get_one(User, principal.user_id), org_id, principal.session_id)
