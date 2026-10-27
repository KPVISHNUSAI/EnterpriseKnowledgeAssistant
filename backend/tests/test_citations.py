"""Citation parsing and validation, quote verification, excerpt selection, status table."""

from __future__ import annotations

import pytest
from test_answer_fakes import passage

from app.rag.citations import (
    best_excerpt,
    final_status,
    split_sentences,
    stem,
    terms,
    unverified_quotes,
    validate,
)
from app.rag.types import AnswerStatus, Citation

LEAVE = (
    "Employees receive 25 days of paid vacation per year. Vacation requests need approval "
    "from a manager. Unused days expire in March."
)
CAFETERIA = "The cafeteria opens at 8am and closes at 3pm."
PASSAGES = [passage("S1", LEAVE, page=3, section="Leave"), passage("S2", CAFETERIA, page=7)]


@pytest.mark.parametrize(
    ("text", "expected_text", "cited"),
    [
        ("Staff get 25 days [S1].", "Staff get 25 days [S1].", ["S1"]),
        ("Staff get 25 days [s1].", "Staff get 25 days [S1].", ["S1"]),
        ("Days [S1, S2].", "Days [S1, S2].", ["S1", "S2"]),
        ("Days [S1; S2].", "Days [S1, S2].", ["S1", "S2"]),
        ("Days [ S2 ,S1 ].", "Days [S2, S1].", ["S2", "S1"]),
        ("Days [S1][S2].", "Days [S1][S2].", ["S1", "S2"]),
        ("Days [S1, S1].", "Days [S1].", ["S1"]),
        # gpt-oss writes full-width brackets.
        ("Days \u3010S1\u3011. Hours \u3010S2\u3011.", "Days [S1]. Hours [S2].", ["S1", "S2"]),
        ("Days \uff3bS1, S2\uff3d.", "Days [S1, S2].", ["S1", "S2"]),
        ("Days (S1). Hours *(S1, S2)*.", "Days [S1]. Hours *[S1, S2]*.", ["S1", "S2"]),
        (
            "Days [S1]. Hours [S2]. More days [S1].",
            "Days [S1]. Hours [S2]. More days [S1].",
            ["S1", "S2"],
        ),
    ],
)
def test_valid_markers_are_canonicalized_and_cited_once(text, expected_text, cited):
    result = validate(text, PASSAGES)
    assert result.text == expected_text
    assert [c.source_id for c in result.citations] == cited
    assert all(c.verified for c in result.citations)
    assert result.invalid_citations == []


@pytest.mark.parametrize(
    ("text", "expected_text", "invalid", "cited"),
    [
        ("Claim [S9].", "Claim.", ["S9"], []),
        ("Claim [S0] here.", "Claim here.", ["S0"], []),
        ("Claim [S3, S9].", "Claim.", ["S3", "S9"], []),
        ("Claim [S1, S9].", "Claim [S1].", ["S9"], ["S1"]),
        ("Claim [S9][S1].", "Claim [S1].", ["S9"], ["S1"]),
        ("Claim [S9]", "Claim", ["S9"], []),
        ("Claim [S9]\nNext line [S1].", "Claim\nNext line [S1].", ["S9"], ["S1"]),
        ("Two bad [S9] and [S9] again.", "Two bad and again.", ["S9"], []),
    ],
)
def test_invalid_and_out_of_range_markers_are_stripped_and_recorded(
    text, expected_text, invalid, cited
):
    result = validate(text, PASSAGES)
    assert result.text == expected_text
    assert result.invalid_citations == invalid
    assert [c.source_id for c in result.citations] == cited


def test_no_passages_means_every_marker_is_invalid():
    result = validate("Answer [S1].", [])
    assert result.text == "Answer."
    assert result.invalid_citations == ["S1"]
    assert result.citations == []


def test_citation_carries_source_metadata_and_best_supporting_excerpt():
    result = validate("Requests must be approved by a manager [S1].", PASSAGES)
    (citation,) = result.citations
    assert (citation.document_title, citation.page_number, citation.section_title) == (
        "Employee Handbook",
        3,
        "Leave",
    )
    assert citation.document_id == PASSAGES[0].candidate.document_id
    assert citation.excerpt == "Vacation requests need approval from a manager."


def test_excerpt_follows_each_citing_sentence_and_markers_before_the_period():
    result = validate("Unused days expire [S1]. The cafeteria closes at 3pm. [S2]", PASSAGES)
    excerpts = {c.source_id: c.excerpt for c in result.citations}
    assert excerpts["S1"] == "Unused days expire in March."
    assert excerpts["S2"] == CAFETERIA


def test_excerpt_falls_back_to_the_first_sentence_without_overlap():
    assert best_excerpt(LEAVE, "zzz qqq") == "Employees receive 25 days of paid vacation per year."
    assert best_excerpt("", "anything") is None


def test_excerpt_adds_an_overlapping_neighbour_and_is_capped():
    excerpt = best_excerpt(LEAVE, "vacation days need manager approval")
    assert excerpt == (
        "Employees receive 25 days of paid vacation per year. "
        "Vacation requests need approval from a manager."
    )
    long_sentence = "vacation " * 120
    capped = best_excerpt(long_sentence, "vacation")
    assert capped is not None and len(capped) <= 505 and capped.endswith(" ...")


@pytest.mark.parametrize(
    "quote",
    [
        '"Vacation requests need approval from a manager"',
        "“Vacation requests need approval from a manager.”",  # curly, trailing period
        '"vacation   requests need\tapproval from a manager"',  # whitespace and case
        '"The cafeteria opens at 8am and closes at 3pm"',
    ],
)
def test_verbatim_quotes_are_accepted(quote):
    assert unverified_quotes(f"It says {quote} in the handbook.", PASSAGES) == []


def test_fabricated_quotes_are_reported_and_short_quotes_ignored():
    text = (
        'The handbook says "employees may carry over ten days" and also '
        '"Vacation requests need approval from a manager". It calls it "paid leave".'
    )
    assert unverified_quotes(text, PASSAGES) == ["employees may carry over ten days"]
    assert validate(text, PASSAGES).unverified_quotes == ["employees may carry over ten days"]


def _cited() -> list[Citation]:
    return [Citation(source_id="S1", verified=True)]


@pytest.mark.parametrize(
    ("declared", "citations", "expected"),
    [
        ("ANSWERED", _cited(), AnswerStatus.ANSWERED),
        ("ANSWERED", [], AnswerStatus.UNVERIFIED),
        ("PARTIAL", _cited(), AnswerStatus.PARTIAL),
        ("PARTIAL", [], AnswerStatus.UNVERIFIED),
        ("INSUFFICIENT", [], AnswerStatus.INSUFFICIENT_EVIDENCE),
        ("INSUFFICIENT", _cited(), AnswerStatus.INSUFFICIENT_EVIDENCE),
        (None, _cited(), AnswerStatus.ANSWERED),
        (None, [], AnswerStatus.UNVERIFIED),
    ],
)
def test_status_decision_table(declared, citations, expected):
    assert final_status(declared, citations) == expected


def test_lexical_helpers():
    assert split_sentences("One. Two!\nThree? Four") == ["One.", "Two!", "Three?", "Four"]
    assert terms("The policies of the Vacation requests") == ["policy", "vacation", "request"]
    assert stem("policies") == stem("policy") == "policy"
    assert stem("closes") == stem("close") == stem("closing") == "clos"
    assert stem("address") == "address"
    assert "the" not in terms("the and or")
