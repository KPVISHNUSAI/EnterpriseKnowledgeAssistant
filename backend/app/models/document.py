from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, Timestamps, UUIDPrimaryKey, str_enum


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


class PrincipalType(StrEnum):
    USER = "user"
    WORKSPACE = "workspace"


class PermissionLevel(StrEnum):
    READ = "read"
    MANAGE = "manage"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class Document(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("workspace_id", "content_hash"),
        ForeignKeyConstraint(
            ["workspace_id", "organization_id"],
            ["workspaces.id", "workspaces.organization_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("file_size >= 0", name="file_size_non_negative"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index(
            "ix_documents_org_workspace_status",
            "organization_id",
            "workspace_id",
            "processing_status",
        ),
        Index("ix_documents_org_created", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID]
    workspace_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(255))
    # Generated server-side from ids; never derived from user input.
    storage_key: Mapped[str] = mapped_column(String(1024))
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex of the original file
    mime_type: Mapped[str] = mapped_column(String(255))
    file_size: Mapped[int] = mapped_column(BigInteger)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    processing_status: Mapped[DocumentStatus] = mapped_column(
        str_enum(DocumentStatus, "document_status"), default=DocumentStatus.QUEUED
    )
    processing_error: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1)
    page_count: Mapped[int | None]
    chunk_count: Mapped[int] = mapped_column(default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    indexed_at: Mapped[datetime | None]


class DocumentPermission(UUIDPrimaryKey, CreatedAt, Base):
    """Read/manage grants. Workspace-wide visibility is itself a grant
    (principal_type=workspace, principal_id=<workspace id>, permission=read)."""

    __tablename__ = "document_permissions"
    __table_args__ = (
        UniqueConstraint("document_id", "principal_type", "principal_id"),
        ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "NOT (principal_type = 'workspace' AND permission = 'manage')",
            name="no_workspace_manage_grant",
        ),
        Index(
            "ix_document_permissions_principal",
            "organization_id",
            "principal_type",
            "principal_id",
        ),
    )

    document_id: Mapped[uuid.UUID]
    organization_id: Mapped[uuid.UUID]
    principal_type: Mapped[PrincipalType] = mapped_column(str_enum(PrincipalType, "principal_type"))
    principal_id: Mapped[uuid.UUID]
    permission: Mapped[PermissionLevel] = mapped_column(
        str_enum(PermissionLevel, "permission_level")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class DocumentChunk(UUIDPrimaryKey, CreatedAt, Base):
    """One retrievable passage. DDL (vector dimension, FTS config, indexes) is owned by
    the Alembic migrations; see docs/ARCHITECTURE.md#embedding-dimensions."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            ondelete="CASCADE",
        ),
        Index("ix_document_chunks_org_document", "organization_id", "document_id"),
    )
    # Bulk inserts should not round-trip the generated tsvector.
    __mapper_args__ = {"eager_defaults": False}

    document_id: Mapped[uuid.UUID]
    organization_id: Mapped[uuid.UUID]
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int]
    embedding: Mapped[Any] = mapped_column(Vector(), deferred=True)
    embedding_model: Mapped[str] = mapped_column(String(255))
    page_number: Mapped[int | None]
    page_end: Mapped[int | None]
    section_title: Mapped[str | None] = mapped_column(String(1000))
    heading_path: Mapped[str | None] = mapped_column(Text)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(heading_path, '')), 'A') || "
            "setweight(to_tsvector('english', content), 'B')",
            persisted=True,
        ),
        deferred=True,
    )


class IngestionJob(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            ondelete="CASCADE",
        ),
        # At most one queued/running job per document: duplicate processing is impossible.
        Index(
            "uq_ingestion_jobs_active_document",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_ingestion_jobs_status_run_after", "status", "run_after"),
        Index("ix_ingestion_jobs_document_created", "document_id", "created_at"),
    )

    document_id: Mapped[uuid.UUID]
    organization_id: Mapped[uuid.UUID]
    document_version: Mapped[int]
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int]
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime] = mapped_column(server_default=text("now()"))
    locked_until: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
