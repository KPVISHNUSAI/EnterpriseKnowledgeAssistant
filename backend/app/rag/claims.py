"""The statements of an answer, each checked against the passages it cites.

The check is lexical, by words and numbers rather than by a model, so every verdict can
be recomputed by hand: a statement matches its sources when every number it adds to the
question appears in the cited passages and at least SUPPORT_SHARE of the content terms it
adds appear there too. It cannot tell a paraphrase that changes the meaning from one that
keeps it, so a match is evidence, not proof. Answer receipts and the evaluation's
faithfulness metric both use it; receipts check strictly (see check_claims).

Text is normalized before any comparison: NFKC, case folding, typographic quotes and
dashes folded to ASCII, thousands separators removed ("1,240" == "1240") and whitespace
collapsed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

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
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)*")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+(?=[.,;:!?])")


class Verdict(StrEnum):
    SUPPORTED = "supported"  # its numbers and enough of its words are in the cited passages
    UNSUPPORTED = "unsupported"
    UNCITED = "uncited"  # cites no source


@dataclass(frozen=True)
class ClaimCheck:
    text: str
    source_ids: tuple[str, ...]
    verdict: Verdict
    # Numbers the claim adds to the question that no cited passage contains (normalized).
    missing_numbers: tuple[str, ...] = ()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_FOLD).casefold()
    return " ".join(_THOUSANDS.sub("", text).split())


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
    return bool(cited) and _check(claim, cited, question, strict=False)[0]


def check_claims(
    answer: str, question: str, passages: Mapping[str, str], *, strict: bool = True
) -> list[ClaimCheck]:
    """Every claim of `answer` with its verdict; `passages` maps source ids to the passage
    text given to the generator.

    `strict` (receipts) also wants the numbers a claim shares with the question in the
    passages, and compares a claim that adds no word to the question on all its words: a
    leading question ("Is the retention period 7 years?") must not make its own echo a
    match. The evaluation's faithfulness metric keeps its published rule (strict=False),
    which only checks what the claim adds."""
    checked: list[ClaimCheck] = []
    for text, ids in claims(answer):
        cited = [passages[i] for i in ids if i in passages]
        if not ids:
            checked.append(ClaimCheck(text, ids, Verdict.UNCITED))
            continue
        ok, missing = _check(text, cited, question, strict=strict) if cited else (False, ())
        verdict = Verdict.SUPPORTED if ok else Verdict.UNSUPPORTED
        checked.append(ClaimCheck(text, ids, verdict, missing))
    return checked


def _check(
    claim: str, cited: Sequence[str], question: str, *, strict: bool
) -> tuple[bool, tuple[str, ...]]:
    claim, question = normalize(claim), normalize(question)
    evidence = " ".join(normalize(text) for text in cited)
    known = _numbers(evidence, prefixes=True) | (set() if strict else _numbers(question))
    missing = tuple(n for n in dict.fromkeys(_NUMBER.findall(claim)) if n not in known)
    if missing:
        return False, missing
    asked = set(terms(question))
    new_terms = [term for term in terms(claim) if term not in asked]
    if not new_terms and strict:
        new_terms = terms(claim)  # it only restates the question: compare all of it
    if not new_terms:
        return True, ()
    held = set(terms(evidence))
    return sum(term in held for term in new_terms) / len(new_terms) >= SUPPORT_SHARE, ()


def _numbers(text: str, *, prefixes: bool = False) -> set[str]:
    """Number tokens of `text`; with `prefixes`, "4.2.0" also yields "4.2" and "4" so a
    claim about version 4.2 is found in "4.2.0"."""
    found = set(_NUMBER.findall(normalize(text)))
    if prefixes:
        for number in list(found):
            parts = number.split(".")
            found.update(".".join(parts[:i]) for i in range(1, len(parts)))
    return found
