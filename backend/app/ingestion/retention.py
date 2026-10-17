"""Retention policy (`eka.maintenance.apply_retention`, run by beat every 6 hours).

Tenant data is deleted per organization inside that organization's tenant context, with
explicit organization filters as well (row-level security is the second layer, not the
only one). Tables without a tenant (sessions, invitations) are cleaned globally.
Audit events always carry an organization (auth never writes tenant-less rows), so
per-organization deletion covers all of them.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, Delete, delete, exists, func, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from app.core.config import get_settings
from app.db.session import session_scope
from app.models import (
    AuditEvent,
    Conversation,
    IngestionJob,
    Invitation,
    JobStatus,
    Message,
    UsageRecord,
    UserSession,
)
from app.models.organization import Organization

logger = logging.getLogger(__name__)

FINISHED_JOB_DAYS = 30  # older finished jobs go, except each document's latest
DEAD_SESSION_DAYS = 7
EXPIRED_INVITATION_DAYS = 30


def apply_retention() -> dict[str, int]:
    with session_scope() as db:
        organization_ids = list(db.scalars(select(Organization.id)))
    totals: Counter[str] = Counter()
    for organization_id in organization_ids:
        totals.update(apply_organization_retention(organization_id))
    totals.update(apply_global_retention())
    logger.info("retention.applied", extra=dict(totals))
    return dict(totals)


def _days_ago(days: int) -> ColumnElement[Any]:
    return func.now() - timedelta(days=days)


def _count(db: Session, stmt: Delete) -> int:
    return int(db.execute(stmt.execution_options(synchronize_session=False)).rowcount)  # type: ignore[attr-defined]


def apply_organization_retention(organization_id: uuid.UUID) -> dict[str, int]:
    settings = get_settings()
    conversation_cutoff = _days_ago(settings.conversation_retention_days)
    recent_message = exists().where(
        Message.conversation_id == Conversation.id,
        Message.created_at >= conversation_cutoff,
    )
    newer_job = aliased(IngestionJob)
    later_job_exists = exists().where(
        newer_job.document_id == IngestionJob.document_id,
        tuple_(newer_job.created_at, newer_job.id)
        > tuple_(IngestionJob.created_at, IngestionJob.id),
    )
    with session_scope(organization_id) as db:
        counts = {
            # Messages go with their conversation (ON DELETE CASCADE).
            "conversations": _count(
                db,
                delete(Conversation).where(
                    Conversation.organization_id == organization_id,
                    Conversation.updated_at < conversation_cutoff,
                    ~recent_message,
                ),
            ),
            "audit_events": _count(
                db,
                delete(AuditEvent).where(
                    AuditEvent.organization_id == organization_id,
                    AuditEvent.created_at < _days_ago(settings.audit_retention_days),
                ),
            ),
            "usage_records": _count(
                db,
                delete(UsageRecord).where(
                    UsageRecord.organization_id == organization_id,
                    UsageRecord.created_at < _days_ago(settings.usage_retention_days),
                ),
            ),
            "ingestion_jobs": _count(
                db,
                delete(IngestionJob).where(
                    IngestionJob.organization_id == organization_id,
                    IngestionJob.status.in_(
                        (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)
                    ),
                    func.coalesce(IngestionJob.completed_at, IngestionJob.updated_at)
                    < _days_ago(FINISHED_JOB_DAYS),
                    later_job_exists,
                ),
            ),
        }
        db.commit()
    return counts


def apply_global_retention() -> dict[str, int]:
    idle = timedelta(minutes=get_settings().session_idle_timeout_minutes)
    dead_before = _days_ago(DEAD_SESSION_DAYS)
    with session_scope() as db:
        counts = {
            "user_sessions": _count(
                db,
                delete(UserSession).where(
                    or_(
                        UserSession.expires_at < dead_before,
                        UserSession.revoked_at < dead_before,
                        # Idle-expired sessions can never be used again either.
                        UserSession.last_seen_at
                        < func.now() - idle - timedelta(days=DEAD_SESSION_DAYS),
                    )
                ),
            ),
            "invitations": _count(
                db,
                delete(Invitation).where(
                    Invitation.expires_at < _days_ago(EXPIRED_INVITATION_DAYS)
                ),
            ),
        }
        db.commit()
    return counts
