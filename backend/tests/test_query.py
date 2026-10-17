"""normalize_query: the cleaning every question gets before retrieval (app/rag/query.py)."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationFailedError
from app.rag.query import normalize_query


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("What is the PTO policy?", "What is the PTO policy?"),  # case, stop words, "?" kept
        ("  vacation \t policy\n\nfor   2026 ", "vacation policy for 2026"),
        (
            "\uff46\uff55\uff4c\uff4c\u3000\uff57\uff49\uff44\uff54\uff48 \ufb01le",
            "full width file",
        ),  # NFKC
        ("co\u00adoperation", "cooperation"),  # soft hyphen
        ("zero\u200bwidth", "zerowidth"),
        ("bell\x07 null\x00 del\x7f", "bell null del"),
        ("a\x1cb\u2028c\x85d", "a b c d"),  # separators and NEL are whitespace
        ("rtl \u202eoverride\u202c", "rtl override"),
        ("lone \ud800surrogate", "lone surrogate"),
        ("Ünïcödé 中文 العربية 🎉", "Ünïcödé 中文 العربية 🎉"),
    ],
)
def test_normalizes_without_changing_meaning(raw, expected):
    assert normalize_query(raw, max_chars=100) == expected


@pytest.mark.parametrize(
    ("raw", "max_chars", "expected"),
    [
        ("alpha beta gamma", 12, "alpha beta"),  # cut inside "gamma" -> drop the word
        ("alpha beta gamma", 10, "alpha beta"),  # cut exactly at a boundary
        ("alpha beta gamma", 11, "alpha beta"),
        ("alpha beta gamma", 16, "alpha beta gamma"),
        ("supercalifragilistic", 5, "super"),  # a single word longer than the limit
        ("  alpha   beta  ", 10, "alpha beta"),  # collapsing happens before trimming
    ],
)
def test_trims_at_a_word_boundary(raw, max_chars, expected):
    assert normalize_query(raw, max_chars=max_chars) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "\x00\x01\x02", "\u200b\ufeff"])
def test_nothing_left_is_an_empty_query_error(raw):
    with pytest.raises(ValidationFailedError) as caught:
        normalize_query(raw, max_chars=100)
    assert caught.value.code == "empty_query"
    assert caught.value.status_code == 422
