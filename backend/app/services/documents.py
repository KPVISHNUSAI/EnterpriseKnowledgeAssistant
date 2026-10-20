"""Document lifecycle: upload, listing, versions, deletion, grants and downloads."""

from __future__ import annotations

import itertools
import logging
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi import Response, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only

from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.core.filetypes import FILE_TYPE_BY_MIME, MIME_TYPES, FileType
from app.db.base import utcnow
from app.ingestion.chunking import flagged_pages
from app.models import (
    ACTIVE_JOB_STATUSES,
    Document,
    DocumentChunk,
    DocumentPermission,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    Message,
    OrganizationMembership,
    PermissionLevel,
    PrincipalType,
    User,
    Workspace,
)
from app.providers.embeddings import cache
from app.schemas.common import Page, PageParams, UserRef
from app.schemas.documents import (
    ChunkOut,
    DocumentDetail,
    DocumentOut,
    DocumentPermissionOut,
    DocumentPermissionsUpdate,
    DocumentSort,
    DocumentStatusOut,
    DocumentUpdate,
    DocumentUploadResponse,
    DocumentVisibility,
    FlaggedTextUpdate,
    IngestionJobOut,
    SortOrder,
)
from app.services import access, audit, uploads
from app.services.access import Principal
from app.services.audit import AuditAction, RequestMeta
from app.services.conversations import HISTORY_DOCUMENTS, without_derived
from app.storage import ObjectStorage, StorageError, build_storage_key
from app.storage.s3 import content_disposition
from app.worker import queue

logger = logging.getLogger(__name__)

_UNIQUE_CONTENT = "uq_documents_workspace_id_content_hash"
_ACTIVE_JOB_INDEX = "uq_ingestion_jobs_active_document"
_DELETED_TITLE = "Deleted document"
_TEXT_TYPES = frozenset({FileType.TXT, FileType.MD})
_INLINE_TYPES = _TEXT_TYPES | {FileType.PDF}
_SORT_COLUMNS: dict[DocumentSort, Any] = {
    DocumentSort.CREATED_AT: Document.created_at,
    DocumentSort.UPDATED_AT: Document.updated_at,
    DocumentSort.TITLE: func.lower(Document.title),
    DocumentSort.FILE_SIZE: Document.file_size,
}
# DocumentOut fields that are not plain Document columns.
_COMPUTED_FIELDS = frozenset(
    {"workspace_name", "file_type", "visibility", "uploaded_by", "can_manage"}
)


# --- Output helpers ------------------------------------------------------------------------


def _document_select(principal: Principal) -> Select[Document, str, User, bool, bool]:
    """Documents with everything DocumentOut needs, in one query (no N+1)."""
    workspace_visible = exists().where(
        DocumentPermission.document_id == Document.id,
        DocumentPermission.organization_id == Document.organization_id,
        DocumentPermission.principal_type == PrincipalType.WORKSPACE,
        DocumentPermission.principal_id == Document.workspace_id,
    )
    return (
        select(
            Document,
            Workspace.name,
            User,
            workspace_visible,
            access.manageable_documents_clause(principal),
        )
        .join(
            Workspace,
            and_(
                Workspace.id == Document.workspace_id,
                Workspace.organization_id == Document.organization_id,
            ),
        )
        .outerjoin(User, User.id == Document.uploaded_by)
    )


def _document_out(row: Any) -> DocumentOut:
    doc, workspace_name, uploader, workspace_visible, can_manage = row
    columns = {
        name: getattr(doc, name)
        for name in DocumentOut.model_fields
        if name not in _COMPUTED_FIELDS
    }
    return DocumentOut(
        **columns,
        workspace_name=workspace_name,
        file_type=FILE_TYPE_BY_MIME[doc.mime_type],
        visibility=(
            DocumentVisibility.WORKSPACE if workspace_visible else DocumentVisibility.RESTRICTED
        ),
        uploaded_by=UserRef.model_validate(uploader) if uploader else None,
        can_manage=bool(can_manage),
    )


def _get_out(db: Session, principal: Principal, document_id: uuid.UUID) -> DocumentOut:
    """DocumentOut for a document the caller was already authorized for."""
    return _document_out(
        db.execute(_document_select(principal).where(Document.id == document_id)).one()
    )


def _latest_job(db: Session, document_id: uuid.UUID) -> IngestionJobOut | None:
    job = db.scalar(
        select(IngestionJob)
        .where(IngestionJob.document_id == document_id)
        .order_by(IngestionJob.created_at.desc())
        .limit(1)
    )
    return IngestionJobOut.model_validate(job) if job else None


def _permissions(db: Session, document_id: uuid.UUID) -> list[DocumentPermissionOut]:
    rows = db.execute(
        select(DocumentPermission, User.full_name, Workspace.name)
        .outerjoin(
            User,
            and_(
                DocumentPermission.principal_type == PrincipalType.USER,
                User.id == DocumentPermission.principal_id,
            ),
        )
        .outerjoin(
            Workspace,
            and_(
                DocumentPermission.principal_type == PrincipalType.WORKSPACE,
                Workspace.id == DocumentPermission.principal_id,
            ),
        )
        .where(DocumentPermission.document_id == document_id)
    )
    grants = [
        DocumentPermissionOut(
            principal_type=grant.principal_type,
            principal_id=grant.principal_id,
            principal_name=user_name or workspace_name or "Unknown",
            permission=grant.permission,
        )
        for grant, user_name, workspace_name in rows
    ]
    # The workspace grant first, then people by name.
    return sorted(
        grants,
        key=lambda g: (g.principal_type != PrincipalType.WORKSPACE, g.principal_name.lower()),
    )


def _flagged_pages(db: Session, document_id: uuid.UUID) -> list[int]:
    rows = db.execute(
        select(DocumentChunk.chunk_metadata, DocumentChunk.page_number).where(
            DocumentChunk.document_id == document_id,
            or_(
                DocumentChunk.chunk_metadata.has_key("flags"),
                DocumentChunk.chunk_metadata.has_key("section_flag"),
            ),
        )
    )
    return flagged_pages((metadata, page) for metadata, page in rows)


def _detail(db: Session, principal: Principal, document_id: uuid.UUID) -> DocumentDetail:
    out = _get_out(db, principal, document_id)
    return DocumentDetail(
        **dict(out),
        latest_job=_latest_job(db, document_id),
        permissions=_permissions(db, document_id) if out.can_manage else None,
        flagged_pages=_flagged_pages(db, document_id) if out.flagged_chunks else [],
    )


def _status_out(doc: Document, latest_job: IngestionJobOut | None) -> DocumentStatusOut:
    return DocumentStatusOut(
        document_id=doc.id,
        processing_status=doc.processing_status,
        processing_error=doc.processing_error,
        version=doc.version,
        chunk_count=doc.chunk_count,
        indexed_at=doc.indexed_at,
        latest_job=latest_job,
    )


# --- Storage and conflict helpers ----------------------------------------------------------


def _storage_unavailable(operation: str, document_id: uuid.UUID) -> ServiceUnavailableError:
    logger.warning(
        "documents.storage_failed",
        extra={"operation": operation, "document_id": str(document_id)},
        exc_info=True,
    )
    return ServiceUnavailableError(
        "File storage is temporarily unavailable, please retry", code="storage_unavailable"
    )


def _store(
    storage: ObjectStorage, key: str, upload: uploads.ValidatedUpload, document_id: uuid.UUID
) -> None:
    try:
        storage.put(key, upload.file, content_type=upload.mime_type)
    except StorageError:
        raise _storage_unavailable("put", document_id) from None


def _open_stream(storage: ObjectStorage, key: str) -> Iterator[bytes]:
    """Read the first chunk now, so a missing object becomes an error response instead
    of a response whose body is cut off."""
    chunks = storage.iter_bytes(key)
    return itertools.chain([next(chunks, b"")], chunks)


def _delete_object(storage: ObjectStorage, key: str) -> None:
    """Best effort: an orphaned object is harmless, failing the request is not."""
    try:
        storage.delete(key)
    except StorageError:
        logger.warning("documents.storage_delete_failed", extra={"storage_key": key}, exc_info=True)


def _violated_constraint(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


def _duplicate(
    db: Session, principal: Principal, workspace_id: uuid.UUID, sha256: str
) -> ConflictError | None:
    """409 for identical content in the workspace. The existing document's id is only
    disclosed to callers who can read it."""
    existing = db.scalar(
        select(Document.id).where(
            Document.workspace_id == workspace_id, Document.content_hash == sha256
        )
    )
    if existing is None:
        return None
    readable = access.readable_document_ids(db, principal, [existing])
    return ConflictError(
        "A document with identical content already exists in this workspace",
        code="duplicate_document",
        details={"document_id": str(existing)} if readable else None,
    )


def _commit_new_file(
    db: Session,
    principal: Principal,
    *,
    storage: ObjectStorage,
    key: str,
    workspace_id: uuid.UUID,
    sha256: str,
) -> None:
    """Commit a transaction that references a freshly stored object; on failure the
    object is removed again."""
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        _delete_object(storage, key)
        if isinstance(exc, IntegrityError) and _violated_constraint(exc) == _UNIQUE_CONTENT:
            # Lost a race with an identical upload.
            raise _duplicate(db, principal, workspace_id, sha256) or ConflictError(
                code="duplicate_document"
            ) from None
        raise


def _new_job(doc: Document) -> IngestionJob:
    return IngestionJob(
        document_id=doc.id,
        organization_id=doc.organization_id,
        document_version=doc.version,
        max_attempts=get_settings().ingestion_max_attempts,
    )


def _grant(
    doc: Document,
    principal_type: PrincipalType,
    principal_id: uuid.UUID,
    permission: PermissionLevel,
    created_by: uuid.UUID,
) -> DocumentPermission:
    return DocumentPermission(
        document_id=doc.id,
        organization_id=doc.organization_id,
        principal_type=principal_type,
        principal_id=principal_id,
        permission=permission,
        created_by=created_by,
    )


def _workspace_grant(doc: Document, created_by: uuid.UUID) -> DocumentPermission:
    return _grant(doc, PrincipalType.WORKSPACE, doc.workspace_id, PermissionLevel.READ, created_by)


# --- Endpoints -----------------------------------------------------------------------------


def upload_document(
    db: Session,
    principal: Principal,
    *,
    file: UploadFile,
    workspace_id: uuid.UUID,
    title: str | None,
    visibility: DocumentVisibility,
    storage: ObjectStorage,
    meta: RequestMeta,
) -> DocumentUploadResponse:
    settings = get_settings()
    # Org admins "can read" any id, so the workspace must also exist in this organization.
    if not access.can_read_workspace(principal, workspace_id) or not db.scalar(
        select(
            exists().where(
                Workspace.id == workspace_id,
                Workspace.organization_id == principal.organization_id,
            )
        )
    ):
        raise NotFoundError("Workspace not found")
    access.require(
        access.can_upload_to_workspace(principal, workspace_id),
        "You do not have permission to upload to this workspace",
    )
    # Return the pooled connection while the file is validated and stored (seconds for
    # large files); the next statement starts a fresh tenant-scoped transaction.
    db.rollback()

    with uploads.validated_upload(file) as upload:
        if conflict := _duplicate(db, principal, workspace_id, upload.sha256):
            raise conflict
        default_title = upload.filename.rsplit(".", 1)[0].strip() or upload.filename
        doc = Document(
            id=uuid.uuid4(),
            organization_id=principal.organization_id,
            workspace_id=workspace_id,
            title=uploads.clean_text(title or "") or default_title,
            original_filename=upload.filename,
            content_hash=upload.sha256,
            mime_type=upload.mime_type,
            file_size=upload.size,
            uploaded_by=principal.user_id,
            processing_status=DocumentStatus.QUEUED,
            version=1,
        )
        doc.storage_key = build_storage_key(
            settings.s3_prefix, doc.organization_id, doc.id, doc.version, upload.extension
        )
        _store(storage, doc.storage_key, upload, doc.id)

    job = _new_job(doc)
    db.add_all([doc, job])
    if visibility == DocumentVisibility.WORKSPACE:
        db.add(_workspace_grant(doc, principal.user_id))
    audit.record(
        db,
        AuditAction.DOCUMENT_UPLOADED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="document",
        resource_id=doc.id,
        metadata={
            "title": doc.title,
            "workspace_id": str(workspace_id),
            "file_type": upload.file_type.value,
            "file_size": upload.size,
            "visibility": visibility.value,
        },
        meta=meta,
    )
    _commit_new_file(
        db,
        principal,
        storage=storage,
        key=doc.storage_key,
        workspace_id=workspace_id,
        sha256=doc.content_hash,
    )
    queue.enqueue_ingestion(job.id, principal.organization_id)
    return DocumentUploadResponse(
        document=_get_out(db, principal, doc.id), job=IngestionJobOut.model_validate(job)
    )


def list_documents(
    db: Session,
    principal: Principal,
    *,
    workspace_ids: list[uuid.UUID] | None,
    statuses: list[DocumentStatus] | None,
    file_types: list[FileType] | None,
    q: str | None,
    uploaded_by: uuid.UUID | None,
    sort: DocumentSort,
    order: SortOrder,
    page: PageParams,
) -> Page[DocumentOut]:
    conditions = [access.readable_documents_clause(principal)]
    if workspace_ids:
        conditions.append(Document.workspace_id.in_(workspace_ids))
    if statuses:
        conditions.append(Document.processing_status.in_(statuses))
    if file_types:
        conditions.append(Document.mime_type.in_([MIME_TYPES[ft] for ft in file_types]))
    if term := uploads.clean_text(q or ""):
        conditions.append(
            or_(
                Document.title.icontains(term, autoescape=True),
                Document.original_filename.icontains(term, autoescape=True),
            )
        )
    if uploaded_by is not None:
        conditions.append(Document.uploaded_by == uploaded_by)

    total = db.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0
    column = _SORT_COLUMNS[sort]
    rows = db.execute(
        _document_select(principal)
        .where(*conditions)
        .order_by(column.asc() if order == SortOrder.ASC else column.desc(), Document.id)
        .offset(page.offset)
        .limit(page.page_size)
    )
    return Page(
        items=[_document_out(row) for row in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


def get_document(db: Session, principal: Principal, document_id: uuid.UUID) -> DocumentDetail:
    doc = access.get_readable_document(db, principal, document_id)
    return _detail(db, principal, doc.id)


def update_document(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: DocumentUpdate,
    *,
    meta: RequestMeta,
) -> DocumentOut:
    doc = access.get_manageable_document(db, principal, document_id, for_update=True)
    title = uploads.clean_text(body.title)
    if not title:
        raise ValidationFailedError("The title must not be empty", code="invalid_title")
    doc.title = title
    audit.record(
        db,
        AuditAction.DOCUMENT_UPDATED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="document",
        resource_id=doc.id,
        metadata={"title": title},
        meta=meta,
    )
    db.commit()
    return _get_out(db, principal, doc.id)


def _scrub(items: Any, document_ref: str, redacted: dict[str, Any]) -> Any:
    if not isinstance(items, list):
        return items
    return [
        {**item, **redacted}
        if isinstance(item, dict) and item.get("document_id") == document_ref
        else item
        for item in items
    ]


def _scrub_conversation_history(db: Session, document_id: uuid.UUID) -> None:
    """Remove a deleted document's text from stored answers: from their sources and
    citations (section titles are document text too), and the answer text itself of every
    answer that drew on it, directly or through the history in its prompt. The document id
    stays in the sources, so reads keep withholding those answers (conversations._redact).

    ponytail: loads matching messages into Python, and only the sources test has a GIN
    index (the history test scans the organization's messages); move to jsonb UPDATEs and
    index retrieval_metadata if a single document is ever behind very many messages."""
    document_ref = str(document_id)
    messages = db.scalars(
        select(Message)
        .options(
            load_only(Message.id, Message.content, Message.sources, Message.retrieval_metadata)
        )
        .where(
            or_(
                Message.sources.contains([{"document_id": document_ref}]),
                Message.retrieval_metadata.contains({HISTORY_DOCUMENTS: [document_ref]}),
            )
        )
    )
    for message in messages:
        message.content = ""
        message.sources = _scrub(
            message.sources,
            document_ref,
            {
                "excerpt": None,
                "redacted": True,
                "document_title": _DELETED_TITLE,
                "section_title": None,
            },
        )
        metadata = message.retrieval_metadata
        if isinstance(metadata, dict):
            metadata = without_derived(metadata)
            if "citations" in metadata:
                metadata["citations"] = _scrub(
                    metadata["citations"],
                    document_ref,
                    {"excerpt": None, "document_title": _DELETED_TITLE, "section_title": None},
                )
            message.retrieval_metadata = metadata


def delete_document(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    storage: ObjectStorage,
    meta: RequestMeta,
) -> None:
    doc = access.get_manageable_document(db, principal, document_id, for_update=True)
    content_hashes = list(
        db.scalars(
            select(DocumentChunk.content_hash).where(DocumentChunk.document_id == doc.id).distinct()
        )
    )
    db.delete(doc)  # foreign keys cascade to chunks/embeddings, jobs and grants
    _scrub_conversation_history(db, doc.id)
    audit.record(
        db,
        AuditAction.DOCUMENT_DELETED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="document",
        resource_id=doc.id,
        metadata={"title": doc.title, "workspace_id": str(doc.workspace_id)},
        meta=meta,
    )
    db.commit()
    _delete_object(storage, doc.storage_key)
    try:
        cache.invalidate(doc.organization_id, content_hashes)
    except Exception:  # the delete is committed; cached vectors expire on their own
        logger.warning(
            "documents.embedding_cache_invalidation_failed",
            extra={"document_id": str(doc.id)},
            exc_info=True,
        )


def reindex_document(
    db: Session, principal: Principal, document_id: uuid.UUID, *, meta: RequestMeta
) -> DocumentStatusOut:
    doc = access.get_manageable_document(db, principal, document_id, for_update=True)
    in_progress = ConflictError(
        "The document is already queued or being processed", code="ingestion_in_progress"
    )
    active = db.scalar(
        select(IngestionJob.id)
        .where(IngestionJob.document_id == doc.id, IngestionJob.status.in_(ACTIVE_JOB_STATUSES))
        .limit(1)
    )
    if active is not None:
        raise in_progress
    job = _new_job(doc)
    db.add(job)
    doc.processing_status = DocumentStatus.QUEUED
    doc.processing_error = None
    audit.record(
        db,
        AuditAction.DOCUMENT_REINDEX_REQUESTED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="document",
        resource_id=doc.id,
        metadata={"title": doc.title, "version": doc.version},
        meta=meta,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _violated_constraint(exc) == _ACTIVE_JOB_INDEX:
            raise in_progress from None
        raise
    queue.enqueue_ingestion(job.id, doc.organization_id)
    return _status_out(doc, IngestionJobOut.model_validate(job))


def replace_document_file(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    file: UploadFile,
    storage: ObjectStorage,
    meta: RequestMeta,
) -> DocumentUploadResponse:
    settings = get_settings()
    doc = access.get_manageable_document(db, principal, document_id, for_update=True)
    previous_key = doc.storage_key
    with uploads.validated_upload(file) as upload:
        if upload.sha256 == doc.content_hash:
            raise ConflictError(
                "The file is identical to the current version", code="unchanged_file"
            )
        if conflict := _duplicate(db, principal, doc.workspace_id, upload.sha256):
            raise conflict
        new_key = build_storage_key(
            settings.s3_prefix, doc.organization_id, doc.id, doc.version + 1, upload.extension
        )
        _store(storage, new_key, upload, doc.id)

    # The superseded job must stop counting as active before the new one exists.
    db.execute(
        update(IngestionJob)
        .where(IngestionJob.document_id == doc.id, IngestionJob.status.in_(ACTIVE_JOB_STATUSES))
        .values(
            status=JobStatus.CANCELLED,
            error_code="superseded",
            error_message="Superseded by a newer version of the document",
            completed_at=utcnow(),
            locked_until=None,
        )
    )
    # Old chunks stay searchable until the worker swaps in the new version.
    doc.version += 1
    doc.original_filename = upload.filename
    doc.mime_type = upload.mime_type
    doc.file_size = upload.size
    doc.content_hash = upload.sha256
    doc.storage_key = new_key
    doc.processing_status = DocumentStatus.QUEUED
    doc.processing_error = None
    job = _new_job(doc)
    db.add(job)
    audit.record(
        db,
        AuditAction.DOCUMENT_REPLACED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="document",
        resource_id=doc.id,
        metadata={
            "title": doc.title,
            "version": doc.version,
            "file_type": upload.file_type.value,
            "file_size": upload.size,
        },
        meta=meta,
    )
    _commit_new_file(
        db,
        principal,
        storage=storage,
        key=new_key,
        workspace_id=doc.workspace_id,
        sha256=doc.content_hash,
    )
    _delete_object(storage, previous_key)
    queue.enqueue_ingestion(job.id, doc.organization_id)
    return DocumentUploadResponse(
        document=_get_out(db, principal, doc.id), job=IngestionJobOut.model_validate(job)
    )


def get_document_status(
    db: Session, principal: Principal, document_id: uuid.UUID
) -> DocumentStatusOut:
    doc = access.get_readable_document(db, principal, document_id)
    return _status_out(doc, _latest_job(db, doc.id))


def download_document(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    inline: bool,
    storage: ObjectStorage,
    meta: RequestMeta,
) -> Response:
    doc = access.get_readable_document(db, principal, document_id)
    file_type = FILE_TYPE_BY_MIME[doc.mime_type]
    inline = inline and file_type in _INLINE_TYPES
    # Text is never served as HTML (or as anything a browser would render as markup).
    content_type = "text/plain; charset=utf-8" if file_type in _TEXT_TYPES else doc.mime_type
    try:
        url = storage.presigned_download_url(
            doc.storage_key,
            filename=doc.original_filename,
            content_type=content_type,
            inline=inline,
        )
        body = None if url else _open_stream(storage, doc.storage_key)
    except StorageError:
        raise _storage_unavailable("get", doc.id) from None

    audit.record(
        db,
        AuditAction.DOCUMENT_DOWNLOADED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="document",
        resource_id=doc.id,
        metadata={"title": doc.title, "version": doc.version, "inline": inline},
        meta=meta,
    )
    db.commit()
    if body is None:
        return RedirectResponse(str(url), status_code=307)
    headers = {
        "Content-Disposition": content_disposition(doc.original_filename, inline=inline),
        "Content-Length": str(doc.file_size),
        "X-Content-Type-Options": "nosniff",
    }
    if inline:
        # The PDF viewer must render inside the app's own iframe; text gets nothing else.
        headers["Content-Security-Policy"] = (
            "frame-ancestors 'self'"
            if file_type == FileType.PDF
            else "default-src 'none'; frame-ancestors 'self'"
        )
    return StreamingResponse(body, media_type=content_type, headers=headers)


def set_flagged_text(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: FlaggedTextUpdate,
    *,
    meta: RequestMeta,
) -> DocumentDetail:
    """Let a document's text addressed to AI assistants into answers, or keep it out
    again. Indexing a new version keeps it out until someone decides again."""
    doc = access.get_manageable_document(db, principal, document_id, for_update=True)
    if not doc.flagged_chunks:
        raise ConflictError(
            "The indexed version of this document has no flagged text", code="nothing_flagged"
        )
    if body.included and body.version != doc.version:
        raise ConflictError(
            "A newer version of this document was uploaded; review its flagged text first",
            code="version_changed",
        )
    if doc.flagged_text_included != body.included:
        doc.flagged_text_included = body.included
        audit.record(
            db,
            AuditAction.DOCUMENT_FLAGGED_TEXT_INCLUDED
            if body.included
            else AuditAction.DOCUMENT_FLAGGED_TEXT_EXCLUDED,
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            resource_type="document",
            resource_id=doc.id,
            metadata={"version": doc.version, "flagged_chunks": doc.flagged_chunks},
            meta=meta,
        )
    db.commit()
    return _detail(db, principal, doc.id)


def list_chunks(
    db: Session, principal: Principal, document_id: uuid.UUID, *, page: PageParams
) -> Page[ChunkOut]:
    doc = access.get_readable_document(db, principal, document_id)
    total = db.scalar(
        select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == doc.id)
    )
    chunks = db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == doc.id)
        .order_by(DocumentChunk.chunk_index)
        .offset(page.offset)
        .limit(page.page_size)
    )
    return Page(
        items=[ChunkOut.model_validate(chunk) for chunk in chunks],
        total=total or 0,
        page=page.page,
        page_size=page.page_size,
    )


def update_permissions(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: DocumentPermissionsUpdate,
    *,
    meta: RequestMeta,
) -> DocumentDetail:
    doc = access.get_manageable_document(db, principal, document_id, for_update=True)
    levels = {grant.user_id: grant.permission for grant in body.users}  # last one wins
    if levels:
        members = set(
            db.scalars(
                select(OrganizationMembership.user_id).where(
                    OrganizationMembership.organization_id == principal.organization_id,
                    OrganizationMembership.user_id.in_(list(levels)),
                )
            )
        )
        invalid = [str(user_id) for user_id in levels if user_id not in members]
        if invalid:
            raise ValidationFailedError(
                "Documents can only be shared with members of the organization",
                code="invalid_grantee",
                details={"user_ids": invalid},
            )

    db.execute(delete(DocumentPermission).where(DocumentPermission.document_id == doc.id))
    db.add_all(
        _grant(doc, PrincipalType.USER, user_id, level, principal.user_id)
        for user_id, level in levels.items()
    )
    if body.visibility == DocumentVisibility.WORKSPACE:
        db.add(_workspace_grant(doc, principal.user_id))
    audit.record(
        db,
        AuditAction.DOCUMENT_PERMISSIONS_CHANGED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="document",
        resource_id=doc.id,
        metadata={
            "visibility": body.visibility.value,
            "user_grants": len(levels),
            "manage_grants": sum(level == PermissionLevel.MANAGE for level in levels.values()),
        },
        meta=meta,
    )
    db.commit()
    # Built without the read check: the caller may just have removed their own access.
    return _detail(db, principal, doc.id)
