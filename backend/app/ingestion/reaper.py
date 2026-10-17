"""Stale-job reaper (`eka.maintenance.requeue_stale_jobs`, run by beat every minute).

Recovers work whose worker died (expired lease) or whose broker message was lost, fails
documents left waiting without a job, and closes chat answers whose API process died
mid-stream. ingestion_jobs is under row-level security,
so the reaper runs once per organization in that organization's tenant context.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import session_scope
from app.models import (
    ACTIVE_JOB_STATUSES,
    Document,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    Message,
    MessageStatus,
)
from app.models.organization import Organization
from app.worker import queue

logger = logging.getLogger(__name__)

WORKER_LOST = "worker_lost"
NOT_PROCESSED = "Processing was interrupted. Reindex the document to try again."
# A live stream finalizes its message well within this (the provider read timeout
# bounds every gap); an older `streaming` row lost its API process.
STALE_STREAM = timedelta(hours=1)


def requeue_stale_jobs() -> dict[str, int]:
    with session_scope() as db:
        organization_ids = list(db.scalars(select(Organization.id)))
    totals: Counter[str] = Counter()
    for organization_id in organization_ids:
        totals.update(reap_organization(organization_id))
    if any(totals.values()):
        logger.info("ingestion.reaped", extra=dict(totals))
    return dict(totals)


def reap_organization(organization_id: uuid.UUID) -> dict[str, int]:
    stale = timedelta(seconds=get_settings().ingestion_stale_queued_seconds)
    counts = {
        "requeued": 0,
        "failed": 0,
        "reenqueued": 0,
        "documents_failed": 0,
        "streams_closed": 0,
    }
    enqueue: list[uuid.UUID] = []
    with session_scope(organization_id) as db:
        # 1. Running jobs whose lease expired: the worker crashed or was killed.
        expired = db.execute(
            select(IngestionJob.id, IngestionJob.document_id, IngestionJob.attempts).where(
                IngestionJob.organization_id == organization_id,
                IngestionJob.status == JobStatus.RUNNING,
                IngestionJob.locked_until < func.now(),
            )
        ).all()
        for job_id, document_id, attempts in expired:
            outcome = _recover(db, job_id, document_id, attempts)
            if outcome == "requeued":
                enqueue.append(job_id)
            if outcome:
                counts[outcome] += 1

        # 2. Queued jobs nobody claimed: the message was lost or never sent. Duplicate
        # messages are harmless (the claim is atomic).
        waiting = db.execute(
            select(IngestionJob.id, IngestionJob.document_id, IngestionJob.attempts).where(
                IngestionJob.organization_id == organization_id,
                IngestionJob.status == JobStatus.QUEUED,
                IngestionJob.run_after <= func.now() - stale,
            )
        ).all()
        for job_id, document_id, attempts in waiting:
            outcome = _recover(db, job_id, document_id, attempts, queued=True)
            if outcome == "requeued":
                enqueue.append(job_id)
                counts["reenqueued"] += 1
            elif outcome == "failed":
                counts["failed"] += 1

        # 3. Documents waiting for a job that does not exist.
        has_active_job = exists().where(
            IngestionJob.document_id == Document.id,
            IngestionJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        result = db.execute(
            update(Document)
            .where(
                Document.organization_id == organization_id,
                Document.processing_status.in_((DocumentStatus.QUEUED, DocumentStatus.PROCESSING)),
                Document.updated_at < func.now() - stale,
                ~has_active_job,
            )
            .values(processing_status=DocumentStatus.FAILED, processing_error=NOT_PROCESSED)
            .execution_options(synchronize_session=False)
        )
        counts["documents_failed"] = result.rowcount  # type: ignore[attr-defined]

        # 4. Answers whose stream died with its API process (deploy, crash): nothing is
        # left to finish them.
        result = db.execute(
            update(Message)
            .where(
                Message.organization_id == organization_id,
                Message.status == MessageStatus.STREAMING,
                Message.updated_at < func.now() - STALE_STREAM,
            )
            .values(status=MessageStatus.INCOMPLETE)
            .execution_options(synchronize_session=False)
        )
        counts["streams_closed"] = result.rowcount  # type: ignore[attr-defined]
        db.commit()
    for job_id in enqueue:
        queue.enqueue_ingestion(job_id, organization_id)
    return counts


def _recover(
    db: Session, job_id: uuid.UUID, document_id: uuid.UUID, attempts: int, *, queued: bool = False
) -> str | None:
    """Re-queue a job that lost its worker, or fail it once attempts are used up.
    Returns "requeued", "failed" or None (left alone). Rows a worker is committing
    right now are skipped (SKIP LOCKED); the next run looks again."""
    version = db.scalar(
        select(Document.version).where(Document.id == document_id).with_for_update(skip_locked=True)
    )
    job = db.execute(
        select(IngestionJob.status, IngestionJob.max_attempts, IngestionJob.document_version)
        .where(IngestionJob.id == job_id, IngestionJob.attempts == attempts)
        .with_for_update(skip_locked=True)
    ).one_or_none()
    expected = JobStatus.QUEUED if queued else JobStatus.RUNNING
    if version is None or job is None or job.status != expected:
        return None
    if queued and attempts < job.max_attempts:
        return "requeued"  # nothing to change, it only needs a new message
    current = version == job.document_version
    job_update = update(IngestionJob).where(IngestionJob.id == job_id)
    document_update = update(Document).where(Document.id == document_id)
    if attempts < job.max_attempts:
        db.execute(
            job_update.values(
                status=JobStatus.QUEUED,
                run_after=func.now(),
                locked_until=None,
                error_code=WORKER_LOST,
                error_message="Processing was interrupted and will be retried.",
            )
        )
        if current:
            db.execute(document_update.values(processing_status=DocumentStatus.QUEUED))
        return "requeued"
    message = (
        f"Processing was interrupted and stopped after {attempts} "
        f"{'attempt' if attempts == 1 else 'attempts'}."
    )
    db.execute(
        job_update.values(
            status=JobStatus.FAILED,
            completed_at=func.now(),
            locked_until=None,
            error_code=WORKER_LOST,
            error_message=message,
        )
    )
    if current:
        db.execute(
            document_update.values(
                processing_status=DocumentStatus.FAILED, processing_error=message
            )
        )
    return "failed"
