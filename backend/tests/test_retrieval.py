"""Retrieval pipeline (app/rag/retrieval.py): permission-aware vector and keyword search,
RRF fusion, reranking, timings and the HNSW query shape."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event, text

from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError
from app.db.session import set_tenant
from app.models import PermissionLevel, WorkspaceRole
from app.providers.embeddings import EmbeddingError, HashingEmbedder
from app.rag.retrieval import fuse, or_tsquery, retrieve
from app.rag.types import Candidate, RetrievalOptions, SearchScope

MODES = ["vector", "keyword", "hybrid"]
EMBEDDER = HashingEmbedder(384)


def options(**overrides) -> RetrievalOptions:
    base = RetrievalOptions.from_settings(get_settings(), rerank=False, final_top_k=50)
    return replace(base, **overrides)


def run(db, principal, query="vacation policy", *, opts=None, reranker=None, **filters):
    set_tenant(db, principal.organization_id)
    scope = SearchScope(principal=principal, **filters)
    return retrieve(
        db, scope, query, options=opts or options(), embedder=EMBEDDER, reranker=reranker
    )


def found(db, principal, *, mode="hybrid", **filters) -> set[str]:
    result = run(db, principal, opts=options(mode=mode), **filters)
    return {c.document_title for c in result.candidates}


@dataclass
class FakeReranker:
    """Scores a passage by the first key it contains (0 when none)."""

    scores: dict[str, float]
    model_name: str = "fake-reranker"
    calls: list[tuple[str, list[str]]] = field(default_factory=list)

    def score(self, query, passages):
        self.calls.append((query, list(passages)))
        return [next((s for key, s in self.scores.items() if key in p), 0.0) for p in passages]


@pytest.fixture
def corpus(factory, tenant, other_tenant):
    t = tenant
    reader = factory.user("reader@acme.example.com")  # owns both workspaces: reads them all
    factory.member(t.org, reader)
    factory.workspace_member(t.workspace, reader, WorkspaceRole.OWNER)
    legal_ws = factory.workspace(
        t.org, "Legal", members={t.outsider: WorkspaceRole.OWNER, reader: WorkspaceRole.OWNER}
    )
    docs = {
        "shared": factory.document(t.workspace, uploaded_by=t.editor, title="shared"),
        "restricted": factory.document(
            t.workspace, uploaded_by=t.editor, title="restricted", restricted=True
        ),
        "granted": factory.document(
            t.workspace,
            uploaded_by=t.manager,
            title="granted",
            restricted=True,
            grants={t.viewer: PermissionLevel.READ},
        ),
        "legal": factory.document(legal_ws, uploaded_by=t.outsider, title="legal"),
        "globex": factory.document(
            other_tenant.workspace, uploaded_by=other_tenant.editor, title="globex"
        ),
    }
    for name, doc in docs.items():
        factory.chunks(
            doc,
            [
                f"The {name} vacation policy grants paid vacation days.",
                f"{name} remote work policy and laptop security.",
            ],
        )
    return SimpleNamespace(t=t, reader=reader, legal_ws=legal_ws, other=other_tenant, **docs)


# --- Permission filtering ----------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_results_only_come_from_readable_documents(db, factory, corpus, mode):
    t = corpus.t
    expected = {
        t.viewer: {"shared", "granted"},
        t.editor: {"shared", "restricted"},  # uploader of the restricted document
        t.manager: {"shared", "restricted", "granted"},  # workspace owner
        t.admin: set(),  # org admins read through membership and grants only
        corpus.reader: {"shared", "restricted", "granted", "legal"},
        t.outsider: {"legal"},
    }
    for user, titles in expected.items():
        assert found(db, factory.principal(user, t.org), mode=mode) == titles, user.email


@pytest.mark.parametrize("mode", MODES)
def test_filters_naming_unreadable_documents_match_nothing(db, factory, corpus, mode):
    viewer = factory.principal(corpus.t.viewer, corpus.t.org)
    restricted, shared = corpus.restricted.id, corpus.shared.id
    assert found(db, viewer, mode=mode, document_ids=(restricted,)) == set()
    assert found(db, viewer, mode=mode, document_ids=(restricted, shared)) == {"shared"}
    assert found(db, viewer, mode=mode, workspace_ids=(corpus.t.workspace.id,)) == {
        "shared",
        "granted",
    }
    assert found(db, viewer, mode=mode, workspace_ids=(corpus.legal_ws.id,)) == set()
    assert found(db, viewer, mode=mode, document_ids=(corpus.globex.id,)) == set()
    assert found(db, viewer, mode=mode, document_ids=()) == set()  # an empty filter set


@pytest.mark.parametrize("mode", MODES)
def test_other_tenants_chunks_are_never_returned(db, factory, corpus, mode):
    reader = factory.principal(corpus.reader, corpus.t.org)
    assert "globex" not in found(db, reader, mode=mode)
    crafted = {"document_ids": (corpus.globex.id,), "workspace_ids": (corpus.other.workspace.id,)}
    assert found(db, reader, mode=mode, **crafted) == set()
    other_owner = factory.principal(corpus.other.manager, corpus.other.org)
    assert found(db, other_owner, mode=mode) == {"globex"}


def test_explicit_access_check_still_applies_when_the_session_tenant_is_wrong(db, factory, corpus):
    """RLS and the access predicate are independent: with the session bound to tenant B,
    RLS hides tenant A's rows and the predicate hides tenant B's rows."""
    reader = factory.principal(corpus.reader, corpus.t.org)
    set_tenant(db, corpus.other.org.id)
    result = retrieve(
        db,
        SearchScope(principal=reader),
        "vacation policy",
        options=options(),
        embedder=EMBEDDER,
        reranker=None,
    )
    assert result.candidates == []


def test_mime_type_filter(db, factory, tenant):
    md = factory.document(
        tenant.workspace, uploaded_by=tenant.editor, title="md", mime_type="text/markdown"
    )
    txt = factory.document(tenant.workspace, uploaded_by=tenant.editor, title="txt")
    for doc in (md, txt):
        factory.chunks(doc, ["vacation policy"])
    viewer = factory.principal(tenant.viewer, tenant.org)
    assert found(db, viewer, mime_types=("text/markdown",)) == {"md"}
    assert found(db, viewer, mime_types=("application/pdf",)) == set()


# --- Fusion ------------------------------------------------------------------------------


def _candidate(chunk_id=None, **evidence) -> Candidate:
    return Candidate(
        chunk_id=chunk_id or uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title="doc",
        workspace_id=uuid.uuid4(),
        mime_type="text/plain",
        chunk_index=0,
        content="text",
        content_hash="0" * 64,
        token_count=1,
        page_number=None,
        page_end=None,
        section_title=None,
        heading_path=None,
        **evidence,
    )


def test_rrf_sums_reciprocal_ranks_and_merges_evidence():
    a, b, c = (_candidate(vector_rank=i, vector_similarity=0.9 - i / 10) for i in (1, 2, 3))
    c_kw = _candidate(c.chunk_id, keyword_rank=1, keyword_score=0.7, matched_terms=["vacat"])
    d_kw = _candidate(keyword_rank=2, keyword_score=0.5)
    a_kw = _candidate(a.chunk_id, keyword_rank=3, keyword_score=0.2)
    fused = fuse([a, b, c], [c_kw, d_kw, a_kw], rrf_k=60)

    # a and c tie (1/61 + 1/63) and b and d tie (1/62): ties keep vector-first order.
    assert [x.chunk_id for x in fused] == [a.chunk_id, c.chunk_id, b.chunk_id, d_kw.chunk_id]
    assert fused[0].rrf_score == pytest.approx(1 / 61 + 1 / 63)
    assert fused[1].rrf_score == pytest.approx(1 / 63 + 1 / 61)
    assert fused[2].rrf_score == pytest.approx(1 / 62)
    assert fused[3].rrf_score == pytest.approx(1 / 62)
    merged = fused[1]
    assert merged is c  # the vector candidate, enriched with keyword evidence
    assert (merged.vector_rank, merged.keyword_rank) == (3, 1)
    assert (merged.keyword_score, merged.matched_terms) == (0.7, ["vacat"])


def test_rrf_k_controls_how_fast_ranks_decay():
    first, second = _candidate(), _candidate()
    fuse([first, second], [], rrf_k=1)
    assert (first.rrf_score, second.rrf_score) == (pytest.approx(1 / 2), pytest.approx(1 / 3))


def test_single_list_fusion_keeps_order_and_scores_by_rank():
    hits = [_candidate(keyword_rank=i) for i in (1, 2, 3)]
    fused = fuse([], hits, rrf_k=60)
    assert fused == hits
    assert [h.rrf_score for h in hits] == [pytest.approx(1 / (60 + r)) for r in (1, 2, 3)]
    assert fuse([], [], rrf_k=60) == []


# --- Modes, limits, timings --------------------------------------------------------------


def test_modes_fill_their_own_evidence(db, factory, corpus):
    viewer = factory.principal(corpus.t.viewer, corpus.t.org)
    vector = run(db, viewer, opts=options(mode="vector"))
    assert all(c.vector_rank and c.keyword_rank is None for c in vector.candidates)
    assert [c.vector_rank for c in vector.candidates] == list(range(1, 5))
    assert all(-1 <= c.vector_similarity <= 1 for c in vector.candidates)
    assert set(vector.timings_ms) == {"embed", "vector", "fuse", "total"}

    keyword = run(db, viewer, opts=options(mode="keyword"))
    assert all(c.keyword_rank and c.vector_rank is None for c in keyword.candidates)
    assert all(0 < c.keyword_score < 1 for c in keyword.candidates)
    assert set(keyword.timings_ms) == {"keyword", "fuse", "total"}

    hybrid = run(db, viewer, opts=options(mode="hybrid"))
    assert all(c.vector_rank and c.keyword_rank for c in hybrid.candidates)
    assert set(hybrid.timings_ms) == {"embed", "vector", "keyword", "fuse", "total"}
    assert all(ms >= 0 for ms in hybrid.timings_ms.values())
    scores = [c.rrf_score for c in hybrid.candidates]
    assert scores == sorted(scores, reverse=True)
    assert (hybrid.query, hybrid.search_query, hybrid.reranked) == (
        "vacation policy",
        "vacation policy",
        False,
    )


def test_per_method_fused_and_final_limits(db, factory, corpus):
    reader = factory.principal(corpus.reader, corpus.t.org)  # 8 readable chunks
    result = run(db, reader, opts=options(vector_top_k=3, keyword_top_k=3))
    assert sum(c.vector_rank is not None for c in result.candidates) == 3
    assert sum(c.keyword_rank is not None for c in result.candidates) == 3
    assert result.total_candidates <= 6

    result = run(db, reader, opts=options(fused_top_k=5, final_top_k=50))
    assert (result.total_candidates, len(result.candidates)) == (8, 5)
    result = run(db, reader, opts=options(fused_top_k=5, final_top_k=2))
    assert (result.total_candidates, len(result.candidates)) == (8, 2)


def test_search_query_is_what_gets_searched(db, factory, corpus):
    viewer = factory.principal(corpus.t.viewer, corpus.t.org)
    set_tenant(db, corpus.t.org.id)
    reranker = FakeReranker({})
    result = retrieve(
        db,
        SearchScope(principal=viewer),
        "and the second one?",
        options=options(mode="keyword", rerank=True),
        embedder=EMBEDDER,
        reranker=reranker,
        search_query="laptop security",
    )
    assert (result.query, result.search_query) == ("and the second one?", "laptop security")
    assert {c.content for c in result.candidates} == {
        "shared remote work policy and laptop security.",
        "granted remote work policy and laptop security.",
    }
    assert reranker.calls[0][0] == "laptop security"


def test_no_candidates(db, factory, corpus):
    viewer = factory.principal(corpus.t.viewer, corpus.t.org)
    reranker = FakeReranker({})
    result = run(
        db, viewer, "xylophone", opts=options(mode="keyword", rerank=True), reranker=reranker
    )
    assert (result.candidates, result.total_candidates, result.reranked) == ([], 0, False)
    assert reranker.calls == []
    # Only stop words: nothing to match by keyword, but vector search still answers.
    assert run(db, viewer, "the of and", opts=options(mode="keyword")).candidates == []
    hybrid = run(db, viewer, "the of and", opts=options(mode="hybrid"))
    assert hybrid.candidates and all(c.keyword_rank is None for c in hybrid.candidates)


def test_metrics_and_log_carry_no_query_text(db, factory, corpus, caplog, monkeypatch):
    viewer = factory.principal(corpus.t.viewer, corpus.t.org)
    before = REGISTRY.get_sample_value("eka_retrieval_duration_seconds_count", {"stage": "total"})
    with caplog.at_level(logging.INFO, logger="app.rag.retrieval"):
        run(db, viewer, "confidential vacation question")
    after = REGISTRY.get_sample_value("eka_retrieval_duration_seconds_count", {"stage": "total"})
    assert after == (before or 0) + 1
    [record] = [r for r in caplog.records if r.getMessage() == "retrieval.completed"]
    assert record.mode == "hybrid" and record.returned == 4
    dumped = str(record.__dict__)
    assert "confidential" not in dumped and "laptop" not in dumped


def test_embedding_failure_is_service_unavailable(db, factory, corpus):
    class BrokenEmbedder(HashingEmbedder):
        def embed_query(self, text):
            raise EmbeddingError("embedding API timed out")

    viewer = factory.principal(corpus.t.viewer, corpus.t.org)
    set_tenant(db, corpus.t.org.id)
    with pytest.raises(ServiceUnavailableError) as caught:
        retrieve(
            db,
            SearchScope(principal=viewer),
            "vacation",
            options=options(),
            embedder=BrokenEmbedder(384),
            reranker=None,
        )
    assert caught.value.code == "embedding_unavailable"


# --- Reranking ---------------------------------------------------------------------------


def test_rerank_orders_by_score_and_applies_min_score(db, factory, corpus):
    reader = factory.principal(corpus.reader, corpus.t.org)
    reranker = FakeReranker({"legal remote": 0.9, "The granted vacation": 0.7, "shared": 0.2})
    result = run(db, reader, opts=options(rerank=True), reranker=reranker)
    assert result.reranked and "rerank" in result.timings_ms
    assert [c.content for c in result.candidates[:2]] == [
        "legal remote work policy and laptop security.",
        "The granted vacation policy grants paid vacation days.",
    ]
    scores = [c.rerank_score for c in result.candidates]
    assert scores == sorted(scores, reverse=True) and len(scores) == 8
    assert len(reranker.calls[0][1]) == 8  # every fused candidate was scored

    result = run(db, reader, opts=options(rerank=True, min_rerank_score=0.5), reranker=reranker)
    assert [c.rerank_score for c in result.candidates] == [0.9, 0.7]
    assert result.total_candidates == 8  # found before the threshold


def test_reranker_sees_only_fused_top_k_and_final_top_k_is_kept(db, factory, corpus):
    reader = factory.principal(corpus.reader, corpus.t.org)
    reranker = FakeReranker({"legal": 0.9})
    result = run(
        db, reader, opts=options(rerank=True, fused_top_k=3, final_top_k=2), reranker=reranker
    )
    assert len(reranker.calls[0][1]) == 3
    assert len(result.candidates) == 2


def test_reranker_input_includes_the_heading_path(db, factory, tenant):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    factory.chunks(doc, [{"content": "Unused days expire.", "heading_path": "Handbook > Leave"}])
    reranker = FakeReranker({})
    run(
        db,
        factory.principal(tenant.viewer, tenant.org),
        "unused days",
        opts=options(rerank=True),
        reranker=reranker,
    )
    assert reranker.calls[0][1] == ["Handbook > Leave\n\nUnused days expire."]


def test_rerank_disabled_unavailable_or_failing_keeps_fused_order(db, factory, corpus):
    viewer = factory.principal(corpus.t.viewer, corpus.t.org)
    baseline = [c.chunk_id for c in run(db, viewer).candidates]

    unused = FakeReranker({"granted": 1.0})
    disabled = run(db, viewer, opts=options(rerank=False), reranker=unused)
    assert not disabled.reranked and unused.calls == []
    unavailable = run(db, viewer, opts=options(rerank=True), reranker=None)
    assert not unavailable.reranked

    class FailingReranker(FakeReranker):
        def score(self, query, passages):
            raise RuntimeError("model failed to load")

    failing = run(db, viewer, opts=options(rerank=True), reranker=FailingReranker({}))
    assert not failing.reranked and "rerank" in failing.timings_ms
    for result in (disabled, unavailable, failing):
        assert [c.chunk_id for c in result.candidates] == baseline
        assert all(c.rerank_score is None for c in result.candidates)


# --- Keyword search ----------------------------------------------------------------------


def _keyword(db, factory, tenant, chunks, query):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    factory.chunks(doc, chunks)
    viewer = factory.principal(tenant.viewer, tenant.org)
    return run(db, viewer, query, opts=options(mode="keyword")).candidates


def test_keyword_search_stems_and_has_or_semantics(db, factory, tenant):
    [hit] = _keyword(
        db,
        factory,
        tenant,
        ["Our travel policy covers flights and hotels.", "Cafeteria opening hours."],
        "policies xylophone",
    )
    assert hit.content.startswith("Our travel policy")
    assert (hit.keyword_rank, hit.matched_terms) == (1, ["polici"])


def test_matched_terms_follow_query_order_and_more_terms_rank_higher(db, factory, tenant):
    hits = _keyword(
        db,
        factory,
        tenant,
        ["Remote work needs a laptop.", "The vacation policy for remote staff."],
        "remote vacation policy",
    )
    assert [h.matched_terms for h in hits] == [["remot", "vacat", "polici"], ["remot"]]
    assert hits[0].keyword_score > hits[1].keyword_score


def test_heading_path_matches_outrank_body_matches(db, factory, tenant):
    hits = _keyword(
        db,
        factory,
        tenant,
        [
            {"content": "This note mentions the expense policy in passing."},
            {"content": "Submit receipts within thirty days.", "heading_path": "Expense Policy"},
        ],
        "expense policy",
    )
    assert [h.chunk_index for h in hits] == [1, 0]


def test_keyword_ties_follow_content_not_random_ids(db, factory, tenant):
    """Equal scores are ordered by content hash, so re-ingesting the same text gives the
    same ranking (chunk ids are random per ingest)."""
    words = ("alpha", "bravo", "delta", "gamma", "kilo", "lima")
    texts = [f"Travel note {word}." for word in words]
    hits = _keyword(db, factory, tenant, texts, "travel")
    assert len({h.keyword_score for h in hits}) == 1
    by_hash = sorted(texts, key=lambda t: hashlib.sha256(t.encode()).hexdigest())
    assert [h.content for h in hits] == by_hash


def test_query_text_is_never_parsed_as_tsquery_syntax(db, factory, tenant):
    hits = _keyword(
        db,
        factory,
        tenant,
        ["Escalate to it's on-call owner at ops@example.com (C:\\temp\\x)."],
        "it's & | ! <-> :* ( ops@example.com C:\\temp\\x '' \\",
    )
    assert len(hits) == 1 and "ops@example.com" in hits[0].matched_terms


@pytest.mark.parametrize("lexeme", ["a\\b", "it's", "''", "\\", "&|!()<->:*", "plain"])
def test_or_tsquery_quotes_each_lexeme_literally(db, lexeme):
    # array_to_tsvector takes lexemes as given (no parsing), so a match proves that the
    # quoting preserved the lexeme exactly and nothing in it acted as an operator.
    matches = text("SELECT array_to_tsvector(CAST(:lexemes AS text[])) @@ CAST(:query AS tsquery)")
    assert db.scalar(matches, {"lexemes": [lexeme], "query": or_tsquery(["zzz", lexeme])})
    assert not db.scalar(matches, {"lexemes": ["other"], "query": or_tsquery([lexeme])})


# --- Vector search -----------------------------------------------------------------------


def test_vector_search_only_compares_chunks_of_the_query_model(db, factory, tenant):
    current = factory.document(tenant.workspace, uploaded_by=tenant.editor, title="current")
    stale = factory.document(tenant.workspace, uploaded_by=tenant.editor, title="stale")
    factory.chunks(current, ["vacation policy"])
    factory.chunks(stale, ["vacation policy"], embedder=HashingEmbedder(384, "old-model"))
    viewer = factory.principal(tenant.viewer, tenant.org)
    assert found(db, viewer, mode="vector") == {"current"}
    assert found(db, viewer, mode="keyword") == {"current", "stale"}  # text is model-free


@contextmanager
def captured_sql(db) -> Iterator[list[tuple[str, object]]]:
    statements: list[tuple[str, object]] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_vector_query_uses_the_hnsw_index_and_iterative_scans(db, factory, tenant):
    """The 450 chunks nearest to the query are hidden from the viewer. A filtered HNSW
    scan stops after hnsw.ef_search candidates unless iterative scans are on, so this
    also proves the transaction-local GUCs take effect."""
    hidden = factory.document(tenant.workspace, uploaded_by=tenant.editor, restricted=True)
    visible = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    factory.chunks(hidden, [f"vacation policy remote work rule {i}" for i in range(450)])
    factory.chunks(visible, [f"cafeteria menu {i} soup salad bread" for i in range(60)])
    viewer = factory.principal(tenant.viewer, tenant.org)
    set_tenant(db, tenant.org.id)
    # Planner hints for a small table: rule out exact plans so the index must serve it.
    db.execute(text("SET LOCAL enable_seqscan = off"))
    db.execute(text("SET LOCAL enable_sort = off"))
    with captured_sql(db) as statements:
        result = retrieve(
            db,
            SearchScope(principal=viewer),
            "vacation policy remote work",
            options=options(mode="vector"),
            embedder=EMBEDDER,
            reranker=None,
        )
    assert result.total_candidates == get_settings().retrieval_vector_top_k == 40
    assert {c.document_id for c in result.candidates} == {visible.id}

    statement, params = next(s for s in statements if "<=>" in s[0])
    plan = "\n".join(db.connection().exec_driver_sql("EXPLAIN " + statement, params).scalars())
    assert "ix_document_chunks_embedding_hnsw" in plan, plan
    settings = db.execute(
        text("SELECT current_setting('hnsw.ef_search'), current_setting('hnsw.iterative_scan')")
    ).one()
    assert tuple(settings) == (str(get_settings().hnsw_ef_search), "strict_order")

    # Control: the same statement without iterative scans comes up short.
    db.execute(text("SET LOCAL hnsw.iterative_scan = off"))
    assert len(db.connection().exec_driver_sql(statement, params).all()) < 40


def test_hnsw_settings_are_transaction_local(db, factory, corpus):
    found(db, factory.principal(corpus.t.viewer, corpus.t.org), mode="vector")
    db.rollback()
    after = db.execute(
        text(
            "SELECT current_setting('hnsw.ef_search', true), "
            "current_setting('hnsw.iterative_scan', true)"
        )
    ).one()
    assert after[0] != str(get_settings().hnsw_ef_search)
    assert after[1] != "strict_order"
