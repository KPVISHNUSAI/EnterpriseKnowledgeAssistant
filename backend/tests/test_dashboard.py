"""Dashboard: counts and lists limited to what the caller can see."""

from __future__ import annotations

from datetime import timedelta

from app.db.base import utcnow
from app.db.session import set_tenant
from app.models import Conversation, DocumentStatus, Message, MessageRole, WorkspaceRole
from app.services import audit
from app.services.audit import AuditAction
from app.services.dashboard import summarize
from tests.test_documents import uploaded

DASHBOARD = "/api/v1/dashboard"


def test_counts_cover_only_readable_documents(login_as, factory, tenant, other_tenant):
    ws = tenant.workspace
    factory.document(ws, uploaded_by=tenant.editor, title="indexed", status=DocumentStatus.INDEXED)
    factory.document(ws, uploaded_by=tenant.editor, title="failed", status=DocumentStatus.FAILED)
    factory.document(
        ws,
        uploaded_by=tenant.manager,
        title="restricted",
        status=DocumentStatus.QUEUED,
        restricted=True,
    )
    legal = factory.workspace(tenant.org, "Legal", members={tenant.outsider: WorkspaceRole.OWNER})
    factory.document(
        legal, uploaded_by=tenant.outsider, title="legal", status=DocumentStatus.PROCESSING
    )
    factory.document(other_tenant.workspace, uploaded_by=other_tenant.editor, title="globex")

    viewer = login_as(tenant.viewer, tenant.org).get(DASHBOARD).json()
    assert viewer["documents_total"] == 2
    assert viewer["documents_by_status"] == {
        "queued": 0,
        "processing": 0,
        "indexed": 1,
        "failed": 1,
    }
    assert viewer["workspaces_count"] == 1
    assert {doc["title"] for doc in viewer["recent_documents"]} == {"indexed", "failed"}

    admin = login_as(tenant.admin, tenant.org).get(DASHBOARD).json()
    assert admin["documents_total"] == 4
    assert admin["documents_by_status"] == {
        "queued": 1,
        "processing": 1,
        "indexed": 1,
        "failed": 1,
    }
    assert admin["workspaces_count"] == 2  # admins see every workspace

    newcomer = factory.user()
    factory.member(tenant.org, newcomer)
    empty = login_as(newcomer, tenant.org).get(DASHBOARD).json()
    assert empty["documents_total"] == 0
    assert set(empty["documents_by_status"].values()) == {0}
    assert (empty["workspaces_count"], empty["recent_documents"]) == (0, [])
    assert (empty["recent_conversations"], empty["recent_activity"]) == ([], [])


def test_recent_documents_are_the_five_newest(login_as, factory, tenant):
    for n in range(7):
        factory.document(tenant.workspace, uploaded_by=tenant.editor, title=f"doc {n}")
    recent = login_as(tenant.viewer, tenant.org).get(DASHBOARD).json()["recent_documents"]
    assert [doc["title"] for doc in recent] == ["doc 6", "doc 5", "doc 4", "doc 3", "doc 2"]
    assert recent[0]["workspace_name"] == tenant.workspace.name
    assert recent[0]["can_manage"] is False


def test_recent_conversations_are_the_callers_own(login_as, tenant, db):
    set_tenant(db, tenant.org.id)
    now = utcnow()
    mine = [
        Conversation(
            organization_id=tenant.org.id,
            user_id=tenant.viewer.id,
            title=f"mine {n}",
            updated_at=now - timedelta(minutes=n),
        )
        for n in range(6)
    ]
    theirs = Conversation(organization_id=tenant.org.id, user_id=tenant.editor.id, title="theirs")
    db.add_all([*mine, theirs])
    db.flush()
    for role, content in [(MessageRole.USER, "Question?"), (MessageRole.ASSISTANT, "Answer.")]:
        db.add(
            Message(
                conversation_id=mine[1].id,
                organization_id=tenant.org.id,
                role=role,
                content=content,
            )
        )
    db.commit()

    recent = login_as(tenant.viewer, tenant.org).get(DASHBOARD).json()["recent_conversations"]
    assert [c["title"] for c in recent] == ["mine 0", "mine 1", "mine 2", "mine 3", "mine 4"]
    assert [c["message_count"] for c in recent] == [0, 2, 0, 0, 0]


def test_activity_admins_see_the_organization_others_their_own(login_as, tenant, db):
    secret = b"TOP SECRET merger terms"
    editor = login_as(tenant.editor, tenant.org)
    doc = uploaded(editor, tenant.workspace.id, filename="Merger.txt", data=secret)["document"]
    viewer = login_as(tenant.viewer, tenant.org)
    assert viewer.get(f"/api/v1/documents/{doc['id']}/download").content == secret

    mine = viewer.get(DASHBOARD).json()["recent_activity"]
    assert [(a["action"], a["actor_email"], a["summary"]) for a in mine] == [
        ("document.downloaded", tenant.viewer.email, "Downloaded “Merger”")
    ]
    assert (mine[0]["resource_type"], mine[0]["resource_id"]) == ("document", doc["id"])

    activity = login_as(tenant.admin, tenant.org).get(DASHBOARD).json()["recent_activity"]
    assert [(a["action"], a["summary"]) for a in activity] == [
        ("document.downloaded", "Downloaded “Merger”"),
        ("document.uploaded", "Uploaded “Merger”"),
    ]
    assert "SECRET" not in str(activity)


def test_activity_is_limited_to_the_ten_newest(login_as, tenant, db):
    set_tenant(db, tenant.org.id)
    for n in range(12):
        audit.record(
            db,
            AuditAction.DOCUMENT_UPDATED,
            organization_id=tenant.org.id,
            actor_user_id=tenant.manager.id,
            resource_type="document",
            resource_id=f"doc-{n}",
            metadata={"title": f"Title {n}"},
        )
        db.flush()
    db.commit()
    activity = login_as(tenant.admin, tenant.org).get(DASHBOARD).json()["recent_activity"]
    assert len(activity) == 10
    assert activity[0]["summary"] == "Updated “Title 11”"


def test_summaries():
    assert summarize("document.deleted", {"title": "Q3 plan"}) == "Deleted “Q3 plan”"
    assert summarize("document.deleted", {}) == "Deleted a document"
    assert summarize("document.permissions_changed", {"user_grants": 2}) == (
        "Changed who can access a document"
    )
    assert summarize("auth.login", {"title": "ignored"}) == "Signed in"
    long = summarize("document.uploaded", {"title": "x" * 200})
    assert len(long) < 100 and long.endswith("…”")
    assert summarize("workspace.archived_all", {}) == "Workspace: archived all"
