"""FastAPI dependencies: database session, authenticated principal, roles, rate limits.

Transactions are explicit: services call ``db.commit()``. Nothing is committed after the
response is produced, so a client never sees success for a write that did not persist.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.logging import request_id_var
from app.core.rate_limit import hit
from app.core.redis import get_redis
from app.core.security import verify_csrf
from app.db.session import get_sessionmaker, set_tenant
from app.models import OrganizationMembership, OrgRole, WorkspaceMembership
from app.services import audit
from app.services.access import Principal
from app.services.audit import AuditAction, RequestMeta
from app.services.sessions import resolve_session

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_db() -> Iterator[Session]:
    """One session per request; anything left uncommitted is rolled back on close."""
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()


def client_ip(request: Request) -> str | None:
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # The trusted load balancer appends the peer it saw; earlier entries are
            # client-controlled and must not be trusted.
            return forwarded.split(",")[-1].strip()[:64] or None
    return request.client.host if request.client else None


def get_request_meta(request: Request) -> RequestMeta:
    return RequestMeta(
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=request_id_var.get(),
    )


def get_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    """Authenticate the session cookie, enforce CSRF on unsafe methods, and bind the
    database session to the caller's active organization (row-level security)."""
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise UnauthorizedError()
    resolved = resolve_session(db, token)
    if resolved is None:
        raise UnauthorizedError("Your session has expired, please sign in again")
    session, user = resolved
    if request.method in UNSAFE_METHODS and not verify_csrf(
        token, request.headers.get(settings.csrf_header_name)
    ):
        raise ForbiddenError("Missing or invalid CSRF token", code="csrf_failed")

    membership = None
    if session.active_organization_id is not None:
        membership = db.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == session.active_organization_id,
                OrganizationMembership.user_id == user.id,
            )
        )
    if membership is None:
        # No active organization, or the user was removed from it: use another one.
        membership = db.scalar(
            select(OrganizationMembership)
            .where(OrganizationMembership.user_id == user.id)
            .order_by(OrganizationMembership.created_at)
            .limit(1)
        )
        if membership is None:
            raise ForbiddenError("You are not a member of any organization", code="no_organization")
        session.active_organization_id = membership.organization_id
    # Persist the session touch; the next transaction starts with the tenant context.
    db.commit()

    set_tenant(db, membership.organization_id)
    workspace_roles = {
        workspace_id: role
        for workspace_id, role in db.execute(
            select(WorkspaceMembership.workspace_id, WorkspaceMembership.role).where(
                WorkspaceMembership.organization_id == membership.organization_id,
                WorkspaceMembership.user_id == user.id,
            )
        )
    }
    # Sync dependencies run in a worker thread, so contextvars set here would not reach
    # the access log; the request middleware reads these from the ASGI scope instead.
    request.state.user_id = str(user.id)
    request.state.organization_id = str(membership.organization_id)
    return Principal(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        organization_id=membership.organization_id,
        org_role=membership.role,
        workspace_roles=workspace_roles,
        session_id=session.id,
    )


def require_roles(*roles: OrgRole) -> Callable[..., Principal]:
    def dependency(
        request: Request,
        principal: Principal = Depends(get_principal),
        db: Session = Depends(get_db),
        meta: RequestMeta = Depends(get_request_meta),
    ) -> Principal:
        if principal.org_role not in roles:
            audit.record(
                db,
                AuditAction.ACCESS_DENIED,
                organization_id=principal.organization_id,
                actor_user_id=principal.user_id,
                resource_type="endpoint",
                resource_id=f"{request.method} {request.url.path}"[:100],
                metadata={"required_roles": [r.value for r in roles]},
                meta=meta,
            )
            db.commit()
            raise ForbiddenError()
        return principal

    return dependency


require_org_admin = require_roles(OrgRole.ORG_ADMIN)


def _hit(bucket: str, identity: str, setting: str, window_seconds: int) -> None:
    settings = get_settings()
    if settings.rate_limit_enabled:
        limit = int(getattr(settings, setting))
        hit(get_redis(), bucket, identity, limit=limit, window_seconds=window_seconds)


def rate_limit_user(bucket: str, setting: str, window_seconds: int = 60) -> Any:
    """Route dependency: `limit = Settings.<setting>` requests per window per user."""

    def dependency(principal: Principal = Depends(get_principal)) -> None:
        _hit(bucket, str(principal.user_id), setting, window_seconds)

    return Depends(dependency)


def rate_limit_ip(bucket: str, setting: str, window_seconds: int = 60) -> Any:
    """Route dependency for unauthenticated endpoints, keyed by client IP."""

    def dependency(request: Request) -> None:
        _hit(bucket, client_ip(request) or "unknown", setting, window_seconds)

    return Depends(dependency)
