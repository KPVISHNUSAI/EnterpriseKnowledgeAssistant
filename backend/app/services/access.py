"""Authorization: who may read or manage what.

This module is the single source of truth for document access. Every code path that
returns documents, chunks or passages (document APIs, search, chat retrieval, citation
rendering) filters with `readable_documents_clause`, so a crafted ID or filter can
never widen access. Row-level security in PostgreSQL is a second, independent layer
that enforces tenant isolation even if application code forgets a filter.

Read rule (non-admins): the document is in the caller's organization AND one of
  * a workspace grant names a workspace the caller belongs to
    (a document's own workspace grant = "visible to the workspace"),
  * a user grant names the caller,
  * the caller uploaded it and still belongs to its workspace,
  * the caller owns its workspace.
Organization admins can read every document in their organization.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, and_, exists, false, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.errors import ForbiddenError, NotFoundError
from app.models import (
    Document,
    DocumentPermission,
    OrgRole,
    PermissionLevel,
    PrincipalType,
    WorkspaceRole,
)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, resolved per request from the session cookie."""

    user_id: uuid.UUID
    email: str
    full_name: str
    organization_id: uuid.UUID
    org_role: OrgRole
    workspace_roles: Mapping[uuid.UUID, WorkspaceRole] = field(default_factory=dict)
    session_id: uuid.UUID | None = None

    @property
    def is_org_admin(self) -> bool:
        return self.org_role == OrgRole.ORG_ADMIN

    @property
    def workspace_ids(self) -> list[uuid.UUID]:
        return list(self.workspace_roles)

    def workspaces_with(self, *roles: WorkspaceRole) -> list[uuid.UUID]:
        return [ws for ws, role in self.workspace_roles.items() if role in roles]


# --- Workspace-level checks ----------------------------------------------------------------


def can_read_workspace(p: Principal, workspace_id: uuid.UUID) -> bool:
    return p.is_org_admin or workspace_id in p.workspace_roles


def can_upload_to_workspace(p: Principal, workspace_id: uuid.UUID) -> bool:
    return p.is_org_admin or p.workspace_roles.get(workspace_id) in (
        WorkspaceRole.OWNER,
        WorkspaceRole.EDITOR,
    )


def can_manage_workspace(p: Principal, workspace_id: uuid.UUID) -> bool:
    return p.is_org_admin or p.workspace_roles.get(workspace_id) == WorkspaceRole.OWNER


def can_create_workspace(p: Principal) -> bool:
    return p.org_role in (OrgRole.ORG_ADMIN, OrgRole.KNOWLEDGE_MANAGER)


def require(condition: bool, message: str | None = None) -> None:
    if not condition:
        raise ForbiddenError(message)


# --- Document-level SQL predicates -----------------------------------------------------------


def _in(
    column: InstrumentedAttribute[uuid.UUID], values: Iterable[uuid.UUID]
) -> ColumnElement[bool]:
    values = list(values)
    return column.in_(values) if values else false()


def readable_documents_clause(p: Principal) -> ColumnElement[bool]:
    """WHERE clause over `Document` selecting exactly the documents `p` may read."""
    in_org = Document.organization_id == p.organization_id
    if p.is_org_admin:
        return in_org
    ws_ids = p.workspace_ids
    granted = exists().where(
        DocumentPermission.document_id == Document.id,
        DocumentPermission.organization_id == Document.organization_id,
        or_(
            and_(
                DocumentPermission.principal_type == PrincipalType.WORKSPACE,
                _in(DocumentPermission.principal_id, ws_ids),
            ),
            and_(
                DocumentPermission.principal_type == PrincipalType.USER,
                DocumentPermission.principal_id == p.user_id,
            ),
        ),
    )
    return and_(
        in_org,
        or_(
            granted,
            and_(Document.uploaded_by == p.user_id, _in(Document.workspace_id, ws_ids)),
            _in(Document.workspace_id, p.workspaces_with(WorkspaceRole.OWNER)),
        ),
    )


def manageable_documents_clause(p: Principal) -> ColumnElement[bool]:
    """WHERE clause over `Document` selecting the documents `p` may modify or delete."""
    in_org = Document.organization_id == p.organization_id
    if p.is_org_admin:
        return in_org
    manage_grant = exists().where(
        DocumentPermission.document_id == Document.id,
        DocumentPermission.organization_id == Document.organization_id,
        DocumentPermission.principal_type == PrincipalType.USER,
        DocumentPermission.principal_id == p.user_id,
        DocumentPermission.permission == PermissionLevel.MANAGE,
    )
    return and_(
        in_org,
        or_(
            _in(Document.workspace_id, p.workspaces_with(WorkspaceRole.OWNER)),
            and_(
                _in(Document.workspace_id, p.workspaces_with(WorkspaceRole.EDITOR)),
                Document.uploaded_by == p.user_id,
            ),
            manage_grant,
        ),
    )


def get_readable_document(db: Session, p: Principal, document_id: uuid.UUID) -> Document:
    """Return the document or raise 404. Unreadable and nonexistent look identical."""
    doc = db.scalar(
        select(Document).where(Document.id == document_id, readable_documents_clause(p))
    )
    if doc is None:
        raise NotFoundError("Document not found")
    return doc


def get_manageable_document(
    db: Session, p: Principal, document_id: uuid.UUID, *, for_update: bool = False
) -> Document:
    """404 if the caller cannot see the document, 403 if they can see but not manage it."""
    doc = get_readable_document(db, p, document_id)
    stmt = select(Document.id).where(Document.id == doc.id, manageable_documents_clause(p))
    if db.scalar(stmt) is None:
        raise ForbiddenError("You do not have permission to manage this document")
    if for_update:
        db.refresh(doc, with_for_update=True)
    return doc


def can_manage_document(db: Session, p: Principal, document_id: uuid.UUID) -> bool:
    stmt = select(Document.id).where(Document.id == document_id, manageable_documents_clause(p))
    return db.scalar(stmt) is not None


def readable_document_ids(
    db: Session, p: Principal, document_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    ids = list(set(document_ids))
    if not ids:
        return set()
    rows = db.scalars(select(Document.id).where(Document.id.in_(ids), readable_documents_clause(p)))
    return set(rows)
