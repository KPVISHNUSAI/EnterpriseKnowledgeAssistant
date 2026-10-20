"""Offline answer generation (LLM_PROVIDER=extractive): the sentences of the retrieved
passages that best cover the question, each cited, written in the same STATUS-line
contract the LLM prompt demands so the rest of the pipeline treats both alike.
Deterministic and dependency-free; the product runs end to end without an API key."""

from __future__ import annotations

import math
from collections.abc import Generator, Sequence
from dataclasses import dataclass

from app.rag.citations import split_sentences, terms
from app.rag.context import REMOVED
from app.rag.types import ContextPassage

INSUFFICIENT_MESSAGE = (
    "The available documents do not contain enough information to answer this question."
)

MAX_SENTENCES = 3
# Coverage = share of the question's idf-weighted terms a sentence contains.
# ponytail: fixed thresholds tuned by hand on the test corpus; calibrate on the eval set.
ANSWERED_COVERAGE = 0.6
MIN_COVERAGE = 0.3
_DUPLICATE_SHARE = 0.8  # a sentence whose terms mostly repeat a kept one adds nothing
_MIN_SENTENCE_CHARS = 20  # headings and fragments are not answers
_CHUNK_WORDS = 4


@dataclass(frozen=True)
class Pick:
    sentence: str
    source_id: str
    coverage: float
    covered: frozenset[str]


def extractive_answer(question: str, passages: Sequence[ContextPassage]) -> str:
    """The complete answer text, STATUS line first."""
    picks = select_sentences(question, passages)
    if not picks:
        return f"STATUS: INSUFFICIENT\n\n{INSUFFICIENT_MESSAGE}"
    weights = _idf(question, passages)
    covered = sum(weights[term] for pick in picks for term in pick.covered)
    status = "ANSWERED" if covered / sum(weights.values()) >= ANSWERED_COVERAGE else "PARTIAL"
    body = "\n".join(f"- {pick.sentence} [{pick.source_id}]" for pick in picks)
    return f"STATUS: {status}\n\n{body}"


def stream_extractive(
    question: str, passages: Sequence[ContextPassage]
) -> Generator[str, None, None]:
    """The answer in small word groups, so the UI behaves as with a streaming model."""
    words = extractive_answer(question, passages).split(" ")
    for start in range(0, len(words), _CHUNK_WORDS):
        chunk = " ".join(words[start : start + _CHUNK_WORDS])
        yield chunk if start + _CHUNK_WORDS >= len(words) else chunk + " "


def select_sentences(question: str, passages: Sequence[ContextPassage]) -> list[Pick]:
    """Best-covering sentences first, at most MAX_SENTENCES, none below MIN_COVERAGE and
    no near-duplicates. Ties keep passage rank, then document order."""
    weights = _idf(question, passages)
    if not weights:
        return []
    total = sum(weights.values())
    scored: list[Pick] = []
    for passage in passages:
        for sentence in split_sentences(passage.text):
            if len(sentence) < _MIN_SENTENCE_CHARS or REMOVED in sentence:
                continue
            covered = frozenset(weights) & frozenset(terms(sentence))
            coverage = sum(weights[term] for term in covered) / total
            if coverage >= MIN_COVERAGE:
                text = sentence.lstrip("-*# ").strip()  # list markers are not answer text
                scored.append(Pick(text, passage.source_id, coverage, covered))
    scored.sort(key=lambda pick: -pick.coverage)  # stable: ties keep retrieval order
    picks: list[Pick] = []
    kept: list[set[str]] = []
    for pick in scored:
        if len(picks) == MAX_SENTENCES:
            break
        own = set(terms(pick.sentence))
        if any(len(own & other) >= _DUPLICATE_SHARE * len(own) for other in kept):
            continue
        picks.append(pick)
        kept.append(own)
    return picks


def _idf(question: str, passages: Sequence[ContextPassage]) -> dict[str, float]:
    """Smoothed inverse passage frequency of each question term: a term present in every
    passage is weak evidence, a rare one strong."""
    wanted = set(terms(question))
    passage_terms = [set(terms(passage.text)) for passage in passages]
    count = len(passages)
    return {
        term: math.log((count + 1) / (sum(term in held for held in passage_terms) + 1)) + 1
        for term in wanted
    }
