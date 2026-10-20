"""Celery application. Redis is only the trigger: the `ingestion_jobs` table is the
source of truth, jobs are claimed with a database lease, and a periodic reaper re-queues
work whose message was lost (e.g. Redis restart, enqueue failure after commit).

    celery -A app.worker.celery_app worker -Q ingestion,maintenance --concurrency 1 -l INFO
    celery -A app.worker.celery_app beat -l INFO

One job at a time per worker; scale with more worker containers (docs/INGESTION.md). On
macOS use `--pool solo`: billiard starts prefork children with spawn there, which Celery's
prefork pool cannot run tasks in.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Celery, signals
from celery.schedules import crontab
from prometheus_client import start_http_server

from app.core.config import get_settings
from app.core.logging import configure_logging

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
        "eka.proof.*": {"queue": "maintenance"},
    },
    beat_schedule={
        "requeue-stale-ingestion-jobs": {
            "task": "eka.maintenance.requeue_stale_jobs",
            "schedule": 60.0,
        },
        "apply-retention-policy": {
            "task": "eka.maintenance.apply_retention",
            # Wall clock, not an interval: beat keeps no state across restarts, and an
            # interval would restart its 6-hour countdown on every deploy.
            "schedule": crontab(minute=15, hour="*/6"),
        },
    },
    timezone="UTC",
)
if settings.permission_proof_schedule:
    minute, hour, day_of_month, month_of_year, day_of_week = (
        settings.permission_proof_schedule.split()
    )
    celery_app.conf.beat_schedule["permission-proof-leak-test"] = {
        "task": "eka.proof.schedule",
        "schedule": crontab(
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            day_of_week=day_of_week,
        ),
    }


@signals.setup_logging.connect
def _configure_logging(**_: Any) -> None:
    """Worker and beat log like the API (structured JSON) instead of Celery's format."""
    configure_logging(settings.log_level, settings.log_format)


@signals.worker_process_init.connect
def _start_metrics_server(**_: Any) -> None:
    """Serve Prometheus metrics from the process that executes tasks: a prefork child, or
    the main process with --pool solo. Sent once per pool process, so with concurrency
    above 1 only the first process gets the port."""
    if not settings.metrics_enabled:
        return
    try:
        start_http_server(settings.worker_metrics_port)
    except OSError:
        logging.getLogger(__name__).warning(
            "worker.metrics_port_in_use", extra={"port": settings.worker_metrics_port}
        )
