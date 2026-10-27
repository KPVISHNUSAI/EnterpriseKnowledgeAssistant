"""Answer receipts: the version of each document whose text is searchable, and when that
version went live. `documents.version` counts uploads; the previous version stays
searchable until the new one is indexed, so answers are pinned to `indexed_version`.

Existing documents are backfilled from their succeeded ingestion jobs, or, when retention
already removed those, from the document itself if it is indexed with no newer upload
pending. Documents mid-replacement whose jobs are gone stay NULL (version unknown) until
the next indexing.

Stop the ingestion workers before running it: a worker of the previous release that
indexes during the rollout swaps in new text without recording its version
(docs/DEPLOYMENT_AWS.md).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("indexed_version", sa.Integer(), nullable=True))
    op.add_column(
        "documents", sa.Column("version_indexed_at", sa.DateTime(timezone=True), nullable=True)
    )
    # The schema owner is subject to FORCE ROW LEVEL SECURITY and has no tenant context,
    # so it would update nothing. Lifted only inside this migration's transaction.
    for table in ("documents", "ingestion_jobs"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE documents d
        SET indexed_version = j.version,
            version_indexed_at = (
                SELECT min(completed_at) FROM ingestion_jobs
                WHERE document_id = d.id AND document_version = j.version
                  AND status = 'succeeded'
            )
        FROM (
            SELECT document_id, max(document_version) AS version FROM ingestion_jobs
            WHERE status = 'succeeded' GROUP BY document_id
        ) j
        WHERE j.document_id = d.id AND d.indexed_at IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE documents SET indexed_version = version, version_indexed_at = indexed_at
        WHERE indexed_version IS NULL AND processing_status = 'indexed'
        """
    )
    for table in ("documents", "ingestion_jobs"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_column("documents", "version_indexed_at")
    op.drop_column("documents", "indexed_version")
