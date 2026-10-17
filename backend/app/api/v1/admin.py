from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_org_admin
from app.schemas.admin import AuditEventOut, UsageResponse
from app.schemas.common import Page, PageParams, errors
from app.services import admin as admin_service
from app.services.access import Principal

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/audit-events", response_model=Page[AuditEventOut], responses=errors(401, 403))
def list_audit_events(
    action: str | None = Query(None, max_length=100, description="Exact action or prefix*"),
    actor_user_id: uuid.UUID | None = Query(None),
    resource_type: str | None = Query(None, max_length=50),
    resource_id: str | None = Query(None, max_length=100),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    page: PageParams = Depends(),
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> Page[AuditEventOut]:
    """Organization audit trail, newest first (org admins only)."""
    return admin_service.list_audit_events(
        db,
        principal,
        action=action,
        actor_user_id=actor_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        since=since,
        until=until,
        page=page,
    )


@router.get("/usage", response_model=UsageResponse, responses=errors(401, 403))
def get_usage(
    days: int = Query(30, ge=1, le=365),
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> UsageResponse:
    """Content, chat volume, token usage, estimated cost, latency percentiles and
    ingestion health for the organization (org admins only)."""
    return admin_service.get_usage(db, principal, days=days)
