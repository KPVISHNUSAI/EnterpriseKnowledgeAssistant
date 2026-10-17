"""Document API: uploads, listing, detail, title/permission updates and the permission
matrix for every document endpoint."""

from __future__ import annotations

import hashlib
import io
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.db.session import set_tenant
from app.models import (
    AuditEvent,
    Document,
    DocumentPermission,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    PermissionLevel,
    PrincipalType,
    WorkspaceRole,
)
from app.services import documents as doc_service
from app.storage import StorageError, get_storage
from app.storage.local import LocalStorage
from tests.test_uploads import DOCX_MIME, MINIMAL_DOCX, docx_bytes, pdf_bytes, zip_bytes

API = "/api/v1/documents"


def upload(client, workspace_id, *, filename="notes.txt", data=b"Onboarding notes.", **kwargs):
    content_type = kwargs.pop("content_type", "text/plain")
    form = {"workspace_id": str(workspace_id), **kwargs}
    return client.post(f"{API}/upload", files={"file": (filename, data, content_type)}, data=form)


def uploaded(client, workspace_id, **kwargs) -> dict:
    response = upload(client, workspace_id, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def stored_document(factory, workspace, *, uploaded_by, **kwargs) -> Document:
    """A factory document plus its (empty) stored file, so downloads work."""
    doc = factory.document(workspace, uploaded_by=uploaded_by, **kwargs)
    get_storage().put(doc.storage_key, io.BytesIO(b""), content_type=doc.mime_type)
    return doc


def stored_files(org_id: uuid.UUID) -> list[Path]:
    root = get_settings().local_storage_path / "orgs" / str(org_id)
    return [path for path in root.rglob("*") if path.is_file()]


def error_code(response) -> str:
    return response.json()["error"]["code"]


# --- Upload --------------------------------------------------------------------------------

SAMPLES = [
    ("handbook.pdf", pdf_bytes(pages=3), "application/pdf", "pdf", "application/pdf"),
    ("policy.docx", docx_bytes(), DOCX_MIME, "docx", DOCX_MIME),
    ("vpn notes.txt", b"Plain text notes about VPN access.\n", "text/plain", "txt", "text/plain"),
    ("guide.md", b"# Guide\n\nUse **MFA** everywhere.\n", "text/markdown", "md", "text/markdown"),
]


@pytest.mark.parametrize(("filename", "data", "content_type", "file_type", "mime"), SAMPLES)
def test_upload_each_supported_type(
    login_as, tenant, db, enqueued, filename, data, content_type, file_type, mime
):
    client = login_as(tenant.editor, tenant.org)
    body = uploaded(
        client, tenant.workspace.id, filename=filename, data=data, content_type=content_type
    )
    doc, job = body["document"], body["job"]
    assert doc["title"] == filename.rsplit(".", 1)[0]
    assert doc["original_filename"] == filename
    assert (doc["file_type"], doc["mime_type"]) == (file_type, mime)
    assert doc["file_size"] == len(data)
    assert doc["content_hash"] == hashlib.sha256(data).hexdigest()
    assert (doc["version"], doc["processing_status"], doc["chunk_count"]) == (1, "queued", 0)
    assert (doc["visibility"], doc["can_manage"]) == ("workspace", True)
    assert doc["workspace_id"] == str(tenant.workspace.id)
    assert doc["workspace_name"] == tenant.workspace.name
    assert doc["uploaded_by"] == {
        "id": str(tenant.editor.id),
        "email": tenant.editor.email,
        "full_name": tenant.editor.full_name,
    }
    assert (job["status"], job["document_version"], job["attempts"]) == ("queued", 1, 0)
    assert job["max_attempts"] == get_settings().ingestion_max_attempts
    assert enqueued.named("eka.ingestion.process") == [[job["id"], str(tenant.org.id)]]

    set_tenant(db, tenant.org.id)
    stored = db.get(Document, uuid.UUID(doc["id"]))
    pattern = rf"orgs/{tenant.org.id}/documents/{doc['id']}/v1/[0-9a-f]{{32}}\.{file_type}"
    assert re.fullmatch(pattern, stored.storage_key)
    assert get_storage().get_bytes(stored.storage_key) == data
    grants = db.scalars(
        select(DocumentPermission).where(DocumentPermission.document_id == stored.id)
    ).all()
    assert [(g.principal_type, g.principal_id, g.permission) for g in grants] == [
        (PrincipalType.WORKSPACE, tenant.workspace.id, PermissionLevel.READ)
    ]
    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "document.uploaded")).one()
    assert (event.resource_id, event.actor_user_id) == (doc["id"], tenant.editor.id)
    assert event.event_metadata["title"] == doc["title"]
    assert event.event_metadata["file_size"] == len(data)


def test_upload_with_title_and_restricted_visibility(login_as, tenant):
    editor = login_as(tenant.editor, tenant.org)
    doc = uploaded(
        editor,
        tenant.workspace.id,
        data=b"Restricted notes",
        title="  Board \t minutes\x00 ",
        visibility="restricted",
    )["document"]
    assert (doc["title"], doc["visibility"]) == ("Board minutes", "restricted")
    assert editor.get(f"{API}/{doc['id']}").status_code == 200
    assert login_as(tenant.viewer, tenant.org).get(f"{API}/{doc['id']}").status_code == 404
    owner_view = login_as(tenant.manager, tenant.org).get(f"{API}/{doc['id']}").json()
    assert owner_view["permissions"] == []


@pytest.mark.parametrize(
    ("filename", "data", "status", "code"),
    [
        ("tool.exe", b"MZ\x90\x00", 415, "unsupported_media_type"),
        ("fake.pdf", b"not really a pdf", 415, "unsupported_media_type"),
        ("binary.txt", b"text\x00with NUL", 415, "unsupported_media_type"),
        ("empty.txt", b"", 422, "empty_file"),
        (
            "macro.docx",
            zip_bytes({**MINIMAL_DOCX, "word/vbaProject.bin": b"vba"}),
            415,
            "macro_enabled_document",
        ),
        ("traversal.docx", zip_bytes({**MINIMAL_DOCX, "../x": b"x"}), 422, "unsafe_archive"),
    ],
)
def test_rejected_uploads_store_nothing(
    login_as, tenant, db, enqueued, filename, data, status, code
):
    client = login_as(tenant.editor, tenant.org)
    response = upload(client, tenant.workspace.id, filename=filename, data=data, content_type="")
    assert response.status_code == status, response.text
    assert error_code(response) == code
    set_tenant(db, tenant.org.id)
    assert db.scalar(select(func.count()).select_from(Document)) == 0
    assert enqueued.tasks == []
    assert stored_files(tenant.org.id) == []


def test_declared_content_type_must_agree(login_as, tenant):
    client = login_as(tenant.editor, tenant.org)
    response = upload(
        client, tenant.workspace.id, filename="r.pdf", data=pdf_bytes(1), content_type="text/html"
    )
    assert response.status_code == 415
    assert error_code(response) == "unsupported_media_type"


def test_upload_size_limits(login_as, tenant, monkeypatch):
    client = login_as(tenant.editor, tenant.org)
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    response = upload(client, tenant.workspace.id, filename="big.txt", data=b"a" * (2**20 + 1))
    assert response.status_code == 413
    assert error_code(response) == "payload_too_large"

    monkeypatch.setattr(get_settings(), "max_docx_uncompressed_mb", 1)
    bomb = zip_bytes({**MINIMAL_DOCX, "word/media/pad.bin": b"\x00" * (2 * 2**20)})
    response = upload(
        client, tenant.workspace.id, filename="bomb.docx", data=bomb, content_type=DOCX_MIME
    )
    assert response.status_code == 422
    assert error_code(response) == "unsafe_archive"


def test_path_traversal_filename_is_stored_safely(login_as, tenant, db):
    client = login_as(tenant.editor, tenant.org)
    doc = uploaded(
        client, tenant.workspace.id, filename="../../../../etc/cron.d/evil.txt", data=b"payload"
    )["document"]
    assert (doc["original_filename"], doc["title"]) == ("evil.txt", "evil")
    set_tenant(db, tenant.org.id)
    key = db.get(Document, uuid.UUID(doc["id"])).storage_key
    assert key.startswith(f"orgs/{tenant.org.id}/documents/{doc['id']}/v1/")
    assert "evil" not in key and ".." not in key
    root = get_settings().local_storage_path.resolve()
    assert (root / key).resolve().is_relative_to(root)
    assert (root / key).read_bytes() == b"payload"


def test_duplicate_reveals_existing_document_only_to_those_who_can_read_it(
    login_as, factory, tenant
):
    second_editor = factory.user(full_name="Second Editor")
    factory.member(tenant.org, second_editor)
    factory.workspace_member(tenant.workspace, second_editor, WorkspaceRole.EDITOR)
    data = b"Identical content"
    editor = login_as(tenant.editor, tenant.org)
    original = uploaded(
        editor, tenant.workspace.id, filename="a.txt", data=data, visibility="restricted"
    )["document"]

    response = upload(login_as(tenant.manager, tenant.org), tenant.workspace.id, data=data)
    assert response.status_code == 409
    assert error_code(response) == "duplicate_document"
    assert response.json()["error"]["details"] == {"document_id": original["id"]}

    response = upload(login_as(second_editor, tenant.org), tenant.workspace.id, data=data)
    assert response.status_code == 409
    assert error_code(response) == "duplicate_document"
    assert response.json()["error"]["details"] is None

    other = factory.workspace(tenant.org, "Other", members={tenant.editor: WorkspaceRole.EDITOR})
    assert upload(editor, other.id, data=data).status_code == 201


def test_identical_upload_racing_past_the_check_is_a_conflict(
    login_as, factory, tenant, monkeypatch
):
    data = b"Raced content"
    existing = factory.document(
        tenant.workspace, uploaded_by=tenant.manager, content_hash=hashlib.sha256(data).hexdigest()
    )
    real_check = doc_service._duplicate
    calls = []

    def first_check_misses(*args):
        calls.append(args)
        return None if len(calls) == 1 else real_check(*args)

    monkeypatch.setattr(doc_service, "_duplicate", first_check_misses)
    response = upload(login_as(tenant.editor, tenant.org), tenant.workspace.id, data=data)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["details"] == {"document_id": str(existing.id)}
    assert stored_files(tenant.org.id) == []  # the new object was removed again


def test_upload_succeeds_when_the_broker_is_down(login_as, tenant, db, enqueued):
    enqueued.fail = True
    body = uploaded(login_as(tenant.editor, tenant.org), tenant.workspace.id)
    set_tenant(db, tenant.org.id)
    job = db.get(IngestionJob, uuid.UUID(body["job"]["id"]))
    assert job.status == JobStatus.QUEUED  # the reaper re-sends it


class BrokenStorage(LocalStorage):
    def put(self, key, fileobj, *, content_type):
        raise StorageError("disk full")


def test_storage_outage_is_503_and_records_nothing(app, login_as, tenant, db, tmp_path):
    app.dependency_overrides[get_storage] = lambda: BrokenStorage(tmp_path)
    response = upload(login_as(tenant.editor, tenant.org), tenant.workspace.id)
    assert response.status_code == 503
    assert error_code(response) == "storage_unavailable"
    set_tenant(db, tenant.org.id)
    assert db.scalar(select(func.count()).select_from(Document)) == 0


def test_upload_requires_upload_rights_in_the_workspace(client, login_as, tenant, other_tenant):
    ws = tenant.workspace.id
    assert upload(client, ws).status_code == 401
    assert upload(login_as(tenant.viewer, tenant.org), ws).status_code == 403
    assert upload(login_as(tenant.outsider, tenant.org), ws).status_code == 404
    assert upload(login_as(other_tenant.admin, other_tenant.org), ws).status_code == 404
    admin = login_as(tenant.admin, tenant.org)  # not a workspace member, still allowed
    assert upload(admin, ws, data=b"by admin").status_code == 201
    assert upload(admin, uuid.uuid4(), data=b"nowhere").status_code == 404
    assert upload(login_as(tenant.manager, tenant.org), ws, data=b"by owner").status_code == 201


# --- Listing -------------------------------------------------------------------------------


def test_list_filters_sort_and_pagination(login_as, factory, tenant, db):
    ws = tenant.workspace
    legal = factory.workspace(tenant.org, "Legal", members={tenant.manager: WorkspaceRole.OWNER})
    alpha = factory.document(
        ws,
        uploaded_by=tenant.editor,
        title="Alpha handbook",
        mime_type="application/pdf",
        filename="alpha.pdf",
    )
    beta = factory.document(
        ws,
        uploaded_by=tenant.manager,
        title="beta notes",
        status=DocumentStatus.FAILED,
        filename="beta.txt",
    )
    factory.document(
        legal,
        uploaded_by=tenant.manager,
        title="Gamma 100% plan",
        mime_type="text/markdown",
        filename="gamma_plan.md",
        status=DocumentStatus.QUEUED,
    )
    factory.document(
        ws,
        uploaded_by=tenant.editor,
        title="Delta",
        mime_type=DOCX_MIME,
        filename="delta.docx",
        status=DocumentStatus.PROCESSING,
    )
    set_tenant(db, tenant.org.id)
    db.execute(update(Document).where(Document.id == alpha.id).values(file_size=300))
    db.execute(update(Document).where(Document.id == beta.id).values(file_size=100))
    db.commit()
    client = login_as(tenant.manager, tenant.org)

    def titles(**params) -> list[str]:
        response = client.get(API, params=params)
        assert response.status_code == 200, response.text
        return [item["title"] for item in response.json()["items"]]

    assert titles() == ["Delta", "Gamma 100% plan", "beta notes", "Alpha handbook"]  # newest
    assert titles(sort="title", order="asc") == [
        "Alpha handbook",
        "beta notes",
        "Delta",
        "Gamma 100% plan",
    ]
    assert titles(sort="file_size", order="desc")[:2] == ["Alpha handbook", "beta notes"]
    assert titles(workspace_id=str(legal.id)) == ["Gamma 100% plan"]
    assert set(titles(status=["failed", "queued"])) == {"beta notes", "Gamma 100% plan"}
    assert titles(file_type="pdf") == ["Alpha handbook"]
    assert set(titles(file_type=["md", "docx"])) == {"Gamma 100% plan", "Delta"}
    assert titles(q="HANDBOOK") == ["Alpha handbook"]
    assert titles(q="gamma_plan") == ["Gamma 100% plan"]  # filename match
    assert titles(q="%") == ["Gamma 100% plan"]  # wildcards are matched literally
    assert titles(q="_") == ["Gamma 100% plan"]
    assert titles(q=" \x00 ") == titles()  # nothing left to search for
    assert set(titles(uploaded_by=str(tenant.editor.id))) == {"Alpha handbook", "Delta"}

    page = client.get(API, params={"sort": "title", "order": "asc", "page": 2, "page_size": 3})
    body = page.json()
    assert (body["total"], body["page"], body["page_size"]) == (4, 2, 3)
    assert [item["title"] for item in body["items"]] == ["Gamma 100% plan"]
    assert client.get(API, params={"page_size": 101}).status_code == 422


def test_list_reports_visibility_and_manage_flags(login_as, factory, tenant, other_tenant):
    ws = tenant.workspace
    factory.document(ws, uploaded_by=tenant.editor, title="own-restricted", restricted=True)
    factory.document(ws, uploaded_by=tenant.manager, title="managers-shared")
    factory.document(ws, uploaded_by=tenant.manager, title="managers-private", restricted=True)
    factory.document(
        ws,
        uploaded_by=tenant.manager,
        title="granted-manage",
        restricted=True,
        grants={tenant.editor: PermissionLevel.MANAGE},
    )
    factory.document(other_tenant.workspace, uploaded_by=other_tenant.editor, title="globex")

    def listed(user) -> dict[str, dict]:
        items = login_as(user, tenant.org).get(API).json()["items"]
        return {item["title"]: item for item in items}

    editor_view = listed(tenant.editor)
    assert set(editor_view) == {"own-restricted", "managers-shared", "granted-manage"}
    assert editor_view["own-restricted"]["visibility"] == "restricted"
    assert editor_view["own-restricted"]["can_manage"] is True
    assert editor_view["managers-shared"]["visibility"] == "workspace"
    assert editor_view["managers-shared"]["can_manage"] is False
    assert editor_view["granted-manage"]["can_manage"] is True

    viewer_view = listed(tenant.viewer)
    assert set(viewer_view) == {"managers-shared"}
    assert viewer_view["managers-shared"]["can_manage"] is False

    admin_view = listed(tenant.admin)
    assert set(admin_view) == {
        "own-restricted",
        "managers-shared",
        "managers-private",
        "granted-manage",
    }
    assert all(item["can_manage"] for item in admin_view.values())
    assert listed(tenant.outsider) == {}


# --- Detail, title and permissions ---------------------------------------------------------


def test_detail_includes_permissions_only_for_managers(login_as, tenant):
    manager = login_as(tenant.manager, tenant.org)
    doc_id = uploaded(manager, tenant.workspace.id, data=b"Detail")["document"]["id"]
    response = manager.put(
        f"{API}/{doc_id}/permissions",
        json={"visibility": "workspace", "users": [{"user_id": str(tenant.viewer.id)}]},
    )
    assert response.status_code == 200, response.text

    detail = manager.get(f"{API}/{doc_id}").json()
    assert detail["latest_job"]["status"] == "queued"
    assert detail["permissions"] == [
        {
            "principal_type": "workspace",
            "principal_id": str(tenant.workspace.id),
            "principal_name": tenant.workspace.name,
            "permission": "read",
        },
        {
            "principal_type": "user",
            "principal_id": str(tenant.viewer.id),
            "principal_name": tenant.viewer.full_name,
            "permission": "read",
        },
    ]
    viewer_detail = login_as(tenant.viewer, tenant.org).get(f"{API}/{doc_id}").json()
    assert viewer_detail["can_manage"] is False
    assert viewer_detail["permissions"] is None
    assert viewer_detail["latest_job"]["id"] == detail["latest_job"]["id"]


def test_update_title(login_as, factory, tenant, db):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.editor, title="Old")
    editor = login_as(tenant.editor, tenant.org)
    response = editor.patch(f"{API}/{doc.id}", json={"title": "  New \n title "})
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "New title"
    assert editor.patch(f"{API}/{doc.id}", json={"title": " \x00 "}).status_code == 422
    assert editor.patch(f"{API}/{doc.id}", json={"title": ""}).status_code == 422
    set_tenant(db, tenant.org.id)
    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "document.updated")).one()
    assert event.event_metadata == {"title": "New title"}


def test_update_permissions_replaces_all_grants(login_as, factory, tenant, db):
    doc = factory.document(
        tenant.workspace,
        uploaded_by=tenant.manager,
        grants={tenant.editor: PermissionLevel.READ},
    )
    manager = login_as(tenant.manager, tenant.org)
    response = manager.put(
        f"{API}/{doc.id}/permissions",
        json={
            "visibility": "restricted",
            "users": [
                {"user_id": str(tenant.viewer.id), "permission": "read"},
                {"user_id": str(tenant.outsider.id), "permission": "manage"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["visibility"] == "restricted"
    assert {(p["principal_name"], p["permission"]) for p in detail["permissions"]} == {
        (tenant.viewer.full_name, "read"),
        (tenant.outsider.full_name, "manage"),
    }
    # The workspace grant and the editor's grant are gone; the new grantees are in.
    assert login_as(tenant.editor, tenant.org).get(f"{API}/{doc.id}").status_code == 404
    assert login_as(tenant.viewer, tenant.org).get(f"{API}/{doc.id}").status_code == 200
    outsider = login_as(tenant.outsider, tenant.org)
    assert outsider.patch(f"{API}/{doc.id}", json={"title": "By grantee"}).status_code == 200

    set_tenant(db, tenant.org.id)
    event = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "document.permissions_changed")
    ).one()
    assert event.event_metadata == {
        "visibility": "restricted",
        "user_grants": 2,
        "manage_grants": 1,
    }


def test_update_permissions_rejects_non_members(login_as, factory, tenant, other_tenant, db):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.manager)
    stranger = uuid.uuid4()
    response = login_as(tenant.manager, tenant.org).put(
        f"{API}/{doc.id}/permissions",
        json={
            "visibility": "restricted",
            "users": [
                {"user_id": str(tenant.viewer.id)},
                {"user_id": str(other_tenant.viewer.id)},
                {"user_id": str(stranger)},
            ],
        },
    )
    assert response.status_code == 422
    assert error_code(response) == "invalid_grantee"
    assert set(response.json()["error"]["details"]["user_ids"]) == {
        str(other_tenant.viewer.id),
        str(stranger),
    }
    set_tenant(db, tenant.org.id)
    grants = db.scalars(select(DocumentPermission).where(DocumentPermission.document_id == doc.id))
    assert [g.principal_type for g in grants] == [PrincipalType.WORKSPACE]  # unchanged


def test_manager_who_removes_their_own_access_gets_a_detail_without_permissions(
    login_as, factory, tenant
):
    doc = factory.document(
        tenant.workspace,
        uploaded_by=tenant.manager,
        restricted=True,
        grants={tenant.viewer: PermissionLevel.MANAGE},
    )
    viewer = login_as(tenant.viewer, tenant.org)
    response = viewer.put(f"{API}/{doc.id}/permissions", json={"visibility": "restricted"})
    assert response.status_code == 200, response.text
    assert response.json()["can_manage"] is False
    assert response.json()["permissions"] is None
    assert viewer.get(f"{API}/{doc.id}").status_code == 404


# --- Permission matrix ---------------------------------------------------------------------

READ_ENDPOINTS = ["", "/status", "/download", "/chunks"]
MANAGE_ENDPOINTS = [
    ("patch", "", {"json": {"title": "Renamed"}}, 200),
    ("post", "/reindex", {}, 202),
    ("put", "/file", {"files": {"file": ("v2.txt", b"Replacement", "text/plain")}}, 202),
    ("put", "/permissions", {"json": {"visibility": "workspace", "users": []}}, 200),
    ("delete", "", {}, 204),
]


@pytest.mark.parametrize(
    ("actor", "uploader", "can_read", "can_manage"),
    [
        ("admin", "editor", True, True),
        ("manager", "editor", True, True),  # workspace owner manages every document
        ("editor", "editor", True, True),  # editors manage their own uploads...
        ("editor", "manager", True, False),  # ...but not other people's
        ("viewer", "editor", True, False),
        ("outsider", "editor", False, False),  # org member without workspace access
    ],
)
def test_permission_matrix(login_as, factory, tenant, actor, uploader, can_read, can_manage):
    client = login_as(getattr(tenant, actor), tenant.org)
    owner = getattr(tenant, uploader)
    for suffix in READ_ENDPOINTS:
        doc = stored_document(factory, tenant.workspace, uploaded_by=owner)
        response = client.get(f"{API}/{doc.id}{suffix}")
        assert response.status_code == (200 if can_read else 404), (suffix, response.text)
    for method, suffix, kwargs, success in MANAGE_ENDPOINTS:
        doc = stored_document(factory, tenant.workspace, uploaded_by=owner)
        response = client.request(method, f"{API}/{doc.id}{suffix}", **kwargs)
        expected = success if can_manage else 403 if can_read else 404
        assert response.status_code == expected, (method, suffix, response.text)


def test_other_tenants_documents_are_not_found(login_as, factory, tenant, other_tenant):
    client = login_as(other_tenant.admin, other_tenant.org)
    for suffix in READ_ENDPOINTS:
        doc = stored_document(factory, tenant.workspace, uploaded_by=tenant.editor)
        assert client.get(f"{API}/{doc.id}{suffix}").status_code == 404, suffix
    for method, suffix, kwargs, _ in MANAGE_ENDPOINTS:
        doc = stored_document(factory, tenant.workspace, uploaded_by=tenant.editor)
        response = client.request(method, f"{API}/{doc.id}{suffix}", **kwargs)
        assert response.status_code == 404, (method, suffix)
    assert client.get(API).json()["total"] == 0


def test_unknown_document_ids_are_not_found(login_as, tenant):
    client = login_as(tenant.admin, tenant.org)
    assert client.get(f"{API}/{uuid.uuid4()}").status_code == 404
    assert client.delete(f"{API}/{uuid.uuid4()}").status_code == 404
