"""Retention policy: per-organization deletes under row-level security, plus the global
cleanup of dead sessions and invitations."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.session import session_scope
from app.ingestion import retention
from app.models import (
    AuditEvent,
    Conversation,
    DocumentStatus,
    IngestionJob,
    Invitation,
    JobStatus,
    Message,
    MessageRole,
    OrgRole,
    UsageKind,
    UsageRecord,
    UserSession,
)


def days_ago(days: float) -> datetime:
    return utcnow() - timedelta(days=days)


def save(organization_id: uuid.UUID | None, *rows: Any) -> None:
    with session_scope(organization_id) as db:
        db.add_all(rows)
        db.commit()


def ids(model: Any, organization_id: uuid.UUID | None = None) -> set[uuid.UUID]:
    with session_scope(organization_id) as db:
        return set(db.scalars(select(model.id)))


def conversation(tenant: Any, updated: datetime, *message_times: datetime) -> Conversation:
    org = tenant.org.id
    row = Conversation(
        id=uuid.uuid4(),
        organization_id=org,
        user_id=tenant.viewer.id,
        title="Question",
        created_at=updated,
        updated_at=updated,
    )
    messages = [
        Message(
            conversation_id=row.id,
            organization_id=org,
            role=MessageRole.USER,
            content="What is the policy?",
            created_at=at,
            updated_at=at,
        )
        for at in message_times
    ]
    save(org, row)
    save(org, *messages)
    return row


def audit(org_id: uuid.UUID | None, created: datetime) -> AuditEvent:
    row = AuditEvent(
        id=uuid.uuid4(), organization_id=org_id, action="auth.login", created_at=created
    )
    save(org_id, row)
    return row


def usage(tenant: Any, created: datetime) -> UsageRecord:
    row = UsageRecord(
        id=uuid.uuid4(),
        organization_id=tenant.org.id,
        kind=UsageKind.CHAT_COMPLETION,
        provider="extractive",
        model="none",
        created_at=created,
    )
    save(tenant.org.id, row)
    return row


def job(doc: Any, status: JobStatus, created: datetime, completed: datetime | None) -> IngestionJob:
    row = IngestionJob(
        id=uuid.uuid4(),
        document_id=doc.id,
        organization_id=doc.organization_id,
        document_version=1,
        max_attempts=3,
        status=status,
        created_at=created,
        updated_at=completed or created,
        completed_at=completed,
    )
    save(doc.organization_id, row)
    return row


def populate(factory: Any, tenant: Any) -> dict[str, set[uuid.UUID]]:
    """Rows on both sides of every cut-off; returns the ids expected to survive."""
    settings = get_settings()
    conversation_days = settings.conversation_retention_days
    keep: dict[str, set[uuid.UUID]] = {"conversations": set(), "audit": set(), "usage": set()}
    conversation(tenant, days_ago(conversation_days + 5), days_ago(conversation_days + 5))
    keep["conversations"] |= {
        conversation(tenant, days_ago(conversation_days + 5), days_ago(1)).id,  # active again
        conversation(tenant, days_ago(2), days_ago(2)).id,
    }
    audit(tenant.org.id, days_ago(settings.audit_retention_days + 1))
    keep["audit"].add(audit(tenant.org.id, days_ago(10)).id)
    usage(tenant, days_ago(settings.usage_retention_days + 1))
    keep["usage"].add(usage(tenant, days_ago(10)).id)

    old = retention.FINISHED_JOB_DAYS + 10
    first = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    job(first, JobStatus.SUCCEEDED, days_ago(old + 3), days_ago(old + 3))
    job(first, JobStatus.FAILED, days_ago(old + 2), days_ago(old + 2))
    latest = job(first, JobStatus.SUCCEEDED, days_ago(old + 1), days_ago(old + 1))
    second = factory.document(
        tenant.workspace, uploaded_by=tenant.editor, status=DocumentStatus.QUEUED
    )
    job(second, JobStatus.CANCELLED, days_ago(old), None)  # no completed_at: updated_at counts
    active = job(second, JobStatus.QUEUED, days_ago(0), None)
    third = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    recent = job(third, JobStatus.SUCCEEDED, days_ago(5), days_ago(5))
    newest = job(third, JobStatus.FAILED, days_ago(1), days_ago(1))
    keep["jobs"] = {latest.id, active.id, recent.id, newest.id}
    return keep


def test_organization_retention(factory, tenant, other_tenant):
    keep = populate(factory, tenant)
    theirs = populate(factory, other_tenant)

    assert retention.apply_organization_retention(tenant.org.id) == {
        "conversations": 1,
        "audit_events": 1,
        "usage_records": 1,
        "ingestion_jobs": 3,  # each document keeps its latest job; active jobs stay
    }
    org = tenant.org.id
    assert ids(Conversation, org) == keep["conversations"]
    assert len(ids(Message, org)) == 2  # messages went with their conversation
    assert ids(AuditEvent, org) == keep["audit"]
    assert ids(UsageRecord, org) == keep["usage"]
    assert ids(IngestionJob, org) == keep["jobs"]

    # The other organization is untouched until its own turn.
    other = other_tenant.org.id
    assert len(ids(Conversation, other)) == 3 and len(ids(IngestionJob, other)) == 7
    retention.apply_organization_retention(other)
    assert ids(Conversation, other) == theirs["conversations"]
    assert ids(IngestionJob, other) == theirs["jobs"]


def test_global_retention(factory, tenant):
    settings = get_settings()
    idle = timedelta(minutes=settings.session_idle_timeout_minutes)
    grace = retention.DEAD_SESSION_DAYS

    def session(expires: datetime, seen: datetime, revoked: datetime | None = None) -> UserSession:
        return UserSession(
            id=uuid.uuid4(),
            user_id=tenant.viewer.id,
            token_hash=uuid.uuid4().hex,
            expires_at=expires,
            last_seen_at=seen,
            revoked_at=revoked,
        )

    soon = utcnow() + timedelta(days=10)
    dead = [
        session(days_ago(grace + 1), days_ago(grace + 1)),  # expired long ago
        session(soon, days_ago(grace + 1), revoked=days_ago(grace + 1)),  # revoked long ago
        session(soon, utcnow() - idle - timedelta(days=grace, hours=1)),  # idle for good
    ]
    alive = [
        session(soon, utcnow()),
        session(days_ago(1), days_ago(1)),  # expired recently: kept for a while
    ]

    def invitation(expires: datetime) -> Invitation:
        return Invitation(
            id=uuid.uuid4(),
            organization_id=tenant.org.id,
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            role=OrgRole.EMPLOYEE,
            token_hash=uuid.uuid4().hex,
            expires_at=expires,
        )

    expired = invitation(days_ago(retention.EXPIRED_INVITATION_DAYS + 1))
    pending = [invitation(days_ago(1)), invitation(soon)]
    save(None, *dead, *alive, expired, *pending)

    assert retention.apply_global_retention() == {"user_sessions": 3, "invitations": 1}
    assert ids(UserSession) == {row.id for row in alive}
    assert ids(Invitation) == {row.id for row in pending}


def test_apply_retention_runs_everything(factory, tenant, other_tenant):
    populate(factory, tenant)
    populate(factory, other_tenant)
    totals = retention.apply_retention()
    assert totals == {
        "conversations": 2,
        "audit_events": 2,
        "usage_records": 2,
        "ingestion_jobs": 6,
        "user_sessions": 0,
        "invitations": 0,
    }
