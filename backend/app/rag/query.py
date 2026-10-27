"""Query normalization, applied to every user question before retrieval, and the small
talk that needs no retrieval at all."""

from __future__ import annotations

import re
import unicodedata

from app.core.errors import ValidationFailedError

# Control characters, invisible format characters (zero-width space, soft hyphen, bidi
# overrides) and lone surrogates carry no meaning in a query and break term matching.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})

# Whole messages that are only a greeting, a question about the assistant, or thanks
# (case folded, punctuation dropped). Anything more ("hi, how many leave days?") is a
# question for the documents.
_GREETING = r"(?:hi|hello|hey|hiya|good (?:morning|afternoon|evening))(?: there)?"
_ABOUT = (
    r"(?:can i ask(?: you)?(?: a| some| a few)? questions?|are you there|who are you"
    r"|what (?:are you|can you do|do you do)|how (?:can|do) you help(?: me)?|help)"
)
_INTRO = re.compile(rf"{_GREETING}(?: {_ABOUT})?|{_ABOUT}")
_THANKS = re.compile(r"(?:thanks|thank you|thx|ok|okay|great|got it)(?: so much| a lot)?")
_PUNCTUATION = re.compile(r"[^\w\s]")
INTRO_REPLY = (
    "Hi! Ask me a question about the documents you can access. I answer only from what "
    "they say, and cite the document and page behind each point. To ask about particular "
    "documents only, choose them with the scope button."
)
THANKS_REPLY = "Glad to help. Ask another question about your documents whenever you like."


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


def small_talk_reply(query: str) -> str | None:
    """The fixed reply to a message that is only small talk, which is answered without
    searching the documents; None for anything else.
    ponytail: English phrases only; other languages go through the normal pipeline."""
    text = " ".join(_PUNCTUATION.sub(" ", query.casefold()).split())
    if _INTRO.fullmatch(text):
        return INTRO_REPLY
    return THANKS_REPLY if _THANKS.fullmatch(text) else None
