"""The offline extractive generator: cites real sources, abstains, streams."""

from __future__ import annotations

from test_answer_fakes import passage

from app.rag.citations import final_status, validate
from app.rag.extractive import (
    INSUFFICIENT_MESSAGE,
    MAX_SENTENCES,
    extractive_answer,
    select_sentences,
    stream_extractive,
)
from app.rag.types import AnswerStatus

LEAVE = (
    "Leave\n"
    "Employees receive 25 days of paid vacation per year. Vacation requests need approval "
    "from a manager. Unused days expire in March."
)
CAFETERIA = "The cafeteria opens at 8am and closes at 3pm. Lunch is served from noon."
PASSAGES = [passage("S1", LEAVE, page=3), passage("S2", CAFETERIA, page=7)]


def _status(text: str) -> str:
    return text.split("\n", 1)[0]


def test_answer_cites_the_sentences_it_uses():
    text = extractive_answer("How many vacation days do employees receive?", PASSAGES)
    assert _status(text) == "STATUS: ANSWERED"
    assert "- Employees receive 25 days of paid vacation per year. [S1]" in text
    assert "[S2]" not in text
    validated = validate(text.split("\n\n", 1)[1], PASSAGES)
    assert validated.invalid_citations == []
    assert [c.source_id for c in validated.citations] == ["S1"]
    assert final_status("ANSWERED", validated.citations) == AnswerStatus.ANSWERED


def test_sentences_from_several_sources_each_cite_their_own():
    text = extractive_answer(
        "When does the cafeteria close and when do vacation days expire?", PASSAGES
    )
    assert "closes at 3pm. [S2]" in text
    assert "expire in March. [S1]" in text


def test_unrelated_question_abstains_without_guessing():
    text = extractive_answer("What is the capital of France?", PASSAGES)
    assert text == f"STATUS: INSUFFICIENT\n\n{INSUFFICIENT_MESSAGE}"
    assert select_sentences("", PASSAGES) == []
    assert extractive_answer("vacation", []).startswith("STATUS: INSUFFICIENT")


def test_partial_coverage_is_declared_partial():
    text = extractive_answer(
        "How many vacation days do employees receive and what is the bonus scheme?", PASSAGES
    )
    assert _status(text) == "STATUS: PARTIAL"
    assert "[S1]" in text


def test_near_duplicate_sentences_are_not_repeated_and_count_is_capped():
    repeated = passage("S3", "Employees receive 25 days of paid vacation per year, as stated.")
    # Distinct sentences (two unique words each) that all answer the question.
    many = [
        passage(f"S{i}", f"Employees receive 25 days of paid vacation at the {place}.")
        for i, place in enumerate(
            ("Berlin office", "Paris branch", "Tokyo headquarters", "Oslo lab"), 4
        )
    ]
    picks = select_sentences("How many vacation days do employees receive?", [*PASSAGES, repeated])
    assert [p.source_id for p in picks] == ["S1"]
    picks = select_sentences("How many vacation days do employees receive?", many)
    assert [p.source_id for p in picks] == ["S4", "S5", "S6"]
    assert len(picks) == MAX_SENTENCES


def test_headings_and_fragments_are_never_answers():
    picks = select_sentences("leave", PASSAGES)
    assert all(pick.sentence != "Leave" for pick in picks)
    # A title shares the question's words best of all, but it answers nothing.
    desk = passage(
        "S1",
        "Clear Desk and Clear Screen Policy\n"
        "Leave Policy..........................12\n"
        "• To lock their desk and filing cabinets at the end of the day\n"
        "Clear desk audits | 4 per year",
    )
    picked = [pick.sentence for pick in select_sentences("What is the clear desk policy?", [desk])]
    assert picked == [
        "Clear desk audits | 4 per year",
        "• To lock their desk and filing cabinets at the end of the day",
    ]


def test_streaming_reassembles_to_the_full_answer():
    question = "How many vacation days do employees receive?"
    chunks = list(stream_extractive(question, PASSAGES))
    assert len(chunks) > 1
    assert "".join(chunks) == extractive_answer(question, PASSAGES)
