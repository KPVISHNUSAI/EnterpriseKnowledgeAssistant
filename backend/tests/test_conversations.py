"""Conversation history APIs: listing, messages (with read-time redaction), deletion."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select, text
from test_answer_fakes import LEAVE, FakeLLM, index_handbook

from app.db.session import set_tenant
from app.models import DocumentPermission, Message, PermissionLevel, PrincipalType
from app.providers.registry import get_llm

CONVERSATIONS = "/api/v1/conversations"
QUESTION = "How many vacation days do employees receive?"


@pytest.fixture
def handbook(factory, tenant):
    return index_handbook(factory, tenant)


@pytest.fixture
def fake_llm(app):
    llm = FakeLLM()
    app.dependency_overrides[get_llm] = lambda: llm
    return llm


def chat(client, message=QUESTION, conversation_id=None) -> str:
    response = client.post(
        "/api/v1/chat", json={"message": message, "conversation_id": conversation_id}
    )
    assert response.status_code == 200, response.text
    return response.json()["conversation_id"]


def test_list_is_private_newest_first_and_counted(login_as, tenant, handbook, fake_llm):
    viewer = login_as(tenant.viewer, tenant.org)
    editor = login_as(tenant.editor, tenant.org)
    first = chat(viewer, "first question?")
    second = chat(viewer, "second question?")
    chat(editor, "editor question?")
    chat(viewer, "follow-up?", first)  # bumps the first conversation

    listing = viewer.get(CONVERSATIONS).json()
    assert [c["id"] for c in listing["items"]] == [first, second]
    assert [c["message_count"] for c in listing["items"]] == [4, 2]
    assert [c["title"] for c in listing["items"]] == ["first question?", "second question?"]
    assert (listing["total"], listing["page"], listing["page_size"]) == (2, 1, 20)
    page = viewer.get(CONVERSATIONS, params={"page": 2, "page_size": 1}).json()
    assert [c["id"] for c in page["items"]] == [second] and page["total"] == 2
    assert [c["title"] for c in editor.get(CONVERSATIONS).json()["items"]] == ["editor question?"]
    assert login_as(tenant.admin, tenant.org).get(CONVERSATIONS).json()["total"] == 0


def test_messages_are_chronological_and_private(login_as, tenant, other_tenant, handbook, fake_llm):
    viewer = login_as(tenant.viewer, tenant.org)
    conversation_id = chat(viewer)
    chat(viewer, "Who approves them?", conversation_id)

    page = viewer.get(f"{CONVERSATIONS}/{conversation_id}/messages").json()
    assert page["total"] == 4
    assert [m["role"] for m in page["items"]] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in page["items"]][::2] == [QUESTION, "Who approves them?"]
    assert all(m["conversation_id"] == conversation_id for m in page["items"])
    assistant = page["items"][1]
    assert assistant["answer_status"] == "answered" and assistant["status"] == "complete"
    assert assistant["sources"][0]["excerpt"] == LEAVE and assistant["citations"][0]["verified"]
    assert "search_query" in assistant["metadata"] and "citations" not in assistant["metadata"]
    second = viewer.get(
        f"{CONVERSATIONS}/{conversation_id}/messages", params={"page": 2, "page_size": 2}
    ).json()
    assert second["items"][0]["content"] == "Who approves them?"

    for client in (
        login_as(tenant.editor, tenant.org),
        login_as(tenant.admin, tenant.org),
        login_as(other_tenant.admin, other_tenant.org),
    ):
        response = client.get(f"{CONVERSATIONS}/{conversation_id}/messages")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "conversation_not_found"
    assert viewer.get(f"{CONVERSATIONS}/{uuid.uuid4()}/messages").status_code == 404


def test_sources_are_redacted_once_access_is_revoked(login_as, tenant, handbook, fake_llm, db):
    viewer = login_as(tenant.viewer, tenant.org)
    conversation_id = chat(viewer)
    url = f"{CONVERSATIONS}/{conversation_id}/messages"
    before = viewer.get(url).json()["items"][1]
    assert before["sources"][0]["excerpt"] == LEAVE and not before["sources"][0]["redacted"]

    # Make the handbook restricted: the viewer (no grant, not the uploader) loses access.
    set_tenant(db, tenant.org.id)
    db.execute(delete(DocumentPermission).where(DocumentPermission.document_id == handbook.id))
    db.commit()
    after = viewer.get(url).json()["items"][1]
    assert after["content"] == before["content"] and after["answer_status"] == "answered"
    for source in after["sources"]:
        assert (source["excerpt"], source["section_title"], source["redacted"]) == (
            None,
            None,
            True,
        )
        assert source["document_title"] == "Employee Handbook"  # already seen, not secret
    (citation,) = after["citations"]
    assert (citation["excerpt"], citation["redacted"], citation["verified"]) == (None, True, True)
    # The stored row is untouched: redaction happens at read time.
    assert db.get(Message, uuid.UUID(after["id"])).sources[0]["excerpt"] == LEAVE

    db.add(
        DocumentPermission(
            document_id=handbook.id,
            organization_id=tenant.org.id,
            principal_type=PrincipalType.USER,
            principal_id=tenant.viewer.id,
            permission=PermissionLevel.READ,
        )
    )
    db.commit()
    restored = viewer.get(url).json()["items"][1]
    assert restored["sources"][0]["excerpt"] == LEAVE and not restored["citations"][0]["redacted"]


def test_delete_removes_messages_and_is_audited(login_as, tenant, handbook, fake_llm, db):
    viewer = login_as(tenant.viewer, tenant.org)
    conversation_id = chat(viewer)
    keep = chat(viewer, "another?")
    assert (
        login_as(tenant.editor, tenant.org).delete(f"{CONVERSATIONS}/{conversation_id}").status_code
        == 404
    )

    response = viewer.delete(f"{CONVERSATIONS}/{conversation_id}")
    assert response.status_code == 204
    assert viewer.get(f"{CONVERSATIONS}/{conversation_id}/messages").status_code == 404
    assert viewer.delete(f"{CONVERSATIONS}/{conversation_id}").status_code == 404
    assert [c["id"] for c in viewer.get(CONVERSATIONS).json()["items"]] == [keep]

    set_tenant(db, tenant.org.id)
    remaining = db.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == uuid.UUID(conversation_id))
    )
    assert remaining == 0
    assert db.scalar(select(func.count()).select_from(Message)) == 2  # the kept conversation
    events = db.execute(
        text("SELECT action, resource_type, resource_id, actor_user_id FROM audit_events")
    ).all()
    assert [tuple(e) for e in events] == [
        ("conversation.deleted", "conversation", conversation_id, tenant.viewer.id)
    ]
