"""Document lifecycle: upload, listing, versions, deletion, grants and downloads."""

from __future__ import annotations

import uuid

from fastapi import Response, UploadFile
from sqlalchemy.orm import Session

from app.core.filetypes import FileType
from app.models import DocumentStatus
from app.schemas.common import Page, PageParams
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
from app.services.access import Principal
from app.services.audit import RequestMeta
from app.storage import ObjectStorage


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
    raise NotImplementedError


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
    raise NotImplementedError


def get_document(db: Session, principal: Principal, document_id: uuid.UUID) -> DocumentDetail:
    raise NotImplementedError


def update_document(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: DocumentUpdate,
    *,
    meta: RequestMeta,
) -> DocumentOut:
    raise NotImplementedError


def delete_document(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    storage: ObjectStorage,
    meta: RequestMeta,
) -> None:
    raise NotImplementedError


def reindex_document(
    db: Session, principal: Principal, document_id: uuid.UUID, *, meta: RequestMeta
) -> DocumentStatusOut:
    raise NotImplementedError


def replace_document_file(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    file: UploadFile,
    storage: ObjectStorage,
    meta: RequestMeta,
) -> DocumentUploadResponse:
    raise NotImplementedError


def get_document_status(
    db: Session, principal: Principal, document_id: uuid.UUID
) -> DocumentStatusOut:
    raise NotImplementedError


def download_document(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    inline: bool,
    storage: ObjectStorage,
    meta: RequestMeta,
) -> Response:
    raise NotImplementedError


def list_chunks(
    db: Session, principal: Principal, document_id: uuid.UUID, *, page: PageParams
) -> Page[ChunkOut]:
    raise NotImplementedError


def update_permissions(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: DocumentPermissionsUpdate,
    *,
    meta: RequestMeta,
) -> DocumentDetail:
    raise NotImplementedError
