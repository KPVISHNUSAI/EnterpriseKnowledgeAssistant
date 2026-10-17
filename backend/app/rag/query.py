"""Query normalization, applied to every user question before retrieval."""

from __future__ import annotations

import unicodedata

from app.core.errors import ValidationFailedError

# Control characters, invisible format characters (zero-width space, soft hyphen, bidi
# overrides) and lone surrogates carry no meaning in a query and break term matching.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


def normalize_query(text: str, *, max_chars: int) -> str:
    """NFKC-normalize, drop control characters, collapse whitespace and trim to
    `max_chars` at a word boundary. Wording, case and punctuation are kept as typed."""
    text = unicodedata.normalize("NFKC", text)
    # Whitespace controls (tab, newline) still separate words; split() collapses them.
    text = "".join(
        ch for ch in text if ch.isspace() or unicodedata.category(ch) not in _DROPPED_CATEGORIES
    )
    text = " ".join(text.split())
    if len(text) > max_chars:
        head = text[: max_chars + 1]  # one extra char shows whether the cut ends a word
        text = head.rsplit(" ", 1)[0] if " " in head else head[:max_chars]
    if not text:
        raise ValidationFailedError("The query is empty", code="empty_query")
    return text
