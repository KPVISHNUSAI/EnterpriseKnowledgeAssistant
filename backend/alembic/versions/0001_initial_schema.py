"""Initial schema: tenancy, documents, chunks (pgvector + full-text), chat, audit, RLS.

The embedding dimension and full-text search configuration are read from settings
(EMBEDDING_DIMENSIONS, FTS_CONFIG) when this migration runs; they are baked into the
column type and generated column. Changing the embedding model to one with a different
dimension needs a new migration plus a full reindex (docs/ARCHITECTURE.md).

Revision ID: 0001
Revises:
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)

TENANT_TABLES = (
    "workspaces",
    "workspace_memberships",
    "documents",
    "document_permissions",
    "document_chunks",
    "ingestion_jobs",
    "conversations",
    "messages",
    "audit_events",
    "usage_records",
)


def _id() -> sa.Column:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created() -> sa.Column:
    return sa.Column("created_at", TS, nullable=False, server_default=sa.func.now())


def _updated() -> sa.Column:
    return sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now())


def _one_of(column: str, values: Sequence[str], name: str) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=name)


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text(f"'{default}'::jsonb")
    )


ORG_ROLES = ("org_admin", "knowledge_manager", "employee")


def upgrade() -> None:
    settings = get_settings()
    dims = settings.embedding_dimensions
    fts = settings.fts_config  # validated identifier
    app_role = settings.db_app_role  # validated identifier

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "organizations",
        _id(),
        sa.Column("name", sa.String(200), nullable=False),
        _created(),
        _updated(),
    )

    op.create_table(
        "users",
        _id(),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auth_provider", sa.String(32), nullable=False, server_default="local"),
        sa.Column("external_subject", sa.String(255)),
        sa.Column("last_login_at", TS),
        _created(),
        _updated(),
        sa.UniqueConstraint("auth_provider", "external_subject"),
    )

    op.create_table(
        "organization_memberships",
        _id(),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        _created(),
        sa.UniqueConstraint("organization_id", "user_id"),
        _one_of("role", ORG_ROLES, "org_role"),
    )
    op.create_index("ix_organization_memberships_user_id", "organization_memberships", ["user_id"])

    op.create_table(
        "invitations",
        _id(),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("invited_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("accepted_at", TS),
        sa.Column("accepted_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("revoked_at", TS),
        _created(),
        _one_of("role", ORG_ROLES, "invitation_role"),
    )
    op.create_index("ix_invitations_organization_id", "invitations", ["organization_id"])

    op.create_table(
        "user_sessions",
        _id(),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "active_organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="SET NULL")
        ),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
        _created(),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])

    op.create_table(
        "workspaces",
        _id(),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000)),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        _created(),
        _updated(),
        sa.UniqueConstraint("id", "organization_id"),
    )
    op.create_index(
        "uq_workspaces_org_lower_name",
        "workspaces",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "workspace_memberships",
        _id(),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        _created(),
        sa.UniqueConstraint("workspace_id", "user_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "organization_id"],
            ["workspaces.id", "workspaces.organization_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            ondelete="CASCADE",
        ),
        _one_of("role", ("owner", "editor", "viewer"), "workspace_role"),
    )
    op.create_index(
        "ix_workspace_memberships_org_user",
        "workspace_memberships",
        ["organization_id", "user_id"],
    )

    op.create_table(
        "documents",
        _id(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("processing_status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("processing_error", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("page_count", sa.Integer()),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_model", sa.String(255)),
        sa.Column("indexed_at", TS),
        _created(),
        _updated(),
        sa.UniqueConstraint("id", "organization_id"),
        sa.UniqueConstraint("workspace_id", "content_hash"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "organization_id"],
            ["workspaces.id", "workspaces.organization_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("file_size >= 0", name="file_size_non_negative"),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        _one_of(
            "processing_status", ("queued", "processing", "indexed", "failed"), "document_status"
        ),
    )
    op.create_index(
        "ix_documents_org_workspace_status",
        "documents",
        ["organization_id", "workspace_id", "processing_status"],
    )
    op.create_index("ix_documents_org_created", "documents", ["organization_id", "created_at"])

    op.create_table(
        "document_permissions",
        _id(),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("principal_type", sa.String(32), nullable=False),
        sa.Column("principal_id", UUID, nullable=False),
        sa.Column("permission", sa.String(32), nullable=False),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        _created(),
        sa.UniqueConstraint("document_id", "principal_type", "principal_id"),
        sa.ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "NOT (principal_type = 'workspace' AND permission = 'manage')",
            name="no_workspace_manage_grant",
        ),
        _one_of("principal_type", ("user", "workspace"), "principal_type"),
        _one_of("permission", ("read", "manage"), "permission_level"),
    )
    op.create_index(
        "ix_document_permissions_principal",
        "document_permissions",
        ["organization_id", "principal_type", "principal_id"],
    )

    op.create_table(
        "document_chunks",
        _id(),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(dims), nullable=False),
        sa.Column("embedding_model", sa.String(255), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("page_end", sa.Integer()),
        sa.Column("section_title", sa.String(1000)),
        sa.Column("heading_path", sa.Text()),
        _jsonb("metadata", "{}"),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                f"setweight(to_tsvector('{fts}', coalesce(heading_path, '')), 'A') || "
                f"setweight(to_tsvector('{fts}', content), 'B')",
                persisted=True,
            ),
        ),
        _created(),
        sa.UniqueConstraint("document_id", "chunk_index"),
        sa.ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_document_chunks_org_document", "document_chunks", ["organization_id", "document_id"]
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_search_vector ON document_chunks USING gin (search_vector)"
    )

    op.create_table(
        "ingestion_jobs",
        _id(),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("run_after", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("locked_until", TS),
        sa.Column("started_at", TS),
        sa.Column("completed_at", TS),
        _jsonb("metrics", "{}"),
        _created(),
        _updated(),
        sa.ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            ondelete="CASCADE",
        ),
        _one_of(
            "status", ("queued", "running", "succeeded", "failed", "cancelled"), "job_status"
        ),
    )
    op.create_index(
        "uq_ingestion_jobs_active_document",
        "ingestion_jobs",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_ingestion_jobs_status_run_after", "ingestion_jobs", ["status", "run_after"]
    )
    op.create_index(
        "ix_ingestion_jobs_document_created", "ingestion_jobs", ["document_id", "created_at"]
    )

    op.create_table(
        "conversations",
        _id(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        _created(),
        _updated(),
        sa.UniqueConstraint("id", "organization_id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_conversations_org_user_updated",
        "conversations",
        ["organization_id", "user_id", "updated_at"],
    )

    op.create_table(
        "messages",
        _id(),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="complete"),
        _jsonb("sources", "[]"),
        _jsonb("retrieval_metadata", "{}"),
        _created(),
        _updated(),
        sa.ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
        ),
        _one_of("role", ("user", "assistant"), "message_role"),
        _one_of("status", ("complete", "streaming", "incomplete", "error"), "message_status"),
    )
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )
    op.create_index(
        "ix_messages_sources",
        "messages",
        ["sources"],
        postgresql_using="gin",
        postgresql_ops={"sources": "jsonb_path_ops"},
    )

    op.create_table(
        "audit_events",
        _id(),
        sa.Column("organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("actor_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(50)),
        sa.Column("resource_id", sa.String(100)),
        _jsonb("metadata", "{}"),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("request_id", sa.String(64)),
        _created(),
    )
    op.create_index(
        "ix_audit_events_org_created", "audit_events", ["organization_id", "created_at"]
    )
    op.create_index("ix_audit_events_org_action", "audit_events", ["organization_id", "action"])

    op.create_table(
        "usage_records",
        _id(),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer()),
        _jsonb("timings", "{}"),
        sa.Column("message_id", UUID),
        _created(),
        _one_of("kind", ("chat_completion", "query_rewrite"), "usage_kind"),
    )
    op.create_index(
        "ix_usage_records_org_created", "usage_records", ["organization_id", "created_at"]
    )

    # --- Row-level security -----------------------------------------------------------
    # Transaction-scoped tenant context set by app/db/session.py. An unset context yields
    # NULL, which matches no rows.
    op.execute(
        "CREATE FUNCTION app_current_org_id() RETURNS uuid LANGUAGE sql STABLE PARALLEL SAFE "
        "AS $$ SELECT NULLIF(current_setting('app.current_org_id', true), '')::uuid $$"
    )
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # Pre-login audit events (e.g. failed logins) have no tenant; they can be written
        # but are never readable through the app role.
        check = (
            "organization_id IS NULL OR organization_id = app_current_org_id()"
            if table == "audit_events"
            else "organization_id = app_current_org_id()"
        )
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (organization_id = app_current_org_id()) WITH CHECK ({check})"
        )

    # --- Least-privilege grants for the runtime role -------------------------------------
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_role}') THEN
            GRANT USAGE ON SCHEMA public TO {app_role};
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_role};
            REVOKE UPDATE ON audit_events FROM {app_role};
            REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {app_role};
            GRANT EXECUTE ON FUNCTION app_current_org_id() TO {app_role};
          ELSE
            RAISE NOTICE 'role % not found; skipping grants', '{app_role}';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    for table in (
        "usage_records",
        "audit_events",
        "messages",
        "conversations",
        "ingestion_jobs",
        "document_chunks",
        "document_permissions",
        "documents",
        "workspace_memberships",
        "workspaces",
        "user_sessions",
        "invitations",
        "organization_memberships",
        "users",
        "organizations",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS app_current_org_id()")
