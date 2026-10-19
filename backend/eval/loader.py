"""Loads the synthetic corpus (eval/corpus.py) through the application's own services, so
the evaluation and the demo seed (scripts/seed_demo.py) exercise the real code paths:
workspaces through `add_workspace`/`add_member`, every document through `upload_document`
(validation, storage, the workspace grant, an ingestion job on the normal queue) and the
restricted document's user grant through `update_permissions`.

Idempotent: everything is looked up first and created only when missing. Documents are
matched by content, the way the upload service detects duplicates; existing users keep
their password and existing memberships their role.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest import mock

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from app.core.errors import ConflictError
from app.core.filetypes import MIME_TYPES, FileType
from app.db.session import get_sessionmaker, set_tenant
from app.ingestion.runner import process_job
from app.models import (
    ACTIVE_JOB_STATUSES,
    DocumentPermission,
    IngestionJob,
    JobStatus,
    Organization,
    OrganizationMembership,
    OrgRole,
    PermissionLevel,
    PrincipalType,
    User,
    Workspace,
    WorkspaceMembership,
)
from app.providers.embeddings import EmbeddingProvider
from app.schemas.documents import DocumentPermissionsUpdate, DocumentVisibility, UserGrant
from app.schemas.workspaces import WorkspaceMemberAdd
from app.services import documents, workspaces
from app.services.access import Principal
from app.services.audit import RequestMeta
from app.storage import ObjectStorage
from app.worker import queue
from eval import corpus

_META = RequestMeta(user_agent="eka-corpus-loader")


@dataclass
class Loaded:
    organization_id: uuid.UUID
    users: dict[str, uuid.UUID]  # person key -> user id
    workspaces: dict[str, uuid.UUID]  # workspace key -> id
    documents: dict[str, uuid.UUID]  # document key -> id
    created: list[str] = field(default_factory=list)  # what this load added, for the log


def load(
    storage: ObjectStorage, new_password_hash: Callable[[corpus.Person], str | None]
) -> Loaded:
    """Create whatever is missing. `new_password_hash` is called once per user created
    (None leaves the account without a password: it cannot sign in)."""
    created: list[str] = []
    with get_sessionmaker()() as db:
        users = _users(db, new_password_hash, created)
        org_id = _organization(db, users["admin"], created)
        _org_memberships(db, org_id, users, created)
    with get_sessionmaker()() as db:
        set_tenant(db, org_id)
        workspace_ids = _workspaces(db, org_id, users, created)
        _workspace_memberships(db, org_id, users, workspace_ids, created)
        document_ids = _documents(db, org_id, users, workspace_ids, storage, created)
        _grants(db, org_id, users, workspace_ids, document_ids, created)
    return Loaded(org_id, users, workspace_ids, document_ids, created)


def principal_for(db: Session, user_id: uuid.UUID, organization_id: uuid.UUID) -> Principal:
    """The principal the API would resolve for this user (app/api/deps.py); `db` must be
    bound to the tenant."""
    user = db.get(User, user_id)
    role = db.scalar(
        select(OrganizationMembership.role).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
    )
    if user is None or role is None:
        raise LookupError("the user is not a member of the organization")
    workspace_roles = dict(
        db.execute(
            select(WorkspaceMembership.workspace_id, WorkspaceMembership.role).where(
                WorkspaceMembership.organization_id == organization_id,
                WorkspaceMembership.user_id == user_id,
            )
        ).all()
    )
    return Principal(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        organization_id=organization_id,
        org_role=role,
        workspace_roles=workspace_roles,
    )


@contextmanager
def captured_ingestion_jobs() -> Iterator[list[uuid.UUID]]:
    """Record the ingestion jobs that uploads enqueue instead of sending them to the
    broker, so they can run in this process (`ingest`).
    ponytail: patches the queue module the documents service calls; an injectable queue
    in app/ would be cleaner if a second caller ever needs this."""
    jobs: list[uuid.UUID] = []

    def record(
        job_id: uuid.UUID, organization_id: uuid.UUID, *, countdown: float | None = None
    ) -> bool:
        jobs.append(job_id)
        return True

    with mock.patch.object(queue, "enqueue_ingestion", record):
        yield jobs


def pending_jobs(organization_id: uuid.UUID, document_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Queued or running ingestion jobs of these documents (an earlier load may have left
    them for a worker that never ran)."""
    with get_sessionmaker()() as db:
        set_tenant(db, organization_id)
        return list(
            db.scalars(
                select(IngestionJob.id)
                .where(
                    IngestionJob.document_id.in_(document_ids),
                    IngestionJob.status.in_(ACTIVE_JOB_STATUSES),
                )
                .order_by(IngestionJob.created_at)
            )
        )


def ingest(
    jobs: Sequence[uuid.UUID],
    organization_id: uuid.UUID,
    *,
    embedder: EmbeddingProvider | None,
    storage: ObjectStorage | None,
) -> None:
    """Run ingestion jobs inline, as the worker would; every one must succeed."""
    for job_id in dict.fromkeys(jobs):
        status = process_job(job_id, organization_id, embedder=embedder, storage=storage)
        if status is not JobStatus.SUCCEEDED:
            raise RuntimeError(f"ingestion job {job_id} ended {status.value}")


def _users(
    db: Session, new_password_hash: Callable[[corpus.Person], str | None], created: list[str]
) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for person in corpus.PEOPLE:
        user = db.scalar(select(User).where(User.email == person.email))
        if user is None:
            user = User(
                email=person.email,
                password_hash=new_password_hash(person),
                full_name=person.full_name,
            )
            db.add(user)
            db.flush()
            created.append(f"user {person.email}")
        ids[person.key] = user.id
    db.commit()
    return ids


def _organization(db: Session, admin_id: uuid.UUID, created: list[str]) -> uuid.UUID:
    """The organization named ORG_NAME that the demo admin administers."""
    org_id = db.scalar(
        select(Organization.id)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .where(
            Organization.name == corpus.ORG_NAME,
            OrganizationMembership.user_id == admin_id,
            OrganizationMembership.role == OrgRole.ORG_ADMIN,
        )
    )
    if org_id is None:
        org = Organization(name=corpus.ORG_NAME)
        db.add(org)
        db.commit()
        created.append(f"organization {corpus.ORG_NAME}")
        org_id = org.id
    return org_id


def _org_memberships(
    db: Session, org_id: uuid.UUID, users: dict[str, uuid.UUID], created: list[str]
) -> None:
    for person in corpus.PEOPLE:
        member = db.scalar(
            select(OrganizationMembership.id).where(
                OrganizationMembership.organization_id == org_id,
                OrganizationMembership.user_id == users[person.key],
            )
        )
        if member is None:
            db.add(
                OrganizationMembership(
                    organization_id=org_id, user_id=users[person.key], role=person.org_role
                )
            )
            created.append(f"organization member {person.email} ({person.org_role.value})")
    db.commit()


def _workspaces(
    db: Session, org_id: uuid.UUID, users: dict[str, uuid.UUID], created: list[str]
) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for spec in corpus.WORKSPACES:
        ws_id = db.scalar(
            select(Workspace.id).where(
                Workspace.organization_id == org_id,
                func.lower(Workspace.name) == spec.name.lower(),
            )
        )
        if ws_id is None:
            # The knowledge manager creates (and so owns) every workspace.
            ws = workspaces.add_workspace(
                db,
                organization_id=org_id,
                owner_id=users[corpus.UPLOADER],
                name=spec.name,
                description=spec.description,
                meta=_META,
            )
            db.commit()
            created.append(f"workspace {spec.name}")
            ws_id = ws.id
        ids[spec.key] = ws_id
    return ids


def _workspace_memberships(
    db: Session,
    org_id: uuid.UUID,
    users: dict[str, uuid.UUID],
    workspace_ids: dict[str, uuid.UUID],
    created: list[str],
) -> None:
    admin = principal_for(db, users["admin"], org_id)
    for person in corpus.PEOPLE:
        for ws_key, role in person.workspaces.items():
            member = db.scalar(
                select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == workspace_ids[ws_key],
                    WorkspaceMembership.user_id == users[person.key],
                )
            )
            if member is None:
                body = WorkspaceMemberAdd(user_id=users[person.key], role=role)
                workspaces.add_member(db, admin, workspace_ids[ws_key], body, meta=_META)
                created.append(f"workspace member {person.email} ({ws_key}, {role.value})")


def _documents(
    db: Session,
    org_id: uuid.UUID,
    users: dict[str, uuid.UUID],
    workspace_ids: dict[str, uuid.UUID],
    storage: ObjectStorage,
    created: list[str],
) -> dict[str, uuid.UUID]:
    uploader = principal_for(db, users[corpus.UPLOADER], org_id)
    ids: dict[str, uuid.UUID] = {}
    for doc in corpus.DOCUMENTS:
        mime_type = MIME_TYPES[FileType(doc.format)]
        upload = UploadFile(
            io.BytesIO(corpus.build(doc)),
            filename=doc.filename,
            headers=Headers({"content-type": mime_type}),
        )
        visibility = (
            DocumentVisibility.RESTRICTED if doc.restricted else DocumentVisibility.WORKSPACE
        )
        try:
            result = documents.upload_document(
                db,
                uploader,
                file=upload,
                workspace_id=workspace_ids[doc.workspace],
                title=doc.title,
                visibility=visibility,
                storage=storage,
                meta=_META,
            )
        except ConflictError as exc:
            if exc.code != "duplicate_document" or not exc.details:
                raise
            ids[doc.key] = uuid.UUID(exc.details["document_id"])
            continue
        ids[doc.key] = result.document.id
        created.append(f"document {doc.filename} ({visibility.value})")
    return ids


def _grants(
    db: Session,
    org_id: uuid.UUID,
    users: dict[str, uuid.UUID],
    workspace_ids: dict[str, uuid.UUID],
    document_ids: dict[str, uuid.UUID],
    created: list[str],
) -> None:
    """User grants as corpus.py declares them; the uploader (a workspace owner) sets them."""
    manager = principal_for(db, users[corpus.UPLOADER], org_id)
    for doc in corpus.DOCUMENTS:
        wanted = {(PrincipalType.USER, users[key], PermissionLevel.READ) for key in doc.grants}
        if not doc.restricted:
            wanted.add(
                (PrincipalType.WORKSPACE, workspace_ids[doc.workspace], PermissionLevel.READ)
            )
        current = set(
            db.execute(
                select(
                    DocumentPermission.principal_type,
                    DocumentPermission.principal_id,
                    DocumentPermission.permission,
                ).where(DocumentPermission.document_id == document_ids[doc.key])
            ).tuples()
        )
        if current == wanted:
            continue
        body = DocumentPermissionsUpdate(
            visibility=DocumentVisibility.RESTRICTED
            if doc.restricted
            else DocumentVisibility.WORKSPACE,
            users=[UserGrant(user_id=users[key]) for key in doc.grants],
        )
        documents.update_permissions(db, manager, document_ids[doc.key], body, meta=_META)
        created.append(f"grants for {doc.filename}: {', '.join(doc.grants) or 'workspace'}")
