"""Evaluation metrics: pure functions over strings and numbers (no database, no models).

Matching is lexical and deliberately simple, so every score can be recomputed by hand.
Text is normalized before any comparison: NFKC, case folding, typographic quotes and
dashes folded to ASCII, thousands separators removed ("1,240" == "1240") and whitespace
collapsed. Regular-expression facts ("re:...") are matched against that normalized text,
so they are written in lower case. Sentence splitting and content terms reuse
app/rag/citations.py, the helpers the application uses for excerpts and the extractive
generator.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

from app.rag.citations import split_sentences, terms

SUPPORT_SHARE = 0.5  # a claim's new terms that must appear in the passages it cites

_FOLD = str.maketrans(
    {
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{EN DASH}": "-",
        "\N{EM DASH}": "-",
        "\N{MINUS SIGN}": "-",
    }
)
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_MARKER = re.compile(r"\[\s*([sS]\d+(?:\s*[,;]\s*[sS]\d+)*)\s*\]")
_SOURCE_ID = re.compile(r"[sS]\d+")
_CANARY_ID = re.compile(r"S\d+")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)*")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+(?=[.,;:!?])")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_FOLD).casefold()
    return " ".join(_THOUSANDS.sub("", text).split())


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


def claims(answer: str) -> list[tuple[str, tuple[str, ...]]]:
    """Answer sentences (and list items) with the source ids cited in them. A sentence
    that is only markers ("[S1]" after the full stop) belongs to the one before it;
    lead-in lines ending in ":" and fragments of fewer than two terms without a number
    are not claims."""
    found: list[tuple[str, tuple[str, ...]]] = []
    for sentence in split_sentences(answer):
        ids = tuple(
            dict.fromkeys(
                m.upper() for g in _MARKER.findall(sentence) for m in _SOURCE_ID.findall(g)
            )
        )
        text = _SPACE_BEFORE_PUNCTUATION.sub("", _LIST_MARKER.sub("", _MARKER.sub("", sentence)))
        text = text.strip()
        if not terms(text) and not _NUMBER.search(normalize(text)):
            if found and ids:
                previous, cited = found[-1]
                found[-1] = (previous, tuple(dict.fromkeys(cited + ids)))
            continue
        if text.endswith(":") or (len(terms(text)) < 2 and not _NUMBER.search(normalize(text))):
            continue
        found.append((text, ids))
    return found


def supported(claim: str, cited: Sequence[str], question: str) -> bool:
    """A claim is supported when it cites at least one passage, every number it adds to
    the question appears in the cited passages, and at least SUPPORT_SHARE of the content
    terms it adds appear there too."""
    if not cited:
        return False
    claim, question = normalize(claim), normalize(question)
    evidence = " ".join(normalize(text) for text in cited)
    asked = set(terms(question))
    new_numbers = _numbers(claim) - _numbers(question)
    if not new_numbers <= _numbers(evidence, prefixes=True):
        return False
    new_terms = [term for term in terms(claim) if term not in asked]
    if not new_terms:
        return True
    held = set(terms(evidence))
    return sum(term in held for term in new_terms) / len(new_terms) >= SUPPORT_SHARE


def faithfulness(
    answer: str, question: str, passages: Mapping[str, str]
) -> dict[str, float | int | None]:
    """Claims, supported claims and uncited claims of one answer; `passages` maps source
    ids to the passage text given to the generator."""
    found = claims(answer)
    support = [
        supported(text, [passages[i] for i in ids if i in passages], question)
        for text, ids in found
    ]
    return {
        "claims": len(found),
        "supported": sum(support),
        "uncited": sum(not ids for _, ids in found),
        "score": sum(support) / len(found) if found else None,
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


def _numbers(text: str, *, prefixes: bool = False) -> set[str]:
    """Number tokens of `text`; with `prefixes`, "4.2.0" also yields "4.2" and "4" so a
    claim about version 4.2 is found in "4.2.0"."""
    found = set(_NUMBER.findall(normalize(text)))
    if prefixes:
        for number in list(found):
            parts = number.split(".")
            found.update(".".join(parts[:i]) for i in range(1, len(parts)))
    return found
