"""Admin audit log listing and the usage report."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.db.base import utcnow
from app.db.session import set_tenant
from app.models import (
    AuditEvent,
    Conversation,
    Document,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    Message,
    MessageRole,
    OrgRole,
    UsageKind,
    UsageRecord,
)

AUDIT = "/api/v1/admin/audit-events"
USAGE = "/api/v1/admin/usage"


def _add(db, org, *rows):
    set_tenant(db, org.id)
    db.add_all(rows)
    db.commit()


def _event(org, action, *, actor=None, resource=None, at=None):
    resource_type, resource_id = resource or (None, None)
    return AuditEvent(
        organization_id=org.id,
        actor_user_id=actor.id if actor else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata={"n": 1},
        created_at=at or utcnow(),
    )


def _usage(org, *, at, kind=UsageKind.CHAT_COMPLETION, tokens=(0, 0), cost="0", timings=None):
    return UsageRecord(
        organization_id=org.id,
        kind=kind,
        provider="extractive",
        model="test-model",
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        cost_usd=Decimal(cost),
        timings=timings or {},
        created_at=at,
    )


def _job(doc, status, *, completed_at=None):
    return IngestionJob(
        document_id=doc.id,
        organization_id=doc.organization_id,
        document_version=1,
        status=status,
        max_attempts=3,
        completed_at=completed_at,
    )


def test_audit_events_newest_first_with_filters_and_pages(login_as, db, tenant, other_tenant):
    t0 = utcnow().replace(microsecond=0) - timedelta(hours=3)
    viewer = ("user", str(tenant.viewer.id))
    workspace = ("workspace", str(tenant.workspace.id))
    _add(
        db,
        tenant.org,
        _event(tenant.org, "auth.login", actor=tenant.viewer, resource=viewer, at=t0),
        _event(
            tenant.org,
            "auth.logout",
            actor=tenant.viewer,
            resource=viewer,
            at=t0 + timedelta(hours=1),
        ),
        _event(
            tenant.org,
            "workspace.created",
            actor=tenant.manager,
            resource=workspace,
            at=t0 + timedelta(hours=2),
        ),
    )
    _add(db, other_tenant.org, _event(other_tenant.org, "auth.login", actor=other_tenant.viewer))
    admin = login_as(tenant.admin, tenant.org)

    def actions(**params):
        response = admin.get(AUDIT, params=params)
        assert response.status_code == 200, response.text
        return [e["action"] for e in response.json()["items"]]

    assert actions() == ["workspace.created", "auth.logout", "auth.login"]
    assert actions(action="auth.login") == ["auth.login"]
    assert actions(action="auth.*") == ["auth.logout", "auth.login"]
    assert actions(action="a%*") == []  # wildcards in the prefix are literal
    assert actions(action="auth") == []  # without '*' the match is exact
    assert actions(actor_user_id=str(tenant.manager.id)) == ["workspace.created"]
    assert actions(resource_type="workspace", resource_id=str(tenant.workspace.id)) == [
        "workspace.created"
    ]
    one_hour = (t0 + timedelta(hours=1)).isoformat()
    assert actions(since=one_hour, until=one_hour) == ["auth.logout"]  # both inclusive
    naive = (t0 + timedelta(hours=1)).replace(tzinfo=None).isoformat()  # taken as UTC
    assert actions(since=naive) == ["workspace.created", "auth.logout"]

    page = admin.get(AUDIT, params={"page": 2, "page_size": 2}).json()
    assert (page["total"], page["page"], page["page_size"]) == (3, 2, 2)
    [event] = page["items"]
    assert event["action"] == "auth.login"
    assert event["actor_user_id"] == str(tenant.viewer.id)
    assert event["actor_email"] == tenant.viewer.email
    assert event["metadata"] == {"n": 1}
    assert (event["resource_type"], event["resource_id"]) == viewer


def test_audit_events_from_real_actions_carry_request_context(login_as, tenant):
    manager = login_as(tenant.manager, tenant.org)
    created = manager.post(
        "/api/v1/workspaces", json={"name": "Audited"}, headers={"X-Request-ID": "req-audit-0001"}
    )
    assert created.status_code == 201
    [event] = (
        login_as(tenant.admin, tenant.org)
        .get(AUDIT, params={"action": "workspace.created"})
        .json()["items"]
    )
    assert event["actor_email"] == tenant.manager.email
    assert event["resource_id"] == created.json()["id"]
    assert event["request_id"] == "req-audit-0001"
    assert event["ip_address"]


def test_admin_endpoints_are_for_org_admins_only(login_as, tenant):
    manager = login_as(tenant.manager, tenant.org)
    assert manager.get(AUDIT).status_code == 403
    assert manager.get(USAGE).status_code == 403


def test_usage_report_aggregates_the_window(login_as, factory, db, tenant, other_tenant):
    now = utcnow()
    indexed = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    failed = factory.document(
        tenant.workspace, uploaded_by=tenant.editor, status=DocumentStatus.FAILED
    )
    queued = factory.document(
        tenant.workspace, uploaded_by=tenant.editor, status=DocumentStatus.QUEUED
    )
    factory.chunks(indexed, ["alpha", "beta"])
    set_tenant(db, tenant.org.id)
    for doc, size in ((indexed, 1000), (failed, 250), (queued, 5)):
        db.execute(update(Document).where(Document.id == doc.id).values(file_size=size))
    conversation = Conversation(organization_id=tenant.org.id, user_id=tenant.viewer.id, title="Q")
    db.add(conversation)
    db.flush()
    for role in (MessageRole.USER, MessageRole.ASSISTANT):
        db.add(
            Message(
                conversation_id=conversation.id,
                organization_id=tenant.org.id,
                role=role,
                content="x",
            )
        )
    db.commit()

    org = tenant.org
    _add(
        db,
        org,
        _usage(
            org,
            at=now,
            tokens=(100, 50),
            cost="0.001",
            timings={"retrieval_ms": 10, "generation_ms": 100.0},
        ),
        _usage(
            org,
            at=now,
            tokens=(200, 80),
            cost="0.002",
            timings={"retrieval_ms": 30.5, "generation_ms": 300},
        ),
        _usage(org, at=now, kind=UsageKind.QUERY_REWRITE, tokens=(20, 5), cost="0.0001"),
        _usage(
            org,
            at=now - timedelta(days=2),
            tokens=(7, 3),
            cost="0.0005",
            timings={"retrieval_ms": 50, "generation_ms": 500},
        ),
        _usage(org, at=now - timedelta(days=40), tokens=(999, 999), cost="9"),  # outside the window
        _job(queued, JobStatus.QUEUED),
        _job(failed, JobStatus.RUNNING),
        _job(failed, JobStatus.FAILED, completed_at=now - timedelta(hours=1)),
        _job(indexed, JobStatus.SUCCEEDED, completed_at=now - timedelta(hours=2)),
        _job(indexed, JobStatus.SUCCEEDED, completed_at=now - timedelta(days=3)),  # too old
    )
    other = other_tenant.org
    _add(db, other, _usage(other, at=now, tokens=(5000, 5000), cost="5"))  # another tenant
    factory.document(other_tenant.workspace, uploaded_by=other_tenant.editor)

    report = login_as(tenant.admin, tenant.org).get(USAGE, params={"days": 7}).json()
    assert report["period_days"] == 7
    assert report["totals"] == {
        "documents": 3,
        "documents_by_status": {"queued": 1, "processing": 0, "indexed": 1, "failed": 1},
        "chunks": 2,
        "storage_bytes": 1255,
        "conversations": 1,
        "messages": 2,
        "chat_requests": 3,
        "input_tokens": 327,
        "output_tokens": 138,
        "estimated_cost_usd": pytest.approx(0.0036),
    }

    daily = report["daily"]
    today, two_days_ago = now.date(), (now - timedelta(days=2)).date()
    assert [d["date"] for d in daily] == [
        (today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)
    ]
    by_date = {d["date"]: d for d in daily}
    assert by_date[today.isoformat()] == {
        "date": today.isoformat(),
        "chat_requests": 2,
        "input_tokens": 320,
        "output_tokens": 135,
        "estimated_cost_usd": pytest.approx(0.0031),
    }
    assert by_date[two_days_ago.isoformat()]["chat_requests"] == 1
    quiet = [d for d in daily if d["date"] not in (today.isoformat(), two_days_ago.isoformat())]
    assert len(quiet) == 5
    assert all(
        d["chat_requests"] == d["input_tokens"] == d["estimated_cost_usd"] == 0 for d in quiet
    )

    assert report["latency"] == {
        "samples": 3,
        "retrieval_p50_ms": pytest.approx(30.5),
        "retrieval_p95_ms": pytest.approx(30.5 + 0.9 * (50 - 30.5)),
        "generation_p50_ms": pytest.approx(300),
        "generation_p95_ms": pytest.approx(480),
    }
    assert report["ingestion"] == {
        "queued": 1,
        "running": 1,
        "failed_last_24h": 1,
        "succeeded_last_24h": 1,
    }


def test_usage_report_for_an_empty_organization(login_as, factory):
    org = factory.org("Empty Inc")
    admin = factory.user()
    factory.member(org, admin, OrgRole.ORG_ADMIN)
    report = login_as(admin, org).get(USAGE, params={"days": 3}).json()
    today = utcnow().date()
    assert report == {
        "period_days": 3,
        "totals": {
            "documents": 0,
            "documents_by_status": {"queued": 0, "processing": 0, "indexed": 0, "failed": 0},
            "chunks": 0,
            "storage_bytes": 0,
            "conversations": 0,
            "messages": 0,
            "chat_requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        },
        "daily": [
            {
                "date": (today - timedelta(days=offset)).isoformat(),
                "chat_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.0,
            }
            for offset in (2, 1, 0)
        ],
        "latency": {
            "samples": 0,
            "retrieval_p50_ms": None,
            "retrieval_p95_ms": None,
            "generation_p50_ms": None,
            "generation_p95_ms": None,
        },
        "ingestion": {"queued": 0, "running": 0, "failed_last_24h": 0, "succeeded_last_24h": 0},
    }
