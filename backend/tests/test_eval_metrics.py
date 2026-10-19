"""Evaluation metrics (eval/metrics.py) against hand-computed examples, and the result
file format (eval/report.py)."""

from __future__ import annotations

import json

import pytest

from eval import metrics, report


def test_normalize_folds_case_quotes_separators_and_space() -> None:
    assert metrics.normalize(
        "Affected  1,240\u00a0drones \u201cfast\u201d \u2013 USD 2,000,000"
    ) == ('affected 1240 drones "fast" - usd 2000000')
    assert metrics.normalize("[S1, S2] 4,5 1,2,3") == "[s1, s2] 4,5 1,2,3"


@pytest.mark.parametrize(
    ("text", "fact", "expected"),
    [
        ("The per diem is USD 65 per day.", "65", True),
        ("The per diem is USD 1.65 per day.", "65", False),
        ("It costs 650 dollars.", "65", False),
        ("Version 65.5 shipped", "65", False),
        ("Lag above 50000 messages", "50,000", True),
        ("Ask the VP Finance.", "vp finance", True),
        ("Three days a week", r"re:\b(3|three) days", True),
        ("Range is 30 metres", r"re:\b30 ?(m|meters|metres)\b", True),
        ("Range is 300 m", r"re:\b30 ?(m|meters|metres)\b", False),
    ],
)
def test_matches(text: str, fact: str, expected: bool) -> None:
    assert metrics.matches(text, fact) is expected


def test_fact_recall() -> None:
    assert metrics.fact_recall("UK 4 weeks, US six weeks", ["4 weeks", "6 weeks"]) == 0.5
    assert metrics.fact_recall("anything", []) is None


def test_canary_hits_include_cited_invalid_source_ids() -> None:
    text = "Replace them every 200 hours. Source: Board Minutes 2026."
    canaries = ["NW-CANARY-5521", "Board Minutes", "S9", "S99"]
    assert metrics.canary_hits(text, canaries, ["S9"]) == ["Board Minutes", "S9"]
    assert metrics.canary_hits("See [S9].", ["S9"]) == ["S9"]
    assert metrics.canary_hits("Every 200 flight hours.", canaries) == []


def test_retrieval_ranks_recall_and_reciprocal_rank() -> None:
    candidates = ["nothing here", "The   NEEDLE one is here.", "the second needle"]
    ranks = metrics.needle_ranks(candidates, ["needle one", "absent", "second needle"])
    assert ranks == [2, None, 3]
    assert metrics.recall_at(ranks, 1) == 0
    assert metrics.recall_at(ranks, 2) == pytest.approx(1 / 3)
    assert metrics.recall_at(ranks, 10) == pytest.approx(2 / 3)
    assert metrics.reciprocal_rank(ranks) == 0.5
    assert metrics.reciprocal_rank([None]) == 0.0


def test_context_precision_and_recall() -> None:
    assert metrics.context_precision(["a", "b", "a", "c"], {"a"}) == 0.5
    assert metrics.context_precision([], {"a"}) is None
    assert metrics.context_recall(["x needle y", "z"], ["needle", "missing"]) == 0.5
    assert metrics.context_recall([], ["needle"]) == 0.0
    assert metrics.context_recall(["x"], []) is None


def test_claims_split_sentences_and_attach_trailing_markers() -> None:
    answer = (
        "The per diem is USD 65 per travel day [S1]. Hotels are capped at USD 210. [S2]\n"
        "The limits are:\n"
        "- 600 requests per minute [S1, S3]\n"
        "Yes."
    )
    assert metrics.claims(answer) == [
        ("The per diem is USD 65 per travel day.", ("S1",)),
        ("Hotels are capped at USD 210.", ("S2",)),
        ("600 requests per minute", ("S1", "S3")),
    ]


def test_supported_needs_citation_numbers_and_terms() -> None:
    passage = "The daily meal per diem is USD 65 per travel day."
    question = "What is the per diem?"
    assert metrics.supported("The per diem is USD 65 per day", [passage], question)
    assert not metrics.supported("The per diem is USD 75 per day", [passage], question)
    assert not metrics.supported("The per diem is USD 65 per day", [], question)
    # New terms: hotel, capped, night, usd, 65 -> only usd and 65 are in the passage (2/5).
    assert not metrics.supported("Hotels are capped at USD 65 per night", [passage], question)
    # "4.2" is found in "4.2.0"; a number taken from the question needs no support.
    assert metrics.supported("Firmware 4.2 raised it", ["Firmware 4.2.0 raised it"], "Why?")
    assert metrics.supported(
        "Reports of USD 7,000 go to the VP Finance",
        ["the VP Finance approves"],
        "Who approves USD 7,000?",
    )


def test_faithfulness_counts_supported_and_uncited_claims() -> None:
    passages = {"S1": "The daily meal per diem is USD 65 per travel day."}
    answer = "The per diem is USD 65 per day [S1]. Lunch is free on Fridays."
    assert metrics.faithfulness(answer, "What is the per diem?", passages) == {
        "claims": 2,
        "supported": 1,
        "uncited": 1,
        "score": 0.5,
    }
    assert metrics.faithfulness("", "q", passages)["score"] is None


def test_citation_scores() -> None:
    cited = [("travel", "per diem is USD 65"), ("expenses", "receipts over USD 25")]
    scores = metrics.citation_scores(cited, ["USD 65", "USD 290"], {"travel"})
    assert scores == {"precision": 0.5, "doc_precision": 0.5, "recall": 0.5}
    assert metrics.citation_scores([], ["USD 65"], {"travel"})["recall"] == 0.0


def test_abstention_confusion_matrix() -> None:
    pairs = [(True, True), (True, False), (False, True), (False, False), (False, False)]
    result = metrics.abstention(pairs)
    assert (result["tp"], result["fn"], result["fp"], result["tn"]) == (1, 1, 1, 2)
    assert result["precision"] == result["recall"] == result["f1"] == 0.5
    assert result["false_abstention_rate"] == pytest.approx(1 / 3)
    assert metrics.abstention([(False, False)])["precision"] is None


def test_percentile_and_mean() -> None:
    assert metrics.percentile([4, 1, 3, 2], 50) == 2.5
    assert metrics.percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    assert metrics.percentile([5], 95) == 5
    assert metrics.percentile([], 50) is None
    assert metrics.mean([1, None, True, 0]) == pytest.approx(2 / 3)
    assert metrics.mean([None]) is None


def test_context_relevance_weights_passages_by_tokens() -> None:
    passages = [("Alpha needle one.", 10), ("Beta.", 30), ("gamma NEEDLE two", 60)]
    assert metrics.context_relevance(passages, ["needle one", "needle two"]) == 0.7
    assert metrics.context_relevance(passages, ["needle one"]) == 0.1
    assert metrics.context_relevance(passages, []) is None
    assert metrics.context_relevance([], ["needle one"]) is None


def test_dump_keeps_each_case_on_one_line_and_round_trips() -> None:
    cases = [{"id": "a", "answer": 'two\nlines, "quoted", a literal \\u0000 1'}, {"id": "b"}]
    data = {"command": "run", "configs": {"hybrid": {"summary": {"mrr": 0.123456}, "cases": cases}}}
    text = report.dump(data)
    assert json.loads(text) == {
        "command": "run",
        "configs": {"hybrid": {"summary": {"mrr": 0.1235}, "cases": cases}},
    }
    rows = [line.strip().rstrip(",") for line in text.splitlines() if '"id": ' in line]
    assert [json.loads(row) for row in rows] == cases


def test_report_header_names_the_files_behind_values_that_differ() -> None:
    assert report._across({"a.json": "x", "b.json": "x"}) == "x"
    env = {
        "cpu": "Apple M4",
        "cpu_count": 10,
        "memory_gb": 16.0,
        "platform": "macOS",
        "python": "3.14.6",
        "postgres": "17.11",
        "pgvector": "0.8.7",
        "device": "mps",
        "model_revisions": {},
        "packages": {},
        "git_commit": "abf0980",
        "git_dirty": False,
    }
    runs = {
        "retrieval.json": {"environment": env},
        "generation-extractive.json": {"environment": env},
        "generation-llm.json": {"environment": {**env, "git_commit": "9a008ba"}},
    }
    lines = report._environment(runs)
    assert "| Python | 3.14.6 |" in lines
    assert (
        "| Git | abf0980 (`retrieval.json`, `generation-extractive.json`); "
        "9a008ba (`generation-llm.json`) |"
    ) in lines
