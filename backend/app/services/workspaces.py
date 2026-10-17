"""Workspaces and workspace memberships."""

from __future__ import annotations

import uuid
from dataclasses import replace

from sqlalchemy import delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models import (
    Document,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)
from app.schemas.workspaces import (
    WorkspaceCreate,
    WorkspaceMemberAdd,
    WorkspaceMemberOut,
    WorkspaceMemberUpdate,
    WorkspaceOut,
    WorkspaceUpdate,
)
from app.services import access, audit
from app.services.access import Principal
from app.services.audit import AuditAction, RequestMeta

_NAME_INDEX = "uq_workspaces_org_lower_name"
_MEMBER_UNIQUE = "uq_workspace_memberships_workspace_id_user_id"


def _flush(db: Session, constraint: str, error: ConflictError) -> None:
    """Flush, turning a violation of the unique `constraint` into `error` (the database
    check also covers concurrent requests)."""
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == constraint:
            raise error from None
        raise


def _name_taken() -> ConflictError:
    return ConflictError("A workspace with this name already exists", code="workspace_name_taken")


def _get_workspace(
    db: Session,
    p: Principal,
    workspace_id: uuid.UUID,
    *,
    manage: bool = False,
    lock: bool = False,
) -> Workspace:
    """404 unless the caller can read the workspace (unreadable and nonexistent look the
    same), then 403 when `manage` is required and not allowed."""
    ws = None
    if access.can_read_workspace(p, workspace_id):
        stmt = select(Workspace).where(
            Workspace.id == workspace_id, Workspace.organization_id == p.organization_id
        )
        ws = db.scalar(stmt.with_for_update() if lock else stmt)
    if ws is None:
        raise NotFoundError("Workspace not found", code="workspace_not_found")
    if manage:
        access.require(
            access.can_manage_workspace(p, ws.id),
            "Only workspace owners and organization admins can do this",
        )
    return ws


def _outs(db: Session, p: Principal, workspaces: list[Workspace]) -> list[WorkspaceOut]:
    """Two grouped queries for any number of workspaces. Document counts use the caller's
    read predicate, so restricted documents they cannot see are not even counted."""
    if not workspaces:
        return []
    ids = [ws.id for ws in workspaces]
    members = dict(
        db.execute(
            select(WorkspaceMembership.workspace_id, func.count())
            .where(WorkspaceMembership.workspace_id.in_(ids))
            .group_by(WorkspaceMembership.workspace_id)
        ).all()
    )
    documents = dict(
        db.execute(
            select(Document.workspace_id, func.count())
            .where(Document.workspace_id.in_(ids), access.readable_documents_clause(p))
            .group_by(Document.workspace_id)
        ).all()
    )
    return [
        WorkspaceOut(
            id=ws.id,
            name=ws.name,
            description=ws.description,
            created_at=ws.created_at,
            my_role=p.workspace_roles.get(ws.id),
            member_count=members.get(ws.id, 0),
            document_count=documents.get(ws.id, 0),
            can_manage=access.can_manage_workspace(p, ws.id),
            can_upload=access.can_upload_to_workspace(p, ws.id),
        )
        for ws in workspaces
    ]


def add_workspace(
    db: Session,
    *,
    organization_id: uuid.UUID,
    owner_id: uuid.UUID,
    name: str,
    description: str | None,
    meta: RequestMeta,
) -> Workspace:
    """Insert a workspace owned by `owner_id` and audit it; the caller commits (also used
    for the starter workspace at registration). 409 if the name is taken."""
    ws = Workspace(
        organization_id=organization_id, name=name, description=description, created_by=owner_id
    )
    db.add(ws)
    _flush(db, _NAME_INDEX, _name_taken())
    db.add(
        WorkspaceMembership(
            workspace_id=ws.id,
            organization_id=organization_id,
            user_id=owner_id,
            role=WorkspaceRole.OWNER,
        )
    )
    audit.record(
        db,
        AuditAction.WORKSPACE_CREATED,
        organization_id=organization_id,
        actor_user_id=owner_id,
        resource_type="workspace",
        resource_id=ws.id,
        metadata={"name": name},
        meta=meta,
    )
    return ws


def list_workspaces(db: Session, principal: Principal) -> list[WorkspaceOut]:
    stmt = (
        select(Workspace)
        .where(Workspace.organization_id == principal.organization_id)
        .order_by(func.lower(Workspace.name))
    )
    if not principal.is_org_admin:
        stmt = stmt.where(Workspace.id.in_(principal.workspace_ids))
    return _outs(db, principal, list(db.scalars(stmt)))


def create_workspace(
    db: Session, principal: Principal, body: WorkspaceCreate, *, meta: RequestMeta
) -> WorkspaceOut:
    access.require(
        access.can_create_workspace(principal),
        "Only organization admins and knowledge managers can create workspaces",
    )
    ws = add_workspace(
        db,
        organization_id=principal.organization_id,
        owner_id=principal.user_id,
        name=body.name,
        description=body.description,
        meta=meta,
    )
    db.commit()
    owner = replace(
        principal, workspace_roles={**principal.workspace_roles, ws.id: WorkspaceRole.OWNER}
    )
    return _outs(db, owner, [ws])[0]


def get_workspace(db: Session, principal: Principal, workspace_id: uuid.UUID) -> WorkspaceOut:
    return _outs(db, principal, [_get_workspace(db, principal, workspace_id)])[0]


def update_workspace(
    db: Session,
    principal: Principal,
    workspace_id: uuid.UUID,
    body: WorkspaceUpdate,
    *,
    meta: RequestMeta,
) -> WorkspaceOut:
    ws = _get_workspace(db, principal, workspace_id, manage=True)
    changed: list[str] = []
    if body.name is not None and body.name != ws.name:
        ws.name = body.name
        changed.append("name")
    if "description" in body.model_fields_set and body.description != ws.description:
        ws.description = body.description
        changed.append("description")
    if changed:
        _flush(db, _NAME_INDEX, _name_taken())
        audit.record(
            db,
            AuditAction.WORKSPACE_UPDATED,
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            resource_type="workspace",
            resource_id=ws.id,
            metadata={"fields": changed},
            meta=meta,
        )
        db.commit()
    return _outs(db, principal, [ws])[0]


def delete_workspace(
    db: Session, principal: Principal, workspace_id: uuid.UUID, *, meta: RequestMeta
) -> None:
    # The row lock makes a concurrent upload either commit first (and be seen below) or
    # fail its foreign key check; otherwise the cascade could delete a fresh document.
    ws = _get_workspace(db, principal, workspace_id, manage=True, lock=True)
    if db.scalar(select(exists().where(Document.workspace_id == ws.id))):
        raise ConflictError("Delete the workspace's documents first", code="workspace_not_empty")
    audit.record(
        db,
        AuditAction.WORKSPACE_DELETED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="workspace",
        resource_id=ws.id,
        metadata={"name": ws.name},
        meta=meta,
    )
    db.execute(delete(Workspace).where(Workspace.id == ws.id))
    db.commit()


def _member_out(membership: WorkspaceMembership, user: User) -> WorkspaceMemberOut:
    return WorkspaceMemberOut(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=membership.role,
        added_at=membership.created_at,
    )


def _membership(db: Session, ws: Workspace, user_id: uuid.UUID) -> tuple[WorkspaceMembership, User]:
    row = db.execute(
        select(WorkspaceMembership, User)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == ws.id, WorkspaceMembership.user_id == user_id)
    ).first()
    if row is None:
        raise NotFoundError("Workspace member not found", code="member_not_found")
    return row


def _ensure_another_owner(db: Session, workspace_id: uuid.UUID) -> None:
    """409 unless another owner remains. Locking the owner rows (in a fixed order)
    serializes concurrent demotions, so two owners cannot remove each other."""
    owners = db.scalars(
        select(WorkspaceMembership.id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.role == WorkspaceRole.OWNER,
        )
        .order_by(WorkspaceMembership.id)
        .with_for_update()
    ).all()
    if len(owners) < 2:
        raise ConflictError("The workspace needs at least one owner", code="last_owner")


def list_members(
    db: Session, principal: Principal, workspace_id: uuid.UUID
) -> list[WorkspaceMemberOut]:
    ws = _get_workspace(db, principal, workspace_id)
    rows = db.execute(
        select(WorkspaceMembership, User)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == ws.id)
        .order_by(User.full_name, User.id)
    )
    return [_member_out(membership, user) for membership, user in rows]


def add_member(
    db: Session,
    principal: Principal,
    workspace_id: uuid.UUID,
    body: WorkspaceMemberAdd,
    *,
    meta: RequestMeta,
) -> WorkspaceMemberOut:
    ws = _get_workspace(db, principal, workspace_id, manage=True)
    user = db.scalar(
        select(User)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .where(
            OrganizationMembership.organization_id == ws.organization_id,
            User.id == body.user_id,
        )
    )
    if user is None:
        raise NotFoundError("No such member in this organization", code="user_not_found")
    membership = WorkspaceMembership(
        workspace_id=ws.id, organization_id=ws.organization_id, user_id=user.id, role=body.role
    )
    db.add(membership)
    _flush(
        db,
        _MEMBER_UNIQUE,
        ConflictError("The user is already a member of this workspace", code="already_member"),
    )
    audit.record(
        db,
        AuditAction.WORKSPACE_MEMBER_ADDED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="workspace",
        resource_id=ws.id,
        metadata={"user_id": str(user.id), "role": body.role.value},
        meta=meta,
    )
    db.commit()
    return _member_out(membership, user)


def update_member(
    db: Session,
    principal: Principal,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: WorkspaceMemberUpdate,
    *,
    meta: RequestMeta,
) -> WorkspaceMemberOut:
    ws = _get_workspace(db, principal, workspace_id, manage=True)
    membership, user = _membership(db, ws, user_id)
    previous = membership.role
    if body.role != previous:
        if previous == WorkspaceRole.OWNER:
            _ensure_another_owner(db, ws.id)
        membership.role = body.role
        audit.record(
            db,
            AuditAction.WORKSPACE_MEMBER_ROLE_CHANGED,
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            resource_type="workspace",
            resource_id=ws.id,
            metadata={"user_id": str(user_id), "from": previous.value, "to": body.role.value},
            meta=meta,
        )
        db.commit()
    return _member_out(membership, user)


def remove_member(
    db: Session,
    principal: Principal,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    meta: RequestMeta,
) -> None:
    ws = _get_workspace(db, principal, workspace_id, manage=True)
    membership, _ = _membership(db, ws, user_id)
    if membership.role == WorkspaceRole.OWNER:
        _ensure_another_owner(db, ws.id)
    db.execute(delete(WorkspaceMembership).where(WorkspaceMembership.id == membership.id))
    audit.record(
        db,
        AuditAction.WORKSPACE_MEMBER_REMOVED,
        organization_id=principal.organization_id,
        actor_user_id=principal.user_id,
        resource_type="workspace",
        resource_id=ws.id,
        metadata={"user_id": str(user_id), "role": membership.role.value},
        meta=meta,
    )
    db.commit()
