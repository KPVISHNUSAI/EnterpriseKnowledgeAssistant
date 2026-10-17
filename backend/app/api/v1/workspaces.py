from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal, get_request_meta
from app.schemas.common import errors
from app.schemas.workspaces import (
    WorkspaceCreate,
    WorkspaceMemberAdd,
    WorkspaceMemberOut,
    WorkspaceMemberUpdate,
    WorkspaceOut,
    WorkspaceUpdate,
)
from app.services import workspaces as ws_service
from app.services.access import Principal
from app.services.audit import RequestMeta

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceOut], responses=errors(401))
def list_workspaces(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[WorkspaceOut]:
    """Workspaces the caller belongs to (org admins see all workspaces)."""
    return ws_service.list_workspaces(db, principal)


@router.post("", response_model=WorkspaceOut, status_code=201, responses=errors(401, 403, 409, 422))
def create_workspace(
    body: WorkspaceCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> WorkspaceOut:
    """Org admins and knowledge managers only; the creator becomes the owner. 409 if the
    name (case-insensitive) is taken in the organization."""
    return ws_service.create_workspace(db, principal, body, meta=meta)


@router.get("/{workspace_id}", response_model=WorkspaceOut, responses=errors(401, 404))
def get_workspace(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> WorkspaceOut:
    return ws_service.get_workspace(db, principal, workspace_id)


@router.patch(
    "/{workspace_id}", response_model=WorkspaceOut, responses=errors(401, 403, 404, 409, 422)
)
def update_workspace(
    workspace_id: uuid.UUID,
    body: WorkspaceUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> WorkspaceOut:
    """Workspace owners and org admins."""
    return ws_service.update_workspace(db, principal, workspace_id, body, meta=meta)


@router.delete("/{workspace_id}", status_code=204, responses=errors(401, 403, 404, 409))
def delete_workspace(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    """Workspace owners and org admins. 409 while the workspace still has documents."""
    ws_service.delete_workspace(db, principal, workspace_id, meta=meta)


@router.get(
    "/{workspace_id}/members",
    response_model=list[WorkspaceMemberOut],
    responses=errors(401, 404),
)
def list_workspace_members(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[WorkspaceMemberOut]:
    return ws_service.list_members(db, principal, workspace_id)


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberOut,
    status_code=201,
    responses=errors(401, 403, 404, 409, 422),
)
def add_workspace_member(
    workspace_id: uuid.UUID,
    body: WorkspaceMemberAdd,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> WorkspaceMemberOut:
    """Workspace owners and org admins. The user must belong to the organization."""
    return ws_service.add_member(db, principal, workspace_id, body, meta=meta)


@router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=WorkspaceMemberOut,
    responses=errors(401, 403, 404, 409, 422),
)
def update_workspace_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: WorkspaceMemberUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> WorkspaceMemberOut:
    """409 when it would leave the workspace without an owner."""
    return ws_service.update_member(db, principal, workspace_id, user_id, body, meta=meta)


@router.delete(
    "/{workspace_id}/members/{user_id}", status_code=204, responses=errors(401, 403, 404, 409)
)
def remove_workspace_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    """409 when it would leave the workspace without an owner."""
    ws_service.remove_member(db, principal, workspace_id, user_id, meta=meta)
