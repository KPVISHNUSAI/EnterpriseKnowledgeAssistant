"""Citation validation: [S1]-style markers are checked against the passages that were
actually given to the model for this request. A marker is verified only when its id was
supplied; invalid markers are removed from the displayed text and reported, quotations
are checked against the passage text, and the final status follows from what survived.
Also home to the small lexical helpers (sentences, terms) the extractive generator and the
follow-up heuristic share."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.rag.types import AnswerStatus, Citation, ContextPassage

MIN_QUOTE_CHARS = 20
EXCERPT_MAX_CHARS = 500

# Square brackets, or what some models write instead: parentheses, or full-width brackets
# (gpt-oss: U+3010/U+3011, U+FF3B/U+FF3D).
_MARKER = re.compile(r"[\[(\u3010\uff3b]\s*([sS]\d+(?:\s*[,;]\s*[sS]\d+)*)\s*[\])\u3011\uff3d]")
_ID = re.compile(r"[sS]\d+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[^\W_]+")
# Straight, curly and guillemet quotes; the quoted text must not contain a line break.
_QUOTE = re.compile(
    rf"\"([^\"\n]{{{MIN_QUOTE_CHARS},}}?)\"|\u201c([^\u201d\n]{{{MIN_QUOTE_CHARS},}}?)\u201d"
    rf"|\u00ab([^\u00bb\n]{{{MIN_QUOTE_CHARS},}}?)\u00bb"
)
_PUNCTUATION = ".,;:!?"
# Typographic quotes and dashes fold onto their ASCII forms before comparing text.
_QUOTE_CHARS = str.maketrans(
    {
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
        "\N{DOUBLE LOW-9 QUOTATION MARK}": '"',
        "\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}": '"',
        "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}": '"',
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{EN DASH}": "-",
        "\N{EM DASH}": "-",
    }
)
_SUFFIXES = (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("ly", ""), ("es", ""))
_STOP_WORD_TEXT = (
    "a an the and or but if of to in on at by for with from as into about is are was were be "
    "been being it its this that these those there their they them he she his her we you your "
    "our i me my us do does did not no can could should would will may might must have has had "
    "what which who whom whose when where why how"
)
STOP_WORDS = frozenset(_STOP_WORD_TEXT.split())


@dataclass
class ValidatedAnswer:
    text: str  # invalid markers removed, valid ones in canonical form
    citations: list[Citation] = field(default_factory=list)  # one per cited source, in order
    invalid_citations: list[str] = field(default_factory=list)
    unverified_quotes: list[str] = field(default_factory=list)


def validate(text: str, passages: Sequence[ContextPassage]) -> ValidatedAnswer:
    by_id = {passage.source_id: passage for passage in passages}
    pieces: list[str] = []
    claims: dict[str, list[str]] = {}  # source id -> answer sentences that cite it
    invalid: list[str] = []
    position = 0
    for match in _MARKER.finditer(text):
        ids = [marker.upper() for marker in _ID.findall(match.group(1))]
        valid = list(dict.fromkeys(marker for marker in ids if marker in by_id))
        invalid.extend(marker for marker in ids if marker not in by_id)
        before = text[position : match.start()]
        if valid:
            pieces.append(before)
            pieces.append(f"[{', '.join(valid)}]")
            sentence = _sentence_around(text, match.start())
            for marker in valid:
                claims.setdefault(marker, []).append(sentence)
        else:
            # Dropping "[S9]" from "claim [S9]." must not leave "claim ." behind.
            after = text[match.end() : match.end() + 1]
            if before.endswith(" ") and (not after or after.isspace() or after in _PUNCTUATION):
                before = before[:-1]
            pieces.append(before)
        position = match.end()
    pieces.append(text[position:])
    cleaned = "".join(pieces)
    return ValidatedAnswer(
        text=cleaned,
        citations=[_citation(by_id[marker], " ".join(cited)) for marker, cited in claims.items()],
        invalid_citations=list(dict.fromkeys(invalid)),
        unverified_quotes=unverified_quotes(cleaned, passages),
    )


def final_status(declared: str | None, citations: Sequence[Citation]) -> AnswerStatus:
    """INSUFFICIENT is the model's abstention and wins; otherwise an answer counts as
    verified only when at least one citation maps to a supplied passage."""
    if declared == "INSUFFICIENT":
        return AnswerStatus.INSUFFICIENT_EVIDENCE
    if not any(citation.verified for citation in citations):
        return AnswerStatus.UNVERIFIED
    return AnswerStatus.PARTIAL if declared == "PARTIAL" else AnswerStatus.ANSWERED


def unverified_quotes(text: str, passages: Sequence[ContextPassage]) -> list[str]:
    """Quoted spans of MIN_QUOTE_CHARS or more that appear in no supplied passage, after
    whitespace, quote-style and case normalization."""
    haystack = " ".join(_normalize(passage.text) for passage in passages)
    quotes = [match.group(match.lastindex or 1) for match in _QUOTE.finditer(text)]
    return [
        quote for quote in quotes if _normalize(quote).strip(_PUNCTUATION + " ") not in haystack
    ]


def best_excerpt(passage_text: str, claim: str) -> str | None:
    """The passage sentence sharing the most terms with `claim`, plus one neighbour when
    it supports the claim too and the excerpt stays short. Falls back to the first sentence."""
    sentences = split_sentences(passage_text)
    if not sentences:
        return None
    wanted = set(terms(claim))
    scores = [len(wanted & set(terms(sentence))) for sentence in sentences]
    best = max(range(len(sentences)), key=lambda i: (scores[i], -i))
    chosen = [best]
    # One adjacent sentence joins when it carries real support too (at least half the
    # best sentence's overlap); ties prefer the following sentence.
    neighbours = [
        i
        for i in (best + 1, best - 1)
        if 0 <= i < len(sentences) and scores[i] > 0 and 2 * scores[i] >= scores[best]
    ]
    if neighbours:
        neighbour = max(neighbours, key=lambda i: scores[i])
        if len(sentences[best]) + len(sentences[neighbour]) <= EXCERPT_MAX_CHARS:
            chosen.append(neighbour)
    excerpt = " ".join(sentences[i] for i in sorted(chosen))
    if len(excerpt) > EXCERPT_MAX_CHARS:
        excerpt = excerpt[:EXCERPT_MAX_CHARS].rsplit(" ", 1)[0] + " ..."
    return excerpt


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(text) if part and part.strip()]


def terms(text: str) -> list[str]:
    """Lowercased content words, crudely stemmed so "policies" matches "policy"."""
    return [stem(word) for word in _WORD.findall(text.casefold()) if _is_content(word)]


def content_words(text: str) -> list[str]:
    """Like `terms`, but the words as written (for building readable queries)."""
    return [word for word in _WORD.findall(text) if _is_content(word.casefold())]


def stem(word: str) -> str:
    """Suffix stripping, good enough for overlap scoring: no dictionary, no dependency.
    ponytail: a Snowball stemmer would be more accurate if scoring quality ever matters."""
    for suffix, replacement in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + replacement
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        word = word[:-1]
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def _is_content(word: str) -> bool:
    return len(word) > 1 and word not in STOP_WORDS


def _citation(passage: ContextPassage, claim: str) -> Citation:
    c = passage.candidate
    return Citation(
        source_id=passage.source_id,
        verified=True,
        document_id=c.document_id,
        document_title=c.document_title,
        page_number=c.page_number,
        section_title=c.section_title,
        excerpt=best_excerpt(passage.text, claim),
    )


def _sentence_around(text: str, index: int) -> str:
    """The answer sentence containing the marker at `index`, markers removed. A marker
    that opens a sentence ("claim. [S1]") belongs to the sentence before it."""
    boundaries = list(_SENTENCE_END.finditer(text, 0, index))
    start = boundaries[-1].end() if boundaries else 0
    if boundaries and not _MARKER.sub("", text[start:index]).strip():
        return _sentence_around(text, boundaries[-1].start() - 1)
    following = _SENTENCE_END.search(text, index)
    end = following.start() if following else len(text)
    return _MARKER.sub("", text[start:end]).strip()


def _normalize(text: str) -> str:
    return " ".join(text.translate(_QUOTE_CHARS).casefold().split())
