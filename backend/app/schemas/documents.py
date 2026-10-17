from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.filetypes import FileType
from app.models import DocumentStatus, JobStatus, PermissionLevel, PrincipalType
from app.schemas.common import Schema, UserRef


class DocumentVisibility(StrEnum):
    WORKSPACE = "workspace"  # every member of the document's workspace can read it
    RESTRICTED = "restricted"  # only explicitly granted users (plus owners/admins)


class DocumentSort(StrEnum):
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    TITLE = "title"
    FILE_SIZE = "file_size"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class IngestionJobOut(Schema):
    id: uuid.UUID
    status: JobStatus
    document_version: int
    attempts: int
    max_attempts: int
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class DocumentOut(Schema):
    id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    title: str
    original_filename: str
    file_type: FileType
    mime_type: str
    file_size: int
    content_hash: str
    version: int
    processing_status: DocumentStatus
    processing_error: str | None = None
    page_count: int | None = None
    chunk_count: int
    visibility: DocumentVisibility
    uploaded_by: UserRef | None = None
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None = None
    can_manage: bool


class DocumentPermissionOut(BaseModel):
    principal_type: PrincipalType
    principal_id: uuid.UUID
    principal_name: str
    permission: PermissionLevel


class DocumentDetail(DocumentOut):
    latest_job: IngestionJobOut | None = None
    permissions: list[DocumentPermissionOut] | None = Field(
        default=None, description="Present only for callers who can manage the document."
    )


class DocumentUploadResponse(BaseModel):
    document: DocumentOut
    job: IngestionJobOut


class DocumentStatusOut(BaseModel):
    document_id: uuid.UUID
    processing_status: DocumentStatus
    processing_error: str | None = None
    version: int
    chunk_count: int
    indexed_at: datetime | None = None
    latest_job: IngestionJobOut | None = None


class DocumentUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=500)


class UserGrant(BaseModel):
    user_id: uuid.UUID
    permission: PermissionLevel = PermissionLevel.READ


class DocumentPermissionsUpdate(BaseModel):
    visibility: DocumentVisibility
    users: list[UserGrant] = Field(default_factory=list, max_length=500)


class ChunkOut(Schema):
    id: uuid.UUID
    chunk_index: int
    content: str
    page_number: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    heading_path: str | None = None
    token_count: int
