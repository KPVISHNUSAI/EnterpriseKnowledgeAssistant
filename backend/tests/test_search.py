"""POST /api/v1/search end to end: explanations, highlights, filters, validation."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.models import WorkspaceRole
from app.providers.registry import get_reranker

HANDBOOK = "Employees receive 25 days of paid vacation. Vacation requests need approval."


def search(client, query="vacation policies", **body):
    response = client.post("/api/v1/search", json={"query": query, **body})
    assert response.status_code == 200, response.text
    return response.json()


def titles(client, **filters) -> set[str]:
    body = search(client, "vacation policy", filters=filters)
    return {result["document_title"] for result in body["results"]}


class FakeReranker:
    model_name = "fake-reranker"

    def score(self, query, passages):
        return [0.9 if "Cafeteria" in p else 0.2 for p in passages]


@pytest.fixture
def handbook(factory, tenant):
    doc = factory.document(
        tenant.workspace,
        uploaded_by=tenant.editor,
        title="Employee Handbook",
        mime_type="text/markdown",
    )
    factory.chunks(
        doc,
        [
            {
                "content": HANDBOOK,
                "page_number": 3,
                "section_title": "Leave",
                "heading_path": "Handbook > Leave",
            },
            "Cafeteria opens at 8am.",
        ],
    )
    return doc


def test_results_are_explained_and_highlighted(client, factory, tenant, handbook):
    factory.authenticate(client, tenant.viewer, tenant.org)
    body = search(client, "  vacation\tpolicies ")
    assert (body["query"], body["mode"], body["reranked"]) == ("vacation policies", "hybrid", False)
    assert body["total_candidates"] == 2
    assert {"embed", "vector", "keyword", "fuse", "total", "highlight"} <= set(body["timings_ms"])

    first, second = body["results"]
    assert first["chunk_id"] and first["document_id"] == str(handbook.id)
    assert (first["document_title"], first["workspace_name"]) == (
        "Employee Handbook",
        tenant.workspace.name,
    )
    assert (first["file_type"], first["mime_type"]) == ("md", "text/markdown")
    assert (first["page_number"], first["section_title"], first["heading_path"]) == (
        3,
        "Leave",
        "Handbook > Leave",
    )
    assert first["content"] == HANDBOOK
    segments = first["highlights"]
    assert "".join(s["text"] for s in segments) == HANDBOOK
    assert [s["text"] for s in segments if s["match"]] == ["vacation", "Vacation"]

    why = first["explanation"]
    assert why["final_rank"] == 1 and why["keyword_rank"] == 1 and why["vector_rank"] >= 1
    assert why["matched_terms"] == ["vacat"]  # "policies" is not in the passage
    assert 0 < why["keyword_score"] < 1 and -1 <= why["vector_similarity"] <= 1
    assert why["rerank_score"] is None and first["score"] == why["rrf_score"]

    # Found by vector similarity only: one unhighlighted segment, no keyword evidence.
    assert second["highlights"] == [{"text": "Cafeteria opens at 8am.", "match": False}]
    assert second["explanation"]["keyword_rank"] is None
    assert second["explanation"]["matched_terms"] == []
    assert second["explanation"]["final_rank"] == 2


def test_vector_and_keyword_modes(client, factory, tenant, handbook):
    factory.authenticate(client, tenant.viewer, tenant.org)
    vector = search(client, mode="vector")
    assert vector["mode"] == "vector" and len(vector["results"]) == 2
    assert all(len(r["highlights"]) == 1 for r in vector["results"])
    assert "keyword" not in vector["timings_ms"]
    keyword = search(client, mode="keyword")
    assert [r["content"] for r in keyword["results"]] == [HANDBOOK]
    assert "embed" not in keyword["timings_ms"]


def test_filters_narrow_by_type_workspace_and_document(client, factory, tenant):
    ws = tenant.workspace
    md = factory.document(ws, uploaded_by=tenant.editor, title="md", mime_type="text/markdown")
    txt = factory.document(ws, uploaded_by=tenant.editor, title="txt")
    ops_ws = factory.workspace(tenant.org, "Ops", members={tenant.viewer: WorkspaceRole.VIEWER})
    ops = factory.document(ops_ws, uploaded_by=tenant.editor, title="ops")
    secret = factory.document(ws, uploaded_by=tenant.editor, title="secret", restricted=True)
    for doc in (md, txt, ops, secret):
        factory.chunks(doc, ["vacation policy details"])
    factory.authenticate(client, tenant.viewer, tenant.org)

    everything = {"md", "txt", "ops"}
    assert titles(client) == everything
    assert titles(client, file_types=["md"]) == {"md"}
    assert titles(client, file_types=["txt", "pdf"]) == {"txt", "ops"}
    assert titles(client, workspace_ids=[str(ops_ws.id)]) == {"ops"}
    assert titles(client, document_ids=[str(md.id), str(secret.id)]) == {"md"}
    assert titles(client, document_ids=[str(secret.id)]) == set()  # never revealed, no error
    assert titles(client, workspace_ids=[], document_ids=[], file_types=[]) == everything


def test_rerank_with_a_configured_reranker(app, client, factory, tenant, handbook):
    app.dependency_overrides[get_reranker] = FakeReranker
    factory.authenticate(client, tenant.viewer, tenant.org)
    body = search(client, rerank=True)
    assert body["reranked"] is True and "rerank" in body["timings_ms"]
    first = body["results"][0]
    assert first["content"] == "Cafeteria opens at 8am."
    assert first["score"] == first["explanation"]["rerank_score"] == 0.9
    assert search(client, rerank=False)["reranked"] is False


def test_rerank_requested_without_a_reranker_is_not_an_error(client, factory, tenant, handbook):
    factory.authenticate(client, tenant.viewer, tenant.org)  # tests run RERANKER_PROVIDER=none
    assert search(client, rerank=True)["reranked"] is False


def test_top_k_is_honoured_and_capped(client, factory, tenant, monkeypatch):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    factory.chunks(doc, [f"vacation policy clause {i}" for i in range(40)])
    monkeypatch.setattr(get_settings(), "retrieval_fused_top_k", 5)
    factory.authenticate(client, tenant.viewer, tenant.org)
    assert len(search(client, top_k=12)["results"]) == 12  # stages widen to top_k
    monkeypatch.setattr(get_settings(), "search_max_results", 3)
    assert len(search(client, top_k=12)["results"]) == 3


def test_unreadable_documents_are_not_searchable(client, factory, tenant, other_tenant):
    secret = factory.document(
        tenant.workspace, uploaded_by=tenant.editor, title="secret", restricted=True
    )
    foreign = factory.document(
        other_tenant.workspace, uploaded_by=other_tenant.editor, title="foreign"
    )
    for doc in (secret, foreign):
        factory.chunks(doc, ["vacation policy details"])
    factory.authenticate(client, tenant.viewer, tenant.org)
    assert titles(client) == set()
    assert titles(client, document_ids=[str(secret.id), str(foreign.id)]) == set()
    factory.authenticate(client, tenant.admin, tenant.org)
    assert titles(client, document_ids=[str(secret.id), str(foreign.id)]) == {"secret"}


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"query": "   "}, "empty_query"),
        ({"query": "\u200b\u0000"}, "empty_query"),
        ({}, "validation_error"),
        ({"query": ""}, "validation_error"),
        ({"query": "x" * 2001}, "validation_error"),
        ({"query": "ok", "top_k": 0}, "validation_error"),
        ({"query": "ok", "top_k": 51}, "validation_error"),
        ({"query": "ok", "mode": "semantic"}, "validation_error"),
        ({"query": "ok", "filters": {"file_types": ["exe"]}}, "validation_error"),
        ({"query": "ok", "filters": {"document_ids": ["not-a-uuid"]}}, "validation_error"),
    ],
)
def test_invalid_requests_are_422(client, factory, tenant, payload, code):
    factory.authenticate(client, tenant.viewer, tenant.org)
    response = client.post("/api/v1/search", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


def test_lone_surrogates_are_rejected_not_a_server_error(client, factory, tenant, handbook):
    factory.authenticate(client, tenant.viewer, tenant.org)
    response = client.post(
        "/api/v1/search",
        content=b'{"query": "vacation \\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_search_requires_a_session(client):
    response = client.post("/api/v1/search", json={"query": "vacation"})
    assert response.status_code == 401
