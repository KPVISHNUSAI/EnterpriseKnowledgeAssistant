from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, str_enum


class ProofKind(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    SIMULATION = "simulation"  # one person tested as if wrongly given a workspace


class ProofStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class ProofVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOTHING_TO_TEST = "nothing_to_test"  # no group has a document it cannot read


class PermissionProofRun(UUIDPrimaryKey, Timestamps, Base):
    """One leak test (app/proof). Reports hold ids and counts, never document text."""

    __tablename__ = "permission_proof_runs"
    __table_args__ = (
        Index("ix_permission_proof_runs_org_created", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    kind: Mapped[ProofKind] = mapped_column(str_enum(ProofKind, "proof_kind"))
    # Simulation only: {"user_id": ..., "workspace_id": ...}
    simulation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[ProofStatus] = mapped_column(
        str_enum(ProofStatus, "proof_status"), default=ProofStatus.QUEUED
    )
    verdict: Mapped[ProofVerdict | None] = mapped_column(str_enum(ProofVerdict, "proof_verdict"))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    groups: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    findings: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
