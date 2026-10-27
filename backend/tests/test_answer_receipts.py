"""Answer receipts: each statement checked against the passages it cites
(app/rag/claims.py), every answer pinned to the document versions it used, and old
answers marked once a cited document has a newer version."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from conftest import _alembic_config
from sqlalchemy import delete, select, text, update
from test_answer_fakes import CAFETERIA, LEAVE, FakeLLM, index_handbook

from app.db.session import session_scope, set_tenant
from app.models import (
    AuditEvent,
    Document,
    DocumentPermission,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    Message,
)
from app.providers.registry import get_llm
from app.rag.claims import ClaimCheck, Verdict, check_claims

QUESTION = "How many vacation days do employees receive?"
MIXED = (
    "STATUS: ANSWERED\n\n"
    "Employees receive 25 days of paid vacation per year [S1]. "
    "Each vacation request needs approval from 3 directors [S1]."
)


@pytest.fixture
def handbook(factory, tenant):
    return index_handbook(factory, tenant)


def ask(app, client, text=MIXED):
    app.dependency_overrides[get_llm] = lambda: FakeLLM(text)
    response = client.post("/api/v1/chat", json={"message": QUESTION})
    assert response.status_code == 200, response.text
    return response.json()


def history(client, conversation_id):
    return client.get(f"/api/v1/conversations/{conversation_id}/messages").json()["items"][1]


def replace_indexed_version(db, tenant, document, since):
    """What the ingestion runner does once a new upload is indexed."""
    set_tenant(db, tenant.org.id)
    db.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(version=2, indexed_version=2, version_indexed_at=since)
    )
    db.commit()


def test_each_statement_is_checked_against_the_passages_it_cites():
    passages = {"S1": LEAVE, "S2": CAFETERIA}
    answer = (
        "Employees receive 25 days of paid vacation per year [S1]. "
        "Employees receive 30 days of paid vacation [S1]. "
        "Managers approve cafeteria menus weekly [S2]. "
        "Remote staff get the same allowance as everyone else. "
        "Vacation requests need approval from a manager [S9]."
    )
    matched, wrong_number, few_words, uncited, unknown = check_claims(
        answer, "How many vacation days?", passages
    )
    assert matched == ClaimCheck(
        "Employees receive 25 days of paid vacation per year.", ("S1",), Verdict.SUPPORTED
    )
    assert (wrong_number.verdict, wrong_number.missing_numbers) == (Verdict.UNSUPPORTED, ("30",))
    assert (few_words.verdict, few_words.missing_numbers) == (Verdict.UNSUPPORTED, ())
    assert (uncited.verdict, uncited.source_ids) == (Verdict.UNCITED, ())
    # A source the answer was not given supports nothing.
    assert (unknown.verdict, unknown.source_ids) == (Verdict.UNSUPPORTED, ("S9",))


def test_an_echo_of_a_leading_question_is_not_a_match():
    """The question's own number and words must be in the passage too: otherwise "7 years"
    passes because the question said it, while the passage says 5."""
    passages = {"S1": "Tax records have a retention period of 5 years."}
    question = "Is the retention period 7 years?"
    (echo,) = check_claims("The retention period is 7 years [S1].", question, passages)
    assert (echo.verdict, echo.missing_numbers) == (Verdict.UNSUPPORTED, ("7",))
    (right,) = check_claims("The retention period is 5 years [S1].", question, passages)
    assert right.verdict is Verdict.SUPPORTED
    # It adds no word to the question, so all its words are compared: only "years" is there.
    other = {"S1": "Laptops are replaced every 7 years."}
    (unrelated,) = check_claims("The retention period is 7 years [S1].", question, other)
    assert unrelated.verdict is Verdict.UNSUPPORTED
    # The evaluation's faithfulness metric keeps its published, looser rule.
    echo_text = "The retention period is 7 years [S1]."
    (loose,) = check_claims(echo_text, question, passages, strict=False)
    assert loose.verdict is Verdict.SUPPORTED


def test_an_answer_comes_with_its_receipt(app, login_as, tenant, handbook, db):
    client = login_as(tenant.viewer, tenant.org)
    body = ask(app, client)
    answer = body["assistant_message"]
    assert answer["claims"] == [
        {
            "text": "Employees receive 25 days of paid vacation per year.",
            "source_ids": ["S1"],
            "verdict": "supported",
            "missing_numbers": [],
        },
        {
            "text": "Each vacation request needs approval from 3 directors.",
            "source_ids": ["S1"],
            "verdict": "unsupported",
            "missing_numbers": ["3"],
        },
    ]
    assert {s["document_version"] for s in answer["sources"]} == {1}
    assert answer["answer_status"] == "answered"  # the receipt never changes the status
    stored = history(client, body["conversation_id"])
    assert stored["claims"] == answer["claims"] and stored["changed_documents"] == []
    set_tenant(db, tenant.org.id)
    audited = db.scalars(select(AuditEvent).where(AuditEvent.action == "chat.answered")).one()
    assert (audited.event_metadata["claims"], audited.event_metadata["claims_supported"]) == (2, 1)


def test_an_abstention_has_no_statements(app, login_as, tenant, handbook):
    client = login_as(tenant.viewer, tenant.org)
    answer = ask(app, client, "STATUS: INSUFFICIENT\n\nThe documents do not say.")
    assert answer["assistant_message"]["answer_status"] == "insufficient_evidence"
    assert answer["assistant_message"]["claims"] == []


def test_an_old_answer_names_the_version_it_used(app, login_as, factory, tenant, handbook, db):
    notes = factory.document(tenant.workspace, uploaded_by=tenant.editor, title="Leave Notes")
    factory.chunks(notes, ["Vacation days carry over to the next year."])
    client = login_as(tenant.viewer, tenant.org)
    body = ask(app, client)
    sources = body["assistant_message"]["sources"]
    cited = {s["document_id"] for s in sources if s["cited"]}
    assert cited == {str(handbook.id)} and str(notes.id) in {s["document_id"] for s in sources}

    since = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)
    for document in (handbook, notes):
        replace_indexed_version(db, tenant, document, since)
    (change,) = history(client, body["conversation_id"])["changed_documents"]
    # Only the cited document: a passage the answer did not cite did not shape it.
    assert change == {
        "document_id": str(handbook.id),
        "document_title": "Employee Handbook",
        "used_version": 1,
        "current_version": 2,
        "current_since": "2026-10-26T09:30:00Z",
    }

    # Once the answer is withheld, so is everything about it.
    set_tenant(db, tenant.org.id)
    db.execute(delete(DocumentPermission).where(DocumentPermission.document_id == handbook.id))
    db.commit()
    withheld = history(client, body["conversation_id"])
    assert withheld["redacted"] and withheld["claims"] == [] and withheld["changed_documents"] == []


def test_an_answer_is_pinned_to_the_indexed_version_not_the_latest_upload(
    app, login_as, tenant, handbook, db
):
    set_tenant(db, tenant.org.id)
    db.execute(update(Document).where(Document.id == handbook.id).values(version=2))
    db.commit()  # version 2 is uploaded, but its text is not indexed yet
    client = login_as(tenant.viewer, tenant.org)
    body = ask(app, client)
    assert {s["document_version"] for s in body["assistant_message"]["sources"]} == {1}
    assert history(client, body["conversation_id"])["changed_documents"] == []


def test_an_answer_with_passages_of_two_versions_counts_the_older(
    app, login_as, tenant, handbook, db
):
    client = login_as(tenant.viewer, tenant.org)
    body = ask(app, client)
    set_tenant(db, tenant.org.id)
    message = db.get(Message, uuid.UUID(body["assistant_message"]["id"]))
    first, *rest = message.sources
    # A version-2 swap committed between the two searches: the first source is the newer.
    message.sources = [
        {**first, "document_version": 2},
        {**first, "source_id": "S9", "document_version": 1, "cited": True},
        *rest,
    ]
    db.commit()
    replace_indexed_version(db, tenant, handbook, datetime(2026, 10, 26, tzinfo=UTC))
    (change,) = history(client, body["conversation_id"])["changed_documents"]
    assert (change["used_version"], change["current_version"]) == (1, 2)


def test_answers_stored_before_versions_were_recorded_are_never_marked(
    app, login_as, tenant, handbook, db
):
    client = login_as(tenant.viewer, tenant.org)
    body = ask(app, client)
    set_tenant(db, tenant.org.id)
    message = db.get(Message, uuid.UUID(body["assistant_message"]["id"]))
    message.sources = [
        {k: v for k, v in source.items() if k != "document_version"} for source in message.sources
    ]
    db.commit()
    replace_indexed_version(db, tenant, handbook, datetime(2026, 10, 26, tzinfo=UTC))
    assert history(client, body["conversation_id"])["changed_documents"] == []


def test_the_migration_backfills_versions_from_ingestion_history(factory, tenant, _database):
    """0004 under the schema owner, which row-level security binds like the app role."""
    t1, t2, t3 = (datetime(2026, 10, day, tzinfo=UTC) for day in (1, 2, 3))
    replaced, plain, pending, unknown = (
        factory.document(tenant.workspace, uploaded_by=tenant.editor) for _ in range(4)
    )
    never = factory.document(
        tenant.workspace, uploaded_by=tenant.editor, status=DocumentStatus.QUEUED
    )
    jobs = [
        (replaced, 1, t1),
        (replaced, 1, t2),  # a re-index keeps the date version 1 went live
        (replaced, 2, t3),
        (pending, 1, t1),
        (pending, 1, t2),
    ]
    with session_scope(tenant.org.id) as db:
        db.add_all(
            IngestionJob(
                document_id=doc.id,
                organization_id=tenant.org.id,
                document_version=version,
                status=JobStatus.SUCCEEDED,
                max_attempts=3,
                completed_at=at,
            )
            for doc, version, at in jobs
        )
        db.execute(update(Document).where(Document.id == replaced.id).values(version=2))
        # Replaced, the new upload not indexed yet: version 1 is still the searchable one.
        db.execute(
            update(Document)
            .where(Document.id.in_([pending.id, unknown.id]))
            .values(version=2, processing_status=DocumentStatus.QUEUED)
        )
        db.commit()

    cfg = _alembic_config()
    command.downgrade(cfg, "0003")
    command.upgrade(cfg, "head")

    with session_scope(tenant.org.id) as db:
        rows = {
            row.id: (row.indexed_version, row.version_indexed_at)
            for row in db.execute(
                select(Document.id, Document.indexed_version, Document.version_indexed_at)
            )
        }
        plain_indexed_at = db.scalar(select(Document.indexed_at).where(Document.id == plain.id))
    assert rows[replaced.id] == (2, t3)
    assert rows[plain.id] == (1, plain_indexed_at)  # its jobs were removed by retention
    assert rows[pending.id] == (1, t1)
    assert rows[unknown.id] == rows[never.id] == (None, None)
    with _database.connect() as conn:
        forced = conn.execute(
            text(
                "SELECT relname, relforcerowsecurity FROM pg_class"
                " WHERE relname IN ('documents', 'ingestion_jobs')"
            )
        ).all()
    assert dict(forced) == {"documents": True, "ingestion_jobs": True}
