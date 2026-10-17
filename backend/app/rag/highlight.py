"""Match highlighting for search results: whole-chunk (text, is_match) segments."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.rag.retrieval import or_tsquery, query_lexemes
from app.rag.types import Candidate

# Control characters as delimiters: cleaned chunk text never contains them, so splitting
# on them cannot be confused by document content (unlike HTML-style markers).
_START, _STOP = "\x02", "\x03"
_OPTIONS = f"HighlightAll=true, StartSel={_START}, StopSel={_STOP}"
_HEADLINES = text(
    "SELECT ts_headline(CAST(:config AS regconfig), t.content, CAST(:query AS tsquery), :options) "
    "FROM unnest(CAST(:contents AS text[])) WITH ORDINALITY AS t(content, n) ORDER BY t.n"
)


def add_highlights(db: Session, candidates: Sequence[Candidate], search_query: str) -> None:
    """Set `highlights` on every candidate to segments that concatenate to its content
    exactly. Only keyword matches get highlighted terms; vector-only matches are a single
    unhighlighted segment."""
    for candidate in candidates:
        candidate.highlights = [(candidate.content, False)]
    keyword_matches = [c for c in candidates if c.keyword_rank is not None]
    lexemes = query_lexemes(db, search_query) if keyword_matches else []
    if not lexemes:
        return
    params = {
        "config": get_settings().fts_config,
        "query": or_tsquery(lexemes),
        "options": _OPTIONS,
        "contents": [c.content for c in keyword_matches],
    }
    for candidate, marked in zip(keyword_matches, db.scalars(_HEADLINES, params), strict=True):
        segments = _segments(marked)
        # ts_headline drops words over 2047 characters; never show text that differs.
        if "".join(segment for segment, _ in segments) == candidate.content:
            candidate.highlights = segments


def _segments(marked: str) -> list[tuple[str, bool]]:
    head, *rest = marked.split(_START)
    segments = [(head, False)]
    for part in rest:
        match, _, tail = part.partition(_STOP)
        segments += [(match, True), (tail, False)]
    return [(segment, is_match) for segment, is_match in segments if segment]
