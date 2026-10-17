"""Celery tasks: thin wrappers, all state lives in the database (see app/ingestion)."""

from __future__ import annotations

import uuid

from app.ingestion import reaper, retention, runner
from app.worker.celery_app import celery_app


@celery_app.task(name="eka.ingestion.process")
def process_ingestion(job_id: str, organization_id: str) -> str:
    return runner.process_job(uuid.UUID(job_id), uuid.UUID(organization_id)).value


@celery_app.task(name="eka.maintenance.requeue_stale_jobs")
def requeue_stale_jobs() -> dict[str, int]:
    return reaper.requeue_stale_jobs()


@celery_app.task(name="eka.maintenance.apply_retention")
def apply_retention() -> dict[str, int]:
    return retention.apply_retention()
