from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, Timestamps, UUIDPrimaryKey, str_enum


class WorkspaceRole(StrEnum):
    OWNER = "owner"  # manage members and every document in the workspace
    EDITOR = "editor"  # upload; manage documents they uploaded
    VIEWER = "viewer"  # read only


class Workspace(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),  # target for tenant-safe composite FKs
        Index("uq_workspaces_org_lower_name", "organization_id", text("lower(name)"), unique=True),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class WorkspaceMembership(UUIDPrimaryKey, CreatedAt, Base):
    """Membership is only possible for organization members: the composite FK to
    organization_memberships cascades, so leaving an org removes workspace access."""

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id"),
        ForeignKeyConstraint(
            ["workspace_id", "organization_id"],
            ["workspaces.id", "workspaces.organization_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            ondelete="CASCADE",
        ),
        Index("ix_workspace_memberships_org_user", "organization_id", "user_id"),
    )

    workspace_id: Mapped[uuid.UUID]
    organization_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    role: Mapped[WorkspaceRole] = mapped_column(str_enum(WorkspaceRole, "workspace_role"))
