"""Permission Proof (app/proof): the leak test passes when access is enforced, and fails
when the application lets through anything the rule forbids. A test that cannot fail
proves nothing, so most tests here break the application on purpose."""

from __future__ import annotations

import json
import logging
import random
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest
from sqlalchemy import or_, update
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.models import (
    Document,
    DocumentPermission,
    OrgRole,
    PermissionLevel,
    PermissionProofRun,
    PrincipalType,
    ProofStatus,
    ProofVerdict,
    WorkspaceRole,
)
from app.proof import engine, oracle
from app.proof.engine import STRIDE, WINDOW, Simulation, TextIndex, allocate
from app.providers.embeddings import HashingEmbedder
from app.services import access, permission_proof
from app.services.access import readable_document_ids

BOARD_TEXT = (
    "Board minutes: the directors approved the acquisition of Nimbus Robotics for "
    "41 million euros, to close in March, and asked the CFO to keep it confidential."
)
ENGINEERING_TEXT = (
    "Engineering handbook: every deploy needs two reviewers, a green pipeline and a "
    "rollback plan written before the change window opens on Tuesday evenings."
)
SALARY_TEXT = (
    "Compensation review: the principal engineer band rises to 9.4 million rupees from "
    "April, with retention grants vesting over four years for the platform team."
)


@dataclass
class Estate:
    """The tenant's Engineering workspace plus a Board workspace only the manager owns."""

    tenant: object
    board: object
    board_doc: Document
    engineering_doc: Document
    salary_doc: Document


@pytest.fixture
def estate(factory, tenant):
    board = factory.workspace(tenant.org, "Board", members={tenant.manager: WorkspaceRole.OWNER})
    board_doc = factory.document(board, uploaded_by=tenant.manager, title="Board minutes")
    factory.chunks(board_doc, [BOARD_TEXT])
    engineering_doc = factory.document(
        tenant.workspace, uploaded_by=tenant.editor, title="Engineering handbook"
    )
    factory.chunks(engineering_doc, [ENGINEERING_TEXT])
    # Restricted: the editor has a user grant; the viewer may not read it.
    salary_doc = factory.document(
        tenant.workspace,
        uploaded_by=tenant.manager,
        title="Compensation review",
        restricted=True,
        grants={tenant.editor: PermissionLevel.READ},
    )
    factory.chunks(salary_doc, [SALARY_TEXT])
    return Estate(tenant, board, board_doc, engineering_doc, salary_doc)


def _run(db, org_id, *, simulation=None, max_probes=1000):
    set_tenant(db, org_id)
    embedder = HashingEmbedder(get_settings().embedding_dimensions)
    return engine.run(db, org_id, embedder=embedder, max_probes=max_probes, simulation=simulation)


def _kinds(report, document_id):
    return {f["kind"] for f in report.findings if f["document_id"] == str(document_id)}


# --- The leak test --------------------------------------------------------------------------


def test_enforced_access_passes(db, estate):
    report = _run(db, estate.tenant.org.id)
    assert report.verdict == ProofVerdict.PASSED
    assert report.findings == []
    # admin and outsider read the same (nothing), but an admin is a group of its own.
    assert report.summary["groups"] == 5
    assert report.summary["members"] == 5
    assert report.summary["probes_run"] == report.summary["probes_planned"] > 0
    assert report.summary["prompts_checked"] > 0  # groups that read something get prompts
    viewer = next(g for g in report.groups if g["representative"] == str(estate.tenant.viewer.id))
    assert viewer["readable_documents"] == 1
    assert viewer["documents_unreadable"] == viewer["documents_probed"] == 2


def test_a_simulated_mis_share_is_caught_without_changing_access(db, factory, estate):
    t = estate.tenant
    report = _run(db, t.org.id, simulation=Simulation(t.viewer.id, estate.board.id))
    assert report.verdict == ProofVerdict.FAILED
    assert len(report.groups) == 1
    assert report.groups[0]["simulated_workspace_id"] == str(estate.board.id)
    assert _kinds(report, estate.board_doc.id) == {"access_extra", "retrieved", "prompt_text"}
    assert _kinds(report, estate.salary_doc.id) == set()  # still correctly withheld
    viewer = factory.principal(t.viewer, t.org)
    assert readable_document_ids(db, viewer, [estate.board_doc.id]) == set()


def test_a_retrieval_filter_bug_is_caught_in_the_prompt(db, estate, monkeypatch):
    """Retrieval ignores permissions; the access code itself is intact, so only the
    passage and prompt checks can notice."""
    monkeypatch.setattr(
        "app.rag.retrieval.readable_documents_clause",
        lambda p: Document.organization_id == p.organization_id,
    )
    report = _run(db, estate.tenant.org.id)
    assert report.verdict == ProofVerdict.FAILED
    kinds = {f["kind"] for f in report.findings}
    assert {"retrieved", "prompt_text"} <= kinds
    assert not kinds & {"access_extra", "access_missing"}
    assert {"retrieved", "prompt_text"} <= _kinds(report, estate.board_doc.id)


def test_an_admin_bypass_put_back_into_the_access_rule_fails_the_run(db, estate, monkeypatch):
    original = access.readable_documents_clause

    def with_admin_bypass(p):
        if p.is_org_admin:
            return Document.organization_id == p.organization_id
        return original(p)

    monkeypatch.setattr(access, "readable_documents_clause", with_admin_bypass)
    monkeypatch.setattr("app.rag.retrieval.readable_documents_clause", with_admin_bypass)
    report = _run(db, estate.tenant.org.id)
    assert report.verdict == ProofVerdict.FAILED
    admin_group = next(
        n for n, g in enumerate(report.groups) if str(estate.tenant.admin.id) in g["members"]
    )
    flagged = {(f["kind"], f["document_id"]) for f in report.findings if f["group"] == admin_group}
    for doc in (estate.board_doc, estate.engineering_doc, estate.salary_doc):
        assert {("access_extra", str(doc.id)), ("retrieved", str(doc.id))} <= flagged
    # The outsider reads what the admin reads (nothing), but an admin is a group of their
    # own, so the bypass is asked about whatever the email order.
    assert report.groups[admin_group]["members"] == [str(estate.tenant.admin.id)]


def test_every_member_is_checked_not_only_the_one_asked(db, factory, estate, monkeypatch):
    """Aaron and the outsider have the same records, except that the outsider once uploaded
    to Board and is no longer in it. A bug in the uploader rule shows only for the
    outsider, who is not the member the group is asked as."""
    t = estate.tenant
    aaron = factory.user("aaron@acme.example.com", full_name="Aaron")  # before "outsider"
    factory.member(t.org, aaron, OrgRole.EMPLOYEE)
    left_behind = factory.document(
        estate.board, uploaded_by=t.outsider, title="Vendor shortlist", restricted=True
    )
    factory.chunks(left_behind, ["Vendor shortlist: three logistics firms for the Pune site."])
    original = access.readable_documents_clause

    def uploader_rule_without_membership(p):
        return or_(original(p), Document.uploaded_by == p.user_id)

    monkeypatch.setattr(access, "readable_documents_clause", uploader_rule_without_membership)
    monkeypatch.setattr(
        "app.rag.retrieval.readable_documents_clause", uploader_rule_without_membership
    )
    report = _run(db, t.org.id)
    number, group = next(
        (n, g) for n, g in enumerate(report.groups) if str(t.outsider.id) in g["members"]
    )
    assert group["representative"] == str(aaron.id)
    assert report.verdict == ProofVerdict.FAILED
    assert {
        "group": number,
        "kind": "access_extra",
        "document_id": str(left_behind.id),
        "count": 1,
    } in report.findings


def test_text_reaching_the_request_another_way_is_caught_in_every_request(db, estate, monkeypatch):
    """Retrieval and access are intact, but the board minutes get into the request some
    other way, so only the text check can notice. It must notice in every request, also
    those asking with the minutes' own text."""
    original = engine.build_messages

    def leaky(question, passages, history):
        *earlier, last = original(question, passages, history)
        return [*earlier, replace(last, content=f"{last.content}\n{BOARD_TEXT}")]

    monkeypatch.setattr(engine, "build_messages", leaky)
    report = _run(db, estate.tenant.org.id)
    assert report.verdict == ProofVerdict.FAILED
    assert {f["kind"] for f in report.findings} == {"prompt_text"}
    viewer = next(
        n
        for n, g in enumerate(report.groups)
        if g["representative"] == str(estate.tenant.viewer.id)
    )
    board = next(
        f
        for f in report.findings
        if f["group"] == viewer and f["document_id"] == str(estate.board_doc.id)
    )
    assert board["count"] == report.groups[viewer]["prompts_checked"] > 0


LONG_TITLE = "Information Security Policy for Third-Party Vendors, Contractors and Temporary Staff"
LONG_SECTION = "Onboarding and offboarding of every vendor account across the company"


def test_a_readable_title_or_section_quoted_in_a_restricted_document_is_not_a_leak(
    db, factory, estate
):
    """The request names each passage's title and section. Quoted in a document the reader
    may not read, they are still the readable document's own words."""
    t = estate.tenant
    policy = factory.document(t.workspace, uploaded_by=t.editor, title=LONG_TITLE)
    factory.chunks(
        policy,
        [{"content": "Vendors sign the access agreement first.", "section_title": LONG_SECTION}],
    )
    memo = factory.document(estate.board, uploaded_by=t.manager, title="Vendor memo")
    factory.chunks(
        memo, [f"The directors want the {LONG_TITLE} rewritten. {LONG_SECTION} is overdue."]
    )
    report = _run(db, t.org.id)
    assert report.verdict == ProofVerdict.PASSED, report.findings


def test_a_capped_run_says_how_much_it_covered(db, estate):
    report = _run(db, estate.tenant.org.id, max_probes=2)
    assert report.summary["probes_run"] == 2
    assert report.summary["probes_planned"] > 2
    assert report.verdict == ProofVerdict.PASSED


def test_nothing_to_test_when_everyone_reads_everything(db, factory, tenant):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    factory.chunks(doc, [ENGINEERING_TEXT])
    for user in (tenant.admin, tenant.outsider):
        factory.workspace_member(tenant.workspace, user)
    report = _run(db, tenant.org.id)
    assert report.verdict == ProofVerdict.NOTHING_TO_TEST


# --- The oracle agrees with the application ----------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_the_oracle_and_the_access_rule_agree_on_random_permissions(db, factory, tenant, seed):
    rng = random.Random(seed)
    users = [tenant.admin, tenant.manager, tenant.editor, tenant.viewer, tenant.outsider]
    workspaces = [tenant.workspace] + [factory.workspace(tenant.org) for _ in range(2)]
    for ws in workspaces[1:]:
        for user in rng.sample(users, 3):
            factory.workspace_member(ws, user, rng.choice(list(WorkspaceRole)))
    docs = []
    for _ in range(12):
        ws = rng.choice(workspaces)
        grants = {u: rng.choice(list(PermissionLevel)) for u in users if rng.random() < 0.15}
        docs.append(
            factory.document(
                ws,
                uploaded_by=rng.choice([*users, None]),
                restricted=rng.random() < 0.5,
                grants=grants,
            )
        )
    with get_sessionmaker()() as session:  # grants naming another workspace
        set_tenant(session, tenant.org.id)
        for doc in rng.sample(docs, 4):
            session.add(
                DocumentPermission(
                    document_id=doc.id,
                    organization_id=tenant.org.id,
                    principal_type=PrincipalType.WORKSPACE,
                    principal_id=rng.choice([w for w in workspaces if w.id != doc.workspace_id]).id,
                    permission=PermissionLevel.READ,
                )
            )
        session.commit()

    set_tenant(db, tenant.org.id)
    snapshot = oracle.load_snapshot(db, tenant.org.id)
    members = {m.user_id: m for m in snapshot.members}
    for user in users:
        principal = factory.principal(user, tenant.org)
        assert dict(members[user.id].workspace_roles) == dict(principal.workspace_roles)
        expected = oracle.readable(snapshot, user.id, members[user.id].workspace_roles)
        assert readable_document_ids(db, principal, [d.id for d in docs]) == expected


def test_deactivated_people_are_not_tested(db, factory, estate):
    with get_sessionmaker()() as session:
        user = session.get(type(estate.tenant.viewer), estate.tenant.viewer.id)
        user.is_active = False
        session.commit()
    set_tenant(db, estate.tenant.org.id)
    snapshot = oracle.load_snapshot(db, estate.tenant.org.id)
    assert estate.tenant.viewer.id not in {m.user_id for m in snapshot.members}


# --- Text check and allocation -----------------------------------------------------------------


SECRET = " ".join(f"token{i:03d}" for i in range(40))  # 319 characters, nothing shared


def test_a_copied_run_is_found_at_any_offset():
    a, b = uuid.uuid4(), uuid.uuid4()
    index = TextIndex({a: [SECRET], b: ["An unrelated public note about lunch."]})
    for offset in range(STRIDE):
        run = SECRET[offset : offset + WINDOW + STRIDE - 1]
        assert index.private(f"<source>{run}</source>", {b}, {}) == {a}


def test_case_spacing_and_escaping_do_not_hide_a_copy():
    a = uuid.uuid4()
    index = TextIndex({a: [SECRET]})
    disguised = SECRET[:120].upper().replace(" ", "  \n ").replace("token001", "token001 ")
    assert index.private(disguised.replace("TOKEN010", "&#84;OKEN010"), set(), {}) == {a}


def test_text_shared_with_a_readable_document_is_not_a_leak():
    shared = "This document is synthetic and was generated for the evaluation of the assistant."
    a, b = uuid.uuid4(), uuid.uuid4()
    index = TextIndex({a: [f"{shared} {SECRET}"], b: [f"Preface. {shared}"]})
    assert index.private(f"Intro {shared} outro", {b}, {}) == set()
    assert index.private(f"{shared} {SECRET[:100]}", {b}, {}) == {a}


def test_a_shared_line_ending_a_passage_is_not_a_leak():
    """The prompt puts a space (from the markup) after a passage's last sentence. A run
    ending in that space is not document text, whatever the alignment."""
    marker = "Written for evaluation and demos; it describes no real company."
    a, b = uuid.uuid4(), uuid.uuid4()
    for pad in range(STRIDE):
        index = TextIndex(
            {a: [f"{'x' * pad} {marker} Confidential: the board meets on Friday."], b: [marker]}
        )
        prompt = f'<source id="S1" title="Runbook">\n{marker}\n</source>\n<source id="S2">'
        assert index.private(prompt, {b}, {}) == set(), pad


def test_runs_shorter_than_the_window_are_not_evidence():
    a = uuid.uuid4()
    assert TextIndex({a: [SECRET]}).private(SECRET[: WINDOW - 1], set(), {}) == set()


@pytest.mark.parametrize(
    ("sizes", "budget"), [([10, 1, 0, 5], 9), ([3, 3], 100), ([7, 7, 7], 1), ([], 5)]
)
def test_allocation_spreads_the_budget_evenly(sizes, budget):
    quotas = allocate(sizes, budget)
    assert sum(quotas) == min(budget, sum(sizes))
    assert all(0 <= q <= s for q, s in zip(quotas, sizes, strict=True))
    unfinished = [q for q, s in zip(quotas, sizes, strict=True) if q < s]
    assert not unfinished or max(unfinished) - min(unfinished) <= 1


# --- API and worker ----------------------------------------------------------------------------


RUNS = "/api/v1/admin/permission-proof/runs"


def _execute(enqueued):
    results = []
    for run_id, org_id in enqueued.named("eka.proof.run"):
        results.append(permission_proof.execute(uuid.UUID(run_id), uuid.UUID(org_id)))
    enqueued.tasks = [t for t in enqueued.tasks if t[0] != "eka.proof.run"]
    return results


def test_only_org_admins_run_or_read_leak_tests(login_as, estate):
    t = estate.tenant
    for user in (t.manager, t.editor, t.viewer):
        client = login_as(user, t.org)
        assert client.post(RUNS, json={}).status_code == 403
        assert client.get(RUNS).status_code == 403


def test_a_run_is_queued_executed_and_reported_without_document_text(login_as, estate, enqueued):
    t = estate.tenant
    admin = login_as(t.admin, t.org)
    started = admin.post(RUNS, json={})
    assert started.status_code == 202, started.text
    run = started.json()
    assert (run["kind"], run["status"], run["requested_by"]["email"]) == (
        "manual",
        "queued",
        t.admin.email,
    )
    assert admin.post(RUNS, json={}).json()["error"]["code"] == "proof_running"

    assert _execute(enqueued) == ["passed"]
    # A message delivered twice runs the test once.
    assert permission_proof.execute(uuid.UUID(run["id"]), t.org.id) == "skipped"
    detail = admin.get(f"{RUNS}/{run['id']}").json()
    assert (detail["status"], detail["verdict"]) == ("completed", "passed")
    assert detail["summary"]["probes_run"] > 0
    names = {p["full_name"] for g in detail["groups"] for p in g["members"]}
    assert t.viewer.full_name in names
    body = json.dumps(detail)
    for secret in (BOARD_TEXT[:40], SALARY_TEXT[:40], ENGINEERING_TEXT[:40], "Nimbus"):
        assert secret not in body

    listing = admin.get(RUNS).json()
    assert listing["total"] == 1 and listing["items"][0]["verdict"] == "passed"
    assert admin.post(RUNS, json={}).status_code == 202  # the finished run no longer blocks


def test_a_simulation_is_caught_and_hides_titles_the_admin_cannot_read(
    login_as, factory, estate, enqueued
):
    t = estate.tenant
    admin = login_as(t.admin, t.org)
    target = {"user_id": str(t.viewer.id), "workspace_id": str(estate.board.id)}
    run = admin.post(RUNS, json={"simulate": target}).json()
    assert run["kind"] == "simulation"
    assert run["simulation"]["workspace"]["name"] == "Board"
    assert _execute(enqueued) == ["failed"]

    detail = admin.get(f"{RUNS}/{run['id']}").json()
    board = [f for f in detail["findings"] if f["document_id"] == str(estate.board_doc.id)]
    assert {f["kind"] for f in board} == {"access_extra", "retrieved", "prompt_text"}
    assert all(f["title"] is None and f["workspace"]["name"] == "Board" for f in board)
    assert "Board minutes" not in json.dumps(detail)

    factory.workspace_member(estate.board, t.admin)  # the admin joins: now they may read it
    detail = admin.get(f"{RUNS}/{run['id']}").json()
    board = [f for f in detail["findings"] if f["document_id"] == str(estate.board_doc.id)]
    assert {f["title"] for f in board} == {"Board minutes"}


@pytest.mark.parametrize(
    ("who", "where", "code"),
    [
        ("viewer", "engineering", "proof_already_member"),
        ("stranger", "board", "proof_unknown_person"),
        ("viewer", "nowhere", "proof_unknown_workspace"),
        ("manager", "empty", "proof_already_member"),
        ("viewer", "empty", "proof_nothing_to_catch"),
        ("viewer", "restricted", "proof_nothing_to_catch"),  # a viewer would gain nothing
    ],
)
def test_a_simulation_that_could_not_catch_anything_is_refused(
    login_as, factory, estate, who, where, code
):
    t = estate.tenant
    empty = factory.workspace(t.org, "Empty", members={t.manager: WorkspaceRole.OWNER})
    restricted = factory.workspace(t.org, "Legal", members={t.manager: WorkspaceRole.OWNER})
    factory.chunks(
        factory.document(restricted, uploaded_by=t.manager, restricted=True), [SALARY_TEXT]
    )
    people = {"viewer": t.viewer.id, "manager": t.manager.id, "stranger": uuid.uuid4()}
    places = {
        "engineering": t.workspace.id,
        "board": estate.board.id,
        "empty": empty.id,
        "restricted": restricted.id,
        "nowhere": uuid.uuid4(),
    }
    response = login_as(t.admin, t.org).post(
        RUNS, json={"simulate": {"user_id": str(people[who]), "workspace_id": str(places[where])}}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


def test_another_organizations_runs_are_invisible(login_as, estate, other_tenant, enqueued):
    t = estate.tenant
    run = login_as(t.admin, t.org).post(RUNS, json={}).json()
    other = login_as(other_tenant.admin, other_tenant.org)
    assert other.get(f"{RUNS}/{run['id']}").status_code == 404
    assert other.get(RUNS).json()["total"] == 0
    assert other.post(RUNS, json={}).status_code == 202  # their own run is not blocked


def test_a_run_that_could_not_be_queued_is_an_error_and_blocks_nothing(login_as, estate, enqueued):
    admin = login_as(estate.tenant.admin, estate.tenant.org)
    enqueued.fail = True
    run = admin.post(RUNS, json={}).json()
    assert run["status"] == "error" and "could not be queued" in run["error"]
    enqueued.fail = False
    assert admin.post(RUNS, json={}).status_code == 202


def test_a_lost_run_shows_as_interrupted_and_blocks_nothing(login_as, estate, enqueued):
    t = estate.tenant
    admin = login_as(t.admin, t.org)
    run = admin.post(RUNS, json={}).json()
    lost = utcnow() - timedelta(seconds=get_settings().ingestion_lease_seconds + 120)
    with get_sessionmaker()() as session:
        set_tenant(session, t.org.id)
        session.execute(
            update(PermissionProofRun)
            .where(PermissionProofRun.id == uuid.UUID(run["id"]))
            .values(created_at=lost)
        )
        session.commit()
    assert admin.get(f"{RUNS}/{run['id']}").json()["status"] == "interrupted"
    assert admin.post(RUNS, json={}).status_code == 202


def test_a_run_that_started_late_is_timed_from_its_start(login_as, estate, enqueued):
    t = estate.tenant
    admin = login_as(t.admin, t.org)
    run = admin.post(RUNS, json={}).json()
    long_ago = utcnow() - timedelta(seconds=get_settings().ingestion_lease_seconds + 120)
    with get_sessionmaker()() as session:
        set_tenant(session, t.org.id)
        session.execute(
            update(PermissionProofRun)
            .where(PermissionProofRun.id == uuid.UUID(run["id"]))
            .values(created_at=long_ago, started_at=utcnow(), status=ProofStatus.RUNNING)
        )
        session.commit()
    assert admin.get(f"{RUNS}/{run['id']}").json()["status"] == "running"
    assert admin.post(RUNS, json={}).status_code == 409


def test_a_run_that_fails_is_recorded_and_logs_no_document_text(
    login_as, estate, enqueued, monkeypatch, caplog
):
    def times_out(*args, **kwargs):
        # A database error carries its statement's parameters: here, a probe's text.
        raise OperationalError("SELECT ... %(q)s", {"q": BOARD_TEXT}, Exception("timeout"))

    monkeypatch.setattr(engine, "run", times_out)
    admin = login_as(estate.tenant.admin, estate.tenant.org)
    run = admin.post(RUNS, json={}).json()
    with caplog.at_level(logging.INFO):
        assert _execute(enqueued) == ["error"]
    detail = admin.get(f"{RUNS}/{run['id']}").json()
    assert (detail["status"], detail["error"]) == (
        "error",
        "The leak test stopped (OperationalError).",
    )
    assert any(r.getMessage() == "proof.failed" for r in caplog.records)
    assert "Nimbus" not in caplog.text
    assert admin.post(RUNS, json={}).status_code == 202


def test_runs_and_results_are_audited_without_text(login_as, estate, enqueued):
    t = estate.tenant
    admin = login_as(t.admin, t.org)
    run = admin.post(RUNS, json={}).json()
    _execute(enqueued)
    events = admin.get("/api/v1/admin/audit-events", params={"action": "permission_proof.*"})
    by_action = {e["action"]: e for e in events.json()["items"]}
    assert by_action["permission_proof.run_requested"]["resource_id"] == run["id"]
    completed = by_action["permission_proof.run_completed"]["metadata"]
    assert completed["verdict"] == "passed" and completed["findings"] == 0


def test_the_weekly_schedule_queues_one_run_per_organization(estate, other_tenant, enqueued):
    assert permission_proof.schedule_all() == 2
    assert len(enqueued.named("eka.proof.run")) == 2
    assert permission_proof.schedule_all() == 0  # both still active
    assert sorted(_execute(enqueued)) == ["nothing_to_test", "passed"]
