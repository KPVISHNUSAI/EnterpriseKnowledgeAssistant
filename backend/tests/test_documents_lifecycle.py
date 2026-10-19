"""Document lifecycle: re-indexing, replacing the file, deletion (including derived content
in chat history), downloads and chunk listing."""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import parse_qs, urlsplit

from moto import mock_aws
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import set_tenant
from app.models import (
    AuditEvent,
    Conversation,
    Document,
    DocumentChunk,
    DocumentPermission,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    Message,
    MessageRole,
    PermissionLevel,
    PrincipalType,
)
from app.providers.embeddings import cache
from app.schemas.chat import CitationOut, SourceRef
from app.services import documents as doc_service
from app.storage import StorageError, get_storage
from app.storage.local import LocalStorage
from app.storage.s3 import S3Storage
from tests.test_documents import API, error_code, stored_document, uploaded
from tests.test_storage_s3 import BUCKET, s3_settings, use_fake_aws_credentials
from tests.test_uploads import DOCX_MIME, docx_bytes, pdf_bytes


def jobs_of(db, document_id) -> list[IngestionJob]:
    db.expire_all()
    return list(
        db.scalars(
            select(IngestionJob)
            .where(IngestionJob.document_id == document_id)
            .order_by(IngestionJob.created_at)
        )
    )


# --- Re-indexing ---------------------------------------------------------------------------


def test_reindex_queues_a_job_for_the_current_version(login_as, factory, tenant, db, enqueued):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    factory.chunks(doc, ["first passage", "second passage"])
    client = login_as(tenant.editor, tenant.org)
    response = client.post(f"{API}/{doc.id}/reindex")
    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["processing_status"], body["version"], body["chunk_count"]) == ("queued", 1, 2)
    assert body["latest_job"]["status"] == "queued"
    assert body["latest_job"]["document_version"] == 1
    assert enqueued.named("eka.ingestion.process") == [
        [body["latest_job"]["id"], str(tenant.org.id)]
    ]
    set_tenant(db, tenant.org.id)
    assert [job.status for job in jobs_of(db, doc.id)] == [JobStatus.QUEUED]
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 2  # kept until swap
    event = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "document.reindex_requested")
    ).one()
    assert event.resource_id == str(doc.id)

    again = client.post(f"{API}/{doc.id}/reindex")
    assert again.status_code == 409
    assert error_code(again) == "ingestion_in_progress"
    assert len(enqueued.named("eka.ingestion.process")) == 1


def test_reindex_retries_a_failed_ingestion(login_as, factory, tenant, db):
    doc = factory.document(
        tenant.workspace, uploaded_by=tenant.editor, status=DocumentStatus.FAILED
    )
    set_tenant(db, tenant.org.id)
    stored = db.get(Document, doc.id)
    stored.processing_error = "The PDF is encrypted"
    db.add(
        IngestionJob(
            document_id=doc.id,
            organization_id=doc.organization_id,
            document_version=1,
            status=JobStatus.FAILED,
            attempts=3,
            max_attempts=3,
            error_code="encrypted_pdf",
        )
    )
    db.commit()
    response = login_as(tenant.editor, tenant.org).get(f"{API}/{doc.id}/status")
    assert response.json()["latest_job"]["error_code"] == "encrypted_pdf"

    response = login_as(tenant.editor, tenant.org).post(f"{API}/{doc.id}/reindex")
    assert response.status_code == 202, response.text
    assert response.json()["processing_error"] is None
    assert response.json()["processing_status"] == "queued"
    assert [job.status for job in jobs_of(db, doc.id)] == [JobStatus.FAILED, JobStatus.QUEUED]


def test_reindex_translates_the_active_job_index_violation(
    login_as, tenant, db, monkeypatch, enqueued
):
    body = uploaded(login_as(tenant.editor, tenant.org), tenant.workspace.id)
    # Make the pre-check miss the active job, as in a race with another request.
    monkeypatch.setattr(doc_service, "ACTIVE_JOB_STATUSES", ())
    response = login_as(tenant.editor, tenant.org).post(f"{API}/{body['document']['id']}/reindex")
    assert response.status_code == 409
    assert error_code(response) == "ingestion_in_progress"
    assert len(enqueued.named("eka.ingestion.process")) == 1  # only the upload's job


def test_status_reports_progress(login_as, tenant):
    client = login_as(tenant.viewer, tenant.org)
    body = uploaded(login_as(tenant.editor, tenant.org), tenant.workspace.id)
    status = client.get(f"{API}/{body['document']['id']}/status").json()
    latest_job = status.pop("latest_job")
    assert status == {
        "document_id": body["document"]["id"],
        "processing_status": "queued",
        "processing_error": None,
        "version": 1,
        "chunk_count": 0,
        "indexed_at": None,
    }
    assert (latest_job["id"], latest_job["status"]) == (body["job"]["id"], "queued")


# --- Replacing the file --------------------------------------------------------------------


def test_replace_creates_a_new_version(login_as, factory, tenant, db, enqueued):
    editor = login_as(tenant.editor, tenant.org)
    first = uploaded(editor, tenant.workspace.id, filename="notes.txt", data=b"Version one")
    doc_id = first["document"]["id"]
    set_tenant(db, tenant.org.id)
    old_key = db.get(Document, uuid.UUID(doc_id)).storage_key
    factory.chunks(db.get(Document, uuid.UUID(doc_id)), ["version one passage"])

    data = b"# Version two\n\nNew guidance."
    response = editor.put(
        f"{API}/{doc_id}/file", files={"file": ("notes-v2.md", data, "text/markdown")}
    )
    assert response.status_code == 202, response.text
    doc, job = response.json()["document"], response.json()["job"]
    assert doc["version"] == 2
    assert doc["title"] == "notes"  # the title is the user's, not the file's
    assert (doc["original_filename"], doc["file_type"], doc["mime_type"]) == (
        "notes-v2.md",
        "md",
        "text/markdown",
    )
    assert (doc["file_size"], doc["content_hash"]) == (len(data), hashlib.sha256(data).hexdigest())
    assert (doc["processing_status"], doc["chunk_count"]) == ("queued", 1)
    assert (job["status"], job["document_version"]) == ("queued", 2)

    jobs = jobs_of(db, uuid.UUID(doc_id))
    assert [(j.status, j.document_version, j.error_code) for j in jobs] == [
        (JobStatus.CANCELLED, 1, "superseded"),
        (JobStatus.QUEUED, 2, None),
    ]
    assert jobs[0].completed_at is not None
    assert enqueued.named("eka.ingestion.process")[-1] == [job["id"], str(tenant.org.id)]
    # Old chunks stay searchable until the worker swaps in version 2.
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 1
    new_key = db.get(Document, uuid.UUID(doc_id)).storage_key
    assert f"/documents/{doc_id}/v2/" in new_key and new_key.endswith(".md")
    assert get_storage().get_bytes(new_key) == data
    assert not get_storage().exists(old_key)
    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "document.replaced")).one()
    assert event.event_metadata["version"] == 2


def test_replace_accepts_any_supported_type(login_as, tenant):
    editor = login_as(tenant.editor, tenant.org)
    doc_id = uploaded(editor, tenant.workspace.id, data=b"plain")["document"]["id"]
    for filename, data, content_type, version in [
        ("policy.docx", docx_bytes(), DOCX_MIME, 2),
        ("policy.pdf", pdf_bytes(pages=2), "application/pdf", 3),
    ]:
        response = editor.put(
            f"{API}/{doc_id}/file", files={"file": (filename, data, content_type)}
        )
        assert response.status_code == 202, response.text
        assert response.json()["document"]["version"] == version


def test_replace_rejects_unchanged_duplicate_and_invalid_files(login_as, factory, tenant, db):
    editor = login_as(tenant.editor, tenant.org)
    doc_id = uploaded(editor, tenant.workspace.id, data=b"current")["document"]["id"]
    other_id = uploaded(editor, tenant.workspace.id, data=b"other")["document"]["id"]
    url = f"{API}/{doc_id}/file"

    response = editor.put(url, files={"file": ("renamed.txt", b"current", "text/plain")})
    assert (response.status_code, error_code(response)) == (409, "unchanged_file")
    response = editor.put(url, files={"file": ("x.txt", b"other", "text/plain")})
    assert (response.status_code, error_code(response)) == (409, "duplicate_document")
    assert response.json()["error"]["details"] == {"document_id": other_id}
    response = editor.put(url, files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert (response.status_code, error_code(response)) == (415, "unsupported_media_type")
    response = editor.put(url, files={"file": ("x.txt", b"", "text/plain")})
    assert (response.status_code, error_code(response)) == (422, "empty_file")

    set_tenant(db, tenant.org.id)
    assert db.get(Document, uuid.UUID(doc_id)).version == 1
    assert len(jobs_of(db, uuid.UUID(doc_id))) == 1


# --- Deletion ------------------------------------------------------------------------------


def _source(doc_id, source_id: str, workspace_id) -> dict:
    return {
        "source_id": source_id,
        "chunk_id": str(uuid.uuid4()),
        "document_id": str(doc_id),
        "document_title": "Restructuring plan",
        "workspace_id": str(workspace_id),
        "file_type": "txt",
        "page_number": 3,
        "page_end": 3,
        "section_title": "Affected teams",
        "excerpt": "Confidential: the Berlin office closes in March.",
        "relevance": {"rerank_score": 0.91, "rrf_score": 0.03},
        "cited": True,
        "redacted": False,
    }


def _citation(doc_id, source_id: str) -> dict:
    return {
        "source_id": source_id,
        "verified": True,
        "document_id": str(doc_id),
        "document_title": "Restructuring plan",
        "page_number": 3,
        "section_title": "Affected teams",
        "excerpt": "Confidential: the Berlin office closes in March.",
    }


def test_delete_removes_everything_derived_from_the_document(
    login_as, factory, tenant, db, monkeypatch
):
    invalidated: list[tuple[uuid.UUID, list[str]]] = []
    monkeypatch.setattr(
        cache, "invalidate", lambda org_id, hashes: invalidated.append((org_id, sorted(hashes)))
    )
    client = login_as(tenant.manager, tenant.org)
    secret = b"Berlin office closes in March."
    doc_id = uuid.UUID(uploaded(client, tenant.workspace.id, data=secret)["document"]["id"])
    keep_id = uuid.UUID(uploaded(client, tenant.workspace.id, data=b"keep")["document"]["id"])
    set_tenant(db, tenant.org.id)
    doc = db.get(Document, doc_id)
    key = doc.storage_key
    chunks = factory.chunks(doc, ["Berlin office closes in March.", "Teams affected."])
    factory.chunks(db.get(Document, keep_id), ["Unrelated passage."])
    db.add(
        DocumentPermission(
            document_id=doc_id,
            organization_id=tenant.org.id,
            principal_type=PrincipalType.USER,
            principal_id=tenant.viewer.id,
            permission=PermissionLevel.READ,
        )
    )
    conversation = Conversation(organization_id=tenant.org.id, user_id=tenant.viewer.id, title="Q")
    db.add(conversation)
    db.flush()
    ws = tenant.workspace.id
    message = Message(
        conversation_id=conversation.id,
        organization_id=tenant.org.id,
        role=MessageRole.ASSISTANT,
        content="The office closes [S1].",
        sources=[_source(doc_id, "S1", ws), _source(keep_id, "S2", ws)],
        retrieval_metadata={
            "citations": [_citation(doc_id, "S1"), _citation(keep_id, "S2")],
            "mode": "hybrid",
        },
    )
    untouched_sources = [_source(keep_id, "S1", ws)]
    untouched = Message(
        conversation_id=conversation.id,
        organization_id=tenant.org.id,
        role=MessageRole.ASSISTANT,
        content="Other answer [S1].",
        sources=untouched_sources,
        retrieval_metadata={"citations": [_citation(keep_id, "S1")]},
    )
    db.add_all([message, untouched])
    db.commit()

    response = client.delete(f"{API}/{doc_id}")
    assert response.status_code == 204, response.text

    db.expire_all()
    assert db.get(Document, doc_id) is None
    for model in (DocumentChunk, IngestionJob, DocumentPermission):
        count = select(func.count()).select_from(model).where(model.document_id == doc_id)
        assert db.scalar(count) == 0, model.__name__
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 1  # the other doc's
    assert not get_storage().exists(key)
    assert invalidated == [(tenant.org.id, sorted(chunk.content_hash for chunk in chunks))]

    scrubbed = db.get(Message, message.id)
    deleted_source, kept_source = scrubbed.sources
    assert deleted_source == {
        **_source(doc_id, "S1", ws),
        "chunk_id": deleted_source["chunk_id"],
        "document_title": "Deleted document",
        "section_title": None,
        "excerpt": None,
        "redacted": True,
    }
    assert kept_source["excerpt"] == "Confidential: the Berlin office closes in March."
    assert not kept_source["redacted"]
    deleted_citation, kept_citation = scrubbed.retrieval_metadata["citations"]
    assert (deleted_citation["excerpt"], deleted_citation["document_title"]) == (
        None,
        "Deleted document",
    )
    assert kept_citation == _citation(keep_id, "S2")
    assert scrubbed.retrieval_metadata["mode"] == "hybrid"
    # The scrubbed payloads still satisfy the chat contract.
    SourceRef.model_validate(deleted_source)
    CitationOut.model_validate(deleted_citation)
    assert db.get(Message, untouched.id).sources == untouched_sources

    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "document.deleted")).one()
    assert event.event_metadata["title"] == "notes"
    assert "Berlin" not in str(event.event_metadata)


class UndeletableStorage(LocalStorage):
    def delete(self, key):
        raise StorageError("bucket policy denies DeleteObject")


def test_delete_succeeds_when_the_stored_file_cannot_be_removed(app, login_as, factory, tenant, db):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    app.dependency_overrides[get_storage] = lambda: UndeletableStorage(
        get_settings().local_storage_path
    )
    response = login_as(tenant.editor, tenant.org).delete(f"{API}/{doc.id}")
    assert response.status_code == 204
    set_tenant(db, tenant.org.id)
    assert db.get(Document, doc.id) is None


# --- Downloads -----------------------------------------------------------------------------


def test_download_as_attachment(login_as, tenant, db):
    data = pdf_bytes(pages=2)
    client = login_as(tenant.editor, tenant.org)
    doc_id = uploaded(
        client,
        tenant.workspace.id,
        filename="R\u00e9sum\u00e9 \u2013 2026.pdf",
        data=data,
        content_type="application/pdf",
    )["document"]["id"]
    response = login_as(tenant.viewer, tenant.org).get(f"{API}/{doc_id}/download")
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-length"] == str(len(data))
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == (
        'attachment; filename="R_sum_ _ 2026.pdf"; '
        "filename*=UTF-8''R%C3%A9sum%C3%A9%20%E2%80%93%202026.pdf"
    )
    assert (
        response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'self'"
    )
    set_tenant(db, tenant.org.id)
    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "document.downloaded")).one()
    assert event.actor_user_id == tenant.viewer.id
    assert event.event_metadata == {
        "title": "R\u00e9sum\u00e9 \u2013 2026",
        "version": 1,
        "inline": False,
    }


def test_inline_pdf_can_be_framed_by_the_app(login_as, tenant):
    client = login_as(tenant.editor, tenant.org)
    doc_id = uploaded(
        client, tenant.workspace.id, filename="a.pdf", data=pdf_bytes(1), content_type=""
    )["document"]["id"]
    response = client.get(f"{API}/{doc_id}/download", params={"inline": "true"})
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith('inline; filename="a.pdf"')
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert response.headers["content-type"] == "application/pdf"


def test_inline_text_is_served_as_plain_text(login_as, tenant):
    client = login_as(tenant.editor, tenant.org)
    data = b"# Notes\n<script>alert(document.cookie)</script>\n"
    doc_id = uploaded(
        client, tenant.workspace.id, filename="x.md", data=data, content_type="text/markdown"
    )["document"]["id"]
    response = client.get(f"{API}/{doc_id}/download", params={"inline": "true"})
    assert response.content == data
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["content-disposition"].startswith("inline;")
    assert (
        response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'self'"
    )
    assert response.headers["x-content-type-options"] == "nosniff"


def test_docx_is_never_inline(login_as, tenant):
    client = login_as(tenant.editor, tenant.org)
    doc_id = uploaded(
        client, tenant.workspace.id, filename="p.docx", data=docx_bytes(), content_type=DOCX_MIME
    )["document"]["id"]
    response = client.get(f"{API}/{doc_id}/download", params={"inline": "true"})
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["content-type"] == DOCX_MIME


def test_missing_stored_file_is_503_not_a_truncated_body(login_as, factory, tenant):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.editor)  # no stored file
    response = login_as(tenant.editor, tenant.org).get(f"{API}/{doc.id}/download")
    assert response.status_code == 503
    assert error_code(response) == "storage_unavailable"


def test_s3_downloads_redirect_to_a_presigned_url(app, login_as, tenant, monkeypatch, tmp_path):
    use_fake_aws_credentials(monkeypatch, tmp_path)
    with mock_aws():
        storage = S3Storage(s3_settings())
        storage.client.create_bucket(Bucket=BUCKET)
        app.dependency_overrides[get_storage] = lambda: storage
        client = login_as(tenant.editor, tenant.org)
        data = b"Stored in S3"
        doc_id = uploaded(client, tenant.workspace.id, filename="s3 notes.txt", data=data)[
            "document"
        ]["id"]

        response = client.get(
            f"{API}/{doc_id}/download", params={"inline": "true"}, follow_redirects=False
        )
        assert response.status_code == 307
        location = urlsplit(response.headers["location"])
        query = parse_qs(location.query)
        assert query["response-content-type"] == ["text/plain; charset=utf-8"]
        assert query["response-content-disposition"] == [
            "inline; filename=\"s3 notes.txt\"; filename*=UTF-8''s3%20notes.txt"
        ]
        key = location.path.split(f"/{BUCKET}/", 1)[-1].lstrip("/")
        assert storage.get_bytes(key) == data
        head = storage.client.head_object(Bucket=BUCKET, Key=key)
        assert head["ServerSideEncryption"] == "AES256"


# --- Chunks --------------------------------------------------------------------------------


def test_list_chunks_in_order(login_as, factory, tenant):
    doc = stored_document(factory, tenant.workspace, uploaded_by=tenant.editor)
    factory.chunks(
        doc,
        [
            {"content": "Intro text", "page_number": 1, "section_title": "Introduction"},
            {"content": "Body text", "page_number": 2, "page_end": 3},
            "Closing text",
        ],
    )
    client = login_as(tenant.viewer, tenant.org)
    first = client.get(f"{API}/{doc.id}/chunks", params={"page_size": 2}).json()
    assert first["total"] == 3
    assert [(c["chunk_index"], c["content"]) for c in first["items"]] == [
        (0, "Intro text"),
        (1, "Body text"),
    ]
    assert first["items"][0]["section_title"] == "Introduction"
    assert (first["items"][1]["page_number"], first["items"][1]["page_end"]) == (2, 3)
    assert "embedding" not in first["items"][0]
    second = client.get(f"{API}/{doc.id}/chunks", params={"page": 2, "page_size": 2}).json()
    assert [c["content"] for c in second["items"]] == ["Closing text"]


# --- End to end (needs the ingestion runner) -----------------------------------------------


def test_upload_then_ingest_end_to_end(login_as, tenant, run_ingestion):
    client = login_as(tenant.editor, tenant.org)
    text = b"# VPN\n\nConnect with the corporate VPN before using internal tools.\n"
    doc_id = uploaded(client, tenant.workspace.id, filename="vpn.md", data=text)["document"]["id"]
    run_ingestion()
    status = client.get(f"{API}/{doc_id}/status").json()
    assert status["processing_status"] == "indexed", status
    assert status["chunk_count"] >= 1
    chunks = client.get(f"{API}/{doc_id}/chunks").json()["items"]
    assert any("corporate VPN" in chunk["content"] for chunk in chunks)
