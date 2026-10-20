"""The evaluation dataset: versioned JSONL files in eval/datasets/ (v1.jsonl, ...).

One JSON object per line. A case names who asks (`principal`, a person key from
eval/corpus.py), what they ask (plus an optional conversation `history` and `scope`), the
behaviour expected, a reference answer, the key facts a correct answer states, and the
evidence: document keys with a `needle`, a verbatim span of the document that supports
the answer. Retrieval is scored by whether retrieved chunks contain the needles.

Expected behaviours:
  answer                     the answer is in documents the principal may read
  abstain                    nothing in the corpus answers the question
  must_not_leak              the answer exists only in documents the principal may not
                             read; `canaries` are strings from those documents
  must_not_follow_injection  retrieved text tries to instruct the model; `canaries` are
                             strings that appear only if the model obeys or repeats it

Key facts, forbidden facts and canaries are plain strings matched on word boundaries, or
regular expressions prefixed with "re:" (eval/metrics.py). Edit a released version only
to fix a labelling error, and record the fix in eval/README.md; otherwise add a version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

DATASETS = Path(__file__).parent / "datasets"
DEFAULT_VERSION = "v2"

Expected = Literal["answer", "abstain", "must_not_leak", "must_not_follow_injection"]
EXPECTED: tuple[Expected, ...] = ("answer", "abstain", "must_not_leak", "must_not_follow_injection")

# Category -> minimum number of cases a dataset version must have.
MIN_CASES = {
    "factual": 10,
    "multi_passage": 6,
    "table": 6,
    "technical": 6,
    "similar_pair": 8,
    "unanswerable": 6,
    "permission": 6,
    "permission_control": 2,
    "injection": 5,
    "injection_control": 2,
    "follow_up": 5,
}

_REQUIRED = {"id", "category", "principal", "question", "expected", "reference", "key_facts"}
_OPTIONAL = {"history", "scope", "forbidden_facts", "evidence", "canaries"}


@dataclass(frozen=True)
class Evidence:
    doc: str  # document key (eval/corpus.py)
    needle: str  # verbatim span of that document


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    principal: str
    question: str
    expected: Expected
    reference: str
    key_facts: tuple[str, ...]
    evidence: tuple[Evidence, ...] = ()
    history: tuple[Turn, ...] = ()
    scope_workspaces: tuple[str, ...] | None = None
    scope_documents: tuple[str, ...] | None = None
    forbidden_facts: tuple[str, ...] = ()
    canaries: tuple[str, ...] = ()

    @property
    def answerable(self) -> bool:
        """The principal can read an answer: the system should answer, not abstain."""
        return self.expected in ("answer", "must_not_follow_injection")

    @property
    def evidence_docs(self) -> frozenset[str]:
        return frozenset(item.doc for item in self.evidence)


def path_for(version: str) -> Path:
    return DATASETS / f"{version}.jsonl"


def load(version: str = DEFAULT_VERSION) -> list[Case]:
    """Parse and structurally validate a dataset version (references to the corpus are
    checked by tests/test_eval_dataset.py and again by the runner against the index)."""
    cases: list[Case] = []
    lines = path_for(version).read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            cases.append(_case(json.loads(line)))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{version}.jsonl line {number}: {exc}") from exc
    ids = [case.id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise ValueError(f"{version}.jsonl: duplicate case ids {duplicates}")
    return cases


def _case(row: dict[str, Any]) -> Case:
    unknown = set(row) - _REQUIRED - _OPTIONAL
    missing = _REQUIRED - set(row)
    if unknown or missing:
        raise ValueError(f"unknown fields {sorted(unknown)}, missing fields {sorted(missing)}")
    if row["category"] not in MIN_CASES:
        raise ValueError(f"unknown category {row['category']!r}")
    if row["expected"] not in EXPECTED:
        raise ValueError(f"unknown expected behaviour {row['expected']!r}")
    expected = cast(Expected, row["expected"])
    evidence = tuple(Evidence(item["doc"], item["needle"]) for item in row.get("evidence", []))
    canaries = tuple(row.get("canaries", []))
    if expected in ("answer", "must_not_follow_injection") and not (evidence and row["key_facts"]):
        raise ValueError(f"{row['id']}: an answerable case needs evidence and key facts")
    if expected in ("abstain", "must_not_leak") and evidence:
        raise ValueError(f"{row['id']}: a case without a readable answer has no evidence")
    if expected in ("must_not_leak", "must_not_follow_injection") and not canaries:
        raise ValueError(f"{row['id']}: {expected} needs canaries")
    history = tuple(Turn(turn["role"], turn["content"]) for turn in row.get("history", []))
    if any(turn.role not in ("user", "assistant") for turn in history):
        raise ValueError(f"{row['id']}: history roles are user or assistant")
    scope = row.get("scope") or {}
    if set(scope) - {"workspaces", "documents"}:
        raise ValueError(f"{row['id']}: scope takes workspaces and/or documents")
    return Case(
        id=row["id"],
        category=row["category"],
        principal=row["principal"],
        question=row["question"],
        expected=expected,
        reference=row["reference"],
        key_facts=tuple(row["key_facts"]),
        evidence=evidence,
        history=history,
        scope_workspaces=tuple(scope["workspaces"]) if "workspaces" in scope else None,
        scope_documents=tuple(scope["documents"]) if "documents" in scope else None,
        forbidden_facts=tuple(row.get("forbidden_facts", [])),
        canaries=canaries,
    )
