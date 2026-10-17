"""Engine, sessions and the transaction-scoped tenant context used by row-level security.

Every transaction begins with ``set_config('app.current_org_id', <org>, true)``. The
``true`` makes the setting transaction-local, so it can never leak to another request
through the connection pool. RLS policies compare each row's organization_id to it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

TENANT_KEY = "tenant_org_id"
_SET_TENANT = text("SELECT set_config('app.current_org_id', :org_id, true)")


@lru_cache
def get_engine() -> Engine:
    s = get_settings()
    return create_engine(
        s.database_url,
        pool_pre_ping=True,
        pool_size=s.db_pool_size,
        max_overflow=s.db_max_overflow,
        connect_args={"options": f"-c statement_timeout={s.db_statement_timeout_ms}"},
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(session: Session, _transaction: object, connection: object) -> None:
    org_id = session.info.get(TENANT_KEY)
    connection.execute(_SET_TENANT, {"org_id": str(org_id) if org_id else ""})  # type: ignore[attr-defined]


def set_tenant(session: Session, org_id: uuid.UUID | None) -> None:
    """Bind the session to a tenant for this and all later transactions."""
    session.info[TENANT_KEY] = org_id
    if session.in_transaction():
        session.execute(_SET_TENANT, {"org_id": str(org_id) if org_id else ""})


@contextmanager
def session_scope(org_id: uuid.UUID | None = None) -> Iterator[Session]:
    """Session for code outside a request (worker, scripts). Rolls back unless committed."""
    session = get_sessionmaker()()
    if org_id is not None:
        session.info[TENANT_KEY] = org_id
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
