"""Permission Proof runs: start, execute (worker), schedule, and report.

The leak test itself is app/proof/engine.py. Runs are org-admin only and one at a time
per organization. A run still queued or running after the worker's hard time limit was
lost (a crashed worker) and shows as interrupted.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.base import utcnow
from app.db.session import session_scope
from app.models import (
    Document,
    Organization,
    PermissionProofRun,
    ProofKind,
    ProofStatus,
    User,
    Workspace,
    WorkspaceRole,
)
from app.proof import engine, oracle
from app.providers.registry import get_embedder
from app.schemas.common import Page, PageParams
from app.schemas.permission_proof import (
    FindingOut,
    GroupOut,
    GroupWorkspace,
    PersonRef,
    RunCreate,
    RunDetail,
    RunOut,
    SimulationIn,
    SimulationOut,
    WorkspaceRef,
)
from app.services import access, audit
from app.services.access import Principal
from app.services.audit import AuditAction, RequestMeta
from app.worker.queue import enqueue_proof

logger = logging.getLogger(__name__)

RESOURCE = "permission_proof_run"
_ACTIVE = (ProofStatus.QUEUED, ProofStatus.RUNNING)


def _lost_before() -> datetime:
    # The worker's hard time limit (app/worker/celery_app.py) plus a margin.
    return utcnow() - timedelta(seconds=get_settings().ingestion_lease_seconds + 60)


def _create(
    db: Session,
    organization_id: uuid.UUID,
    *,
    kind: ProofKind,
    requested_by: uuid.UUID | None,
    simulation: SimulationIn | None = None,
    meta: RequestMeta | None = None,
) -> PermissionProofRun:
    """Insert a queued run and its audit event (not committed). 409 while another run is
    active; the organization row lock makes two concurrent starts wait for each other."""
    db.execute(select(Organization.id).where(Organization.id == organization_id).with_for_update())
    active = db.scalar(
        select(PermissionProofRun.id).where(
            PermissionProofRun.organization_id == organization_id,
            PermissionProofRun.status.in_(_ACTIVE),
            # A running run is timed from its start: the worker's limit counts from there.
            func.coalesce(PermissionProofRun.started_at, PermissionProofRun.created_at)
            > _lost_before(),
        )
    )
    if active is not None:
        raise ConflictError("A leak test is already running", code="proof_running")
    run = PermissionProofRun(
        id=uuid.uuid4(),
        organization_id=organization_id,
        requested_by=requested_by,
        kind=kind,
        simulation=(
            {"user_id": str(simulation.user_id), "workspace_id": str(simulation.workspace_id)}
            if simulation
            else None
        ),
    )
    db.add(run)
    audit.record(
        db,
        AuditAction.PERMISSION_PROOF_REQUESTED,
        organization_id=organization_id,
        actor_user_id=requested_by,
        resource_type=RESOURCE,
        resource_id=run.id,
        metadata={"kind": kind.value, **({"simulation": run.simulation} if simulation else {})},
        meta=meta,
    )
    return run


def _enqueue(db: Session, run: PermissionProofRun) -> None:
    if not enqueue_proof(run.id, run.organization_id):
        run.status = ProofStatus.ERROR
        run.error = "The leak test could not be queued. Try again."
        run.finished_at = utcnow()
        db.commit()


def start_run(
    db: Session, principal: Principal, body: RunCreate, *, meta: RequestMeta
) -> RunDetail:
    access.require(principal.is_org_admin)
    if body.simulate is not None:
        _check_simulation(db, principal.organization_id, body.simulate)
    run = _create(
        db,
        principal.organization_id,
        kind=ProofKind.SIMULATION if body.simulate else ProofKind.MANUAL,
        requested_by=principal.user_id,
        simulation=body.simulate,
        meta=meta,
    )
    db.commit()
    _enqueue(db, run)
    return _detail(db, principal, run)


def _check_simulation(db: Session, organization_id: uuid.UUID, target: SimulationIn) -> None:
    """422 unless the run could catch something: an active member, and a workspace of this
    organization they are not in that holds indexed documents they cannot read."""
    snapshot = oracle.load_snapshot(db, organization_id)
    member = next((m for m in snapshot.members if m.user_id == target.user_id), None)
    if member is None:
        raise ValidationFailedError(
            "Pick an active member of this organization", code="proof_unknown_person"
        )
    if db.get(Workspace, target.workspace_id) is None:  # row-level security: this org only
        raise ValidationFailedError(
            "Pick a workspace of this organization", code="proof_unknown_workspace"
        )
    if target.workspace_id in member.workspace_roles:
        raise ValidationFailedError(
            "This person is already in that workspace; pick one they are not in",
            code="proof_already_member",
        )
    # What a viewer of the workspace would gain: its workspace-visible documents, not its
    # restricted ones. With nothing to gain, the run could not report anything.
    real = oracle.readable(snapshot, member.user_id, member.workspace_roles)
    simulated = oracle.readable(
        snapshot,
        member.user_id,
        {**member.workspace_roles, target.workspace_id: WorkspaceRole.VIEWER},
    )
    if not any(snapshot.documents[doc].indexed for doc in simulated - real):
        raise ValidationFailedError(
            "Joining that workspace as a viewer would not let this person read any indexed "
            "document they cannot already read",
            code="proof_nothing_to_catch",
        )


# --- Worker -----------------------------------------------------------------------------------


def execute(run_id: uuid.UUID, organization_id: uuid.UUID) -> str:
    """Claim a queued run, test, and store the report. Returns the outcome for the log."""
    with session_scope(organization_id) as db:
        claimed = db.execute(
            update(PermissionProofRun)
            .where(PermissionProofRun.id == run_id, PermissionProofRun.status == ProofStatus.QUEUED)
            .values(status=ProofStatus.RUNNING, started_at=utcnow())
            .returning(PermissionProofRun.simulation)
        ).first()
        db.commit()
    if claimed is None:
        return "skipped"
    target = claimed.simulation
    simulation = (
        engine.Simulation(uuid.UUID(target["user_id"]), uuid.UUID(target["workspace_id"]))
        if target
        else None
    )
    try:
        with session_scope(organization_id) as db:
            # One snapshot for the whole test: a change made meanwhile cannot look like a leak.
            db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            report = engine.run(
                db,
                organization_id,
                embedder=get_embedder(),
                max_probes=get_settings().permission_proof_max_probes,
                simulation=simulation,
            )
    except Exception as exc:  # recorded on the run; the worker keeps going
        # No exc_info: a database error's parameters would hold probe text copied from
        # documents.
        logger.error(
            "proof.failed", extra={"run_id": str(run_id), "error_type": type(exc).__name__}
        )
        _finish(run_id, organization_id, error=f"The leak test stopped ({type(exc).__name__}).")
        return "error"
    _finish(run_id, organization_id, report=report)
    return report.verdict.value


def _finish(
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    report: engine.Report | None = None,
    error: str | None = None,
) -> None:
    with session_scope(organization_id) as db:
        run = db.get_one(PermissionProofRun, run_id)
        run.finished_at = utcnow()
        if report is None:
            run.status, run.error = ProofStatus.ERROR, error
        else:
            run.status, run.verdict = ProofStatus.COMPLETED, report.verdict
            run.summary, run.groups, run.findings = report.summary, report.groups, report.findings
        audit.record(
            db,
            AuditAction.PERMISSION_PROOF_COMPLETED,
            organization_id=organization_id,
            actor_user_id=None,
            resource_type=RESOURCE,
            resource_id=run_id,
            metadata={
                "kind": run.kind.value,
                "status": run.status.value,
                "verdict": run.verdict.value if run.verdict else None,
                "probes_run": run.summary.get("probes_run", 0),
                "findings": len(run.findings),
            },
        )
        db.commit()


def schedule_all() -> int:
    """Queue the weekly run for every organization without an active one."""
    with session_scope() as db:
        organization_ids = list(db.scalars(select(Organization.id)))
    queued = 0
    for organization_id in organization_ids:
        with session_scope(organization_id) as db:
            try:
                run = _create(db, organization_id, kind=ProofKind.SCHEDULED, requested_by=None)
            except ConflictError:
                continue
            db.commit()
            _enqueue(db, run)
            queued += 1
    return queued


# --- Reading ----------------------------------------------------------------------------------


def list_runs(db: Session, principal: Principal, *, page: PageParams) -> Page[RunOut]:
    access.require(principal.is_org_admin)
    org = PermissionProofRun.organization_id == principal.organization_id
    total = db.scalar(select(func.count()).select_from(PermissionProofRun).where(org)) or 0
    runs = list(
        db.scalars(
            select(PermissionProofRun)
            .where(org)
            .order_by(PermissionProofRun.created_at.desc(), PermissionProofRun.id.desc())
            .offset(page.offset)
            .limit(page.page_size)
        )
    )
    names = _Names(db, runs)
    return Page(
        items=[_out(run, names) for run in runs],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


def get_run(db: Session, principal: Principal, run_id: uuid.UUID) -> RunDetail:
    access.require(principal.is_org_admin)
    run = db.scalar(
        select(PermissionProofRun).where(
            PermissionProofRun.id == run_id,
            PermissionProofRun.organization_id == principal.organization_id,
        )
    )
    if run is None:
        raise NotFoundError("Leak test not found")
    return _detail(db, principal, run)


class _Names:
    """People and workspace names for a page of runs, in two queries."""

    def __init__(self, db: Session, runs: list[PermissionProofRun]) -> None:
        user_ids: set[uuid.UUID] = set()
        workspace_ids: set[uuid.UUID] = set()
        for run in runs:
            user_ids.update(filter(None, [run.requested_by]))
            if run.simulation:
                user_ids.add(uuid.UUID(run.simulation["user_id"]))
                workspace_ids.add(uuid.UUID(run.simulation["workspace_id"]))
            for group in run.groups:
                user_ids.update(map(uuid.UUID, group["members"]))
                workspace_ids.update(uuid.UUID(w["workspace_id"]) for w in group["workspaces"])
        self.people = {u.id: u for u in db.scalars(select(User).where(User.id.in_(user_ids)))}
        self.workspaces = _workspace_names(db, workspace_ids)

    def person(self, user_id: uuid.UUID | str) -> PersonRef:
        user_id = uuid.UUID(str(user_id))
        user = self.people.get(user_id)
        return PersonRef(
            user_id=user_id,
            full_name=user.full_name if user else None,
            email=user.email if user else None,
        )

    def workspace(self, workspace_id: uuid.UUID | str) -> WorkspaceRef:
        workspace_id = uuid.UUID(str(workspace_id))
        return WorkspaceRef(workspace_id=workspace_id, name=self.workspaces.get(workspace_id))


def _workspace_names(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    rows = db.execute(select(Workspace.id, Workspace.name).where(Workspace.id.in_(ids)))
    return {workspace_id: name for workspace_id, name in rows}


def _status(run: PermissionProofRun) -> str:
    if run.status in _ACTIVE and (run.started_at or run.created_at) <= _lost_before():
        return "interrupted"
    return run.status.value


def _out(run: PermissionProofRun, names: _Names) -> RunOut:
    return RunOut(
        id=run.id,
        kind=run.kind.value,
        status=_status(run),
        verdict=run.verdict.value if run.verdict else None,
        requested_by=names.person(run.requested_by) if run.requested_by else None,
        simulation=(
            SimulationOut(
                person=names.person(run.simulation["user_id"]),
                workspace=names.workspace(run.simulation["workspace_id"]),
            )
            if run.simulation
            else None
        ),
        error=run.error,
        summary=run.summary,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _detail(db: Session, principal: Principal, run: PermissionProofRun) -> RunDetail:
    names = _Names(db, [run])
    doc_ids = {uuid.UUID(f["document_id"]) for f in run.findings}
    # Titles only for documents the viewer may read: an admin reads none by role.
    readable = access.readable_document_ids(db, principal, doc_ids)
    docs = {
        doc_id: (title, workspace_id)
        for doc_id, title, workspace_id in db.execute(
            select(Document.id, Document.title, Document.workspace_id).where(
                Document.id.in_(doc_ids)
            )
        )
    }
    names.workspaces.update(
        _workspace_names(db, {ws for _, ws in docs.values()} - names.workspaces.keys())
    )
    groups = [
        GroupOut(
            number=number,
            members=[names.person(m) for m in group["members"]],
            representative=names.person(group["representative"]),
            workspaces=[
                GroupWorkspace(**names.workspace(w["workspace_id"]).model_dump(), role=w["role"])
                for w in group["workspaces"]
            ],
            simulated_workspace=(
                names.workspace(group["simulated_workspace_id"])
                if group.get("simulated_workspace_id")
                else None
            ),
            **{
                key: group[key]
                for key in (
                    "readable_documents",
                    "documents_unreadable",
                    "documents_probed",
                    "probes_planned",
                    "probes_run",
                    "prompts_checked",
                    "passages_checked",
                    "findings",
                )
            },
        )
        for number, group in enumerate(run.groups)
    ]
    findings = []
    for f in run.findings:
        doc_id = uuid.UUID(f["document_id"])
        title, workspace_id = docs.get(doc_id, (None, None))
        findings.append(
            FindingOut(
                group=f["group"],
                kind=f["kind"],
                document_id=doc_id,
                title=title if doc_id in readable else None,
                workspace=names.workspace(workspace_id) if workspace_id else None,
                count=f["count"],
            )
        )
    return RunDetail(**_out(run, names).model_dump(), groups=groups, findings=findings)
