"""Text addressed to AI assistants (app/ingestion/instructions.py): how many chunks of a
document's indexed version carry flags, and whether a document manager included the
flagged text in answers.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("flagged_chunks", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "documents",
        sa.Column(
            "flagged_text_included", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "flagged_text_included")
    op.drop_column("documents", "flagged_chunks")
