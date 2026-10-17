"""The active organization, its member directory, role changes, removal and invitations."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.security import hash_token
from app.db.base import utcnow
from app.db.session import set_tenant
from app.models import (
    AuditEvent,
    Conversation,
    DocumentPermission,
    Invitation,
    Message,
    MessageRole,
    OrgRole,
    PermissionLevel,
    WorkspaceMembership,
)

MEMBERS = "/api/v1/organization/members"
INVITATIONS = "/api/v1/organization/invitations"


def _events(db, org_id, action):
    set_tenant(db, org_id)
    return list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == org_id, AuditEvent.action == action)
            .order_by(AuditEvent.created_at)
        )
    )


def _count(db, model, *conditions):
    return db.scalar(select(func.count()).select_from(model).where(*conditions))


def test_get_organization_counts_members(login_as, tenant):
    body = login_as(tenant.viewer, tenant.org).get("/api/v1/organization").json()
    assert body["id"] == str(tenant.org.id)
    assert body["name"] == "Acme Corp"
    assert body["member_count"] == 5


def test_only_admins_rename_the_organization(login_as, tenant, db):
    viewer = login_as(tenant.viewer, tenant.org)
    assert viewer.patch("/api/v1/organization", json={"name": "Hijacked"}).status_code == 403

    admin = login_as(tenant.admin, tenant.org)
    response = admin.patch("/api/v1/organization", json={"name": "Acme Holdings"})
    assert response.status_code == 200
    assert response.json()["name"] == "Acme Holdings"
    assert viewer.get("/api/v1/organization").json()["name"] == "Acme Holdings"
    events = _events(db, tenant.org.id, "organization.updated")
    assert [(e.actor_user_id, e.event_metadata) for e in events] == [
        (tenant.admin.id, {"fields": ["name"]})
    ]


def test_any_member_lists_the_directory_with_search_and_pages(
    login_as, factory, tenant, other_tenant
):
    session = login_as(tenant.viewer, tenant.org)

    def names(**params):
        response = session.get(MEMBERS, params=params)
        assert response.status_code == 200
        return [m["full_name"] for m in response.json()["items"]]

    first = session.get(MEMBERS, params={"page_size": 2}).json()
    assert first["total"] == 5
    assert (first["page"], first["page_size"]) == (1, 2)
    assert [m["full_name"] for m in first["items"]] == ["Acme Admin", "Acme Editor"]
    assert names(page_size=2, page=3) == ["Acme Viewer"]
    assert first["items"][0]["role"] == "org_admin"
    assert first["items"][0]["email"] == "admin@acme.example.com"

    # Case-insensitive search over name and email; other tenants never appear.
    assert names(q="MANAGER") == ["Acme Manager"]
    assert names(q="outsider@acme") == ["Acme Outsider"]
    assert names(q="globex") == []
    # LIKE wildcards in the search are literal characters.
    factory.member(tenant.org, factory.user("percent@acme.example.com", full_name="100% Done"))
    assert names(q="%") == ["100% Done"]
    assert names(q="_") == []


def test_admin_changes_roles_but_never_drops_the_last_admin(login_as, tenant, db):
    admin = login_as(tenant.admin, tenant.org)
    response = admin.patch(f"{MEMBERS}/{tenant.admin.id}", json={"role": "employee"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_admin"

    response = admin.patch(f"{MEMBERS}/{tenant.viewer.id}", json={"role": "org_admin"})
    assert response.status_code == 200
    assert response.json()["role"] == "org_admin"
    assert response.json()["user_id"] == str(tenant.viewer.id)
    # With a second admin, the first may step down; it applies from the next request on.
    response = admin.patch(f"{MEMBERS}/{tenant.admin.id}", json={"role": "employee"})
    assert response.status_code == 200
    assert admin.get(INVITATIONS).status_code == 403

    events = _events(db, tenant.org.id, "organization.member_role_changed")
    assert [(e.resource_id, e.event_metadata) for e in events] == [
        (str(tenant.viewer.id), {"from": "employee", "to": "org_admin"}),
        (str(tenant.admin.id), {"from": "org_admin", "to": "employee"}),
    ]


def test_non_admins_cannot_manage_members(login_as, tenant):
    manager = login_as(tenant.manager, tenant.org)
    url = f"{MEMBERS}/{tenant.viewer.id}"
    assert manager.patch(url, json={"role": "org_admin"}).status_code == 403
    assert manager.delete(url).status_code == 403


def test_members_of_other_organizations_are_not_found(login_as, tenant, other_tenant):
    admin = login_as(tenant.admin, tenant.org)
    for user_id in (other_tenant.viewer.id, uuid.uuid4()):
        response = admin.patch(f"{MEMBERS}/{user_id}", json={"role": "employee"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "member_not_found"
        assert admin.delete(f"{MEMBERS}/{user_id}").status_code == 404


def test_last_admin_cannot_be_removed(login_as, tenant):
    admin = login_as(tenant.admin, tenant.org)
    response = admin.delete(f"{MEMBERS}/{tenant.admin.id}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_admin"


def test_removing_a_member_revokes_access_in_that_organization_only(
    login_as, factory, tenant, other_tenant, db
):
    user = tenant.editor
    factory.member(other_tenant.org, user)
    factory.document(
        tenant.workspace,
        uploaded_by=tenant.manager,
        restricted=True,
        grants={user: PermissionLevel.READ},
    )
    set_tenant(db, tenant.org.id)
    conversation = Conversation(organization_id=tenant.org.id, user_id=user.id, title="Q3")
    db.add(conversation)
    db.flush()
    db.add(
        Message(
            conversation_id=conversation.id,
            organization_id=tenant.org.id,
            role=MessageRole.USER,
            content="hello",
        )
    )
    db.commit()
    in_acme, in_globex = login_as(user, tenant.org), login_as(user, other_tenant.org)

    response = login_as(tenant.admin, tenant.org).delete(f"{MEMBERS}/{user.id}")
    assert response.status_code == 204
    assert in_acme.get("/api/v1/auth/me").status_code == 401
    assert in_globex.get("/api/v1/auth/me").status_code == 200

    set_tenant(db, tenant.org.id)
    assert _count(db, WorkspaceMembership, WorkspaceMembership.user_id == user.id) == 0
    assert _count(db, Conversation, Conversation.user_id == user.id) == 0
    assert _count(db, Message, Message.organization_id == tenant.org.id) == 0
    assert _count(db, DocumentPermission, DocumentPermission.principal_id == user.id) == 0
    members = login_as(tenant.viewer, tenant.org).get(MEMBERS).json()
    assert user.email not in [m["email"] for m in members["items"]]
    events = _events(db, tenant.org.id, "organization.member_removed")
    assert [(e.resource_id, e.event_metadata) for e in events] == [
        (str(user.id), {"role": "employee"})
    ]


def test_invitations_are_created_listed_and_revoked_by_admins(client, login_as, tenant, db):
    admin = login_as(tenant.admin, tenant.org)
    first = admin.post(INVITATIONS, json={"email": "Newcomer@Example.com"})
    assert first.status_code == 201, first.text
    first = first.json()
    assert first["email"] == "newcomer@example.com"
    assert first["role"] == "employee"
    assert first["invited_by_email"] == tenant.admin.email
    assert first["invite_url"] == f"http://localhost:3000/invite/{first['token']}"
    second = admin.post(
        INVITATIONS, json={"email": "newcomer@example.com", "role": "knowledge_manager"}
    ).json()

    listing = admin.get(INVITATIONS).json()
    assert listing["total"] == 2
    assert [i["id"] for i in listing["items"]] == [second["id"], first["id"]]  # newest first
    newest, superseded = listing["items"]
    assert newest["revoked_at"] is None
    assert superseded["revoked_at"] is not None  # a new invitation replaces the old link
    assert "token" not in newest
    assert admin.get(INVITATIONS, params={"page_size": 1, "page": 2}).json()["items"] == [
        superseded
    ]
    assert client.get(f"/api/v1/auth/invitations/{first['token']}").status_code == 404

    # Only the token's hash is stored, and the audit trail never sees the token.
    row = db.get(Invitation, uuid.UUID(second["id"]))
    assert row.token_hash == hash_token(second["token"])
    created = _events(db, tenant.org.id, "organization.invitation_created")
    assert [e.event_metadata for e in created] == [
        {"email": "newcomer@example.com", "role": "employee"},
        {"email": "newcomer@example.com", "role": "knowledge_manager"},
    ]
    assert all(second["token"] not in str(e.event_metadata) for e in created)

    assert admin.delete(f"{INVITATIONS}/{second['id']}").status_code == 204
    assert admin.delete(f"{INVITATIONS}/{second['id']}").status_code == 404  # not pending
    assert client.get(f"/api/v1/auth/invitations/{second['token']}").status_code == 404
    assert len(_events(db, tenant.org.id, "organization.invitation_revoked")) == 1


def test_only_admins_manage_invitations(login_as, tenant):
    manager = login_as(tenant.manager, tenant.org)
    assert manager.get(INVITATIONS).status_code == 403
    assert manager.post(INVITATIONS, json={"email": "x@example.com"}).status_code == 403
    invitation = login_as(tenant.admin, tenant.org).post(
        INVITATIONS, json={"email": "x@example.com"}
    )
    assert manager.delete(f"{INVITATIONS}/{invitation.json()['id']}").status_code == 403


def test_members_cannot_be_invited_again(login_as, tenant):
    response = login_as(tenant.admin, tenant.org).post(
        INVITATIONS, json={"email": tenant.viewer.email.upper()}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_member"


@pytest.mark.parametrize("state", ["other_tenant", "expired", "accepted"])
def test_only_pending_invitations_of_the_organization_can_be_revoked(
    login_as, tenant, other_tenant, db, state
):
    org = other_tenant.org if state == "other_tenant" else tenant.org
    invitation = Invitation(
        organization_id=org.id,
        email="someone@example.com",
        role=OrgRole.EMPLOYEE,
        token_hash=hash_token(uuid.uuid4().hex),
        expires_at=utcnow() + timedelta(days=-1 if state == "expired" else 1),
        accepted_at=utcnow() if state == "accepted" else None,
    )
    db.add(invitation)
    db.commit()
    response = login_as(tenant.admin, tenant.org).delete(f"{INVITATIONS}/{invitation.id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invitation_not_found"
