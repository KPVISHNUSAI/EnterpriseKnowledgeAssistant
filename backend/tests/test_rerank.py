"""CrossEncoderReranker (app/providers/rerankers/cross_encoder.py): configuration, device
choice, lazy thread-safe loading, sigmoid scores, and the real model end to end."""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.core.config import get_settings
from app.db.session import set_tenant
from app.providers.embeddings import HashingEmbedder
from app.providers.rerankers.cross_encoder import CrossEncoderReranker, resolve_device
from app.rag.retrieval import retrieve
from app.rag.types import RetrievalOptions, SearchScope

# Needs the local-models extra (not installed in CI).
sentence_transformers = pytest.importorskip("sentence_transformers")
torch = pytest.importorskip("torch")


@pytest.fixture
def stub_model(monkeypatch):
    """Stands in for sentence_transformers.CrossEncoder: logit = 10 * count('good') - 5."""

    class StubCrossEncoder:
        loads = 0
        predict_calls: list[dict] = []

        def __init__(self, model_name, *, device, max_length):
            type(self).loads += 1
            self.config = (model_name, device, max_length)
            time.sleep(0.05)  # widen the window for concurrent first calls

        def predict(self, pairs, **kwargs):
            type(self).predict_calls.append(kwargs)
            logits = torch.tensor([10.0 * passage.count("good") - 5 for _, passage in pairs])
            return kwargs["activation_fn"](logits).numpy()

    monkeypatch.setattr(sentence_transformers, "CrossEncoder", StubCrossEncoder)
    return StubCrossEncoder


def test_from_settings_maps_configuration(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "reranker_model", "org/some-reranker")
    monkeypatch.setattr(settings, "reranker_device", "cpu")
    monkeypatch.setattr(settings, "reranker_batch_size", 7)
    monkeypatch.setattr(settings, "reranker_max_length", 256)
    reranker = CrossEncoderReranker.from_settings(settings)
    assert reranker.model_name == "org/some-reranker"
    assert (reranker._device, reranker._batch_size, reranker._max_length) == ("cpu", 7, 256)


@pytest.mark.parametrize(
    ("cuda", "mps", "expected"), [(True, True, "cuda"), (False, True, "mps"), (False, False, "cpu")]
)
def test_auto_device_prefers_cuda_then_mps_then_cpu(monkeypatch, cuda, mps, expected):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: mps)
    assert resolve_device("auto") == expected
    assert resolve_device("cuda:1") == "cuda:1"  # explicit devices are used as given


def test_scores_are_sigmoid_normalized(stub_model):
    reranker = CrossEncoderReranker("stub", device="cpu", batch_size=4, max_length=128)
    scores = reranker.score("query", ["bad", "good", "good good"])
    assert scores == pytest.approx([1 / (1 + math.exp(5)), 1 / (1 + math.exp(-5)), 1.0], abs=1e-4)
    assert all(0 <= s <= 1 for s in scores)
    [call] = stub_model.predict_calls
    assert call["batch_size"] == 4 and call["show_progress_bar"] is False


def test_model_loads_lazily_and_once_across_threads(stub_model):
    reranker = CrossEncoderReranker("stub", device="cpu", max_length=128)
    assert stub_model.loads == 0  # constructing (app start-up) loads nothing
    assert reranker.score("query", []) == []
    assert stub_model.loads == 0

    start = threading.Barrier(8)

    def first_call(_):
        start.wait()
        return reranker.score("query", ["good"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(first_call, range(8)))
    assert stub_model.loads == 1
    assert reranker._model.config == ("stub", "cpu", 128)
    assert all(r == results[0] for r in results)


# --- The real model ------------------------------------------------------------------------

QUESTION = "How many vacation days do employees get each year?"
ANSWER = "Full-time staff are entitled to 25 days of paid annual leave per calendar year."
FAQ_BAIT = (
    "Vacation days FAQ for employees: questions about vacation days each year are collected "
    "on the vacation days page."
)
INDEX_BAIT = (
    "Index: vacation days, employees, each year, holidays, payroll, vacation days calendar."
)
UNRELATED = "The cafeteria serves lunch every day between noon and two."


@pytest.mark.slow
def test_real_cross_encoder_moves_the_answer_to_the_top(db, factory, tenant):
    doc = factory.document(tenant.workspace, uploaded_by=tenant.editor)
    factory.chunks(doc, [FAQ_BAIT, INDEX_BAIT, ANSWER, UNRELATED])
    set_tenant(db, tenant.org.id)
    scope = SearchScope(principal=factory.principal(tenant.viewer, tenant.org))
    embedder = HashingEmbedder(get_settings().embedding_dimensions)
    hybrid = RetrievalOptions.from_settings(get_settings(), mode="hybrid", rerank=True)

    lexical = retrieve(db, scope, QUESTION, options=hybrid, embedder=embedder, reranker=None)
    assert lexical.candidates[0].content == FAQ_BAIT  # word overlap favours the bait

    reranker = CrossEncoderReranker.from_settings(get_settings())  # RERANKER_MODEL, local cache
    result = retrieve(db, scope, QUESTION, options=hybrid, embedder=embedder, reranker=reranker)
    assert result.reranked
    assert result.candidates[0].content == ANSWER
    assert all(0 <= c.rerank_score <= 1 for c in result.candidates)

    # Sigmoid scores make a fixed threshold meaningful: the unrelated passage falls away.
    strict = replace(hybrid, min_rerank_score=0.5)
    kept = retrieve(db, scope, QUESTION, options=strict, embedder=embedder, reranker=reranker)
    assert ANSWER in [c.content for c in kept.candidates]
    assert UNRELATED not in [c.content for c in kept.candidates]
