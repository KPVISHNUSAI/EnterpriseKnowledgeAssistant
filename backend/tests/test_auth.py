"""Registration, sign-in, sessions, invitations, password changes and organization switching."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import rate_limit
from app.core.config import get_settings
from app.core.redis import get_redis
from app.core.security import hash_token, password_needs_rehash
from app.db.base import utcnow
from app.db.session import set_tenant
from app.models import AuditEvent, Invitation, OrgRole, User, UserSession, WorkspaceRole
from app.services import auth as auth_service

PASSWORD = "Correct-Horse-Battery-9"  # the factory's default password
NEW_PASSWORD = "Another-Long-Passphrase-7"
STRONG_PASSWORD = "Sturdy-Passphrase-42"  # 20 characters
WRONG_PASSWORD = "Wrong-Pass-123"
REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"


def _register(client, email="alice@example.com", password=STRONG_PASSWORD, **fields):
    body = {
        "email": email,
        "password": password,
        "full_name": "Alice Example",
        "organization_name": "Alice Co",
        **fields,
    }
    return client.post(REGISTER, json=body)


def _events(db, org_id, action=None):
    set_tenant(db, org_id)
    stmt = select(AuditEvent).where(AuditEvent.organization_id == org_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    return list(db.scalars(stmt.order_by(AuditEvent.created_at)))


def _invitation(db, org, email, *, role=OrgRole.EMPLOYEE, expires_in=timedelta(days=1), **fields):
    token = uuid.uuid4().hex
    db.add(
        Invitation(
            organization_id=org.id,
            email=email,
            role=role,
            token_hash=hash_token(token),
            expires_at=utcnow() + expires_in,
            **fields,
        )
    )
    db.commit()
    return token


def _error(response):
    return {k: v for k, v in response.json()["error"].items() if k != "request_id"}


def test_register_me_logout_login_round_trip(client, db):
    response = _register(client, email="Alice@Example.com")
    assert response.status_code == 201, response.text
    me = response.json()
    assert me["user"]["email"] == "alice@example.com"
    assert me["organization"]["name"] == "Alice Co"
    assert me["organization"]["role"] == "org_admin"
    assert me["organizations"] == [me["organization"]]
    assert [(w["name"], w["role"]) for w in me["workspaces"]] == [("General", "owner")]
    assert all(me["capabilities"].values())

    cookies = {c.split("=", 1)[0]: c.lower() for c in response.headers.get_list("set-cookie")}
    session_cookie, csrf_cookie = cookies["eka_session"], cookies["eka_csrf"]
    assert "httponly" in session_cookie
    assert "httponly" not in csrf_cookie  # the SPA reads it and echoes it in X-CSRF-Token
    for cookie in (session_cookie, csrf_cookie):
        assert "samesite=lax" in cookie
        assert "path=/" in cookie

    token = client.cookies.get("eka_session")
    # Only the token's hash is stored.
    assert db.scalar(select(UserSession.token_hash)) == hash_token(token)
    assert client.get(ME).json()["user"]["id"] == me["user"]["id"]

    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.cookies.get("eka_session") is None
    assert client.get(ME).status_code == 401
    client.cookies.set("eka_session", token, domain="testserver.local")
    assert client.get(ME).status_code == 401  # revoked server-side, not just forgotten
    client.cookies.clear()

    response = client.post(LOGIN, json={"email": "ALICE@example.com", "password": STRONG_PASSWORD})
    assert response.status_code == 200, response.text
    assert response.json()["organization"] == me["organization"]
    assert client.get(ME).status_code == 200

    org_id = uuid.UUID(me["organization"]["id"])
    # Sorted: rows flushed together can share a created_at microsecond.
    assert sorted(e.action for e in _events(db, org_id)) == [
        "auth.login",
        "auth.logout",
        "auth.register",
        "workspace.created",
    ]
    user = db.scalar(select(User).where(User.email == "alice@example.com"))
    assert user.last_login_at is not None


def test_wrong_password_and_unknown_email_look_identical(client, tenant, db, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    wrong = client.post(LOGIN, json={"email": tenant.viewer.email, "password": WRONG_PASSWORD})
    unknown = client.post(
        LOGIN, json={"email": "nobody@acme.example.com", "password": "Wrong-Pass-1"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert _error(wrong) == _error(unknown)
    assert _error(wrong)["code"] == "invalid_credentials"

    # The existing account is audited in its organization (reason only); the unknown
    # email is only logged, and neither email nor password reaches the logs.
    failures = _events(db, tenant.org.id, "auth.login_failed")
    assert [(e.actor_user_id, e.event_metadata) for e in failures] == [
        (tenant.viewer.id, {"reason": "invalid_password"})
    ]
    reasons = [r.reason for r in caplog.records if r.getMessage() == "auth.login_failed"]
    assert reasons == ["invalid_password", "unknown_email"]
    assert "nobody@acme.example.com" not in caplog.text
    assert "Wrong-Pass" not in caplog.text


def test_failed_login_is_audited_in_every_organization_of_the_account(
    client, factory, tenant, other_tenant, db
):
    factory.member(other_tenant.org, tenant.viewer)
    response = client.post(LOGIN, json={"email": tenant.viewer.email, "password": "Wrong-Pass-1"})
    assert response.status_code == 401
    for org in (tenant.org, other_tenant.org):
        assert len(_events(db, org.id, "auth.login_failed")) == 1


def test_failed_login_is_audited_only_after_the_response(app, tenant, monkeypatch):
    """SEC-02: the audit writes for an existing account happen after the 401 has been
    sent, so failures for real accounts are not slower than for unknown emails."""
    events: list[str] = []
    record = auth_service._record_login_failure

    def audited(*args):
        events.append("audit")
        record(*args)

    monkeypatch.setattr(auth_service, "_record_login_failure", audited)

    async def spy(scope, receive, send):
        async def spied_send(message):
            if message["type"] == "http.response.body" and not message.get("more_body"):
                events.append("response")
            await send(message)

        await app(scope, receive, spied_send)

    with TestClient(spy) as client:  # returns once the app is done, background tasks too
        for email, expected in (
            (tenant.viewer.email, ["response", "audit"]),
            ("nobody@acme.example.com", ["response"]),
        ):
            events.clear()
            response = client.post(LOGIN, json={"email": email, "password": WRONG_PASSWORD})
            assert response.status_code == 401
            assert events == expected


def test_inactive_user_fails_like_a_wrong_password(client, factory, tenant, db):
    user = factory.user("former@acme.example.com", is_active=False)
    factory.member(tenant.org, user)
    response = client.post(LOGIN, json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 401
    assert _error(response)["code"] == "invalid_credentials"
    failures = _events(db, tenant.org.id, "auth.login_failed")
    assert [e.event_metadata for e in failures] == [{"reason": "inactive"}]


def test_user_without_organization_cannot_sign_in(client, factory):
    user = factory.user("lonely@example.com")
    response = client.post(LOGIN, json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "no_organization"


def test_login_upgrades_outdated_password_hashes(client, tenant, db):
    outdated = PasswordHasher(time_cost=2, memory_cost=2048, parallelism=1).hash(PASSWORD)
    user = db.get(User, tenant.viewer.id)
    user.password_hash = outdated
    db.commit()
    assert client.post(LOGIN, json={"email": user.email, "password": PASSWORD}).status_code == 200
    db.refresh(user)
    assert user.password_hash != outdated
    assert not password_needs_rehash(user.password_hash)


@pytest.fixture
def rate_limited(monkeypatch):
    """Rate limiting on, in one fixed window so a test cannot straddle a minute boundary.
    Tests use fresh emails: Redis counters outlive the test."""
    try:
        get_redis().ping()
    except Exception:
        pytest.skip("redis not running")
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: 1_900_000_000.0))
    return settings


def _fresh_email(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


def test_login_is_rate_limited_per_email(client, factory, tenant, monkeypatch, rate_limited):
    monkeypatch.setattr(rate_limited, "rate_limit_login_per_minute", 3)
    # Every attempt from a different address, so only the per-email limit can trigger.
    monkeypatch.setattr(rate_limited, "trust_proxy_headers", True)
    email = _fresh_email("limited")
    factory.member(tenant.org, factory.user(email))

    def attempt(address_email, password=WRONG_PASSWORD):
        headers = {"X-Forwarded-For": uuid.uuid4().hex}
        body = {"email": address_email, "password": password}
        return client.post(LOGIN, json=body, headers=headers)

    assert [attempt(email).status_code for _ in range(3)] == [401, 401, 401]
    blocked = attempt(email, PASSWORD)  # even the right password
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert attempt(_fresh_email("other")).status_code == 401  # other accounts unaffected


def test_password_reentry_shares_the_per_account_limit(
    login_as, factory, tenant, monkeypatch, rate_limited
):
    monkeypatch.setattr(rate_limited, "rate_limit_login_per_minute", 2)
    user = factory.user(_fresh_email("stolen-session"))
    factory.member(tenant.org, user)
    session = login_as(user, tenant.org)
    body = {"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD}
    statuses = [session.post("/api/v1/auth/me/password", json=body).status_code for _ in range(3)]
    assert statuses == [403, 403, 429]


def test_public_registration_can_be_disabled(client, monkeypatch, tenant, db):
    monkeypatch.setattr(get_settings(), "allow_public_registration", False)
    response = _register(client)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "registration_disabled"

    token = _invitation(db, tenant.org, "invited@example.com")
    response = _register(client, email="invited@example.com", invite_token=token)
    assert response.status_code == 201, response.text


def test_organization_name_is_required_without_invitation(client):
    response = _register(client, organization_name=None)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "organization_name_required"


@pytest.mark.parametrize("password", ["Short-9", "my-alice-passphrase-1", "ALICE-in-wonderland-2"])
def test_weak_passwords_are_rejected(client, db, password):
    response = _register(client, password=password)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "weak_password"
    assert db.scalar(select(User)) is None


def test_password_length_follows_settings(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "password_min_length", 21)
    response = _register(client)  # STRONG_PASSWORD is 20 characters
    assert response.json()["error"]["code"] == "weak_password"
    monkeypatch.setattr(settings, "password_min_length", 8)
    assert _register(client, password="Tiny-pw9").status_code == 201


def test_duplicate_email_is_rejected(client, tenant):
    response = _register(client, email=tenant.viewer.email.upper())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


def test_invitation_flow_registers_with_the_invited_role(client, login_as, tenant, db):
    admin = login_as(tenant.admin, tenant.org)
    created = admin.post(
        "/api/v1/organization/invitations",
        json={"email": "New.Hire@Example.com", "role": "knowledge_manager"},
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]

    preview = client.get(f"/api/v1/auth/invitations/{token}")
    assert preview.status_code == 200
    assert preview.json()["organization_name"] == "Acme Corp"
    assert preview.json()["email"] == "new.hire@example.com"
    assert preview.json()["role"] == "knowledge_manager"

    response = _register(
        client, email="new.hire@example.com", invite_token=token, organization_name=None
    )
    assert response.status_code == 201, response.text
    me = response.json()
    assert me["organization"] == {
        "id": str(tenant.org.id),
        "name": "Acme Corp",
        "role": "knowledge_manager",
    }
    assert me["workspaces"] == []
    assert me["capabilities"]["can_create_workspace"] is True
    assert me["capabilities"]["can_manage_organization"] is False

    # Used up: no second preview or registration.
    assert client.get(f"/api/v1/auth/invitations/{token}").status_code == 404
    again = _register(client, email="new.hire@example.com", invite_token=token)
    assert again.status_code == 404

    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(token)))
    assert invitation.accepted_at is not None
    assert str(invitation.accepted_by) == me["user"]["id"]
    assert sorted(e.action for e in _events(db, tenant.org.id)) == [
        "auth.register",
        "organization.invitation_accepted",
        "organization.invitation_created",
    ]


@pytest.mark.parametrize("state", ["expired", "revoked", "accepted", "other_email", "unknown"])
def test_unusable_invitations_are_not_found(client, tenant, db, state):
    fields = {
        "expired": {"expires_in": timedelta(seconds=-1)},
        "revoked": {"revoked_at": utcnow()},
        "accepted": {"accepted_at": utcnow()},
    }.get(state, {})
    token = _invitation(db, tenant.org, "invitee@example.com", **fields)
    if state == "unknown":
        token = uuid.uuid4().hex
    email = "someone.else@example.com" if state == "other_email" else "invitee@example.com"

    response = _register(client, email=email, invite_token=token, organization_name=None)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invitation_not_found"
    assert db.scalar(select(User).where(User.email == email)) is None
    if state != "other_email":
        assert client.get(f"/api/v1/auth/invitations/{token}").status_code == 404


def test_existing_user_accepts_an_invitation_and_switches_to_it(login_as, tenant, other_tenant, db):
    user = other_tenant.viewer
    session = login_as(user, other_tenant.org)
    accept = "/api/v1/auth/invitations/accept"

    stranger_token = _invitation(db, tenant.org, "someone@example.com")
    assert session.post(accept, json={"token": stranger_token}).status_code == 404

    token = _invitation(db, tenant.org, user.email, role=OrgRole.EMPLOYEE)
    response = session.post(accept, json={"token": token})
    assert response.status_code == 200, response.text
    me = response.json()
    assert me["organization"]["id"] == str(tenant.org.id)
    assert me["organization"]["role"] == "employee"
    assert {o["id"] for o in me["organizations"]} == {str(tenant.org.id), str(other_tenant.org.id)}
    assert session.get("/api/v1/organization").json()["id"] == str(tenant.org.id)
    assert [
        e.actor_user_id for e in _events(db, tenant.org.id, "organization.invitation_accepted")
    ] == [user.id]

    second = _invitation(db, tenant.org, user.email)
    response = session.post(accept, json={"token": second})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_member"


def test_me_lists_workspaces_and_capabilities_by_role(login_as, factory, tenant):
    factory.workspace(tenant.org, "Legal", members={tenant.admin: WorkspaceRole.VIEWER})
    expected = {
        "admin": (True, True, True, True),
        "manager": (True, False, False, True),
        "editor": (False, False, False, True),
        "viewer": (False, False, False, False),
        "outsider": (False, False, False, False),
    }
    for who, flags in expected.items():
        me = login_as(getattr(tenant, who), tenant.org).get(ME).json()
        caps = me["capabilities"]
        assert (
            caps["can_create_workspace"],
            caps["can_manage_organization"],
            caps["can_view_audit_log"],
            caps["can_upload"],
        ) == flags, who
    me = login_as(tenant.admin, tenant.org).get(ME).json()
    # Org admins see every workspace; role is their own membership role, if any.
    assert [(w["name"], w["role"]) for w in me["workspaces"]] == [
        ("Acme Engineering", None),
        ("Legal", "viewer"),
    ]
    me = login_as(tenant.outsider, tenant.org).get(ME).json()
    assert me["workspaces"] == []


def test_update_profile(login_as, tenant):
    session = login_as(tenant.viewer, tenant.org)
    response = session.patch(ME, json={"full_name": "Vera Viewer"})
    assert response.status_code == 200
    assert response.json()["full_name"] == "Vera Viewer"
    assert session.get(ME).json()["user"]["full_name"] == "Vera Viewer"


def test_password_change_revokes_other_sessions_only(client, login_as, tenant, db):
    user = tenant.viewer
    current, other = login_as(user, tenant.org), login_as(user, tenant.org)
    url = "/api/v1/auth/me/password"

    wrong = current.post(
        url, json={"current_password": "Not-My-Pass-1", "new_password": NEW_PASSWORD}
    )
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "invalid_password"
    weak = current.post(url, json={"current_password": PASSWORD, "new_password": "viewer-pass-123"})
    assert weak.json()["error"]["code"] == "weak_password"
    assert other.get(ME).status_code == 200  # failed attempts change nothing

    ok = current.post(url, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert ok.status_code == 204
    assert current.get(ME).status_code == 200
    assert other.get(ME).status_code == 401

    assert client.post(LOGIN, json={"email": user.email, "password": PASSWORD}).status_code == 401
    assert (
        client.post(LOGIN, json={"email": user.email, "password": NEW_PASSWORD}).status_code == 200
    )
    assert len(_events(db, tenant.org.id, "auth.password_changed")) == 1


def test_switching_organization_changes_tenant_scope(login_as, factory, tenant, other_tenant, db):
    user = tenant.editor
    factory.member(other_tenant.org, user)
    research = factory.workspace(
        other_tenant.org, "Globex Research", members={user: WorkspaceRole.VIEWER}
    )
    session = login_as(user, tenant.org)
    assert [w["name"] for w in session.get("/api/v1/workspaces").json()] == ["Acme Engineering"]

    response = session.post(
        "/api/v1/auth/switch-organization", json={"organization_id": str(other_tenant.org.id)}
    )
    assert response.status_code == 200, response.text
    me = response.json()
    assert me["organization"] == {
        "id": str(other_tenant.org.id),
        "name": "Globex Corp",
        "role": "employee",
    }
    assert me["workspaces"] == [
        {"id": str(research.id), "name": "Globex Research", "role": "viewer"}
    ]
    assert me["capabilities"]["can_upload"] is False

    # The session stays switched, and organization A's data is out of scope.
    assert session.get(ME).json()["organization"]["id"] == str(other_tenant.org.id)
    assert [w["name"] for w in session.get("/api/v1/workspaces").json()] == ["Globex Research"]
    assert session.get(f"/api/v1/workspaces/{tenant.workspace.id}").status_code == 404
    assert session.get("/api/v1/organization").json()["name"] == "Globex Corp"

    switched = _events(db, other_tenant.org.id, "auth.organization_switched")
    assert [e.event_metadata for e in switched] == [{"from_organization_id": str(tenant.org.id)}]

    stranger = factory.org("Initech")
    response = session.post(
        "/api/v1/auth/switch-organization", json={"organization_id": str(stranger.id)}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "organization_not_found"


def test_logout_without_csrf_clears_cookies_but_keeps_the_session(login_as, tenant):
    session = login_as(tenant.viewer, tenant.org)
    token = session.cookies.get("eka_session")
    response = session.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "forged"})
    assert response.status_code == 204
    assert session.cookies.get("eka_session") is None
    # A forged cross-site request cannot sign the user out server-side.
    session.cookies.set("eka_session", token, domain="testserver.local")
    assert session.get(ME).status_code == 200


def test_logout_without_session_succeeds(client):
    assert client.post("/api/v1/auth/logout").status_code == 204
