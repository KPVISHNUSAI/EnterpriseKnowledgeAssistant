"""Integrity of the evaluation corpus and dataset (eval/): every case points at real
documents and people, needles are verbatim and survive parsing and chunking, labels agree
with the reference answers, and canaries can only appear through a leak or an obeyed
injection. No database and no ML models."""

from __future__ import annotations

import hashlib
from collections import Counter

import pytest

from app.core.filetypes import FileType
from app.ingestion.chunking import chunk_blocks
from app.ingestion.cleaning import clean_blocks
from app.ingestion.parsers import parse
from app.models import OrgRole
from app.rag.prompts import SYSTEM_PROMPT
from eval import corpus, metrics
from eval.dataset import MIN_CASES, Case, load

CASES = load()
BY_ID = {case.id: case for case in CASES}


def _chunks(doc: corpus.Doc) -> list[str]:
    parsed = parse(corpus.build(doc), FileType(doc.format))
    return [chunk.content for chunk in chunk_blocks(clean_blocks(parsed.blocks), **corpus.CHUNKING)]


CHUNKS = {doc.key: _chunks(doc) for doc in corpus.DOCUMENTS}


def _readable(person: str) -> list[corpus.Doc]:
    return [doc for doc in corpus.DOCUMENTS if corpus.can_read(corpus.PEOPLE_BY_KEY[person], doc)]


def _unreadable(person: str) -> list[corpus.Doc]:
    readable = _readable(person)
    return [doc for doc in corpus.DOCUMENTS if doc not in readable]


def _ids(cases: list[Case]) -> list[str]:
    return [case.id for case in cases]


# --- Corpus -------------------------------------------------------------------------------------


def test_corpus_shape() -> None:
    assert 15 <= len(corpus.DOCUMENTS) <= 25
    assert {doc.format for doc in corpus.DOCUMENTS} == {"pdf", "docx", "md", "txt"}
    assert len(corpus.DOCUMENTS_BY_KEY) == len(corpus.DOCUMENTS)
    assert {doc.workspace for doc in corpus.DOCUMENTS} <= set(corpus.WORKSPACES_BY_KEY)
    restricted = [doc for doc in corpus.DOCUMENTS if doc.restricted]
    assert restricted and all(doc.grants for doc in restricted)
    employee = corpus.PEOPLE_BY_KEY["employee"]
    assert employee.org_role is OrgRole.EMPLOYEE
    assert not any(corpus.can_read(employee, doc) for doc in restricted)


@pytest.mark.parametrize("doc", corpus.DOCUMENTS, ids=lambda doc: doc.key)
def test_every_document_is_marked_synthetic(doc: corpus.Doc) -> None:
    first_line = doc.source_text().splitlines()[0]
    assert first_line.startswith(f"{corpus.SYNTHETIC_MARKER} - {corpus.ORG_NAME}:")
    assert first_line in CHUNKS[doc.key][0]  # the marker survives into the index


@pytest.mark.parametrize("doc", corpus.DOCUMENTS, ids=lambda doc: doc.key)
def test_documents_build_deterministically(doc: corpus.Doc) -> None:
    assert hashlib.sha256(corpus.build(doc)).digest() == hashlib.sha256(corpus.build(doc)).digest()


# --- Dataset structure --------------------------------------------------------------------------


def test_category_minimums_and_size() -> None:
    counts = Counter(case.category for case in CASES)
    assert 60 <= len(CASES) <= 90
    for category, minimum in MIN_CASES.items():
        assert counts[category] >= minimum, category


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_case_references_exist(case: Case) -> None:
    assert case.principal in corpus.PEOPLE_BY_KEY
    assert case.evidence_docs <= set(corpus.DOCUMENTS_BY_KEY)
    assert set(case.scope_workspaces or ()) <= set(corpus.WORKSPACES_BY_KEY)
    assert set(case.scope_documents or ()) <= set(corpus.DOCUMENTS_BY_KEY)
    assert bool(case.history) == (case.category == "follow_up")


def test_similar_questions_come_in_pairs() -> None:
    pairs = Counter(case.id[:-1] for case in CASES if case.category == "similar_pair")
    assert pairs and set(pairs.values()) == {2}
    for case in CASES:
        if case.category == "similar_pair":
            assert case.forbidden_facts, case.id


# --- Evidence -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.evidence], ids=_ids([c for c in CASES if c.evidence])
)
def test_needles_are_verbatim_and_indexed(case: Case) -> None:
    for item in case.evidence:
        doc = corpus.DOCUMENTS_BY_KEY[item.doc]
        assert item.needle in doc.source_text(), item.needle
        assert any(metrics.contains(chunk, item.needle) for chunk in CHUNKS[item.doc]), item.needle
        assert corpus.can_read(corpus.PEOPLE_BY_KEY[case.principal], doc)
        # Retrieval counts any chunk holding the needle, so it must identify one document.
        holders = [
            d.key for d in corpus.DOCUMENTS if metrics.contains(d.source_text(), item.needle)
        ]
        assert holders == [item.doc], item.needle


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.evidence], ids=_ids([c for c in CASES if c.evidence])
)
def test_labels_agree_with_reference_and_evidence(case: Case) -> None:
    evidence_text = " ".join(
        corpus.DOCUMENTS_BY_KEY[doc].source_text() for doc in case.evidence_docs
    )
    for fact in case.key_facts:
        assert metrics.matches(case.reference, fact), fact
        assert metrics.matches(evidence_text, fact), fact
    for fact in case.forbidden_facts:
        assert not metrics.matches(case.reference, fact), fact


# --- Canaries -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.canaries], ids=_ids([c for c in CASES if c.canaries])
)
def test_canaries_are_not_in_the_conversation(case: Case) -> None:
    asked = " ".join([case.question, *(turn.content for turn in case.history)])
    for canary in case.canaries:
        assert not metrics.matches(asked, canary), canary
        assert not metrics.matches(case.reference, canary), canary


LEAK_CASES = [c for c in CASES if c.expected == "must_not_leak"]


@pytest.mark.parametrize("case", LEAK_CASES, ids=_ids(LEAK_CASES))
def test_leak_canaries_exist_only_in_unreadable_documents(case: Case) -> None:
    unreadable = " ".join(doc.source_text() for doc in _unreadable(case.principal))
    for canary in case.canaries:
        assert metrics.matches(unreadable, canary), canary
        for doc in _readable(case.principal):
            assert not metrics.matches(doc.source_text(), canary), (canary, doc.key)


INJECTION_CASES = [c for c in CASES if c.expected == "must_not_follow_injection"]


@pytest.mark.parametrize("case", INJECTION_CASES, ids=_ids(INJECTION_CASES))
def test_injection_canaries_come_from_the_injection_or_the_prompt(case: Case) -> None:
    (injected,) = case.evidence_docs
    for canary in case.canaries:
        others = [doc for doc in corpus.DOCUMENTS if doc.key != injected]
        assert not any(metrics.matches(doc.source_text(), canary) for doc in others), canary
        source = corpus.DOCUMENTS_BY_KEY[injected].source_text()
        assert metrics.matches(source, canary) or canary in SYSTEM_PROMPT, canary


@pytest.mark.parametrize("case", INJECTION_CASES, ids=_ids(INJECTION_CASES))
def test_injection_key_facts_are_not_in_the_injected_text(case: Case) -> None:
    """An answer that repeats the injection must not score as correct."""
    (injected,) = case.evidence_docs
    paragraphs = corpus.DOCUMENTS_BY_KEY[injected].source_text().split("\n\n")
    hostile = [p for p in paragraphs if metrics.canary_hits(p, case.canaries)]
    assert hostile
    for fact in case.key_facts:
        assert not any(metrics.matches(paragraph, fact) for paragraph in hostile), fact
