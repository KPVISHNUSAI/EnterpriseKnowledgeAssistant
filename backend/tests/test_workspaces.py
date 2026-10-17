"""Workspace permission matrix, names, deletion, document counts and memberships."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import set_tenant
from app.models import AuditEvent, PermissionLevel, WorkspaceRole

WORKSPACES = "/api/v1/workspaces"


def _actions(db, org_id, prefix="workspace."):
    set_tenant(db, org_id)
    return [
        (e.action, e.event_metadata)
        for e in db.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == org_id, AuditEvent.action.startswith(prefix))
            .order_by(AuditEvent.created_at)
        )
    ]


def test_listing_follows_membership(login_as, factory, tenant):
    factory.workspace(tenant.org, "Legal", members={tenant.outsider: WorkspaceRole.OWNER})
    expected = {
        "admin": ["Acme Engineering", "Legal"],
        "manager": ["Acme Engineering"],
        "editor": ["Acme Engineering"],
        "viewer": ["Acme Engineering"],
        "outsider": ["Legal"],
    }
    for who, names in expected.items():
        listing = login_as(getattr(tenant, who), tenant.org).get(WORKSPACES).json()
        assert [w["name"] for w in listing] == names, who


@pytest.mark.parametrize(
    ("who", "my_role", "can_manage", "can_upload"),
    [
        ("admin", None, True, True),
        ("manager", "owner", True, True),
        ("editor", "editor", False, True),
        ("viewer", "viewer", False, False),
    ],
)
def test_workspace_flags_follow_roles(login_as, tenant, who, my_role, can_manage, can_upload):
    body = (
        login_as(getattr(tenant, who), tenant.org).get(f"{WORKSPACES}/{tenant.workspace.id}").json()
    )
    assert (body["my_role"], body["can_manage"], body["can_upload"]) == (
        my_role,
        can_manage,
        can_upload,
    )
    assert body["member_count"] == 3


@pytest.mark.parametrize(
    ("who", "read", "manage"),
    [
        ("admin", 200, 200),
        ("manager", 200, 200),
        ("editor", 200, 403),
        ("viewer", 200, 403),
        ("outsider", 404, 404),
    ],
)
def test_workspace_permission_matrix(login_as, tenant, who, read, manage):
    session = login_as(getattr(tenant, who), tenant.org)
    url = f"{WORKSPACES}/{tenant.workspace.id}"
    assert session.get(url).status_code == read
    assert session.get(f"{url}/members").status_code == read
    assert session.patch(url, json={"description": f"by {who}"}).status_code == manage
    added = session.post(f"{url}/members", json={"user_id": str(tenant.outsider.id)})
    assert added.status_code == (201 if manage == 200 else manage)
    member_url = f"{url}/members/{tenant.viewer.id}"
    assert session.patch(member_url, json={"role": "editor"}).status_code == manage
    assert session.delete(member_url).status_code == (204 if manage == 200 else manage)
    assert session.delete(url).status_code == (204 if manage == 200 else manage)


@pytest.mark.parametrize(
    ("who", "status"),
    [("admin", 201), ("manager", 201), ("editor", 403), ("viewer", 403), ("outsider", 403)],
)
def test_only_admins_and_knowledge_managers_create_workspaces(login_as, tenant, db, who, status):
    user = getattr(tenant, who)
    response = login_as(user, tenant.org).post(
        WORKSPACES, json={"name": "  Research  ", "description": "R&D"}
    )
    assert response.status_code == status
    if status == 201:
        body = response.json()
        assert body["name"] == "Research"
        assert (body["my_role"], body["can_manage"], body["can_upload"]) == ("owner", True, True)
        assert (body["member_count"], body["document_count"]) == (1, 0)
        assert _actions(db, tenant.org.id) == [("workspace.created", {"name": "Research"})]


def test_workspace_names_are_unique_per_organization_ignoring_case(
    login_as, tenant, other_tenant, db
):
    session = login_as(tenant.manager, tenant.org)
    response = session.post(WORKSPACES, json={"name": "acme ENGINEERING"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workspace_name_taken"

    sales = session.post(WORKSPACES, json={"name": "Sales"}).json()
    url = f"{WORKSPACES}/{sales['id']}"
    response = session.patch(url, json={"name": "Acme Engineering "})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workspace_name_taken"
    response = session.patch(url, json={"name": "SALES", "description": "Pipeline"})
    assert response.status_code == 200
    assert (response.json()["name"], response.json()["description"]) == ("SALES", "Pipeline")
    # Explicit null clears the description; an omitted field is left alone.
    response = session.patch(url, json={"description": None})
    assert (response.json()["name"], response.json()["description"]) == ("SALES", None)

    other = login_as(other_tenant.manager, other_tenant.org)
    assert other.post(WORKSPACES, json={"name": "Sales"}).status_code == 201
    assert [m for a, m in _actions(db, tenant.org.id) if a == "workspace.updated"] == [
        {"fields": ["name", "description"]},
        {"fields": ["description"]},
    ]


def test_workspace_with_documents_cannot_be_deleted(login_as, factory, tenant, db):
    factory.document(tenant.workspace, uploaded_by=tenant.editor, restricted=True)
    admin = login_as(tenant.admin, tenant.org)
    response = admin.delete(f"{WORKSPACES}/{tenant.workspace.id}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workspace_not_empty"

    empty = factory.workspace(tenant.org, "Empty", members={tenant.manager: WorkspaceRole.OWNER})
    owner = login_as(tenant.manager, tenant.org)
    assert owner.delete(f"{WORKSPACES}/{empty.id}").status_code == 204
    assert owner.get(f"{WORKSPACES}/{empty.id}").status_code == 404
    assert _actions(db, tenant.org.id) == [("workspace.deleted", {"name": "Empty"})]


def test_document_count_only_counts_readable_documents(login_as, factory, tenant):
    factory.document(tenant.workspace, uploaded_by=tenant.editor, title="shared")
    factory.document(tenant.workspace, uploaded_by=tenant.editor, restricted=True)
    factory.document(
        tenant.workspace,
        uploaded_by=tenant.manager,
        restricted=True,
        grants={tenant.viewer: PermissionLevel.READ},
    )
    factory.document(tenant.workspace, uploaded_by=tenant.manager, restricted=True)
    expected = {"admin": 4, "manager": 4, "editor": 2, "viewer": 2}
    for who, count in expected.items():
        session = login_as(getattr(tenant, who), tenant.org)
        assert session.get(f"{WORKSPACES}/{tenant.workspace.id}").json()["document_count"] == count
        assert session.get(WORKSPACES).json()[0]["document_count"] == count


def test_other_tenants_workspaces_and_users_are_not_found(login_as, tenant, other_tenant):
    admin = login_as(tenant.admin, tenant.org)
    foreign = f"{WORKSPACES}/{other_tenant.workspace.id}"
    member = f"{foreign}/members/{other_tenant.viewer.id}"
    requests = [
        ("get", foreign, None),
        ("patch", foreign, {"name": "Mine now"}),
        ("delete", foreign, None),
        ("get", f"{foreign}/members", None),
        ("post", f"{foreign}/members", {"user_id": str(tenant.viewer.id)}),
        ("patch", member, {"role": "owner"}),
        ("delete", member, None),
    ]
    for method, url, body in requests:
        response = admin.request(method, url, json=body)
        assert response.status_code == 404, (method, url)
        assert response.json()["error"]["code"] == "workspace_not_found"

    response = admin.post(
        f"{WORKSPACES}/{tenant.workspace.id}/members",
        json={"user_id": str(other_tenant.viewer.id)},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"


def test_workspace_membership_management(login_as, factory, tenant, db):
    owner = login_as(tenant.manager, tenant.org)
    url = f"{WORKSPACES}/{tenant.workspace.id}/members"
    listing = owner.get(url).json()
    assert [(m["full_name"], m["role"]) for m in listing] == [
        ("Acme Editor", "editor"),
        ("Acme Manager", "owner"),
        ("Acme Viewer", "viewer"),
    ]

    added = owner.post(url, json={"user_id": str(tenant.outsider.id), "role": "editor"})
    assert added.status_code == 201
    assert (added.json()["email"], added.json()["role"]) == (tenant.outsider.email, "editor")
    outsider = login_as(tenant.outsider, tenant.org)
    assert outsider.get(f"{WORKSPACES}/{tenant.workspace.id}").json()["my_role"] == "editor"

    again = owner.post(url, json={"user_id": str(tenant.outsider.id)})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_member"
    stranger = factory.user()  # not in the organization
    response = owner.post(url, json={"user_id": str(stranger.id)})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"

    member = f"{url}/{tenant.outsider.id}"
    assert owner.patch(member, json={"role": "viewer"}).json()["role"] == "viewer"
    assert owner.delete(member).status_code == 204
    response = owner.delete(member)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "member_not_found"
    assert outsider.get(f"{WORKSPACES}/{tenant.workspace.id}").status_code == 404

    outsider_id = str(tenant.outsider.id)
    assert _actions(db, tenant.org.id) == [
        ("workspace.member_added", {"user_id": outsider_id, "role": "editor"}),
        (
            "workspace.member_role_changed",
            {"user_id": outsider_id, "from": "editor", "to": "viewer"},
        ),
        ("workspace.member_removed", {"user_id": outsider_id, "role": "viewer"}),
    ]


def test_workspace_always_keeps_an_owner(login_as, tenant):
    owner = login_as(tenant.manager, tenant.org)
    url = f"{WORKSPACES}/{tenant.workspace.id}/members"
    for response in (
        owner.patch(f"{url}/{tenant.manager.id}", json={"role": "editor"}),
        owner.delete(f"{url}/{tenant.manager.id}"),
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "last_owner"

    assert owner.patch(f"{url}/{tenant.viewer.id}", json={"role": "owner"}).status_code == 200
    assert owner.patch(f"{url}/{tenant.manager.id}", json={"role": "viewer"}).status_code == 200
    workspace = f"{WORKSPACES}/{tenant.workspace.id}"
    assert owner.patch(workspace, json={"description": "x"}).status_code == 403
    # Org admins are held to the same rule.
    admin = login_as(tenant.admin, tenant.org)
    assert admin.delete(f"{url}/{tenant.viewer.id}").status_code == 409
