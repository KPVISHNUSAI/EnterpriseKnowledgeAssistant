"""Celery tasks: thin wrappers, all state lives in the database (see app/ingestion)."""

from __future__ import annotations

import uuid

from app.ingestion import reaper, retention, runner
from app.services import permission_proof
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


@celery_app.task(name="eka.proof.run")
def run_permission_proof(run_id: str, organization_id: str) -> str:
    return permission_proof.execute(uuid.UUID(run_id), uuid.UUID(organization_id))


@celery_app.task(name="eka.proof.schedule")
def schedule_permission_proofs() -> int:
    return permission_proof.schedule_all()
