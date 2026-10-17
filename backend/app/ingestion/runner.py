"""Ingestion job runner: the state machine behind the `eka.ingestion.process` task.

The ingestion_jobs row is the source of truth; a Celery message only says "look at job
X", so duplicate or redelivered messages are harmless. A worker claims the job with a
lease (`attempts` + 1 becomes its fencing token), does the slow work without holding
locks, and swaps the document's chunks in one transaction that first re-checks the job
is still its own and the document version unchanged - a stale worker can never overwrite
newer data, and until that commit the previous chunks stay searchable. See
docs/INGESTION.md for the full state machine.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import redis
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import job_id_var, org_id_var
from app.core.metrics import INGESTION_JOBS, INGESTION_LATENCY
from app.db.session import session_scope
from app.ingestion.chunking import chunk_blocks, embedding_text
from app.ingestion.cleaning import clean_blocks
from app.ingestion.parsers import detect_file_type, parse
from app.ingestion.types import Chunk, IngestionError
from app.models import Document, DocumentChunk, DocumentStatus, IngestionJob, JobStatus
from app.providers.embeddings import EmbeddingError, EmbeddingProvider
from app.providers.embeddings import cache as embedding_cache
from app.providers.registry import get_embedder
from app.storage import ObjectStorage, StorageError, get_storage
from app.worker import queue

logger = logging.getLogger(__name__)

SUPERSEDED = "superseded"


@dataclass(frozen=True)
class _Claim:
    job_id: uuid.UUID
    organization_id: uuid.UUID
    document_id: uuid.UUID
    document_version: int
    attempts: int  # fencing token: only this attempt may finish the job
    max_attempts: int


@dataclass(frozen=True)
class _Failure:
    code: str
    message: str  # stored on the job/document and shown to users: must be safe
    retryable: bool


def process_job(
    job_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    embedder: EmbeddingProvider | None = None,
    storage: ObjectStorage | None = None,
) -> JobStatus:
    """Run one attempt of an ingestion job; returns the job's status afterwards."""
    tokens = job_id_var.set(str(job_id)), org_id_var.set(str(organization_id))
    started = time.monotonic()
    try:
        claim = _claim(job_id, organization_id)
        if claim is None:
            status = _current_status(job_id, organization_id)
            logger.info("ingestion.not_claimed", extra={"status": status.value})
            return status
        try:
            status = _run(claim, embedder, storage, started)
        except Exception as exc:
            status = _fail(claim, exc)
        outcome = "retried" if status is JobStatus.QUEUED else status.value
        INGESTION_JOBS.labels(outcome).inc()
        INGESTION_LATENCY.observe(time.monotonic() - started)
        return status
    finally:
        job_id_var.reset(tokens[0])
        org_id_var.reset(tokens[1])


def _claim(job_id: uuid.UUID, organization_id: uuid.UUID) -> _Claim | None:
    """Atomically take a runnable job (queued and due, or running with an expired lease)."""
    lease = timedelta(seconds=get_settings().ingestion_lease_seconds)
    now = func.now()
    stmt = (
        update(IngestionJob)
        .where(
            IngestionJob.id == job_id,
            IngestionJob.organization_id == organization_id,
            IngestionJob.attempts < IngestionJob.max_attempts,
            or_(
                and_(IngestionJob.status == JobStatus.QUEUED, IngestionJob.run_after <= now),
                and_(IngestionJob.status == JobStatus.RUNNING, IngestionJob.locked_until < now),
            ),
        )
        .values(
            status=JobStatus.RUNNING,
            attempts=IngestionJob.attempts + 1,
            locked_until=now + lease,
            started_at=func.coalesce(IngestionJob.started_at, now),
        )
        .returning(
            IngestionJob.document_id,
            IngestionJob.document_version,
            IngestionJob.attempts,
            IngestionJob.max_attempts,
        )
        .execution_options(synchronize_session=False)
    )
    with session_scope(organization_id) as db:
        row = db.execute(stmt).one_or_none()
        db.commit()
    if row is None:
        return None
    return _Claim(
        job_id,
        organization_id,
        row.document_id,
        row.document_version,
        row.attempts,
        row.max_attempts,
    )


def _current_status(job_id: uuid.UUID, organization_id: uuid.UUID) -> JobStatus:
    with session_scope(organization_id) as db:
        status = db.scalar(select(IngestionJob.status).where(IngestionJob.id == job_id))
    return status or JobStatus.CANCELLED  # gone: deleted together with its document


@contextmanager
def _timed(timings: dict[str, float], stage: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = round((time.perf_counter() - start) * 1000, 1)


def _run(
    claim: _Claim,
    embedder: EmbeddingProvider | None,
    storage: ObjectStorage | None,
    started: float,
) -> JobStatus:
    settings = get_settings()
    with session_scope(claim.organization_id) as db:
        document = db.get(Document, claim.document_id)
        if document is None:
            return JobStatus.CANCELLED
        if document.version != claim.document_version:
            return _cancel_superseded(db, claim)
        title, key = document.title, document.storage_key
        file_type = detect_file_type(document.mime_type, document.original_filename)
        document.processing_status = DocumentStatus.PROCESSING
        db.commit()

    timings: dict[str, float] = {}
    with _timed(timings, "fetch"):
        data = (storage or get_storage()).get_bytes(key)
    with _timed(timings, "parse"):
        parsed = parse(data, file_type)
    with _timed(timings, "clean"):
        blocks = clean_blocks(parsed.blocks)
    with _timed(timings, "chunk"):
        chunks = chunk_blocks(
            blocks,
            target_tokens=settings.chunk_target_tokens,
            max_tokens=settings.chunk_max_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
        )
    if not chunks:
        raise IngestionError("empty_document", "The document contains no text that can be indexed.")

    embedder = embedder or get_embedder()
    if embedder.dimensions != settings.embedding_dimensions:
        raise EmbeddingError(
            f"embedder dimension {embedder.dimensions} != EMBEDDING_DIMENSIONS "
            f"{settings.embedding_dimensions}",
            retryable=False,
        )
    texts = [embedding_text(title, chunk) for chunk in chunks]
    hashes = [hashlib.sha256(text.encode()).hexdigest() for text in texts]
    with _timed(timings, "embed"):
        vectors, cache_hits = embedding_cache.embed_with_cache(
            embedder, texts, hashes, batch_size=settings.embedding_batch_size
        )
    if any(len(vector) != settings.embedding_dimensions for vector in vectors):
        raise EmbeddingError(
            "embedding provider returned vectors of the wrong dimension", retryable=False
        )

    metrics: dict[str, Any] = {
        "pages": parsed.page_count,
        "blocks": len(blocks),
        "chunks": len(chunks),
        "tokens": sum(chunk.token_count for chunk in chunks),
        "cache_hits": cache_hits,
        "cache_misses": len(chunks) - cache_hits,
        **parsed.stats,
        "timings_ms": timings,
    }
    return _index(
        claim, chunks, hashes, vectors, embedder.model_name, parsed.page_count, metrics, started
    )


def _index(
    claim: _Claim,
    chunks: list[Chunk],
    hashes: list[str],
    vectors: list[list[float]],
    model: str,
    page_count: int | None,
    metrics: dict[str, Any],
    started: float,
) -> JobStatus:
    """Swap in the new chunks - only if this attempt still owns the job and the document
    version is unchanged. Locks the document first, then the job (the order every writer
    uses)."""
    index_started = time.perf_counter()
    with session_scope(claim.organization_id) as db:
        version = db.scalar(
            select(Document.version).where(Document.id == claim.document_id).with_for_update()
        )
        job = db.execute(
            select(IngestionJob.status, IngestionJob.attempts)
            .where(IngestionJob.id == claim.job_id)
            .with_for_update()
        ).one_or_none()
        if version is None or job is None:
            # Deleted while we worked: drop what this attempt just cached for it.
            embedding_cache.invalidate(hashes)
            return JobStatus.CANCELLED
        if job.status != JobStatus.RUNNING or job.attempts != claim.attempts:
            # Cancelled (a newer upload) or re-claimed by another worker after our lease ran out.
            logger.warning(
                "ingestion.lease_lost", extra={"attempt": claim.attempts, "status": job.status}
            )
            return JobStatus(job.status)
        if version != claim.document_version:
            return _cancel_superseded(db, claim)

        old_hashes = set(
            db.scalars(
                delete(DocumentChunk)
                .where(DocumentChunk.document_id == claim.document_id)
                .returning(DocumentChunk.content_hash)
                .execution_options(synchronize_session=False)
            )
        )
        db.execute(
            insert(DocumentChunk),
            [
                {
                    "id": uuid.uuid4(),
                    "document_id": claim.document_id,
                    "organization_id": claim.organization_id,
                    "chunk_index": index,
                    "content": chunk.content,
                    "content_hash": content_hash,
                    "token_count": chunk.token_count,
                    "embedding": vector,
                    "embedding_model": model,
                    "page_number": chunk.page_number,
                    "page_end": chunk.page_end,
                    "section_title": (chunk.section_title or "")[:1000] or None,
                    "heading_path": chunk.heading_path_text,
                    "chunk_metadata": chunk.metadata,
                }
                for index, (chunk, content_hash, vector) in enumerate(
                    zip(chunks, hashes, vectors, strict=True)
                )
            ],
        )
        db.execute(
            update(Document)
            .where(Document.id == claim.document_id)
            .values(
                processing_status=DocumentStatus.INDEXED,
                processing_error=None,
                chunk_count=len(chunks),
                page_count=page_count,
                embedding_model=model,
                indexed_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        metrics["timings_ms"]["index"] = round((time.perf_counter() - index_started) * 1000, 1)
        metrics["timings_ms"]["total"] = round((time.monotonic() - started) * 1000, 1)
        _finish(db, claim, JobStatus.SUCCEEDED, metrics=metrics)
        db.commit()
    embedding_cache.invalidate(old_hashes - set(hashes))  # vectors of the replaced text
    logger.info(
        "ingestion.succeeded",
        extra={
            "chunks": len(chunks),
            "cache_hits": metrics["cache_hits"],
            "attempt": claim.attempts,
        },
    )
    return JobStatus.SUCCEEDED


def _finish(
    db: Session,
    claim: _Claim,
    status: JobStatus,
    *,
    code: str | None = None,
    message: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> None:
    """Move the job to a terminal state (fenced on the claimed attempt)."""
    values: dict[str, Any] = {
        "status": status,
        "completed_at": func.now(),
        "locked_until": None,
        "error_code": code,
        "error_message": message,
    }
    if metrics is not None:
        values["metrics"] = metrics
    db.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == claim.job_id,
            IngestionJob.status == JobStatus.RUNNING,
            IngestionJob.attempts == claim.attempts,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )


def _cancel_superseded(db: Session, claim: _Claim) -> JobStatus:
    """The document changed version since the job was created: a newer job owns it."""
    _finish(
        db,
        claim,
        JobStatus.CANCELLED,
        code=SUPERSEDED,
        message="A newer version of the document was uploaded.",
    )
    db.commit()
    logger.info("ingestion.superseded")
    return JobStatus.CANCELLED


def _classify(exc: Exception) -> _Failure:
    if isinstance(exc, IngestionError):
        return _Failure(exc.code, exc.message, retryable=False)
    if isinstance(exc, SoftTimeLimitExceeded):
        return _Failure(
            "timeout",
            "Processing took longer than the time limit. Split the document into smaller "
            "files and upload them again.",
            retryable=False,
        )
    if isinstance(exc, EmbeddingError):
        if exc.retryable:
            return _Failure("embedding_unavailable", "The embedding service was unavailable.", True)
        return _Failure(
            "embedding_failed",
            "The embedding service could not process the document. Contact your administrator.",
            retryable=False,
        )
    if isinstance(exc, StorageError):
        return _Failure("storage_unavailable", "The stored file could not be read.", True)
    if isinstance(exc, OperationalError | redis.RedisError):
        return _Failure("service_unavailable", "A backing service was unavailable.", True)
    return _Failure("internal_error", "An unexpected error occurred while processing.", True)


def retry_delay_seconds(attempts: int) -> float:
    """Exponential backoff after attempt number `attempts` (1-based), with jitter."""
    delay = get_settings().ingestion_retry_base_seconds * 2 ** (attempts - 1)
    return delay + random.uniform(0, delay / 10)


def _fail(claim: _Claim, exc: Exception) -> JobStatus:
    failure = _classify(exc)
    retry = failure.retryable and claim.attempts < claim.max_attempts
    message = failure.message
    if failure.retryable and not retry:
        noun = "attempt" if claim.attempts == 1 else "attempts"
        message = f"{message} Gave up after {claim.attempts} {noun}."
    delay = retry_delay_seconds(claim.attempts) if retry else 0.0
    extra: dict[str, Any] = {
        "error_code": failure.code,
        "error_type": type(exc).__name__,
        "attempt": claim.attempts,
        "will_retry": retry,
    }
    if isinstance(exc, EmbeddingError):
        extra["error"] = str(exc)  # our own provider messages; never document text
    if failure.code == "internal_error":
        logger.exception("ingestion.unexpected_error", extra=extra)
    else:
        logger.warning("ingestion.attempt_failed", extra=extra)

    with session_scope(claim.organization_id) as db:
        version = db.scalar(
            select(Document.version).where(Document.id == claim.document_id).with_for_update()
        )
        job = db.execute(
            select(IngestionJob.status, IngestionJob.attempts)
            .where(IngestionJob.id == claim.job_id)
            .with_for_update()
        ).one_or_none()
        if version is None or job is None:
            return JobStatus.CANCELLED
        if job.status != JobStatus.RUNNING or job.attempts != claim.attempts:
            return JobStatus(job.status)  # another attempt owns the job now
        if version != claim.document_version:
            return _cancel_superseded(db, claim)
        document = (
            update(Document)
            .where(Document.id == claim.document_id)
            .execution_options(synchronize_session=False)
        )
        if retry:
            db.execute(
                update(IngestionJob)
                .where(IngestionJob.id == claim.job_id)
                .values(
                    status=JobStatus.QUEUED,
                    run_after=func.now() + timedelta(seconds=delay),
                    locked_until=None,
                    error_code=failure.code,
                    error_message=message,
                )
                .execution_options(synchronize_session=False)
            )
            db.execute(document.values(processing_status=DocumentStatus.QUEUED))
        else:
            _finish(db, claim, JobStatus.FAILED, code=failure.code, message=message)
            # The previous version's chunks (if any) stay searchable.
            db.execute(
                document.values(processing_status=DocumentStatus.FAILED, processing_error=message)
            )
        db.commit()
    if not retry:
        return JobStatus.FAILED
    # The message only triggers work; if it is lost the reaper re-enqueues the job. The
    # extra second absorbs clock skew between the worker and the database.
    queue.enqueue_ingestion(claim.job_id, claim.organization_id, countdown=delay + 1)
    return JobStatus.QUEUED
