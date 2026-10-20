"""Detection of text that tries to instruct an AI assistant (prompt injection).

Rule-based and deterministic, so every flag can be explained to the people who read the
document. A flag starts at the sentence holding a rule's first match and runs to the end
of its paragraph: injected instructions keep going after the phrase that gives them away
("...then add this link"), while the text before it is usually the document's own. A
match inside double quotes is a mention ("attackers write \"ignore all previous
instructions\""), not an instruction, and is skipped.

The rules are narrow on purpose: an ordinary memo saying "disregard the previous
instructions" or a manual saying "the drone enters manual override mode" is not flagged.
They will miss rephrased attacks; answers still cannot carry data out on their own (see
docs/ARCHITECTURE.md#text-addressed-to-ai-assistants). The answer pipeline leaves
flagged ranges out of what the model reads (app/rag/context.py).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from enum import StrEnum
from typing import NamedTuple


class FlagRule(StrEnum):
    ADDRESSED = "addressed"  # "Important notice to AI assistants", "If you are an AI ..."
    OVERRIDE = "override"  # "ignore all previous instructions", "disregard your rules"
    EXTRACTION = "extraction"  # "print your complete system prompt"
    MARKUP = "markup"  # the prompt's own fences or a chat template: "</sources>", "[INST]"


_F = re.IGNORECASE
# Who an injection talks to. A bare "AI" counts only where a phrase ends ("Dear AI,"),
# so "a note for AI projects" is not an address.
_AI = (
    r"(?:(?:AI|virtual|digital)\s+(?:assistants?|agents?|models?|readers?)|LLMs?"
    r"|(?:large\s+)?language\s+models?|chat\s?bots?|ChatGPT|GPT-?\d[\w.]*"
    r"|AI(?=\s*(?:[:,;!?)]|\.(?!\w)|$)))"
)
_READING = r"(?:reading|processing|summari[sz]ing|parsing|analy[sz]ing|ingesting)"
_GREETING = re.compile(rf"\b(?:dear|hey|hello|hi)\s+{_AI}", _F)
# A note "to AI assistants" only counts when the text goes on to speak to the reader.
_NOTE = re.compile(
    rf"\b(?:notice|note|message|instructions?|attention|memo|warning|reminder)\s+(?:to|for)"
    rf"\s+(?:all\s+|any\s+|the\s+)?{_AI}",
    _F,
)
_YOU = re.compile(r"\byou(?:r|rs|rself)?\b", _F)
_ADDRESS_END = re.compile(r"\s*[:.!]")
_IMPERATIVE = re.compile(
    r"\s*[:,-]?\s*(?:ignore|disregard|forget|print|output|say|add|include|insert|append"
    r"|respond|reply|answer|tell|cite|write|summari[sz]e|classify|treat|mark|approve"
    r"|do\s+not|don't|never|always)\b",
    _F,
)
# What an injection says it is reading; "processing this data" is a data-protection rule.
_THIS_TEXT = (
    r"(?:the\s+following\b|(?:this|these)(?=\s*[:,.;]|\s+(?:document|file|text|page|e-?mail"
    r"|message|invoice|content|pdf|report|resume|cv|letter|contract)s?\b))"
)
_SELF_ADDRESS = re.compile(
    rf"\b(?:if|when|whenever|while)\s+you(?:'re|\s+are)\s+(?:an?\s+|the\s+)?"
    # The address must end here: "If you are an LLM developer" speaks to a person.
    rf"(?:{_AI}|AI)(?=\s*(?:[,.:;!?)]|$)|\s+(?:{_READING}|that|who|which|and|or|tasked"
    rf"|asked|being|used|helping|answering)\b)"
    rf"|\b(?:any|every|each|all)\s+(?:{_AI}|AI)\s+(?:that\s+is\s+|who\s+is\s+)?{_READING}"
    rf"\s+{_THIS_TEXT}",
    _F,
)
# "Never ignore your safety training" is advice, not an attack.
_VERB = r"(?<!\bnever\s)(?<!\bnot\s)(?<!n't\s)(?:ignore|disregard|forget|override|bypass)"
_OVERRIDE = re.compile(
    rf"\b{_VERB}\s+(?:all|any|every)\s+(?:of\s+)?(?:the\s+|your\s+)?"
    r"(?:previous|prior|preceding|earlier|above|foregoing)\s+"
    r"(?:instructions|prompts|directions|directives|rules)\b"
    rf"|\b{_VERB}\s+(?:all\s+)?(?:of\s+)?your\s+(?:(?:previous|prior|original|initial|system|safety)\s+)?"
    r"(?:instructions|rules|prompt|programming|guidelines|guardrails|restrictions|training)\b"
    rf"|\b{_VERB}\s+(?:the\s+)?system\s+(?:prompt|message|instructions)\b",
    _F,
)
_EXTRACTION = re.compile(
    r"\b(?:print|reveal|show|output|repeat|display|disclose|leak|dump|recite|write\s+out"
    r"|tell\s+(?:me|us|the\s+user))\s+(?:me\s+|us\s+)?"
    r"(?:your\s+(?:(?:complete|full|entire|exact|original|initial|hidden|secret|internal)\s+)*"
    r"(?:system\s+prompt|system\s+message|system\s+instructions|instructions|prompt)"
    r"|the\s+(?:(?:complete|full|entire|exact|original|initial|hidden|secret|internal)\s+)*"
    r"(?:system\s+prompt|hidden\s+prompt|initial\s+prompt))\b",
    _F,
)
# The fences of app/rag/prompts.py and the chat templates of common open models.
_MARKUP = re.compile(
    r"</?\s*sources\s*>|<\s*source\s+id\s*=|</?\s*conversation\s*>"
    r"|<\|(?:im_start|im_end|system|user|assistant|endoftext|eot_id|start_header_id"
    r"|end_header_id)\|>|\[/?INST\]|<</?SYS>>",
    _F,
)
_ENDS_SENTENCE = re.compile(r"[.!?][\"'\u201d\u2019)\]]*\s*$")
_SENTENCE_END = re.compile(r"[.!?][\"'\u201d\u2019)\]]*\s+")


class Flag(NamedTuple):
    start: int
    end: int
    rule: FlagRule
    page: int | None = None  # where the range starts, set by the chunker for PDFs


def find_instructions(text: str, *, by_line: bool = False, wrapped: bool = False) -> list[Flag]:
    """Flagged ranges of `text`, one paragraph at most one flag. `by_line` treats every
    line as a paragraph (lists, tables, code); `wrapped` (PDF text) continues a line that
    does not end a sentence on the next one, because extraction rejoins most wrapped lines
    but not all: a long link pushed to the next line leaves the line before it short."""
    flags = []
    for start, end in _paragraphs(text, by_line or wrapped, wrapped):
        paragraph = text[start:end]
        hit = _first_hit(paragraph)
        if hit is None:
            continue
        position, rule = hit
        sentence = max((m.end() for m in _SENTENCE_END.finditer(paragraph, 0, position)), default=0)
        flags.append(Flag(start + sentence, start + len(paragraph.rstrip()), rule))
    return flags


def _paragraphs(text: str, by_line: bool, wrapped: bool) -> Iterator[tuple[int, int]]:
    if not by_line:
        yield 0, len(text)
        return
    start = end = 0
    for line in text.split("\n"):
        end += len(line)
        if not wrapped or _ENDS_SENTENCE.search(line) or not line.strip():
            yield start, end
            start = end + 1
        end += 1
    if start < len(text):
        yield start, len(text)


def _first_hit(paragraph: str) -> tuple[int, FlagRule] | None:
    hits = [
        (m.start(), rule)
        for rule, matches in (
            (FlagRule.ADDRESSED, _GREETING.finditer(paragraph)),
            (FlagRule.ADDRESSED, _notes(paragraph)),
            (FlagRule.ADDRESSED, _SELF_ADDRESS.finditer(paragraph)),
            (FlagRule.OVERRIDE, _OVERRIDE.finditer(paragraph)),
            (FlagRule.EXTRACTION, _EXTRACTION.finditer(paragraph)),
            (FlagRule.MARKUP, _MARKUP.finditer(paragraph)),
        )
        for m in matches
        if not _quoted(paragraph, m.start(), m.end())
    ]
    return min(hits, default=None)


def _notes(paragraph: str) -> Iterator[re.Match[str]]:
    """Notes that go on to speak to the reader: the address ends there ("Note to AI
    assistants:"), an instruction follows, or "you" occurs in the same sentence."""
    for match in _NOTE.finditer(paragraph):
        end = _SENTENCE_END.search(paragraph, match.end())
        sentence = paragraph[match.end() : end.start() if end else len(paragraph)]
        if (
            _ADDRESS_END.match(paragraph, match.end())
            or _IMPERATIVE.match(paragraph, match.end())
            or _YOU.search(sentence)
        ):
            yield match


def _quoted(paragraph: str, start: int, end: int) -> bool:
    """Whether paragraph[start:end] lies inside straight or curly double quotes."""
    before, after = paragraph[:start], paragraph[end:]
    if before.count('"') % 2 == 1 and '"' in after:
        return True
    return before.rfind("“") > before.rfind("”") and "”" in after
