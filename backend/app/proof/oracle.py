"""The read rule, stated a second time, for the leak test (app/proof/engine.py).

It shares no code with app/services/access.py on purpose: the leak test checks what the
application's retrieval returns against what this module says each person may read, so a
bug in one is not repeated in the other. It follows the rule in access.py's docstring: a
document is readable by a member of its organization when
  * a workspace grant on it names a workspace the member belongs to,
  * a user grant on it names the member,
  * the member uploaded it and still belongs to its workspace, or
  * the member owns its workspace.
Organization roles give nothing. Grant levels (read, manage) both allow reading.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentPermission,
    DocumentStatus,
    OrganizationMembership,
    OrgRole,
    PrincipalType,
    User,
    WorkspaceMembership,
    WorkspaceRole,
)


@dataclass(frozen=True)
class Member:
    user_id: uuid.UUID
    email: str
    full_name: str
    org_role: OrgRole
    workspace_roles: Mapping[uuid.UUID, WorkspaceRole]


@dataclass(frozen=True)
class DocumentRow:
    id: uuid.UUID
    workspace_id: uuid.UUID
    uploaded_by: uuid.UUID | None
    title: str
    indexed: bool


@dataclass
class Snapshot:
    organization_id: uuid.UUID
    members: list[Member]  # active members, by email
    documents: dict[uuid.UUID, DocumentRow]
    workspace_grants: dict[uuid.UUID, set[uuid.UUID]] = field(default_factory=dict)
    user_grants: dict[uuid.UUID, set[uuid.UUID]] = field(default_factory=dict)


def load_snapshot(db: Session, organization_id: uuid.UUID) -> Snapshot:
    """The rows the rule reads, for one organization. Deactivated accounts cannot sign
    in, so they are left out."""
    roles: dict[uuid.UUID, dict[uuid.UUID, WorkspaceRole]] = defaultdict(dict)
    for user_id, workspace_id, role in db.execute(
        select(
            WorkspaceMembership.user_id, WorkspaceMembership.workspace_id, WorkspaceMembership.role
        ).where(WorkspaceMembership.organization_id == organization_id)
    ):
        roles[user_id][workspace_id] = role
    members = [
        Member(user.id, user.email, user.full_name, org_role, dict(roles.get(user.id, {})))
        for user, org_role in db.execute(
            select(User, OrganizationMembership.role)
            .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
            .where(OrganizationMembership.organization_id == organization_id, User.is_active)
            .order_by(User.email)
        )
    ]
    documents = {
        row.id: DocumentRow(
            row.id,
            row.workspace_id,
            row.uploaded_by,
            row.title,
            row.processing_status == DocumentStatus.INDEXED,
        )
        for row in db.execute(
            select(
                Document.id,
                Document.workspace_id,
                Document.uploaded_by,
                Document.title,
                Document.processing_status,
            ).where(Document.organization_id == organization_id)
        )
    }
    snapshot = Snapshot(organization_id, members, documents)
    for document_id, kind, principal_id in db.execute(
        select(
            DocumentPermission.document_id,
            DocumentPermission.principal_type,
            DocumentPermission.principal_id,
        ).where(DocumentPermission.organization_id == organization_id)
    ):
        grants = (
            snapshot.workspace_grants if kind == PrincipalType.WORKSPACE else snapshot.user_grants
        )
        grants.setdefault(document_id, set()).add(principal_id)
    return snapshot


def readable(
    snapshot: Snapshot, user_id: uuid.UUID, workspace_roles: Mapping[uuid.UUID, WorkspaceRole]
) -> frozenset[uuid.UUID]:
    """Every document of the snapshot that this person may read under the rule above."""
    owned = {ws for ws, role in workspace_roles.items() if role == WorkspaceRole.OWNER}
    return frozenset(
        doc.id
        for doc in snapshot.documents.values()
        if snapshot.workspace_grants.get(doc.id, set()) & workspace_roles.keys()
        or user_id in snapshot.user_grants.get(doc.id, set())
        or (doc.uploaded_by == user_id and doc.workspace_id in workspace_roles)
        or doc.workspace_id in owned
    )
