"""The document read/manage rules in app/services/access.py (used by every read path)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import ForbiddenError, NotFoundError
from app.db.session import set_tenant
from app.models import Document, OrgRole, PermissionLevel, WorkspaceRole
from app.services.access import (
    get_manageable_document,
    get_readable_document,
    manageable_documents_clause,
    readable_document_ids,
    readable_documents_clause,
)


def _readable(db, principal):
    set_tenant(db, principal.organization_id)
    return set(db.scalars(select(Document.title).where(readable_documents_clause(principal))))


def _manageable(db, principal):
    set_tenant(db, principal.organization_id)
    return set(db.scalars(select(Document.title).where(manageable_documents_clause(principal))))


@pytest.fixture
def docs(factory, tenant):
    t = tenant
    factory.document(t.workspace, uploaded_by=t.editor, title="shared")
    factory.document(t.workspace, uploaded_by=t.editor, title="editor-private", restricted=True)
    factory.document(
        t.workspace,
        uploaded_by=t.manager,
        title="granted-to-viewer",
        restricted=True,
        grants={t.viewer: PermissionLevel.READ},
    )
    factory.document(
        t.workspace,
        uploaded_by=t.manager,
        title="viewer-can-manage",
        restricted=True,
        grants={t.viewer: PermissionLevel.MANAGE},
    )
    other_ws = factory.workspace(t.org, "Legal", members={t.outsider: WorkspaceRole.OWNER})
    factory.document(other_ws, uploaded_by=t.outsider, title="legal-only")
    return t


ALL = {"shared", "editor-private", "granted-to-viewer", "viewer-can-manage", "legal-only"}


def test_org_admin_reads_no_document_by_role_alone(db, factory, docs):
    # Admins manage people and workspaces, not content: like anyone else they read
    # through workspace membership and grants (joining a workspace is audited).
    assert _readable(db, factory.principal(docs.admin, docs.org)) == set()
    assert _manageable(db, factory.principal(docs.admin, docs.org)) == set()


def test_workspace_owner_reads_all_workspace_documents(db, factory, docs):
    assert _readable(db, factory.principal(docs.manager, docs.org)) == ALL - {"legal-only"}


def test_editor_reads_shared_and_own_restricted(db, factory, docs):
    assert _readable(db, factory.principal(docs.editor, docs.org)) == {"shared", "editor-private"}


def test_viewer_reads_shared_and_granted(db, factory, docs):
    assert _readable(db, factory.principal(docs.viewer, docs.org)) == {
        "shared",
        "granted-to-viewer",
        "viewer-can-manage",
    }


def test_member_without_workspace_access_reads_nothing_there(db, factory, docs):
    assert _readable(db, factory.principal(docs.outsider, docs.org)) == {"legal-only"}


def test_uploader_loses_access_after_leaving_workspace(db, factory, tenant):
    lone = factory.user()
    factory.member(tenant.org, lone, OrgRole.EMPLOYEE)
    ws = factory.workspace(tenant.org, "Temp")  # lone is not a member
    factory.document(ws, uploaded_by=lone, title="orphan", restricted=True)
    assert _readable(db, factory.principal(lone, tenant.org)) == set()


def test_manage_rules(db, factory, docs):
    assert _manageable(db, factory.principal(docs.manager, docs.org)) == ALL - {"legal-only"}
    assert _manageable(db, factory.principal(docs.editor, docs.org)) == {
        "shared",
        "editor-private",
    }
    assert _manageable(db, factory.principal(docs.viewer, docs.org)) == {"viewer-can-manage"}


def test_unreadable_document_is_404_and_readable_unmanageable_is_403(db, factory, docs):
    viewer = factory.principal(docs.viewer, docs.org)
    set_tenant(db, docs.org.id)
    private = db.scalar(select(Document).where(Document.title == "editor-private"))
    shared = db.scalar(select(Document).where(Document.title == "shared"))
    with pytest.raises(NotFoundError):
        get_readable_document(db, viewer, private.id)
    with pytest.raises(ForbiddenError):
        get_manageable_document(db, viewer, shared.id)
    assert get_readable_document(db, viewer, shared.id).id == shared.id


def test_crafted_ids_from_another_tenant_are_not_readable(db, factory, docs, other_tenant):
    foreign = factory.document(other_tenant.workspace, uploaded_by=other_tenant.editor)
    # Even an org admin of tenant A, with tenant A's RLS context, cannot see B's document.
    admin = factory.principal(docs.admin, docs.org)
    set_tenant(db, docs.org.id)
    assert readable_document_ids(db, admin, [foreign.id]) == set()
    with pytest.raises(NotFoundError):
        get_readable_document(db, admin, foreign.id)
