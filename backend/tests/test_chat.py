"""POST /api/v1/chat end to end: conversations, the persisted message format, usage rows,
request validation and isolation between users and tenants."""

from __future__ import annotations

import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from anyio import to_thread
from sqlalchemy import select
from test_answer_fakes import LEAVE, FakeLLM, index_handbook

from app.core.config import get_settings
from app.db.session import set_tenant
from app.models import AuditEvent, Message, MessageRole, MessageStatus, UsageKind, UsageRecord
from app.providers.llm import LLMUnavailableError
from app.providers.registry import get_llm
from app.rag.answer import ERROR_MESSAGE, REFUSED_MESSAGE

CHAT = "/api/v1/chat"
QUESTION = "How many vacation days do employees receive?"
SECRET = "Confidential Project Nightjar: the Berlin office closes in March."


@pytest.fixture
def handbook(factory, tenant):
    return index_handbook(factory, tenant)


def chat(client, message=QUESTION, **body):
    response = client.post(CHAT, json={"message": message, **body})
    assert response.status_code == 200, response.text
    return response.json()


def test_new_conversation_then_follow_up(app, login_as, tenant, handbook):
    llm = FakeLLM(rewrite="Who approves vacation requests?")
    app.dependency_overrides[get_llm] = lambda: llm
    client = login_as(tenant.viewer, tenant.org)

    body = chat(client, "  How many vacation days\ndo employees receive?  ")
    user, assistant = body["user_message"], body["assistant_message"]
    assert user["conversation_id"] == assistant["conversation_id"] == body["conversation_id"]
    assert (user["role"], user["content"], user["status"]) == ("user", QUESTION, "complete")
    assert user["sources"] == [] and user["citations"] == [] and user["answer_status"] is None
    assert (assistant["role"], assistant["status"], assistant["answer_status"]) == (
        "assistant",
        "complete",
        "answered",
    )
    assert assistant["content"] == "Employees receive 25 days of paid vacation per year [S1]."
    assert [(s["source_id"], s["cited"]) for s in assistant["sources"]] == [
        ("S1", True),
        ("S2", False),
    ]
    first = assistant["sources"][0]
    assert (first["document_id"], first["document_title"]) == (
        str(handbook.id),
        "Employee Handbook",
    )
    assert (first["workspace_id"], first["file_type"]) == (str(tenant.workspace.id), "md")
    assert (first["page_number"], first["section_title"], first["excerpt"]) == (3, "Leave", LEAVE)
    assert first["relevance"]["rrf_score"] > 0 and first["redacted"] is False
    (citation,) = assistant["citations"]
    assert citation["source_id"] == "S1" and citation["verified"] is True
    assert citation["document_id"] == str(handbook.id) and citation["page_number"] == 3
    assert citation["excerpt"] == "Employees receive 25 days of paid vacation per year."
    meta = assistant["metadata"]
    assert (meta["search_query"], meta["mode"], meta["reranked"]) == (QUESTION, "hybrid", False)
    assert {"retrieval", "generation", "total"} <= set(meta["timings_ms"])
    assert meta["usage"] == {"input_tokens": 120, "output_tokens": 30, "cost_usd": 0.0}
    assert (meta["provider"], meta["model"]) == ("fake", "fake-model")
    assert meta["invalid_citations"] == [] and meta["unverified_quotes"] == []
    assert "answer_status" not in meta and "citations" not in meta

    follow_up = chat(client, "Who approves them?", conversation_id=body["conversation_id"])
    assert follow_up["conversation_id"] == body["conversation_id"]
    assert follow_up["assistant_message"]["metadata"]["search_query"] == (
        "Who approves vacation requests?"
    )
    answer_request = llm.answer_requests[-1]  # the model saw the earlier turns
    assert [m.role for m in answer_request.messages] == ["user", "assistant", "user"]
    assert answer_request.messages[0].content == QUESTION
    assert answer_request.messages[1].content == assistant["content"]

    listing = client.get("/api/v1/conversations").json()
    assert listing["total"] == 1
    (conversation,) = listing["items"]
    assert conversation["title"] == "How many vacation days"  # first line, collapsed
    assert conversation["message_count"] == 4
    assert conversation["updated_at"] >= conversation["created_at"]


def test_title_is_cut_at_eighty_characters(app, login_as, tenant, handbook):
    app.dependency_overrides[get_llm] = lambda: FakeLLM()
    client = login_as(tenant.viewer, tenant.org)
    chat(client, "word " * 40)
    title = client.get("/api/v1/conversations").json()["items"][0]["title"]
    assert title == ("word " * 16).strip() and len(title) <= 80


def test_persisted_format_and_usage_rows(app, login_as, tenant, handbook, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_input_cost_per_mtok", 1.0)
    monkeypatch.setattr(settings, "llm_output_cost_per_mtok", 10.0)
    monkeypatch.setattr(settings, "llm_rewrite_input_cost_per_mtok", 0.5)
    llm = FakeLLM(rewrite="Who approves vacation requests?")
    app.dependency_overrides[get_llm] = lambda: llm
    client = login_as(tenant.viewer, tenant.org)
    first = chat(client)
    chat(client, "Who approves them?", conversation_id=first["conversation_id"])

    set_tenant(db, tenant.org.id)
    messages = db.scalars(select(Message).order_by(Message.created_at)).all()
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT] * 2
    assert all(m.status == MessageStatus.COMPLETE for m in messages)
    assistant = messages[1]
    assert set(assistant.sources[0]) == {
        "source_id",
        "chunk_id",
        "document_id",
        "document_title",
        "workspace_id",
        "file_type",
        "page_number",
        "page_end",
        "section_title",
        "excerpt",
        "relevance",
        "cited",
        "redacted",
    }
    assert assistant.sources[0]["document_id"] == str(handbook.id)
    assert set(assistant.retrieval_metadata) >= {
        "answer_status",
        "citations",
        "invalid_citations",
        "unverified_quotes",
        "search_query",
        "mode",
        "reranked",
        "timings_ms",
        "usage",
        "provider",
        "model",
    }
    (citation,) = assistant.retrieval_metadata["citations"]
    assert set(citation) >= {"source_id", "verified", "document_id", "document_title", "excerpt"}
    assert citation["document_id"] == str(handbook.id)
    assert messages[3].retrieval_metadata["search_query"] == "Who approves vacation requests?"

    rows = db.scalars(select(UsageRecord)).all()
    assert Counter((r.kind, r.provider, r.model) for r in rows) == {
        (UsageKind.CHAT_COMPLETION, "fake", "fake-model"): 2,
        # Requested from the configured rewrite model (LLM_MODEL unless overridden).
        (UsageKind.QUERY_REWRITE, "fake", settings.query_rewrite_model): 1,
    }
    by_kind = {row.kind: row for row in rows}
    completion = by_kind[UsageKind.CHAT_COMPLETION]
    assert (completion.input_tokens, completion.output_tokens) == (120, 30)
    assert completion.cost_usd == Decimal("0.000420")  # 120 * 1 + 30 * 10 per million
    assert set(completion.timings) == {"retrieval_ms", "generation_ms", "total_ms"}
    assert completion.latency_ms == round(completion.timings["total_ms"])
    assert completion.message_id in {messages[1].id, messages[3].id}
    assert completion.user_id == tenant.viewer.id
    rewrite = by_kind[UsageKind.QUERY_REWRITE]
    assert (rewrite.input_tokens, rewrite.output_tokens) == (10, 5)
    assert rewrite.cost_usd == Decimal("0.000055")  # 10 * 0.5 + 5 * 10 (output falls back)
    assert rewrite.message_id == messages[3].id


def test_conversations_of_other_users_and_tenants_are_not_found(
    app, login_as, tenant, other_tenant, handbook
):
    app.dependency_overrides[get_llm] = lambda: FakeLLM()
    conversation_id = chat(login_as(tenant.viewer, tenant.org))["conversation_id"]
    for client in (
        login_as(tenant.editor, tenant.org),
        login_as(tenant.admin, tenant.org),
        login_as(other_tenant.admin, other_tenant.org),
    ):
        response = client.post(CHAT, json={"message": QUESTION, "conversation_id": conversation_id})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "conversation_not_found"
    response = login_as(tenant.viewer, tenant.org).post(
        CHAT, json={"message": QUESTION, "conversation_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"message": "   "}, "empty_query"),
        ({"message": "​"}, "empty_query"),
        ({}, "validation_error"),
        ({"message": ""}, "validation_error"),
        ({"message": "x" * 2001}, "validation_error"),
        ({"message": "ok", "conversation_id": "nope"}, "validation_error"),
        ({"message": "ok", "scope": {"document_ids": ["bad"]}}, "validation_error"),
    ],
)
def test_invalid_requests_are_422_and_persist_nothing(login_as, tenant, payload, code):
    client = login_as(tenant.viewer, tenant.org)
    response = client.post(CHAT, json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert client.get("/api/v1/conversations").json()["total"] == 0


def test_scope_narrows_retrieval_but_never_widens_access(app, login_as, factory, tenant, handbook):
    secret = factory.document(
        tenant.workspace, uploaded_by=tenant.manager, title="Restructuring", restricted=True
    )
    factory.chunks(secret, [SECRET])
    llm = FakeLLM("STATUS: ANSWERED\n\nThe Berlin office closes in March [S1].")
    app.dependency_overrides[get_llm] = lambda: llm
    question = "When does the Berlin office close?"
    viewer = login_as(tenant.viewer, tenant.org)

    scoped = chat(viewer, question, scope={"document_ids": [str(secret.id)]})["assistant_message"]
    assert scoped["answer_status"] == "insufficient_evidence" and scoped["sources"] == []
    elsewhere = chat(viewer, question, scope={"workspace_ids": [str(uuid.uuid4())]})
    assert elsewhere["assistant_message"]["sources"] == []
    unscoped = chat(viewer, question, scope={"workspace_ids": [], "document_ids": []})
    assert {s["document_id"] for s in unscoped["assistant_message"]["sources"]} == {
        str(handbook.id)
    }
    assert all("Nightjar" not in m.content for r in llm.requests for m in r.messages)

    owner = chat(
        login_as(tenant.manager, tenant.org), question, scope={"document_ids": [str(secret.id)]}
    )
    assert [s["excerpt"] for s in owner["assistant_message"]["sources"]] == [SECRET]
    assert owner["assistant_message"]["answer_status"] == "answered"


def test_provider_failure_is_persisted_as_an_error_turn(app, login_as, tenant, handbook, db):
    app.dependency_overrides[get_llm] = lambda: FakeLLM(
        error=LLMUnavailableError("down", retryable=True)
    )
    body = chat(login_as(tenant.viewer, tenant.org))
    assistant = body["assistant_message"]
    assert (assistant["status"], assistant["answer_status"]) == ("error", "error")
    assert assistant["content"] == ERROR_MESSAGE
    assert assistant["metadata"]["error"] == {"kind": "unavailable", "retryable": True}
    assert [s["source_id"] for s in assistant["sources"]] == ["S1", "S2"]
    set_tenant(db, tenant.org.id)
    (row,) = db.scalars(select(UsageRecord)).all()
    assert (row.kind, row.input_tokens, row.output_tokens) == (UsageKind.CHAT_COMPLETION, 0, 0)


def test_refusal_is_a_neutral_message_with_uncited_sources(app, login_as, tenant, handbook):
    app.dependency_overrides[get_llm] = lambda: FakeLLM("No.", stop_reason="refusal")
    assistant = chat(login_as(tenant.viewer, tenant.org))["assistant_message"]
    assert (assistant["status"], assistant["answer_status"]) == ("complete", "refused")
    assert assistant["content"] == REFUSED_MESSAGE
    assert assistant["citations"] == [] and not any(s["cited"] for s in assistant["sources"])


def test_default_configuration_answers_extractively(login_as, tenant, handbook, db):
    assistant = chat(login_as(tenant.viewer, tenant.org))["assistant_message"]
    assert assistant["answer_status"] == "answered"
    assert assistant["content"] == "- Employees receive 25 days of paid vacation per year. [S1]"
    assert assistant["citations"][0]["verified"] is True
    assert (assistant["metadata"]["provider"], assistant["metadata"]["model"]) == (
        "extractive",
        None,
    )
    set_tenant(db, tenant.org.id)
    (row,) = db.scalars(select(UsageRecord)).all()
    assert (row.provider, row.model, row.input_tokens, row.cost_usd) == (
        "extractive",
        "extractive",
        0,
        Decimal(0),
    )
    assert set(row.timings) == {"retrieval_ms", "generation_ms", "total_ms"}


def test_answers_and_searches_are_audited_by_document_never_by_text(login_as, tenant, handbook, db):
    client = login_as(tenant.viewer, tenant.org)
    assistant = chat(client)["assistant_message"]
    search = {"query": "vacation days", "mode": "keyword", "rerank": False}
    assert client.post("/api/v1/search", json=search).status_code == 200

    set_tenant(db, tenant.org.id)
    events = {e.action: e for e in db.scalars(select(AuditEvent))}
    answered, searched = events["chat.answered"], events["search.performed"]
    assert (answered.actor_user_id, answered.resource_id) == (tenant.viewer.id, assistant["id"])
    document = str(handbook.id)
    assert answered.event_metadata == {
        "conversation_id": assistant["conversation_id"],
        "status": "complete",
        "documents_retrieved": [document],
        "documents_cited": [document],
        "documents_from_history": [],
    }
    assert searched.actor_user_id == tenant.viewer.id
    assert searched.event_metadata == {"mode": "keyword", "results": 1, "documents": [document]}
    recorded = str([answered.event_metadata, searched.event_metadata]).lower()
    assert "vacation" not in recorded  # neither the question nor the passages


def test_chat_requires_a_session(client):
    assert client.post(CHAT, json={"message": QUESTION}).status_code == 401


class BlockingLLM(FakeLLM):
    """Generates only once the test sets `release`, like a slow model."""

    def __init__(self) -> None:
        super().__init__()
        self.generating, self.release = threading.Event(), threading.Event()

    def stream(self, request: Any) -> Iterator[Any]:
        self.generating.set()
        self.release.wait(timeout=60)
        yield from super().stream(request)


def test_generation_does_not_occupy_the_shared_worker_threads(app, login_as, tenant, handbook):
    """SEC-03: sync endpoints (auth, health, documents, ...) share one worker-thread pool.
    A non-streaming chat must not keep one of those threads while the model generates,
    or a few slow chats stall the whole API. Here the pool has a single thread."""
    llm = BlockingLLM()
    app.dependency_overrides[get_llm] = lambda: llm
    responses: dict[str, Any] = {}

    async def one_shared_thread() -> None:
        to_thread.current_default_thread_limiter().total_tokens = 1

    with login_as(tenant.viewer, tenant.org) as client:  # one event loop for both requests
        client.portal.call(one_shared_thread)

        def ask() -> None:
            responses["chat"] = client.post(CHAT, json={"message": QUESTION})

        def me() -> None:
            responses["me"] = client.get("/api/v1/auth/me")

        asking = threading.Thread(target=ask)
        asking.start()
        try:
            assert llm.generating.wait(timeout=30)
            other = threading.Thread(target=me)
            other.start()
            other.join(timeout=15)
            assert "me" in responses, "a sync endpoint waited for the generation to finish"
            assert responses["me"].status_code == 200
            assert "chat" not in responses  # still generating
        finally:
            llm.release.set()
            asking.join(timeout=60)
            if "me" not in responses:
                other.join(timeout=60)
    assert responses["chat"].status_code == 200, responses["chat"].text
    assert responses["chat"].json()["assistant_message"]["answer_status"] == "answered"
