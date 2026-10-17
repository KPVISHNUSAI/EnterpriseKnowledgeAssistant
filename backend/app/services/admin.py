"""Audit log queries and usage reporting for organization admins."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.schemas.admin import AuditEventOut, UsageResponse
from app.schemas.common import Page, PageParams
from app.services.access import Principal


def list_audit_events(
    db: Session,
    principal: Principal,
    *,
    action: str | None,
    actor_user_id: uuid.UUID | None,
    resource_type: str | None,
    resource_id: str | None,
    since: datetime | None,
    until: datetime | None,
    page: PageParams,
) -> Page[AuditEventOut]:
    raise NotImplementedError


def get_usage(db: Session, principal: Principal, *, days: int) -> UsageResponse:
    raise NotImplementedError
