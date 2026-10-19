"""Platform plumbing: health/readiness, OpenAPI contract, middleware, sessions, RLS, SSE."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app import main
from app.core.errors import RateLimitedError
from app.core.rate_limit import hit
from app.core.redis import get_redis
from app.core.sse import HEARTBEAT, encode_event, stream_from_thread
from app.db.session import set_tenant
from app.models import Workspace

SPEC_ENDPOINTS = [
    ("post", "/api/v1/auth/register"),
    ("post", "/api/v1/auth/login"),
    ("post", "/api/v1/auth/logout"),
    ("get", "/api/v1/auth/me"),
    ("post", "/api/v1/documents/upload"),
    ("get", "/api/v1/documents"),
    ("get", "/api/v1/documents/{document_id}"),
    ("delete", "/api/v1/documents/{document_id}"),
    ("post", "/api/v1/documents/{document_id}/reindex"),
    ("get", "/api/v1/documents/{document_id}/status"),
    ("post", "/api/v1/search"),
    ("post", "/api/v1/chat"),
    ("post", "/api/v1/chat/stream"),
    ("get", "/api/v1/conversations"),
    ("get", "/api/v1/conversations/{conversation_id}/messages"),
    ("delete", "/api/v1/conversations/{conversation_id}"),
    ("get", "/api/v1/admin/audit-events"),
    ("get", "/api/v1/admin/usage"),
    ("get", "/api/v1/health"),
    ("get", "/api/v1/ready"),
]


def test_health_has_request_id_and_security_headers(anon_client):
    response = anon_client.get("/api/v1/health", headers={"X-Request-ID": "req-12345678"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"] == "req-12345678"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_invalid_request_id_is_replaced(anon_client):
    response = anon_client.get("/api/v1/health", headers={"X-Request-ID": "bad id\n"})
    assert response.headers["x-request-id"] != "bad id\n"
    assert len(response.headers["x-request-id"]) == 32


def test_ready_checks_database_redis_and_storage(client):
    response = client.get("/api/v1/ready")
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"database", "redis", "storage"}


def test_openapi_documents_every_specified_endpoint(anon_client):
    spec = anon_client.get("/api/v1/openapi.json").json()
    for method, path in SPEC_ENDPOINTS:
        assert method in spec["paths"].get(path, {}), f"{method.upper()} {path} missing"
        operation = spec["paths"][path][method]
        assert operation.get("description") or operation.get("summary"), path


def test_metrics_endpoint(anon_client):
    anon_client.get("/api/v1/health")
    response = anon_client.get("/metrics")
    assert response.status_code == 200
    assert "eka_http_requests_total" in response.text


def test_model_warm_up_loads_the_models_and_only_logs_a_failure(monkeypatch, caplog):
    calls: list[str] = []

    class Embedder:
        def embed_query(self, text: str) -> None:
            calls.append("embed")

    class Reranker:
        def score(self, query: str, passages: list[str]) -> None:
            calls.append("rerank")

    monkeypatch.setattr(main, "get_embedder", Embedder)
    monkeypatch.setattr(main, "get_reranker", Reranker)
    main._warm_up_models()
    assert calls == ["embed", "rerank"]

    def offline() -> None:
        raise OSError("model download failed")

    monkeypatch.setattr(main, "get_embedder", offline)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        main._warm_up_models()  # the API keeps starting
    assert "models.warm_up_failed" in caplog.messages


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/auth/me"),
        ("get", "/api/v1/documents"),
        ("post", "/api/v1/search"),
        ("post", "/api/v1/chat"),
        ("get", "/api/v1/conversations"),
        ("get", "/api/v1/admin/audit-events"),
        ("get", "/api/v1/dashboard"),
    ],
)
def test_protected_endpoints_require_a_session(client, method, path):
    response = client.request(method, path, json={})
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["request_id"]


def test_unsafe_methods_require_csrf_token(client, factory, tenant):
    factory.authenticate(client, tenant.manager, tenant.org)
    response = client.post(
        "/api/v1/workspaces", json={"name": "New"}, headers={"X-CSRF-Token": "forged"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_cross_site_origin_is_rejected(anon_client):
    response = anon_client.post(
        "/api/v1/auth/login",
        json={"email": "a@example.com", "password": "x"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"


def test_oversized_body_is_rejected_before_parsing(anon_client):
    response = anon_client.post(
        "/api/v1/auth/login",
        content=b"x" * (1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_validation_errors_do_not_echo_input(anon_client):
    response = anon_client.post(
        "/api/v1/auth/login", json={"email": "not-an-email", "password": "hunter2-secret"}
    )
    assert response.status_code == 422
    assert "hunter2-secret" not in response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_expired_session_is_rejected(client, factory, tenant):
    factory.authenticate(client, tenant.viewer, tenant.org)
    factory.expire_sessions(tenant.viewer)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_non_admin_is_denied_admin_endpoints_and_it_is_audited(client, factory, tenant, db):
    factory.authenticate(client, tenant.viewer, tenant.org)
    response = client.get("/api/v1/admin/audit-events")
    assert response.status_code == 403
    set_tenant(db, tenant.org.id)
    actions = db.scalars(text("SELECT action FROM audit_events")).all()
    assert actions == ["security.access_denied"]


def test_session_for_removed_member_has_no_organization(client, factory):
    user = factory.user()
    org = factory.org()
    factory.member(org, user)
    factory.authenticate(client, user, org)
    with factory._maker() as session:
        session.execute(text("DELETE FROM organization_memberships"))
        session.commit()
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "no_organization"


# --- Row-level security ---------------------------------------------------------------------


def test_rls_hides_other_tenants_rows(db, tenant, other_tenant):
    set_tenant(db, tenant.org.id)
    names = set(db.scalars(select(Workspace.name)))
    assert names == {tenant.workspace.name}

    set_tenant(db, other_tenant.org.id)
    assert set(db.scalars(select(Workspace.name))) == {other_tenant.workspace.name}

    set_tenant(db, None)
    assert db.scalars(select(Workspace.name)).all() == []


def test_rls_rejects_writes_into_another_tenant(db, tenant, other_tenant):
    set_tenant(db, tenant.org.id)
    db.add(Workspace(organization_id=other_tenant.org.id, name="Smuggled"))
    with pytest.raises(DBAPIError, match="row-level security"):
        db.flush()
    db.rollback()


def test_rls_rejects_writes_without_tenant_context(db, tenant):
    set_tenant(db, None)
    db.add(Workspace(organization_id=tenant.org.id, name="No context"))
    with pytest.raises(DBAPIError, match="row-level security"):
        db.flush()
    db.rollback()


def test_tenant_context_is_transaction_scoped(db, tenant):
    set_tenant(db, tenant.org.id)
    assert db.scalar(text("SELECT current_setting('app.current_org_id', true)")) == str(
        tenant.org.id
    )
    db.commit()
    # A raw connection from the pool must not inherit the previous transaction's tenant.
    with db.get_bind().connect() as conn:
        assert conn.scalar(text("SELECT current_setting('app.current_org_id', true)")) in ("", None)


def test_app_role_cannot_bypass_rls_or_rewrite_audit_log(db, tenant):
    set_tenant(db, tenant.org.id)
    row = db.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).one()
    assert tuple(row) == (False, False)
    with pytest.raises(DBAPIError, match="permission denied"):
        db.execute(text("UPDATE audit_events SET action = 'x'"))
    db.rollback()


# --- Rate limiting --------------------------------------------------------------------------


def test_rate_limit_counts_per_identity_window():
    client = get_redis()
    identity = uuid.uuid4().hex
    try:
        client.ping()
    except Exception:
        pytest.skip("redis not running")
    for _ in range(3):
        hit(client, "test", identity, limit=3, window_seconds=60)
    with pytest.raises(RateLimitedError) as excinfo:
        hit(client, "test", identity, limit=3, window_seconds=60)
    assert int(excinfo.value.headers["Retry-After"]) >= 1
    hit(client, "test", uuid.uuid4().hex, limit=3, window_seconds=60)  # other identity ok


# --- SSE bridge -----------------------------------------------------------------------------


async def _collect(agen, limit: int | None = None) -> list[bytes]:
    out = []
    async for chunk in agen:
        out.append(chunk)
        if limit is not None and len(out) >= limit:
            break
    return out


def test_sse_bridge_streams_events_and_heartbeats():
    def produce(cancel: threading.Event) -> Iterator[bytes]:
        yield encode_event("token", {"text": "Hel"})
        time.sleep(0.15)  # quiet period -> heartbeat
        yield encode_event("done", {"ok": True})

    chunks = asyncio.run(_collect(stream_from_thread(produce, heartbeat_seconds=0.05)))
    assert chunks[0] == b'event: token\ndata: {"text": "Hel"}\n\n'
    assert HEARTBEAT in chunks
    assert chunks[-1].startswith(b"event: done")


def test_sse_bridge_cancels_producer_when_client_disconnects():
    cancelled = threading.Event()

    def produce(cancel: threading.Event) -> Iterator[bytes]:
        yield b"event: token\ndata: {}\n\n"
        while not cancel.wait(0.01):
            pass
        cancelled.set()

    async def scenario() -> None:
        agen = stream_from_thread(produce, heartbeat_seconds=0.05)
        await _collect(agen, limit=1)
        await agen.aclose()

    asyncio.run(scenario())
    assert cancelled.wait(2), "producer was not told to stop"


def test_sse_bridge_turns_producer_crash_into_error_event():
    def produce(cancel: threading.Event) -> Iterator[bytes]:
        yield b"event: token\ndata: {}\n\n"
        raise RuntimeError("boom")

    chunks = asyncio.run(_collect(stream_from_thread(produce)))
    assert chunks[-1].startswith(b"event: error")
    assert b"boom" not in chunks[-1]
