"""Celery application. Redis is only the trigger: the `ingestion_jobs` table is the
source of truth, jobs are claimed with a database lease, and a periodic reaper re-queues
work whose message was lost (e.g. Redis restart, enqueue failure after commit).

    celery -A app.worker.celery_app worker -Q ingestion,maintenance -l INFO
    celery -A app.worker.celery_app beat -l INFO
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery("eka", broker=settings.celery_broker, include=["app.worker.tasks"])
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,  # a crashed worker's message is redelivered...
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_time_limit=settings.ingestion_lease_seconds,
    task_soft_time_limit=max(settings.ingestion_lease_seconds - 60, 60),
    # ...and the DB lease (claim) makes redelivery idempotent.
    broker_transport_options={"visibility_timeout": settings.ingestion_lease_seconds * 2},
    task_routes={
        "eka.ingestion.*": {"queue": "ingestion"},
        "eka.maintenance.*": {"queue": "maintenance"},
    },
    beat_schedule={
        "requeue-stale-ingestion-jobs": {
            "task": "eka.maintenance.requeue_stale_jobs",
            "schedule": 60.0,
        },
        "apply-retention-policy": {
            "task": "eka.maintenance.apply_retention",
            "schedule": 6 * 3600.0,
        },
    },
    timezone="UTC",
)
