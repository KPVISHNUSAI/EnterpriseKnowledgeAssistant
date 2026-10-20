"""Worker wiring (tasks, routing, schedule, signals) and the stale-job reaper."""

from __future__ import annotations

import io
import logging
import socket
from datetime import timedelta
from typing import Any

from celery import signals
from celery.schedules import crontab
from sqlalchemy import func, update

from app.core.config import get_settings
from app.db.session import session_scope
from app.ingestion import reaper
from app.models import (
    Conversation,
    Document,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    Message,
    MessageRole,
    MessageStatus,
)
from app.storage import get_storage
from app.worker import celery_app as celery_module
from app.worker import tasks
from app.worker.celery_app import celery_app

STALE = timedelta(seconds=get_settings().ingestion_stale_queued_seconds)
NO_CHANGES = {
    "requeued": 0,
    "failed": 0,
    "reenqueued": 0,
    "documents_failed": 0,
    "streams_closed": 0,
}


def ago(delta: timedelta) -> Any:
    return func.now() - delta


def execute(organization_id: Any, statement: Any) -> None:
    with session_scope(organization_id) as db:
        db.execute(statement)
        db.commit()


def make_document(factory: Any, tenant: Any, **values: Any) -> Document:
    doc = factory.document(
        tenant.workspace, uploaded_by=tenant.editor, status=DocumentStatus.QUEUED
    )
    if values:
        execute(doc.organization_id, update(Document).where(Document.id == doc.id).values(**values))
    return doc


def make_job(doc: Document, **values: Any) -> IngestionJob:
    """A job row; SQL expressions in `values` (e.g. timestamps) are applied after insert."""
    columns = {key: values.pop(key) for key in ("status", "attempts") if key in values}
    job = IngestionJob(
        document_id=doc.id,
        organization_id=doc.organization_id,
        document_version=values.pop("document_version", doc.version),
        max_attempts=values.pop("max_attempts", 3),
        **columns,
    )
    with session_scope(doc.organization_id) as db:
        db.add(job)
        db.commit()
    if values:
        execute(
            doc.organization_id,
            update(IngestionJob).where(IngestionJob.id == job.id).values(**values),
        )
    return job


def load(model: Any, row: Any) -> Any:
    with session_scope(row.organization_id) as db:
        return db.get(model, row.id)


# --- Reaper -------------------------------------------------------------------------------


def test_expired_lease_is_requeued(factory, tenant, enqueued):
    doc = make_document(factory, tenant, processing_status=DocumentStatus.PROCESSING)
    job = make_job(
        doc, status=JobStatus.RUNNING, attempts=1, locked_until=ago(timedelta(minutes=1))
    )

    assert reaper.reap_organization(tenant.org.id) == {**NO_CHANGES, "requeued": 1}
    row = load(IngestionJob, job)
    assert (row.status, row.attempts, row.error_code, row.locked_until) == (
        JobStatus.QUEUED,
        1,
        reaper.WORKER_LOST,
        None,
    )
    assert load(Document, doc).processing_status == DocumentStatus.QUEUED
    assert enqueued.named("eka.ingestion.process") == [[str(job.id), str(tenant.org.id)]]


def test_expired_lease_without_attempts_left_fails(factory, tenant, enqueued):
    doc = make_document(factory, tenant, processing_status=DocumentStatus.PROCESSING)
    job = make_job(
        doc, status=JobStatus.RUNNING, attempts=3, locked_until=ago(timedelta(minutes=1))
    )

    assert reaper.reap_organization(tenant.org.id) == {**NO_CHANGES, "failed": 1}
    row = load(IngestionJob, job)
    message = "Processing was interrupted and stopped after 3 attempts."
    assert (row.status, row.error_code, row.error_message) == (
        JobStatus.FAILED,
        reaper.WORKER_LOST,
        message,
    )
    assert row.completed_at is not None and row.locked_until is None
    stored = load(Document, doc)
    assert (stored.processing_status, stored.processing_error) == (DocumentStatus.FAILED, message)
    assert enqueued.tasks == []


def test_valid_lease_and_fresh_queue_entries_are_left_alone(factory, tenant, enqueued):
    running = make_job(
        make_document(factory, tenant),
        status=JobStatus.RUNNING,
        attempts=1,
        locked_until=func.now() + timedelta(minutes=5),
    )
    waiting = make_job(make_document(factory, tenant))
    assert reaper.reap_organization(tenant.org.id) == NO_CHANGES
    assert load(IngestionJob, running).status == JobStatus.RUNNING
    assert load(IngestionJob, waiting).status == JobStatus.QUEUED
    assert enqueued.tasks == []


def test_lost_message_is_sent_again(factory, tenant, enqueued):
    job = make_job(make_document(factory, tenant), run_after=ago(STALE + timedelta(minutes=1)))
    assert reaper.reap_organization(tenant.org.id) == {**NO_CHANGES, "reenqueued": 1}
    row = load(IngestionJob, job)
    assert (row.status, row.attempts, row.error_code) == (JobStatus.QUEUED, 0, None)
    assert enqueued.named("eka.ingestion.process") == [[str(job.id), str(tenant.org.id)]]


def test_queued_job_without_attempts_left_fails(factory, tenant, enqueued):
    doc = make_document(factory, tenant)
    job = make_job(doc, attempts=3, run_after=ago(STALE + timedelta(minutes=1)))
    assert reaper.reap_organization(tenant.org.id) == {**NO_CHANGES, "failed": 1}
    assert load(IngestionJob, job).status == JobStatus.FAILED
    assert load(Document, doc).processing_status == DocumentStatus.FAILED
    assert enqueued.tasks == []


def test_document_waiting_without_a_job_is_failed(factory, tenant):
    old = ago(STALE + timedelta(minutes=1))
    orphan = make_document(
        factory, tenant, processing_status=DocumentStatus.PROCESSING, updated_at=old
    )
    with_job = make_document(factory, tenant, updated_at=old)
    make_job(with_job)
    recent = make_document(factory, tenant)
    indexed = make_document(
        factory, tenant, processing_status=DocumentStatus.INDEXED, updated_at=old
    )

    assert reaper.reap_organization(tenant.org.id) == {**NO_CHANGES, "documents_failed": 1}
    stored = load(Document, orphan)
    assert (stored.processing_status, stored.processing_error) == (
        DocumentStatus.FAILED,
        reaper.NOT_PROCESSED,
    )
    assert load(Document, with_job).processing_status == DocumentStatus.QUEUED
    assert load(Document, recent).processing_status == DocumentStatus.QUEUED
    assert load(Document, indexed).processing_status == DocumentStatus.INDEXED


def test_orphaned_streaming_answer_is_closed(factory, tenant):
    with session_scope(tenant.org.id) as db:
        conversation = Conversation(
            organization_id=tenant.org.id, user_id=tenant.viewer.id, title="q"
        )
        db.add(conversation)
        db.flush()
        rows = {
            age: Message(
                conversation_id=conversation.id,
                organization_id=tenant.org.id,
                role=MessageRole.ASSISTANT,
                content="",
                status=MessageStatus.STREAMING,
            )
            for age in ("old", "live")
        }
        db.add_all(rows.values())
        db.commit()
    old = rows["old"]
    execute(
        tenant.org.id,
        update(Message)
        .where(Message.id == old.id)
        .values(updated_at=ago(reaper.STALE_STREAM + timedelta(minutes=1))),
    )

    assert reaper.reap_organization(tenant.org.id) == {**NO_CHANGES, "streams_closed": 1}
    assert load(Message, old).status == MessageStatus.INCOMPLETE
    assert load(Message, rows["live"]).status == MessageStatus.STREAMING


def test_superseded_job_is_requeued_without_touching_the_document(factory, tenant, enqueued):
    doc = make_document(factory, tenant, processing_status=DocumentStatus.INDEXED)
    job = make_job(
        doc, status=JobStatus.RUNNING, attempts=1, locked_until=ago(timedelta(minutes=1))
    )
    execute(tenant.org.id, update(Document).where(Document.id == doc.id).values(version=2))

    assert reaper.reap_organization(tenant.org.id)["requeued"] == 1
    assert load(IngestionJob, job).status == JobStatus.QUEUED  # the claim will cancel it
    assert load(Document, doc).processing_status == DocumentStatus.INDEXED


def test_reaper_covers_every_organization(factory, tenant, other_tenant, enqueued):
    expired = {
        "status": JobStatus.RUNNING,
        "attempts": 1,
        "locked_until": ago(timedelta(minutes=1)),
    }
    ours = make_job(make_document(factory, tenant), **expired)
    theirs = make_job(make_document(factory, other_tenant), **expired)

    assert tasks.requeue_stale_jobs() == {**NO_CHANGES, "requeued": 2}
    assert sorted(enqueued.named("eka.ingestion.process")) == sorted(
        [[str(ours.id), str(tenant.org.id)], [str(theirs.id), str(other_tenant.org.id)]]
    )
    assert reaper.reap_organization(tenant.org.id) == NO_CHANGES  # nothing left to do


# --- Tasks and Celery configuration -------------------------------------------------------


def test_process_task_runs_the_job(factory, tenant):
    doc = factory.document(
        tenant.workspace,
        uploaded_by=tenant.editor,
        status=DocumentStatus.QUEUED,
        mime_type="text/plain",
        filename="notes.txt",
    )
    get_storage().put(
        doc.storage_key, io.BytesIO(b"Remember the fire drill."), content_type="text/plain"
    )
    job = make_job(doc)
    assert tasks.process_ingestion(str(job.id), str(tenant.org.id)) == "succeeded"
    assert tasks.process_ingestion(str(job.id), str(tenant.org.id)) == "succeeded"  # redelivery


def test_retention_task(factory, tenant):
    counts = tasks.apply_retention()
    assert set(counts) == {
        "conversations",
        "audit_events",
        "usage_records",
        "ingestion_jobs",
        "user_sessions",
        "invitations",
    }


def test_tasks_routes_and_schedule():
    assert {
        "eka.ingestion.process",
        "eka.maintenance.requeue_stale_jobs",
        "eka.maintenance.apply_retention",
        "eka.proof.run",
        "eka.proof.schedule",
    } <= set(celery_app.tasks)
    conf = celery_app.conf
    assert conf.task_routes == {
        "eka.ingestion.*": {"queue": "ingestion"},
        "eka.maintenance.*": {"queue": "maintenance"},
        "eka.proof.*": {"queue": "maintenance"},
    }
    schedule = {entry["task"]: entry["schedule"] for entry in conf.beat_schedule.values()}
    assert schedule == {
        "eka.maintenance.requeue_stale_jobs": 60.0,
        "eka.maintenance.apply_retention": crontab(minute=15, hour="*/6"),
        "eka.proof.schedule": crontab(minute=0, hour=2, day_of_week=1),  # Mondays
    }
    lease = get_settings().ingestion_lease_seconds
    assert (conf.task_time_limit, conf.task_soft_time_limit) == (lease, lease - 60)
    assert conf.task_acks_late and conf.task_reject_on_worker_lost
    assert conf.worker_prefetch_multiplier == 1


def test_worker_process_serves_metrics(monkeypatch):
    started: list[int] = []
    monkeypatch.setattr(celery_module, "start_http_server", started.append)
    signals.worker_process_init.send(sender=None)
    assert started == [get_settings().worker_metrics_port]

    monkeypatch.setattr(get_settings(), "metrics_enabled", False)
    signals.worker_process_init.send(sender=None)
    assert len(started) == 1


def test_busy_metrics_port_is_logged_not_fatal(monkeypatch, caplog):
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))  # noqa: S104 - where the metrics server listens
        busy.listen()
        monkeypatch.setattr(get_settings(), "worker_metrics_port", busy.getsockname()[1])
        with caplog.at_level(logging.WARNING, logger="app.worker.celery_app"):
            signals.worker_process_init.send(sender=None)  # real prometheus server, port taken
    assert "worker.metrics_port_in_use" in caplog.messages


def test_worker_logs_like_the_api(monkeypatch):
    configured: list[tuple[str, str]] = []
    monkeypatch.setattr(celery_module, "configure_logging", lambda *args: configured.append(args))
    signals.setup_logging.send(
        sender=None, loglevel="INFO", logfile=None, format="", colorize=False
    )
    settings = get_settings()
    assert configured == [(settings.log_level, settings.log_format)]
