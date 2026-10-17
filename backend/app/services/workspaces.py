"""Workspaces and workspace memberships."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.schemas.workspaces import (
    WorkspaceCreate,
    WorkspaceMemberAdd,
    WorkspaceMemberOut,
    WorkspaceMemberUpdate,
    WorkspaceOut,
    WorkspaceUpdate,
)
from app.services.access import Principal
from app.services.audit import RequestMeta


def list_workspaces(db: Session, principal: Principal) -> list[WorkspaceOut]:
    raise NotImplementedError


def create_workspace(
    db: Session, principal: Principal, body: WorkspaceCreate, *, meta: RequestMeta
) -> WorkspaceOut:
    raise NotImplementedError


def get_workspace(db: Session, principal: Principal, workspace_id: uuid.UUID) -> WorkspaceOut:
    raise NotImplementedError


def update_workspace(
    db: Session,
    principal: Principal,
    workspace_id: uuid.UUID,
    body: WorkspaceUpdate,
    *,
    meta: RequestMeta,
) -> WorkspaceOut:
    raise NotImplementedError


def delete_workspace(
    db: Session, principal: Principal, workspace_id: uuid.UUID, *, meta: RequestMeta
) -> None:
    raise NotImplementedError


def list_members(
    db: Session, principal: Principal, workspace_id: uuid.UUID
) -> list[WorkspaceMemberOut]:
    raise NotImplementedError


def add_member(
    db: Session,
    principal: Principal,
    workspace_id: uuid.UUID,
    body: WorkspaceMemberAdd,
    *,
    meta: RequestMeta,
) -> WorkspaceMemberOut:
    raise NotImplementedError


def update_member(
    db: Session,
    principal: Principal,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: WorkspaceMemberUpdate,
    *,
    meta: RequestMeta,
) -> WorkspaceMemberOut:
    raise NotImplementedError


def remove_member(
    db: Session,
    principal: Principal,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    meta: RequestMeta,
) -> None:
    raise NotImplementedError
