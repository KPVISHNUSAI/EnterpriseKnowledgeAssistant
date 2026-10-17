"""Audit log queries and usage reporting for organization admins."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import ColumnElement, Date, Float, and_, cast, func, literal_column, or_, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import (
    ACTIVE_JOB_STATUSES,
    AuditEvent,
    Conversation,
    Document,
    DocumentChunk,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    Message,
    UsageKind,
    UsageRecord,
    User,
)
from app.schemas.admin import (
    AuditEventOut,
    IngestionHealth,
    LatencyStats,
    UsageDay,
    UsageResponse,
    UsageTotals,
)
from app.schemas.common import Page, PageParams
from app.services import access
from app.services.access import Principal


def _aware(value: datetime) -> datetime:
    """Query parameters without an offset are taken as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def list_audit_events(
    db: Session,
    principal: Principal,
    *,
    action: str | None,
    actor_user_id: uuid.UUID | None,
    resource_type: str | None,
    resource_id: str | None,
    since: datetime | None,
    until: datetime | None,
    page: PageParams,
) -> Page[AuditEventOut]:
    """Newest first. `action` matches exactly, or as a prefix when it ends with '*';
    `since` and `until` are inclusive."""
    access.require(principal.is_org_admin)
    conditions: list[ColumnElement[bool]] = [
        AuditEvent.organization_id == principal.organization_id
    ]
    if action:
        conditions.append(
            AuditEvent.action.startswith(action[:-1], autoescape=True)
            if action.endswith("*")
            else AuditEvent.action == action
        )
    if actor_user_id is not None:
        conditions.append(AuditEvent.actor_user_id == actor_user_id)
    if resource_type:
        conditions.append(AuditEvent.resource_type == resource_type)
    if resource_id:
        conditions.append(AuditEvent.resource_id == resource_id)
    if since is not None:
        conditions.append(AuditEvent.created_at >= _aware(since))
    if until is not None:
        conditions.append(AuditEvent.created_at <= _aware(until))

    total = db.scalar(select(func.count()).select_from(AuditEvent).where(*conditions))
    rows = db.execute(
        select(AuditEvent, User.email)
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .where(*conditions)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .offset(page.offset)
        .limit(page.page_size)
    )
    items = [
        AuditEventOut(
            id=event.id,
            action=event.action,
            actor_user_id=event.actor_user_id,
            actor_email=email,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            metadata=event.event_metadata,
            ip_address=event.ip_address,
            request_id=event.request_id,
            created_at=event.created_at,
        )
        for event, email in rows
    ]
    return Page(items=items, total=total or 0, page=page.page, page_size=page.page_size)


def _count(db: Session, condition: ColumnElement[bool]) -> int:
    return db.scalar(select(func.count()).where(condition)) or 0


def _percentile(fraction: float, key: str) -> ColumnElement[float]:
    return func.percentile_cont(fraction).within_group(cast(UsageRecord.timings[key].astext, Float))


def get_usage(db: Session, principal: Principal, *, days: int) -> UsageResponse:
    """The window is the last `days` UTC calendar days, today included; content counts
    (documents, chunks, conversations, messages) are current totals."""
    access.require(principal.is_org_admin)
    org_id = principal.organization_id
    now = utcnow()
    first_day = now.date() - timedelta(days=days - 1)
    start = datetime.combine(first_day, time(), tzinfo=UTC)
    in_window = and_(
        UsageRecord.organization_id == org_id,
        UsageRecord.created_at >= start,
        UsageRecord.created_at < start + timedelta(days=days),
    )
    is_chat = UsageRecord.kind == UsageKind.CHAT_COMPLETION

    by_status = {status.value: 0 for status in DocumentStatus}
    storage_bytes = 0
    for status, count, size in db.execute(
        select(Document.processing_status, func.count(), func.sum(Document.file_size))
        .where(Document.organization_id == org_id)
        .group_by(Document.processing_status)
    ):
        by_status[status.value] = count
        storage_bytes += int(size)  # sum(bigint) is numeric

    # A literal (not a bound parameter), so GROUP BY matches the selected expression.
    utc_day = cast(func.timezone(literal_column("'UTC'"), UsageRecord.created_at), Date)
    per_day: dict[date, tuple[int, int, int, Decimal]] = {
        day: (chats, input_tokens, output_tokens, cost)
        for day, chats, input_tokens, output_tokens, cost in db.execute(
            select(
                utc_day,
                func.count().filter(is_chat),
                func.sum(UsageRecord.input_tokens),
                func.sum(UsageRecord.output_tokens),
                func.sum(UsageRecord.cost_usd),
            )
            .where(in_window)
            .group_by(utc_day)
        )
    }
    daily = []
    for offset in range(days):
        d = first_day + timedelta(days=offset)
        chats, input_tokens, output_tokens, cost = per_day.get(d, (0, 0, 0, Decimal(0)))
        daily.append(
            UsageDay(
                date=d,
                chat_requests=chats,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=float(cost),
            )
        )

    samples, retrieval_p50, retrieval_p95, generation_p50, generation_p95 = db.execute(
        select(
            func.count(),
            _percentile(0.5, "retrieval_ms"),
            _percentile(0.95, "retrieval_ms"),
            _percentile(0.5, "generation_ms"),
            _percentile(0.95, "generation_ms"),
        ).where(in_window, is_chat)
    ).one()

    finished_at = func.coalesce(IngestionJob.completed_at, IngestionJob.updated_at)
    jobs = dict(
        db.execute(
            select(IngestionJob.status, func.count())
            .where(
                IngestionJob.organization_id == org_id,
                or_(
                    IngestionJob.status.in_(ACTIVE_JOB_STATUSES),
                    and_(
                        IngestionJob.status.in_((JobStatus.SUCCEEDED, JobStatus.FAILED)),
                        finished_at >= now - timedelta(hours=24),
                    ),
                ),
            )
            .group_by(IngestionJob.status)
        ).all()
    )

    return UsageResponse(
        period_days=days,
        totals=UsageTotals(
            documents=sum(by_status.values()),
            documents_by_status=by_status,
            chunks=_count(db, DocumentChunk.organization_id == org_id),
            storage_bytes=storage_bytes,
            conversations=_count(db, Conversation.organization_id == org_id),
            messages=_count(db, Message.organization_id == org_id),
            chat_requests=sum(day.chat_requests for day in daily),
            input_tokens=sum(day.input_tokens for day in daily),
            output_tokens=sum(day.output_tokens for day in daily),
            # Summed as Decimal, so the total does not drift from the stored values.
            estimated_cost_usd=float(sum((v[3] for v in per_day.values()), Decimal(0))),
        ),
        daily=daily,
        latency=LatencyStats(
            samples=samples,
            retrieval_p50_ms=retrieval_p50,
            retrieval_p95_ms=retrieval_p95,
            generation_p50_ms=generation_p50,
            generation_p95_ms=generation_p95,
        ),
        ingestion=IngestionHealth(
            queued=jobs.get(JobStatus.QUEUED, 0),
            running=jobs.get(JobStatus.RUNNING, 0),
            failed_last_24h=jobs.get(JobStatus.FAILED, 0),
            succeeded_last_24h=jobs.get(JobStatus.SUCCEEDED, 0),
        ),
    )
