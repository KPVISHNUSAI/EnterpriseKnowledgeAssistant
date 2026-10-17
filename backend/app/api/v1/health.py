from __future__ import annotations

import logging
from functools import lru_cache

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Response
from sqlalchemy import text

from app.core.config import BACKEND_DIR, get_settings
from app.core.redis import get_redis
from app.db.session import get_engine
from app.schemas.dashboard import HealthResponse, ReadinessCheck, ReadinessResponse
from app.storage import get_storage

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@lru_cache
def _migration_head() -> str | None:
    return ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head()


def _check_database() -> ReadinessCheck:
    try:
        with get_engine().connect() as conn:
            current = conn.scalar(text("SELECT version_num FROM alembic_version"))
            dims = conn.scalar(
                text(
                    "SELECT atttypmod FROM pg_attribute "
                    "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'"
                )
            )
    except Exception:
        logger.exception("ready.database_failed")
        return ReadinessCheck(ok=False, detail="database unreachable or not migrated")
    head = _migration_head()
    if current != head:
        return ReadinessCheck(ok=False, detail=f"migrations pending (db={current}, code={head})")
    configured = get_settings().embedding_dimensions
    if dims != configured:
        return ReadinessCheck(
            ok=False,
            detail=f"embedding dimension mismatch (db={dims}, EMBEDDING_DIMENSIONS={configured})",
        )
    return ReadinessCheck(ok=True)


def _check_redis() -> ReadinessCheck:
    try:
        get_redis().ping()
    except Exception:
        logger.exception("ready.redis_failed")
        return ReadinessCheck(ok=False, detail="redis unreachable")
    return ReadinessCheck(ok=True)


def _check_storage() -> ReadinessCheck:
    try:
        get_storage().check()
    except Exception:
        logger.exception("ready.storage_failed")
        return ReadinessCheck(ok=False, detail="object storage unavailable")
    return ReadinessCheck(ok=True)


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness: the process is serving requests (dependencies are not checked)."""
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A dependency is not ready"}},
)
def ready(response: Response) -> ReadinessResponse:
    """Readiness: database reachable and migrated with the configured embedding
    dimension, Redis reachable, object storage usable."""
    checks = {
        "database": _check_database(),
        "redis": _check_redis(),
        "storage": _check_storage(),
    }
    ok = all(check.ok for check in checks.values())
    if not ok:
        response.status_code = 503
    return ReadinessResponse(status="ready" if ok else "not_ready", checks=checks)
