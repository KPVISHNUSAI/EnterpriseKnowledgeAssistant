"""Evaluation metrics: pure functions over strings and numbers (no database, no models).

Matching is lexical and deliberately simple, so every score can be recomputed by hand.
Text is normalized before any comparison: NFKC, case folding, typographic quotes and
dashes folded to ASCII, thousands separators removed ("1,240" == "1240") and whitespace
collapsed. Regular-expression facts ("re:...") are matched against that normalized text,
so they are written in lower case. Normalization and the claim check come from
app/rag/claims.py, the same code that builds answer receipts.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence

from app.rag.claims import SUPPORT_SHARE as SUPPORT_SHARE
from app.rag.claims import Verdict, check_claims
from app.rag.claims import claims as claims
from app.rag.claims import normalize as normalize
from app.rag.claims import supported as supported

_CANARY_ID = re.compile(r"S\d+")


def matches(text: str, fact: str) -> bool:
    """`fact` occurs in `text`: a plain string on word boundaries ("65" is not found in
    "1.65" or "650"), or a regular expression after "re:" applied to the normalized text."""
    haystack = normalize(text)
    if fact.startswith("re:"):
        return re.search(fact[3:], haystack) is not None
    pattern = rf"(?<![\w.]){re.escape(normalize(fact))}(?!\w|\.\d)"
    return re.search(pattern, haystack) is not None


def fact_recall(text: str, facts: Sequence[str]) -> float | None:
    """Share of `facts` stated in `text` (None when there is nothing to check)."""
    if not facts:
        return None
    return sum(matches(text, fact) for fact in facts) / len(facts)


def canary_hits(
    text: str, canaries: Sequence[str], invalid_citations: Sequence[str] = ()
) -> list[str]:
    """Canaries present in an answer. A source-id canary ("S9") also counts when the
    model cited it: the application removes invalid markers from the text and lists them
    in `invalid_citations` instead."""
    invalid = {marker.upper() for marker in invalid_citations}
    return [
        canary
        for canary in canaries
        if matches(text, canary) or (_CANARY_ID.fullmatch(canary) and canary in invalid)
    ]


def contains(text: str, needle: str) -> bool:
    return normalize(needle) in normalize(text)


# --- Retrieval -------------------------------------------------------------------------------


def needle_ranks(candidates: Sequence[str], needles: Sequence[str]) -> list[int | None]:
    """For each needle, the 1-based rank of the first candidate text containing it."""
    texts = [normalize(text) for text in candidates]
    ranks: list[int | None] = []
    for needle in needles:
        wanted = normalize(needle)
        ranks.append(next((i for i, text in enumerate(texts, 1) if wanted in text), None))
    return ranks


def recall_at(ranks: Sequence[int | None], k: int) -> float:
    """Share of the evidence found in the top k (hit@k when there is one needle)."""
    return sum(rank is not None and rank <= k for rank in ranks) / len(ranks)


def reciprocal_rank(ranks: Sequence[int | None]) -> float:
    found = [rank for rank in ranks if rank is not None]
    return 1 / min(found) if found else 0.0


def context_precision(passage_docs: Sequence[str], evidence_docs: Iterable[str]) -> float | None:
    """Share of the context passages that come from an evidence document."""
    if not passage_docs:
        return None
    wanted = set(evidence_docs)
    return sum(doc in wanted for doc in passage_docs) / len(passage_docs)


def context_recall(passage_texts: Sequence[str], needles: Sequence[str]) -> float | None:
    """Share of the evidence needles that reached the context (what the generator saw)."""
    if not needles:
        return None
    return recall_at(needle_ranks(passage_texts, needles), len(passage_texts))


def context_relevance(passages: Sequence[tuple[str, int]], needles: Sequence[str]) -> float | None:
    """Share of the context's tokens that sit in a passage holding an evidence needle: how
    much of the prompt budget goes to text that answers the question. `passages` are
    (text, token count) pairs."""
    total = sum(tokens for _, tokens in passages)
    if not needles or not total:
        return None
    held = sum(tokens for text, tokens in passages if any(contains(text, n) for n in needles))
    return held / total


# --- Generation ------------------------------------------------------------------------------


def faithfulness(
    answer: str, question: str, passages: Mapping[str, str]
) -> dict[str, float | int | None]:
    """Claims, supported claims and uncited claims of one answer; `passages` maps source
    ids to the passage text given to the generator."""
    found = check_claims(answer, question, passages, strict=False)
    support = sum(claim.verdict is Verdict.SUPPORTED for claim in found)
    return {
        "claims": len(found),
        "supported": support,
        "uncited": sum(claim.verdict is Verdict.UNCITED for claim in found),
        "score": support / len(found) if found else None,
    }


def citation_scores(
    cited: Sequence[tuple[str, str]], needles: Sequence[str], evidence_docs: Iterable[str]
) -> dict[str, float | None]:
    """`cited` holds (document key, passage text) for each verified citation. Precision:
    cited passages containing an evidence needle; document precision: cited passages from
    an evidence document; recall: needles inside some cited passage."""
    if not cited:
        return {"precision": None, "doc_precision": None, "recall": 0.0 if needles else None}
    wanted = set(evidence_docs)
    texts = [text for _, text in cited]
    return {
        "precision": sum(any(contains(text, n) for n in needles) for text in texts) / len(texts),
        "doc_precision": sum(doc in wanted for doc, _ in cited) / len(cited),
        "recall": recall_at(needle_ranks(texts, needles), len(texts)) if needles else None,
    }


def abstention(pairs: Iterable[tuple[bool, bool]]) -> dict[str, float | int | None]:
    """Abstention as a binary classifier over (should_abstain, abstained) pairs."""
    tp = fp = fn = tn = 0
    for should, did in pairs:
        tp += should and did
        fp += did and not should
        fn += should and not did
        tn += not should and not did
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_abstention_rate": fp / (fp + tn) if fp + tn else None,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


# --- Aggregation -----------------------------------------------------------------------------


def mean(values: Iterable[float | int | bool | None]) -> float | None:
    kept = [float(value) for value in values if value is not None]
    return sum(kept) / len(kept) if kept else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default method)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
