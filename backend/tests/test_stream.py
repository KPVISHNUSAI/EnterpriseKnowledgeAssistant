"""POST /api/v1/chat/stream: event order, provider failures, cancellation, validation."""

from __future__ import annotations

import json
import threading
import uuid

import pytest
from sqlalchemy import func, select
from test_answer_fakes import FakeLLM, index_handbook

from app.core.config import get_settings
from app.db.session import set_tenant
from app.models import AuditEvent, Message, MessageStatus, UsageRecord
from app.providers.embeddings import HashingEmbedder
from app.providers.llm import LLMRateLimitError, LLMUnavailableError
from app.providers.registry import get_llm
from app.rag.answer import ERROR_MESSAGE, RATE_LIMITED_MESSAGE
from app.schemas.chat import ChatRequest
from app.services import chat as chat_service
from app.services.audit import RequestMeta

STREAM = "/api/v1/chat/stream"
QUESTION = "How many vacation days do employees receive?"
LONG_ANSWER = "STATUS: ANSWERED\n\n" + "Employees receive 25 days of paid vacation [S1]. " * 30


@pytest.fixture
def handbook(factory, tenant):
    return index_handbook(factory, tenant)


def parse_events(lines) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name, data = None, []
    for line in lines:
        if line == "":
            if name is not None:
                events.append((name, json.loads("\n".join(data))))
            name, data = None, []
        elif line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data.append(line[len("data: ") :])
    return events


def stream(client, **body):
    settings = get_settings()
    headers = {settings.csrf_header_name: client.cookies.get(settings.csrf_cookie_name)}
    with client.stream("POST", STREAM, json={"message": QUESTION, **body}, headers=headers) as r:
        if r.status_code != 200:
            r.read()
            return r.status_code, r.json()
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache, no-transform"
        return r.status_code, parse_events(r.iter_lines())


def names(events) -> list[str]:
    return [name for name, _ in events]


def test_events_arrive_in_order_and_the_final_message_is_persisted(
    app, login_as, tenant, handbook, db
):
    llm = FakeLLM(chunk=4)
    app.dependency_overrides[get_llm] = lambda: llm
    client = login_as(tenant.viewer, tenant.org)
    status, events = stream(client)
    assert status == 200
    assert names(events)[:2] == ["meta", "sources"] and names(events)[-1] == "done"
    assert set(names(events)[2:-1]) == {"token"}

    meta = events[0][1]
    assert set(meta) == {
        "conversation_id",
        "user_message_id",
        "assistant_message_id",
        "search_query",
    }
    sources = events[1][1]
    assert [s["source_id"] for s in sources["sources"]] == ["S1", "S2"]
    assert sources["search_query"] == QUESTION
    assert not any(s["cited"] for s in sources["sources"])  # citations are known only at the end
    tokens = "".join(data["text"] for name, data in events if name == "token")
    assert tokens == "Employees receive 25 days of paid vacation per year [S1]."
    assert "STATUS" not in tokens
    done = events[-1][1]["message"]
    assert done["id"] == meta["assistant_message_id"]
    assert done["conversation_id"] == meta["conversation_id"]
    assert (done["status"], done["answer_status"], done["content"]) == (
        "complete",
        "answered",
        tokens,
    )
    assert [s["cited"] for s in done["sources"]] == [True, False]
    assert done["citations"][0]["source_id"] == "S1"
    assert [(c["verdict"], c["source_ids"]) for c in done["claims"]] == [("supported", ["S1"])]

    set_tenant(db, tenant.org.id)
    rows = {m.id: m for m in db.scalars(select(Message)).all()}
    assert rows[uuid.UUID(meta["user_message_id"])].content == QUESTION
    stored = rows[uuid.UUID(meta["assistant_message_id"])]
    assert stored.status == MessageStatus.COMPLETE and stored.content == tokens
    assert db.scalar(select(func.count()).select_from(UsageRecord)) == 1
    messages = client.get(f"/api/v1/conversations/{meta['conversation_id']}/messages").json()
    assert [m["status"] for m in messages["items"]] == ["complete", "complete"]


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (LLMUnavailableError("down", retryable=True), "llm_unavailable", ERROR_MESSAGE),
        # A usage limit (e.g. a free tier's tokens per minute) says to wait, not that it broke.
        (LLMRateLimitError("429", retry_after=40), "llm_rate_limited", RATE_LIMITED_MESSAGE),
    ],
)
def test_provider_failure_emits_an_error_event_and_persists_an_error_turn(
    app, login_as, tenant, handbook, db, error, code, message
):
    app.dependency_overrides[get_llm] = lambda: FakeLLM(error=error)
    status, events = stream(login_as(tenant.viewer, tenant.org))
    assert status == 200 and names(events) == ["meta", "sources", "error"]
    assert events[-1][1] == {"code": code, "message": message}
    set_tenant(db, tenant.org.id)
    stored = db.get(Message, uuid.UUID(events[0][1]["assistant_message_id"]))
    assert stored.status == MessageStatus.ERROR and stored.content == message
    assert stored.retrieval_metadata["answer_status"] == "error"
    assert len(stored.sources) == 2


def test_mid_stream_failure_keeps_the_partial_text(app, login_as, tenant, handbook, db):
    app.dependency_overrides[get_llm] = lambda: FakeLLM(
        LONG_ANSWER, error=LLMUnavailableError("dropped", retryable=True), fail_after=12, chunk=5
    )
    _, events = stream(login_as(tenant.viewer, tenant.org))
    assert names(events)[-1] == "error" and "token" in names(events)
    partial = "".join(d["text"] for n, d in events if n == "token")
    set_tenant(db, tenant.org.id)
    stored = db.get(Message, uuid.UUID(events[0][1]["assistant_message_id"]))
    assert stored.status == MessageStatus.ERROR
    assert stored.content == partial.strip() and 0 < len(partial) < len(LONG_ANSWER)


def test_request_errors_are_http_statuses_before_any_event(login_as, tenant, handbook):
    client = login_as(tenant.viewer, tenant.org)
    status, body = stream(client, conversation_id=str(uuid.uuid4()))
    assert status == 404 and body["error"]["code"] == "conversation_not_found"
    status, body = stream(client, message="   ")
    assert status == 422 and body["error"]["code"] == "empty_query"
    status, body = stream(client, scope={"document_ids": ["nope"]})
    assert status == 422 and body["error"]["code"] == "validation_error"
    assert client.get("/api/v1/conversations").json()["total"] == 0


def test_stream_requires_a_session_and_csrf(client, factory, tenant):
    assert client.post(STREAM, json={"message": QUESTION}).status_code == 401
    factory.authenticate(client, tenant.viewer, tenant.org)
    with client.stream("POST", STREAM, json={"message": QUESTION}) as response:  # no CSRF header
        assert response.status_code == 403


# --- Cancellation (driving the producer directly, as the SSE bridge does) -----------------


@pytest.fixture
def produce(db, factory, tenant, handbook):
    """produce(llm, cancel) -> the SSE producer for a fresh question by the viewer."""

    def make(llm, **body):
        principal = factory.principal(tenant.viewer, tenant.org)
        set_tenant(db, tenant.org.id)
        settings = get_settings()
        return chat_service.start_chat_stream(
            db,
            principal,
            ChatRequest(message=QUESTION, **body),
            embedder=HashingEmbedder(settings.embedding_dimensions),
            reranker=None,
            llm=llm,
            meta=RequestMeta(),
        )

    return make


def decode(chunk: bytes) -> tuple[str, dict]:
    (event,) = parse_events(chunk.decode().split("\n"))
    return event


def test_cancellation_mid_answer_persists_an_incomplete_message(produce, db, tenant):
    llm = FakeLLM(LONG_ANSWER, chunk=3, delay=0.002)
    cancel = threading.Event()
    events = []
    for chunk in produce(llm)(cancel):
        events.append(decode(chunk))
        if events[-1][0] == "token":
            cancel.set()
    assert names(events)[:2] == ["meta", "sources"] and names(events)[-1] == "done"
    done = events[-1][1]["message"]
    assert done["status"] == "incomplete"
    partial = "".join(d["text"] for n, d in events if n == "token")
    assert 0 < len(partial) < len(LONG_ANSWER) and done["content"] == partial.strip()
    assert done["metadata"]["cancelled"] is True
    assert llm.closed_early

    set_tenant(db, tenant.org.id)
    stored = db.get(Message, uuid.UUID(done["id"]))
    assert stored.status == MessageStatus.INCOMPLETE and stored.content == done["content"]
    assert db.scalar(select(func.count()).select_from(UsageRecord)) == 1
    assert db.scalar(select(func.count()).select_from(Message)) == 2


def test_abandoned_producer_still_persists_the_partial_answer(produce, db, tenant):
    """The bridge stops iterating once the client is gone; the finaliser must still run."""
    llm = FakeLLM(LONG_ANSWER, chunk=3, delay=0.002)
    cancel = threading.Event()
    generator = produce(llm)(cancel)
    events = [decode(next(generator)), decode(next(generator))]
    while events[-1][0] != "token":
        events.append(decode(next(generator)))
    cancel.set()
    generator.close()  # GeneratorExit inside the producer, before the pipeline noticed

    assert llm.closed_early
    set_tenant(db, tenant.org.id)
    stored = db.get(Message, uuid.UUID(events[0][1]["assistant_message_id"]))
    assert stored.status == MessageStatus.INCOMPLETE
    assert stored.content and stored.content in LONG_ANSWER
    assert [s["source_id"] for s in stored.sources] == ["S1", "S2"]
    assert stored.retrieval_metadata["search_query"] == QUESTION
    assert db.scalar(select(func.count()).select_from(Message)) == 2
    # Its sources already reached the client, so the answer is audited too.
    (event,) = db.scalars(select(AuditEvent).where(AuditEvent.action == "chat.answered"))
    assert (event.resource_id, event.event_metadata["status"]) == (str(stored.id), "incomplete")


def test_streamed_follow_ups_record_the_documents_behind_their_history(
    app, login_as, tenant, handbook, produce, db
):
    """Finished and abandoned follow-ups both store what reached them through the history,
    so they are withheld along with the answers they build on (see test_conversations)."""
    app.dependency_overrides[get_llm] = lambda: FakeLLM(chunk=4)
    client = login_as(tenant.viewer, tenant.org)
    _, first = stream(client)
    conversation_id = first[0][1]["conversation_id"]
    _, follow_up = stream(client, conversation_id=conversation_id)
    cancel = threading.Event()
    slow = FakeLLM(LONG_ANSWER, chunk=3, delay=0.002)
    generator = produce(slow, conversation_id=uuid.UUID(conversation_id))(cancel)
    events = [decode(next(generator))]
    while events[-1][0] != "token":
        events.append(decode(next(generator)))
    cancel.set()
    generator.close()

    set_tenant(db, tenant.org.id)

    def stored(meta: dict) -> Message:
        return db.get(Message, uuid.UUID(meta["assistant_message_id"]))

    assert "history_document_ids" not in stored(first[0][1]).retrieval_metadata
    assert stored(follow_up[0][1]).retrieval_metadata["history_document_ids"] == [str(handbook.id)]
    abandoned = stored(events[0][1])
    assert abandoned.status == MessageStatus.INCOMPLETE
    assert abandoned.retrieval_metadata["history_document_ids"] == [str(handbook.id)]


def test_producer_abandoned_right_after_meta_closes_the_message(produce, db, tenant):
    """The client can leave before the bridge resumes the producer past its first event."""
    llm = FakeLLM()
    cancel = threading.Event()
    generator = produce(llm)(cancel)
    meta = decode(next(generator))
    cancel.set()
    generator.close()

    assert llm.answer_requests == []
    set_tenant(db, tenant.org.id)
    stored = db.get(Message, uuid.UUID(meta[1]["assistant_message_id"]))
    assert stored.status == MessageStatus.INCOMPLETE and stored.content == ""


def test_cancellation_before_generation_makes_no_model_call(produce, db, tenant):
    llm = FakeLLM()
    cancel = threading.Event()
    cancel.set()
    events = [decode(chunk) for chunk in produce(llm)(cancel)]
    assert names(events) == ["meta", "sources", "done"]
    assert llm.answer_requests == []
    done = events[-1][1]["message"]
    assert done["status"] == "incomplete" and done["content"] == ""
    assert done["answer_status"] == "unverified" and done["metadata"]["cancelled"] is True
    set_tenant(db, tenant.org.id)
    assert db.get(Message, uuid.UUID(done["id"])).status == MessageStatus.INCOMPLETE


def test_no_message_is_ever_left_streaming_after_an_unexpected_failure(
    produce, db, tenant, monkeypatch
):
    def explode(*args, **kwargs):
        raise RuntimeError("pipeline bug")

    monkeypatch.setattr(chat_service, "stream_answer", explode)
    events = [decode(chunk) for chunk in produce(FakeLLM())(threading.Event())]
    assert names(events) == ["meta", "error"]
    assert events[-1][1] == {
        "code": "internal_error",
        "message": "The response failed unexpectedly",
    }
    set_tenant(db, tenant.org.id)
    stored = db.get(Message, uuid.UUID(events[0][1]["assistant_message_id"]))
    assert stored.status == MessageStatus.ERROR and stored.content == ""
    assert stored.retrieval_metadata["error"] == {"code": "internal_error"}
