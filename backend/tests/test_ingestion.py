"""Ingestion runner end to end: PostgreSQL with row-level security, local file storage,
the Redis embedding cache and deterministic hashing embeddings."""

from __future__ import annotations

import hashlib
import io
import logging
import math
import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import timedelta
from typing import Any

import pytest
import redis
from celery.exceptions import SoftTimeLimitExceeded
from prometheus_client import REGISTRY
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import undefer

from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.db.session import session_scope
from app.ingestion import runner
from app.ingestion.chunking import embedding_text
from app.ingestion.runner import process_job, retry_delay_seconds
from app.ingestion.tokens import count_tokens
from app.ingestion.types import HEADING_SEPARATOR, Chunk, IngestionError
from app.models import Document, DocumentChunk, DocumentStatus, IngestionJob, JobStatus
from app.providers.embeddings import EmbeddingError, HashingEmbedder, cache
from app.storage import StorageError, get_storage
from app.worker import queue
from app.worker.celery_app import celery_app

DIM = get_settings().embedding_dimensions

GUIDE = b"""# Onboarding Guide

Welcome to the team. This guide explains the first week.

## Accounts

Request a laptop from IT. Your manager approves access to the internal tools.

```bash
make setup
```

## Benefits

| Benefit | Detail |
| --- | --- |
| Vacation | 25 days |
"""


# --- Helpers ------------------------------------------------------------------------------


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def store_document(
    factory: Any,
    tenant: Any,
    data: bytes,
    *,
    filename: str = "guide.md",
    mime: str = "text/markdown",
    title: str | None = None,
) -> Document:
    """A queued document with its file in storage. The title is unique by default so the
    embedding texts (and their cache keys) never collide with other tests or runs."""
    doc = factory.document(
        tenant.workspace,
        uploaded_by=tenant.editor,
        title=title or f"Guide {uuid.uuid4().hex[:8]}",
        status=DocumentStatus.QUEUED,
        mime_type=mime,
        filename=filename,
    )
    get_storage().put(doc.storage_key, io.BytesIO(data), content_type=mime)
    return doc


def add_job(doc: Document, **values: Any) -> IngestionJob:
    job = IngestionJob(
        document_id=doc.id,
        organization_id=doc.organization_id,
        document_version=values.pop("document_version", doc.version),
        max_attempts=values.pop("max_attempts", 3),
        **values,
    )
    with session_scope(doc.organization_id) as db:
        db.add(job)
        db.commit()
    return job


def job_row(job: IngestionJob) -> IngestionJob:
    with session_scope(job.organization_id) as db:
        row = db.get(IngestionJob, job.id)
    assert row is not None
    return row


def document_row(doc: Document) -> Document:
    with session_scope(doc.organization_id) as db:
        row = db.get(Document, doc.id)
    assert row is not None
    return row


def chunk_rows(doc: Document) -> list[DocumentChunk]:
    with session_scope(doc.organization_id) as db:
        return list(
            db.scalars(
                select(DocumentChunk)
                .options(undefer(DocumentChunk.embedding))
                .where(DocumentChunk.document_id == doc.id)
                .order_by(DocumentChunk.chunk_index)
            )
        )


def execute(doc: Document, statement: Any) -> None:
    with session_scope(doc.organization_id) as db:
        db.execute(statement)
        db.commit()


def seconds_until_run(job: IngestionJob) -> float:
    with session_scope(job.organization_id) as db:
        value = db.scalar(
            select(func.extract("epoch", IngestionJob.run_after - func.now())).where(
                IngestionJob.id == job.id
            )
        )
    return float(value)


def counter(status: str) -> float:
    return REGISTRY.get_sample_value("eka_ingestion_jobs_total", {"status": status}) or 0.0


class Embedder(HashingEmbedder):
    """Hashing embeddings that can fail, or run `during` (e.g. a concurrent change) first."""

    def __init__(
        self,
        *,
        fail: Exception | None = None,
        during: Callable[[], None] | None = None,
        dimensions: int = DIM,
    ) -> None:
        super().__init__(dimensions)
        self.fail, self.during = fail, during
        self.calls = 0

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.during:
            self.during()
        if self.fail:
            raise self.fail
        return super().embed_documents(texts)


class BrokenStorage:
    backend = "broken"

    def get_bytes(self, key: str) -> bytes:
        raise StorageError("connection reset")


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, list[Any], dict[str, Any]]]:
    """Broker stub that keeps the send options (countdown, queue)."""
    calls: list[tuple[str, list[Any], dict[str, Any]]] = []

    def send_task(name: str, args: Sequence[Any] | None = None, **options: Any) -> None:
        calls.append((name, list(args or []), options))

    monkeypatch.setattr(celery_app, "send_task", send_task)
    return calls


@pytest.fixture
def invalidated(monkeypatch) -> list[tuple[uuid.UUID, set[str]]]:
    """(organization id, hashes) of every cache invalidation."""
    calls: list[tuple[uuid.UUID, set[str]]] = []
    real = cache.invalidate

    def record(organization_id: uuid.UUID, content_hashes: Any) -> None:
        hashes = set(content_hashes)
        calls.append((organization_id, hashes))
        real(organization_id, hashes)

    monkeypatch.setattr(cache, "invalidate", record)
    return calls


def two_page_pdf() -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for line in (
        "The travel policy covers flights and hotels.",
        "Expense reports are due monthly.",
    ):
        pdf.drawString(72, 760, line)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


# --- Success ------------------------------------------------------------------------------


def test_markdown_document_is_indexed(factory, tenant):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    status_during: list[str] = []
    embedder = Embedder(during=lambda: status_during.append(document_row(doc).processing_status))

    assert process_job(job.id, doc.organization_id, embedder=embedder) is JobStatus.SUCCEEDED
    assert status_during == [DocumentStatus.PROCESSING]

    chunks = chunk_rows(doc)
    assert [(c.chunk_index, c.section_title, c.heading_path) for c in chunks] == [
        (0, "Onboarding Guide", "Onboarding Guide"),
        (1, "Accounts", "Onboarding Guide > Accounts"),
        (2, "Benefits", "Onboarding Guide > Benefits"),
    ]
    assert [c.content for c in chunks] == [
        "Welcome to the team. This guide explains the first week.",
        "Request a laptop from IT. Your manager approves access to the internal tools."
        "\n\n```bash\nmake setup\n```",
        "| Benefit | Detail |\n| --- | --- |\n| Vacation | 25 days |",
    ]
    assert [c.chunk_metadata for c in chunks] == [
        {"kinds": ["paragraph"]},
        {"kinds": ["code", "paragraph"]},
        {"kinds": ["table"]},
    ]
    for row in chunks:
        path = tuple(row.heading_path.split(HEADING_SEPARATOR))
        sent_text = embedding_text(doc.title, Chunk(row.content, row.token_count, path, None, None))
        assert sent_text.startswith(f"{doc.title} > Onboarding Guide")
        assert row.content_hash == sha256(sent_text)  # hash of exactly what was embedded
        assert list(row.embedding) == pytest.approx(
            HashingEmbedder(DIM).embed_documents([sent_text])[0], abs=1e-6
        )
        assert math.isclose(math.sqrt(sum(v * v for v in row.embedding)), 1.0, abs_tol=1e-5)
        assert row.token_count == count_tokens(row.content)
        assert row.embedding_model == "hashing-v1"
        assert (row.page_number, row.page_end) == (None, None)
        assert row.organization_id == doc.organization_id

    stored = document_row(doc)
    assert stored.processing_status == DocumentStatus.INDEXED
    assert (stored.chunk_count, stored.page_count, stored.embedding_model) == (
        3,
        None,
        "hashing-v1",
    )
    assert stored.indexed_at is not None and stored.processing_error is None

    done = job_row(job)
    assert (done.status, done.attempts, done.error_code, done.locked_until) == (
        JobStatus.SUCCEEDED,
        1,
        None,
        None,
    )
    assert done.started_at is not None and done.completed_at is not None
    metrics = done.metrics
    assert {k: metrics[k] for k in ("pages", "chunks", "cache_hits", "cache_misses")} == {
        "pages": None,
        "chunks": 3,
        "cache_hits": 0,
        "cache_misses": 3,
    }
    assert metrics["tokens"] == sum(c.token_count for c in chunks)
    assert set(metrics["timings_ms"]) == {
        "fetch",
        "parse",
        "clean",
        "chunk",
        "embed",
        "index",
        "total",
    }


def test_pdf_pages_are_recorded(factory, tenant):
    doc = store_document(
        factory, tenant, two_page_pdf(), filename="policy.pdf", mime="application/pdf"
    )
    job = add_job(doc)
    assert process_job(job.id, doc.organization_id) is JobStatus.SUCCEEDED
    (only,) = chunk_rows(doc)
    assert only.content == (
        "The travel policy covers flights and hotels.\n\nExpense reports are due monthly."
    )
    assert (only.page_number, only.page_end) == (1, 2)
    assert document_row(doc).page_count == 2
    assert job_row(job).metrics["pages"] == 2
    assert job_row(job).metrics["pages_failed"] == 0


def test_reindex_reuses_cached_embeddings(factory, tenant):
    doc = store_document(factory, tenant, GUIDE)
    assert process_job(add_job(doc).id, doc.organization_id) is JobStatus.SUCCEEDED
    before = [(c.content, c.content_hash) for c in chunk_rows(doc)]
    first = document_row(doc)
    assert first.indexed_version == 1 and first.version_indexed_at == first.indexed_at

    embedder = Embedder()
    job = add_job(doc)  # a reindex of the same version
    assert process_job(job.id, doc.organization_id, embedder=embedder) is JobStatus.SUCCEEDED
    assert embedder.calls == 0  # every vector came from the cache
    assert (job_row(job).metrics["cache_hits"], job_row(job).metrics["cache_misses"]) == (3, 0)
    assert [(c.content, c.content_hash) for c in chunk_rows(doc)] == before
    again = document_row(doc)
    # Still version 1, live since its first indexing: answers keep their "current" status.
    assert again.indexed_at > first.indexed_at
    assert (again.indexed_version, again.version_indexed_at) == (1, first.version_indexed_at)


def test_identical_document_in_another_tenant_is_embedded_again(factory, tenant, other_tenant):
    """SEC-01: cached embeddings belong to one organization. Otherwise a tenant could tell
    from ingestion speed that another tenant indexed byte-identical text, and one tenant's
    deletion would drop the other's entries."""
    title = f"Guide {uuid.uuid4().hex[:8]}"  # same title + text: same embedding texts
    ours, theirs = (store_document(factory, t, GUIDE, title=title) for t in (tenant, other_tenant))
    assert process_job(add_job(ours).id, ours.organization_id) is JobStatus.SUCCEEDED

    embedder = Embedder()
    job = add_job(theirs)
    assert process_job(job.id, theirs.organization_id, embedder=embedder) is JobStatus.SUCCEEDED
    assert embedder.calls == 1  # nothing came from the first tenant's entries
    assert (job_row(job).metrics["cache_hits"], job_row(job).metrics["cache_misses"]) == (0, 3)
    assert [c.content_hash for c in chunk_rows(ours)] == [
        c.content_hash for c in chunk_rows(theirs)
    ]

    # The first tenant deletes its copy: the other tenant's entries survive.
    cache.invalidate(ours.organization_id, [c.content_hash for c in chunk_rows(ours)])
    embedder = Embedder()
    job = add_job(theirs)
    assert process_job(job.id, theirs.organization_id, embedder=embedder) is JobStatus.SUCCEEDED
    assert embedder.calls == 0 and job_row(job).metrics["cache_hits"] == 3


def test_redelivered_message_is_a_no_op(factory, tenant):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    assert process_job(job.id, doc.organization_id) is JobStatus.SUCCEEDED
    ids = [c.id for c in chunk_rows(doc)]
    embedder = Embedder()
    assert process_job(job.id, doc.organization_id, embedder=embedder) is JobStatus.SUCCEEDED
    assert embedder.calls == 0
    assert job_row(job).attempts == 1
    assert [c.id for c in chunk_rows(doc)] == ids


def test_run_ingestion_fixture_runs_enqueued_jobs(factory, tenant, run_ingestion):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    assert queue.enqueue_ingestion(job.id, doc.organization_id)
    assert run_ingestion() == [JobStatus.SUCCEEDED]
    assert document_row(doc).chunk_count == 3


# --- Versions, fencing and concurrent changes ----------------------------------------------


def test_job_for_an_older_version_is_cancelled(factory, tenant):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    execute(doc, update(Document).where(Document.id == doc.id).values(version=2))
    embedder = Embedder()
    assert process_job(job.id, doc.organization_id, embedder=embedder) is JobStatus.CANCELLED
    done = job_row(job)
    assert (done.status, done.error_code) == (JobStatus.CANCELLED, runner.SUPERSEDED)
    assert done.completed_at is not None
    assert embedder.calls == 0 and chunk_rows(doc) == []
    assert document_row(doc).processing_status == DocumentStatus.QUEUED  # the newer job's


def test_new_version_uploaded_while_processing_wins(factory, tenant):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    bump = update(Document).where(Document.id == doc.id).values(version=Document.version + 1)
    embedder = Embedder(during=lambda: execute(doc, bump))
    assert process_job(job.id, doc.organization_id, embedder=embedder) is JobStatus.CANCELLED
    assert job_row(job).error_code == runner.SUPERSEDED
    assert chunk_rows(doc) == []


def test_stale_worker_cannot_overwrite_a_newer_attempt(factory, tenant, caplog):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    # Another worker claims the job while this one is still embedding (its lease ran out).
    reclaim = update(IngestionJob).where(IngestionJob.id == job.id).values(attempts=2)
    embedder = Embedder(during=lambda: execute(doc, reclaim))
    with caplog.at_level(logging.WARNING, logger="app.ingestion.runner"):
        assert process_job(job.id, doc.organization_id, embedder=embedder) is JobStatus.RUNNING
    assert "ingestion.lease_lost" in caplog.messages
    assert chunk_rows(doc) == []
    current = job_row(job)
    assert (current.status, current.attempts, current.completed_at) == (JobStatus.RUNNING, 2, None)


def test_stale_worker_failure_leaves_the_newer_attempt_alone(factory, tenant, sent):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    reclaim = update(IngestionJob).where(IngestionJob.id == job.id).values(attempts=2)
    embedder = Embedder(fail=EmbeddingError("down"), during=lambda: execute(doc, reclaim))
    assert process_job(job.id, doc.organization_id, embedder=embedder) is JobStatus.RUNNING
    current = job_row(job)
    assert (current.status, current.error_code) == (JobStatus.RUNNING, None)
    assert sent == []


def test_document_deleted_while_processing(factory, tenant, invalidated):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    remove = delete(Document).where(Document.id == doc.id)  # cascades to the job
    embedder = Embedder(during=lambda: execute(doc, remove))
    assert process_job(job.id, doc.organization_id, embedder=embedder) is JobStatus.CANCELLED
    # The vectors this attempt cached for the deleted document are dropped again.
    assert [(org, len(hashes)) for org, hashes in invalidated] == [(doc.organization_id, 3)]
    assert process_job(job.id, doc.organization_id) is JobStatus.CANCELLED  # job is gone


def test_failure_after_deletion_is_not_logged_as_an_error(factory, tenant, caplog):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    remove = delete(Document).where(Document.id == doc.id)  # cascades to the job
    embedder = Embedder(during=lambda: execute(doc, remove), fail=RuntimeError("boom"))
    with caplog.at_level(logging.INFO, logger="app.ingestion.runner"):
        assert process_job(job.id, doc.organization_id, embedder=embedder) is JobStatus.CANCELLED
    assert "ingestion.document_deleted" in caplog.messages
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_job_of_another_organization_is_invisible(factory, tenant, other_tenant):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    assert process_job(job.id, other_tenant.org.id) is JobStatus.CANCELLED
    untouched = job_row(job)
    assert (untouched.status, untouched.attempts) == (JobStatus.QUEUED, 0)


def test_lease_is_respected_until_it_expires(factory, tenant):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc, status=JobStatus.RUNNING, attempts=1)
    locked = update(IngestionJob).where(IngestionJob.id == job.id)
    execute(doc, locked.values(locked_until=func.now() + timedelta(minutes=10)))
    assert process_job(job.id, doc.organization_id) is JobStatus.RUNNING  # held elsewhere
    assert job_row(job).attempts == 1

    execute(doc, locked.values(locked_until=func.now() - timedelta(minutes=1)))
    assert process_job(job.id, doc.organization_id) is JobStatus.SUCCEEDED  # worker died
    assert job_row(job).attempts == 2


def test_job_without_attempts_left_is_not_claimed(factory, tenant):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc, attempts=3, max_attempts=3)
    assert process_job(job.id, doc.organization_id) is JobStatus.QUEUED  # the reaper fails it
    assert job_row(job).attempts == 3


# --- Failures and retries -------------------------------------------------------------------


def test_transient_failure_is_retried_with_backoff(factory, tenant, sent):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    failing = Embedder(fail=EmbeddingError("connection refused"))
    assert process_job(job.id, doc.organization_id, embedder=failing) is JobStatus.QUEUED

    waiting = job_row(job)
    assert (waiting.status, waiting.attempts, waiting.error_code, waiting.locked_until) == (
        JobStatus.QUEUED,
        1,
        "embedding_unavailable",
        None,
    )
    assert waiting.error_message == "The embedding service was unavailable."
    base = get_settings().ingestion_retry_base_seconds
    assert base - 5 < seconds_until_run(job) <= base * 1.1
    assert document_row(doc).processing_status == DocumentStatus.QUEUED
    ((name, args, options),) = sent
    assert (name, args, options["queue"]) == (
        "eka.ingestion.process",
        [str(job.id), str(doc.organization_id)],
        "ingestion",
    )
    assert base + 1 <= options["countdown"] <= base * 1.1 + 1

    # Not due yet: an early (duplicate) message does nothing.
    assert process_job(job.id, doc.organization_id) is JobStatus.QUEUED
    assert job_row(job).attempts == 1

    execute(doc, update(IngestionJob).where(IngestionJob.id == job.id).values(run_after=func.now()))
    assert process_job(job.id, doc.organization_id) is JobStatus.SUCCEEDED
    done = job_row(job)
    assert (done.attempts, done.error_code, done.error_message) == (2, None, None)
    assert done.started_at == waiting.started_at  # the first attempt's start is kept


def test_retries_stop_after_max_attempts(factory, tenant, sent):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc, max_attempts=2)
    failing = Embedder(fail=EmbeddingError("timeout"))
    assert process_job(job.id, doc.organization_id, embedder=failing) is JobStatus.QUEUED
    execute(doc, update(IngestionJob).where(IngestionJob.id == job.id).values(run_after=func.now()))
    assert process_job(job.id, doc.organization_id, embedder=failing) is JobStatus.FAILED

    failed = job_row(job)
    message = "The embedding service was unavailable. Gave up after 2 attempts."
    assert (failed.status, failed.attempts, failed.error_code, failed.error_message) == (
        JobStatus.FAILED,
        2,
        "embedding_unavailable",
        message,
    )
    assert failed.completed_at is not None and failed.locked_until is None
    stored = document_row(doc)
    assert (stored.processing_status, stored.processing_error) == (DocumentStatus.FAILED, message)
    assert len(sent) == 1  # only the first failure was re-enqueued


def test_document_problem_fails_at_once(factory, tenant, sent):
    doc = store_document(
        factory, tenant, b"not really a pdf", filename="scan.pdf", mime="application/pdf"
    )
    job = add_job(doc)
    assert process_job(job.id, doc.organization_id) is JobStatus.FAILED
    failed = job_row(job)
    assert (failed.attempts, failed.error_code) == (1, "corrupt_file")
    assert failed.error_message.startswith("The PDF could not be read.")
    stored = document_row(doc)
    assert stored.processing_status == DocumentStatus.FAILED
    assert stored.processing_error == failed.error_message
    assert sent == []


@pytest.mark.parametrize(
    ("data", "mime", "filename", "code"),
    [
        (b"# Only a title\n\n   \n", "text/markdown", "empty.md", "empty_document"),
        (b"PK\x03\x04", "application/zip", "archive.zip", "unsupported_file_type"),
        (b"\x81\x8d\x81", "text/plain", "legacy.txt", "unreadable_text"),
    ],
    ids=["empty", "unsupported", "undecodable"],
)
def test_permanent_errors(factory, tenant, data, mime, filename, code):
    doc = store_document(factory, tenant, data, filename=filename, mime=mime)
    job = add_job(doc)
    assert process_job(job.id, doc.organization_id) is JobStatus.FAILED
    assert job_row(job).error_code == code


@pytest.mark.parametrize(
    ("embedder", "storage", "status", "code"),
    [
        (Embedder(fail=SoftTimeLimitExceeded()), None, JobStatus.FAILED, "timeout"),
        (Embedder(dimensions=8), None, JobStatus.FAILED, "embedding_failed"),
        (
            Embedder(fail=EmbeddingError("bad", retryable=False)),
            None,
            JobStatus.FAILED,
            "embedding_failed",
        ),
        (None, BrokenStorage(), JobStatus.QUEUED, "storage_unavailable"),
        (Embedder(fail=RuntimeError("bug")), None, JobStatus.QUEUED, "internal_error"),
    ],
    ids=["time-limit", "wrong-dimension", "rejected", "storage", "unexpected"],
)
def test_failure_classification(factory, tenant, sent, embedder, storage, status, code):
    doc = store_document(factory, tenant, GUIDE)
    job = add_job(doc)
    result = process_job(job.id, doc.organization_id, embedder=embedder, storage=storage)
    assert result is status
    assert job_row(job).error_code == code
    assert len(sent) == (1 if status is JobStatus.QUEUED else 0)


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (IngestionError("corrupt_file", "Damaged."), "corrupt_file", False),
        (SoftTimeLimitExceeded(), "timeout", False),
        (EmbeddingError("down"), "embedding_unavailable", True),
        (EmbeddingError("bad config", retryable=False), "embedding_failed", False),
        (StorageError("reset"), "storage_unavailable", True),
        (OperationalError("SELECT 1", {}, Exception("server closed")), "service_unavailable", True),
        (redis.ConnectionError("refused"), "service_unavailable", True),
        (KeyError("boom"), "internal_error", True),
    ],
)
def test_error_classes(error, code, retryable):
    failure = runner._classify(error)
    assert (failure.code, failure.retryable) == (code, retryable)
    assert "boom" not in failure.message and "server closed" not in failure.message


def test_retry_delay_grows_exponentially():
    base = get_settings().ingestion_retry_base_seconds
    for attempt in (1, 2, 3):
        delay = retry_delay_seconds(attempt)
        assert base * 2 ** (attempt - 1) <= delay <= base * 2 ** (attempt - 1) * 1.1


def test_failed_reindex_keeps_the_previous_chunks(factory, tenant):
    doc = store_document(factory, tenant, GUIDE)
    assert process_job(add_job(doc).id, doc.organization_id) is JobStatus.SUCCEEDED
    before = [(c.id, c.content) for c in chunk_rows(doc)]

    get_storage().put(doc.storage_key, io.BytesIO(b"\x81\x8d"), content_type="text/markdown")
    job = add_job(doc)
    assert process_job(job.id, doc.organization_id) is JobStatus.FAILED
    assert [(c.id, c.content) for c in chunk_rows(doc)] == before  # still searchable
    stored = document_row(doc)
    assert (stored.processing_status, stored.chunk_count) == (DocumentStatus.FAILED, 3)
    assert stored.indexed_version == 1
    assert stored.processing_error == job_row(job).error_message


def test_new_version_replaces_the_chunks(factory, tenant, invalidated):
    doc = store_document(factory, tenant, GUIDE)
    assert process_job(add_job(doc).id, doc.organization_id) is JobStatus.SUCCEEDED
    old = {c.content_hash for c in chunk_rows(doc)}
    v1_since = document_row(doc).version_indexed_at

    v2 = GUIDE.split(b"## Accounts")[0] + b"## Equipment\n\nLaptops ship before your first day.\n"
    new_key = doc.storage_key.replace("/v1/", "/v2/")
    get_storage().put(new_key, io.BytesIO(v2), content_type="text/markdown")
    execute(
        doc,
        update(Document)
        .where(Document.id == doc.id)
        .values(version=2, storage_key=new_key, processing_status=DocumentStatus.QUEUED),
    )
    invalidated.clear()
    job = add_job(doc, document_version=2)
    assert process_job(job.id, doc.organization_id) is JobStatus.SUCCEEDED

    chunks = chunk_rows(doc)
    assert [c.content for c in chunks] == [
        "Welcome to the team. This guide explains the first week.",
        "Laptops ship before your first day.",
    ]
    new = {c.content_hash for c in chunks}
    assert len(old & new) == 1  # the unchanged section keeps its hash (and cached vector)
    assert invalidated == [(doc.organization_id, old - new)]
    stored = document_row(doc)
    assert stored.chunk_count == 2
    assert stored.indexed_version == 2 and stored.version_indexed_at > v1_since


# --- Observability ------------------------------------------------------------------------


def test_outcome_metrics(factory, tenant, sent):
    succeeded, retried, failed = counter("succeeded"), counter("retried"), counter("failed")
    durations = REGISTRY.get_sample_value("eka_ingestion_duration_seconds_count") or 0.0

    doc = store_document(factory, tenant, GUIDE)
    process_job(add_job(doc).id, doc.organization_id)
    other = store_document(factory, tenant, GUIDE)
    process_job(
        add_job(other).id, other.organization_id, embedder=Embedder(fail=EmbeddingError("x"))
    )
    broken = store_document(
        factory, tenant, b"%PDF-1.4 junk", filename="x.pdf", mime="application/pdf"
    )
    process_job(add_job(broken).id, broken.organization_id)

    assert (counter("succeeded") - succeeded, counter("retried") - retried) == (1, 1)
    assert counter("failed") - failed == 1
    assert (REGISTRY.get_sample_value("eka_ingestion_duration_seconds_count") or 0) - durations == 3


class JsonLines(logging.Handler):
    """Formats records when they are emitted, as production logging does (the job and
    organization ids come from context variables that only exist during the job)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def json_logs() -> Iterator[list[str]]:
    handler, logger = JsonLines(), logging.getLogger("app")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler.lines
    logger.removeHandler(handler)
    logger.setLevel(level)


def test_logs_carry_ids_but_never_document_text(factory, tenant, sent, json_logs):
    secret = b"The merger with Initech closes on the ninth of March."
    data = GUIDE + b"\n## Confidential\n\n" + secret + b"\n"
    doc = store_document(factory, tenant, data)
    assert process_job(add_job(doc).id, doc.organization_id) is JobStatus.SUCCEEDED
    other = store_document(factory, tenant, data)  # new title: nothing cached yet
    failing = Embedder(fail=RuntimeError("embedding exploded"))
    job = add_job(other)
    assert process_job(job.id, other.organization_id, embedder=failing) is JobStatus.QUEUED

    lines = json_logs
    assert any("ingestion.succeeded" in line for line in lines)
    unexpected = next(line for line in lines if "ingestion.unexpected_error" in line)
    assert str(job.id) in unexpected and "Traceback" in unexpected  # correlated, with stack
    logged = "\n".join(lines)
    assert "Initech" not in logged and "merger" not in logged
    assert "Onboarding Guide" not in logged
    assert doc.title not in logged and other.title not in logged
