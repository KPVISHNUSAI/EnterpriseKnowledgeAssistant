"""Permission Proof runs (app/proof): the leak test's history and reports.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import get_settings

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
TABLE = "permission_proof_runs"


def _one_of(column: str, values: Sequence[str], name: str) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=name)


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text(f"'{default}'::jsonb")
    )


def upgrade() -> None:
    app_role = get_settings().db_app_role  # validated identifier
    op.create_table(
        TABLE,
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requested_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("simulation", postgresql.JSONB()),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("verdict", sa.String(32)),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
        sa.Column("error", sa.String(500)),
        _jsonb("summary", "{}"),
        _jsonb("groups", "[]"),
        _jsonb("findings", "[]"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        _one_of("kind", ("manual", "scheduled", "simulation"), "proof_kind"),
        _one_of("status", ("queued", "running", "completed", "error"), "proof_status"),
        _one_of("verdict", ("passed", "failed", "nothing_to_test"), "proof_verdict"),
    )
    op.create_index(f"ix_{TABLE}_org_created", TABLE, ["organization_id", "created_at"])

    # Same tenant isolation as every table in 0001.
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {TABLE} "
        "USING (organization_id = app_current_org_id()) "
        "WITH CHECK (organization_id = app_current_org_id())"
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_role}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {app_role};
          ELSE
            RAISE NOTICE 'role % not found; skipping grants', '{app_role}';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_table(TABLE)
