from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal, get_request_meta, rate_limit_user
from app.core.filetypes import FileType
from app.models import DocumentStatus
from app.schemas.common import Page, PageParams, errors
from app.schemas.documents import (
    ChunkOut,
    DocumentDetail,
    DocumentOut,
    DocumentPermissionsUpdate,
    DocumentSort,
    DocumentStatusOut,
    DocumentUpdate,
    DocumentUploadResponse,
    DocumentVisibility,
    SortOrder,
)
from app.services import documents as doc_service
from app.services.access import Principal
from app.services.audit import RequestMeta
from app.storage import ObjectStorage, get_storage

router = APIRouter(prefix="/documents", tags=["documents"])

_upload_limit = rate_limit_user("upload", "rate_limit_upload_per_minute")


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=201,
    responses=errors(401, 403, 404, 409, 413, 415, 422, 429),
    dependencies=[_upload_limit],
)
def upload_document(
    file: UploadFile = File(..., description="PDF, DOCX, TXT or Markdown"),
    workspace_id: uuid.UUID = Form(...),
    title: str | None = Form(None, max_length=500, description="Defaults to the filename"),
    visibility: DocumentVisibility = Form(DocumentVisibility.WORKSPACE),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
    meta: RequestMeta = Depends(get_request_meta),
) -> DocumentUploadResponse:
    """Upload a document into a workspace (workspace owners/editors and org admins).

    The file is validated (extension, declared type and content signature must agree;
    size within MAX_UPLOAD_MB), stored, and queued for ingestion. 409 with
    `details.document_id` when identical content already exists in the workspace.
    """
    return doc_service.upload_document(
        db,
        principal,
        file=file,
        workspace_id=workspace_id,
        title=title,
        visibility=visibility,
        storage=storage,
        meta=meta,
    )


@router.get("", response_model=Page[DocumentOut], responses=errors(401, 422))
def list_documents(
    workspace_id: list[uuid.UUID] | None = Query(None),
    status: list[DocumentStatus] | None = Query(None),
    file_type: list[FileType] | None = Query(None),
    q: str | None = Query(None, max_length=200, description="Match title or filename"),
    uploaded_by: uuid.UUID | None = Query(None),
    sort: DocumentSort = Query(DocumentSort.CREATED_AT),
    order: SortOrder = Query(SortOrder.DESC),
    page: PageParams = Depends(),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Page[DocumentOut]:
    """Documents the caller can read, filtered and sorted."""
    return doc_service.list_documents(
        db,
        principal,
        workspace_ids=workspace_id,
        statuses=status,
        file_types=file_type,
        q=q,
        uploaded_by=uploaded_by,
        sort=sort,
        order=order,
        page=page,
    )


@router.get("/{document_id}", response_model=DocumentDetail, responses=errors(401, 404))
def get_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> DocumentDetail:
    return doc_service.get_document(db, principal, document_id)


@router.patch("/{document_id}", response_model=DocumentOut, responses=errors(401, 403, 404, 422))
def update_document(
    document_id: uuid.UUID,
    body: DocumentUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> DocumentOut:
    return doc_service.update_document(db, principal, document_id, body, meta=meta)


@router.delete("/{document_id}", status_code=204, responses=errors(401, 403, 404))
def delete_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    """Delete the document, its chunks/embeddings, jobs, grants and stored file."""
    doc_service.delete_document(db, principal, document_id, storage=storage, meta=meta)


@router.post(
    "/{document_id}/reindex",
    response_model=DocumentStatusOut,
    status_code=202,
    responses=errors(401, 403, 404, 409),
)
def reindex_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> DocumentStatusOut:
    """Queue re-ingestion (also used to retry a failed ingestion). 409 if a job is
    already queued or running."""
    return doc_service.reindex_document(db, principal, document_id, meta=meta)


@router.put(
    "/{document_id}/file",
    response_model=DocumentUploadResponse,
    status_code=202,
    responses=errors(401, 403, 404, 409, 413, 415, 422, 429),
    dependencies=[_upload_limit],
)
def replace_document_file(
    document_id: uuid.UUID,
    file: UploadFile = File(...),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
    meta: RequestMeta = Depends(get_request_meta),
) -> DocumentUploadResponse:
    """Upload a new version. The previous chunks stay searchable until the new version
    is indexed, then are swapped atomically."""
    return doc_service.replace_document_file(
        db, principal, document_id, file=file, storage=storage, meta=meta
    )


@router.get("/{document_id}/status", response_model=DocumentStatusOut, responses=errors(401, 404))
def get_document_status(
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> DocumentStatusOut:
    return doc_service.get_document_status(db, principal, document_id)


@router.get(
    "/{document_id}/download",
    response_class=Response,
    responses={
        200: {"description": "The original file", "content": {"application/octet-stream": {}}},
        307: {"description": "Redirect to a short-lived presigned S3 URL"},
        **errors(401, 404),
    },
)
def download_document(
    document_id: uuid.UUID,
    inline: bool = Query(False, description="Content-Disposition inline (PDF preview)"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
    meta: RequestMeta = Depends(get_request_meta),
) -> Response:
    return doc_service.download_document(
        db, principal, document_id, inline=inline, storage=storage, meta=meta
    )


@router.get("/{document_id}/chunks", response_model=Page[ChunkOut], responses=errors(401, 404))
def list_document_chunks(
    document_id: uuid.UUID,
    page: PageParams = Depends(),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Page[ChunkOut]:
    """Extracted text of the indexed version, in order, with page/section metadata."""
    return doc_service.list_chunks(db, principal, document_id, page=page)


@router.put(
    "/{document_id}/permissions",
    response_model=DocumentDetail,
    responses=errors(401, 403, 404, 422),
)
def update_document_permissions(
    document_id: uuid.UUID,
    body: DocumentPermissionsUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> DocumentDetail:
    """Replace the document's grants: workspace-wide visibility or restricted to the
    listed users (who must be organization members)."""
    return doc_service.update_permissions(db, principal, document_id, body, meta=meta)
