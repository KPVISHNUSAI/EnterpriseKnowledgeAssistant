"""Follow-up questions ("what about contractors?") are rewritten into standalone search
queries before retrieval. Only the search text changes: the user's wording is what gets
answered and shown. With an LLM the rewrite model does it; without one a deterministic
heuristic borrows the salient terms of the previous question."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.errors import ValidationFailedError
from app.providers.llm import LLMError, LLMProvider, LLMRequest
from app.rag.citations import content_words, stem, terms
from app.rag.prompts import REWRITE_SYSTEM_PROMPT, build_rewrite_messages
from app.rag.query import normalize_query
from app.rag.types import ChatTurn, TokenUsage

logger = logging.getLogger(__name__)

# Thinking counts against the cap on Claude; a tiny cap can yield an empty rewrite.
REWRITE_MAX_OUTPUT_TOKENS = 1024
_LABEL = re.compile(r"^(?:standalone\s+)?(?:search\s+)?query\s*:\s*", re.IGNORECASE)
_QUOTES = "\"'\u201c\u201d\u2018\u2019\u00ab\u00bb"
_WORD = re.compile(r"[^\W_]+")
# Words that point back at something said earlier.
_ANAPHORA_TEXT = (
    "it its this that these those they them their there he she him her same one ones also too "
    "else more another other again about instead"
)
_ANAPHORA = frozenset(_ANAPHORA_TEXT.split())
_SHORT_FOLLOW_UP_TERMS = 1
# A longer question names its own subject even with a pronoun in it: the "their" in "Can
# employees share their email passwords?" is the employees.
_ANAPHORA_MAX_TERMS = 3


@dataclass
class Rewrite:
    query: str
    usage: TokenUsage | None = None  # set whenever an LLM call was made, accepted or not
    model: str | None = None


def rewrite_query(
    question: str, history: Sequence[ChatTurn], *, llm: LLMProvider | None
) -> Rewrite:
    """`question` is already normalized. Falls back to it on any problem."""
    settings = get_settings()
    recent = list(history)[-settings.chat_history_messages :]
    if not recent:
        return Rewrite(question)
    if llm is None:
        return Rewrite(heuristic_rewrite(question, recent))
    request = LLMRequest(
        system=REWRITE_SYSTEM_PROMPT,
        messages=build_rewrite_messages(question, recent),
        max_output_tokens=REWRITE_MAX_OUTPUT_TOKENS,
        effort=settings.anthropic_rewrite_effort,
        model=settings.query_rewrite_model,
        allow_fallbacks=False,
        purpose="query_rewrite",
    )
    try:
        response = llm.generate(request)
    except LLMError as exc:
        logger.warning("rewrite.failed", extra={"kind": exc.kind, "retryable": exc.retryable})
        return Rewrite(question)
    usage = TokenUsage(response.usage.input_tokens, response.usage.output_tokens)
    accepted = _accept(response.text, max_chars=settings.max_query_chars)
    return Rewrite(accepted or question, usage, response.model)


def heuristic_rewrite(question: str, history: Sequence[ChatTurn]) -> str:
    """A short question, or one of a few terms that refers back ("why is that?", "does it
    apply to contractors?"), gets the content words of the previous question appended;
    anything else is taken as standalone.
    ponytail: a lexical guess; the LLM rewrite is the real thing when a model is configured."""
    previous = next((turn.content for turn in reversed(history) if turn.role == "user"), None)
    if previous is None:
        return question
    own = set(terms(question))
    words = set(_WORD.findall(question.casefold()))
    refers_back = bool(words & _ANAPHORA) and len(own) <= _ANAPHORA_MAX_TERMS
    if len(own) > _SHORT_FOLLOW_UP_TERMS and not refers_back:
        return question
    salient = [word for word in content_words(previous) if stem(word.casefold()) not in own]
    return f"{question} {' '.join(dict.fromkeys(salient))}" if salient else question


def _accept(text: str, *, max_chars: int) -> str | None:
    """The model's query, or None when the output is unusable (empty, over-long, or
    prose instead of a query)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        reason = "empty"
    elif len(lines) > 1:
        reason = "multiline"
    elif len(lines[0]) > max_chars:
        reason = "too_long"
    else:
        query = _LABEL.sub("", lines[0]).strip(_QUOTES + " ")
        try:
            return normalize_query(query, max_chars=max_chars)
        except ValidationFailedError:
            reason = "empty"
    logger.info("rewrite.rejected", extra={"reason": reason})
    return None
