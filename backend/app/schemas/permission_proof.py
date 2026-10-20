from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal["queued", "running", "completed", "error", "interrupted"]
FindingKind = Literal["retrieved", "prompt_text", "access_extra", "access_missing"]


class SimulationIn(BaseModel):
    user_id: uuid.UUID
    workspace_id: uuid.UUID


class RunCreate(BaseModel):
    simulate: SimulationIn | None = Field(
        None,
        description="Test one person as if the application had wrongly let them read this "
        "workspace. Nothing is changed; the run must report findings.",
    )


class PersonRef(BaseModel):
    user_id: uuid.UUID
    full_name: str | None = None
    email: str | None = None


class WorkspaceRef(BaseModel):
    workspace_id: uuid.UUID
    name: str | None = None  # None if the workspace has since been deleted


class SimulationOut(BaseModel):
    person: PersonRef
    workspace: WorkspaceRef


class RunOut(BaseModel):
    id: uuid.UUID
    kind: Literal["manual", "scheduled", "simulation"]
    status: RunStatus
    verdict: Literal["passed", "failed", "nothing_to_test"] | None = None
    requested_by: PersonRef | None = None
    simulation: SimulationOut | None = None
    error: str | None = None
    summary: dict[str, Any]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class GroupWorkspace(WorkspaceRef):
    role: str


class GroupOut(BaseModel):
    number: int
    members: list[PersonRef]
    representative: PersonRef
    workspaces: list[GroupWorkspace]
    simulated_workspace: WorkspaceRef | None = None
    readable_documents: int
    documents_unreadable: int
    documents_probed: int
    probes_planned: int
    probes_run: int
    prompts_checked: int
    passages_checked: int
    findings: int


class FindingOut(BaseModel):
    group: int
    kind: FindingKind
    document_id: uuid.UUID
    # Shown only if the viewer may read the document; otherwise only its workspace.
    title: str | None = None
    workspace: WorkspaceRef | None = None
    count: int


class RunDetail(RunOut):
    groups: list[GroupOut]
    findings: list[FindingOut]
