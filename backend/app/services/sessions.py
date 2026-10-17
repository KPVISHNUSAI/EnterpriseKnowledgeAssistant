"""Server-side sessions and their cookies (used by auth endpoints and request auth)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import csrf_token_for, hash_token, new_token
from app.db.base import utcnow
from app.models import User, UserSession
from app.services.audit import RequestMeta

_TOUCH_INTERVAL = timedelta(seconds=60)


def create_session(
    db: Session,
    user: User,
    *,
    organization_id: uuid.UUID | None,
    meta: RequestMeta,
) -> tuple[UserSession, str]:
    """Create a session; returns (row, raw token). The raw token is never stored."""
    settings = get_settings()
    now = utcnow()
    token = new_token()
    session = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        active_organization_id=organization_id,
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
        last_seen_at=now,
        ip_address=meta.ip_address,
        user_agent=(meta.user_agent or "")[:512] or None,
    )
    db.add(session)
    db.flush()
    return session, token


def resolve_session(db: Session, token: str) -> tuple[UserSession, User] | None:
    """Return the live session and its active user, or None if invalid/expired/idle."""
    settings = get_settings()
    row = db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == hash_token(token))
    ).first()
    if row is None:
        return None
    session, user = row
    now = utcnow()
    idle_limit = timedelta(minutes=settings.session_idle_timeout_minutes)
    if (
        session.revoked_at is not None
        or session.expires_at <= now
        or now - session.last_seen_at > idle_limit
        or not user.is_active
    ):
        return None
    if now - session.last_seen_at > _TOUCH_INTERVAL:
        session.last_seen_at = now
        db.flush()
    return session, user


def revoke_session(db: Session, session_id: uuid.UUID) -> None:
    db.execute(
        update(UserSession)
        .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def revoke_user_sessions(
    db: Session, user_id: uuid.UUID, *, except_session_id: uuid.UUID | None = None
) -> None:
    stmt = update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if except_session_id is not None:
        stmt = stmt.where(UserSession.id != except_session_id)
    db.execute(stmt.values(revoked_at=utcnow()))


def set_session_cookies(response: Response, token: str) -> None:
    """Session cookie is HttpOnly; the CSRF cookie is readable by the SPA, which echoes it
    in the X-CSRF-Token header on state-changing requests."""
    s = get_settings()
    max_age = s.session_ttl_hours * 3600
    common = {
        "max_age": max_age,
        "secure": s.cookie_secure,
        "samesite": "lax",
        "domain": s.cookie_domain,
        "path": "/",
    }
    response.set_cookie(s.session_cookie_name, token, httponly=True, **common)  # type: ignore[arg-type]
    response.set_cookie(s.csrf_cookie_name, csrf_token_for(token), httponly=False, **common)  # type: ignore[arg-type]


def clear_session_cookies(response: Response) -> None:
    s = get_settings()
    for name in (s.session_cookie_name, s.csrf_cookie_name):
        response.delete_cookie(name, path="/", domain=s.cookie_domain)
