from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_request_meta, require_org_admin
from app.schemas.admin import AuditEventOut, UsageResponse
from app.schemas.common import Page, PageParams, errors
from app.schemas.permission_proof import RunCreate, RunDetail, RunOut
from app.services import admin as admin_service
from app.services import permission_proof
from app.services.access import Principal
from app.services.audit import RequestMeta

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


@router.post(
    "/permission-proof/runs",
    response_model=RunDetail,
    status_code=202,
    responses=errors(401, 403, 409, 422),
)
def start_permission_proof(
    body: RunCreate,
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> RunDetail:
    """Queue a leak test: as every access group, ask about every document the group may
    not read and check what would reach the model. With `simulate`, test one person as if
    wrongly given a workspace; nothing is changed, and the run must report findings. 409
    while a run is active; 422 for a simulation that could not catch anything."""
    return permission_proof.start_run(db, principal, body, meta=meta)


@router.get("/permission-proof/runs", response_model=Page[RunOut], responses=errors(401, 403))
def list_permission_proofs(
    page: PageParams = Depends(),
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> Page[RunOut]:
    """Leak tests, newest first."""
    return permission_proof.list_runs(db, principal, page=page)


@router.get(
    "/permission-proof/runs/{run_id}", response_model=RunDetail, responses=errors(401, 403, 404)
)
def get_permission_proof(
    run_id: uuid.UUID,
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> RunDetail:
    """One leak test with its access groups and findings. Reports never hold document
    text, and a document's title only if the caller may read the document."""
    return permission_proof.get_run(db, principal, run_id)
