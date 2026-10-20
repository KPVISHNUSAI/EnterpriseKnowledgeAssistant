"""Enqueue helpers used by the API. Tasks are sent by name so the API process never
imports the worker (and its ML dependencies)."""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

INGESTION_TASK = "eka.ingestion.process"
PROOF_TASK = "eka.proof.run"


def enqueue_ingestion(
    job_id: uuid.UUID, organization_id: uuid.UUID, *, countdown: float | None = None
) -> bool:
    """Best effort: call only after the job row is committed. A failure is logged and
    the periodic reaper re-enqueues the job, so it is never lost."""
    from app.worker.celery_app import celery_app

    try:
        celery_app.send_task(
            INGESTION_TASK,
            args=[str(job_id), str(organization_id)],
            countdown=countdown,
            queue="ingestion",
        )
        return True
    except Exception:
        logger.exception("ingestion.enqueue_failed", extra={"job_id": str(job_id)})
        return False


def enqueue_proof(run_id: uuid.UUID, organization_id: uuid.UUID) -> bool:
    """Best effort, after the run row is committed; the caller marks the run failed."""
    from app.worker.celery_app import celery_app

    try:
        celery_app.send_task(
            PROOF_TASK, args=[str(run_id), str(organization_id)], queue="maintenance"
        )
        return True
    except Exception:
        logger.exception("proof.enqueue_failed", extra={"run_id": str(run_id)})
        return False
