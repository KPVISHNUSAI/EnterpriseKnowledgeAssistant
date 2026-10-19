"""Evaluation runner. Builds an isolated, migrated database, loads the synthetic corpus
through the application's services, ingests it with the configured embedding model, then
scores retrieval (four configurations, no generation) and grounded answering (the
extractive generator and, optionally, an LLM) on the dataset. Every number written comes
from this run.

eval/__main__.py pins the environment before this module is imported (settings are
cached on first use), so the run never depends on the developer's .env.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import BACKEND_DIR, REPO_ROOT, Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import get_sessionmaker, set_tenant
from app.models import DocumentChunk
from app.providers.embeddings.sentence_transformers import resolve_device
from app.providers.llm import LLMMessage, LLMProvider, LLMRequest, LLMResponse, LLMStreamEvent
from app.providers.registry import get_embedder, get_llm, get_reranker
from app.rag.answer import answer_question
from app.rag.context import build_context
from app.rag.query import normalize_query
from app.rag.retrieval import retrieve
from app.rag.rewrite import rewrite_query
from app.rag.types import (
    AnswerResult,
    AnswerStatus,
    ChatTurn,
    RetrievalMode,
    RetrievalOptions,
    RetrievalResult,
    SearchScope,
)
from app.services.access import Principal, readable_document_ids
from app.storage.local import LocalStorage
from eval import corpus, loader, metrics, report
from eval.dataset import Case

CACHE = Path(__file__).parent / ".cache"

RETRIEVAL_CONFIGS: dict[str, tuple[RetrievalMode, bool]] = {
    "vector": ("vector", False),
    "keyword": ("keyword", False),
    "hybrid": ("hybrid", False),
    "hybrid_rerank": ("hybrid", True),
}
EXTRACTIVE_CONFIGS = ("vector", "hybrid", "hybrid_rerank")
TOP_K = (1, 3, 5, 10)
PRIVATE_WINDOW = 50  # characters; prompts are searched for any window of unreadable text
ANSWER_CHARS = 1500  # answer text kept per case in the JSON results
ABSTAINED = (AnswerStatus.INSUFFICIENT_EVIDENCE, AnswerStatus.REFUSED)
_PROTECTED_DATABASES = frozenset({"eka", "postgres", "template0", "template1"})
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
# A crashed PostgreSQL backend makes the server drop every connection and recover for
# about ten seconds; a case that hits that window runs again instead of ending the run.
DB_ATTEMPTS = 8
DB_RETRY_SECONDS = 15.0


# --- Database ---------------------------------------------------------------------------------


def provision_database(*, admin_url: str, owner_url: str, app_url: str) -> None:
    """A fresh, migrated database, set up like tests/conftest.py: the roles when missing
    (the app role without BYPASSRLS, so row-level security applies), the database owned
    by the migration role, an empty public schema with pgvector, `alembic upgrade head`
    as the owner. Drops everything in the database first."""
    app, owner = make_url(app_url), make_url(owner_url)
    name = app.database or ""
    if name in _PROTECTED_DATABASES or not _IDENTIFIER.fullmatch(name) or owner.database != name:
        raise ValueError(f"refusing to reset database {name!r}")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        roles = set(conn.scalars(text("SELECT rolname FROM pg_roles")))
        for url, extra in ((owner, ""), (app, " NOSUPERUSER NOBYPASSRLS")):
            if url.username not in roles:
                password = (url.password or "").replace("'", "''")
                conn.execute(
                    text(f"CREATE ROLE \"{url.username}\" LOGIN PASSWORD '{password}'{extra}")
                )
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{name}" OWNER "{owner.username}"'))
    admin.dispose()
    target = create_engine(
        make_url(admin_url).set(database=name), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    with target.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text(f'CREATE SCHEMA public AUTHORIZATION "{owner.username}"'))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public"))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO "{app.username}"'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{app.username}"'))
    target.dispose()
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["database_url"] = owner_url
    command.upgrade(cfg, "head")


# --- Index ------------------------------------------------------------------------------------


@dataclass
class Index:
    """The loaded corpus as the database holds it, plus the ground truth derived from
    eval/corpus.py (never from the application's own access code)."""

    loaded: loader.Loaded
    principals: dict[str, Principal]
    doc_keys: dict[uuid.UUID, str]
    chunks: dict[str, list[str]]  # document key -> chunk texts in order
    readable: dict[str, frozenset[str]]  # person key -> readable document keys
    private: dict[str, list[str]]  # person key -> text windows found only in unreadable docs
    description: dict[str, Any]

    @contextmanager
    def session(self) -> Iterator[Session]:
        with get_sessionmaker()() as db:
            set_tenant(db, self.loaded.organization_id)
            yield db

    def scope(self, case: Case) -> SearchScope:
        def ids(keys: Sequence[str] | None, by_key: dict[str, uuid.UUID]) -> Any:
            return None if keys is None else tuple(by_key[key] for key in keys)

        return SearchScope(
            principal=self.principals[case.principal],
            workspace_ids=ids(case.scope_workspaces, self.loaded.workspaces),
            document_ids=ids(case.scope_documents, self.loaded.documents),
        )

    def in_scope(self, case: Case, doc_key: str) -> bool:
        doc = corpus.DOCUMENTS_BY_KEY[doc_key]
        return (case.scope_workspaces is None or doc.workspace in case.scope_workspaces) and (
            case.scope_documents is None or doc_key in case.scope_documents
        )


def build_index(settings: Settings, cases: Sequence[Case]) -> Index:
    chunking = (
        settings.chunk_target_tokens,
        settings.chunk_max_tokens,
        settings.chunk_overlap_tokens,
    )
    if chunking != tuple(corpus.CHUNKING.values()):
        raise ValueError("the chunk settings must match eval/corpus.py CHUNKING")
    storage_root = Path(settings.local_storage_path).resolve()
    if not storage_root.is_relative_to(CACHE.resolve()):
        raise ValueError("LOCAL_STORAGE_PATH must be inside eval/.cache for an evaluation run")
    shutil.rmtree(storage_root, ignore_errors=True)
    storage = LocalStorage(storage_root)
    started = time.perf_counter()
    with loader.captured_ingestion_jobs() as jobs:
        loaded = loader.load(storage, lambda _person: None)
    loader.ingest(jobs, loaded.organization_id, embedder=get_embedder(), storage=storage)
    ingestion_seconds = time.perf_counter() - started

    doc_keys = {doc_id: key for key, doc_id in loaded.documents.items()}
    chunks: dict[str, list[str]] = {doc.key: [] for doc in corpus.DOCUMENTS}
    fingerprint = hashlib.sha256()
    tokens = 0
    with get_sessionmaker()() as db:
        set_tenant(db, loaded.organization_id)
        principals = {
            person.key: loader.principal_for(db, loaded.users[person.key], loaded.organization_id)
            for person in corpus.PEOPLE
        }
        rows = db.execute(
            select(
                DocumentChunk.document_id,
                DocumentChunk.chunk_index,
                DocumentChunk.content,
                DocumentChunk.content_hash,
                DocumentChunk.token_count,
            ).order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
        ).all()
        app_readable = {
            key: {doc_keys[i] for i in readable_document_ids(db, principal, doc_keys)}
            for key, principal in principals.items()
        }
    for row in sorted(rows, key=lambda r: (doc_keys[r.document_id], r.chunk_index)):
        key = doc_keys[row.document_id]
        fingerprint.update(f"{key}:{row.chunk_index}:{row.content_hash}\n".encode())
    for row in rows:
        chunks[doc_keys[row.document_id]].append(row.content)
        tokens += row.token_count

    for case in cases:
        for item in case.evidence:
            if not any(metrics.contains(chunk, item.needle) for chunk in chunks[item.doc]):
                raise RuntimeError(f"{case.id}: evidence needle not found in {item.doc} chunks")

    readable = {
        person.key: frozenset(doc.key for doc in corpus.DOCUMENTS if corpus.can_read(person, doc))
        for person in corpus.PEOPLE
    }
    access = {
        key: {
            "expected": len(readable[key]),
            "application": len(app_readable[key]),
            "mismatch": sorted(readable[key] ^ app_readable[key]),
        }
        for key in readable
    }
    description = {
        "documents": len(loaded.documents),
        "formats": dict(Counter(doc.format for doc in corpus.DOCUMENTS)),
        "chunks": len(rows),
        "chunk_tokens": tokens,
        "chunks_per_document": {key: len(texts) for key, texts in chunks.items()},
        "fingerprint": fingerprint.hexdigest()[:16],
        "ingestion_seconds": ingestion_seconds,
        "access_check": access,
    }
    return Index(
        loaded=loaded,
        principals=principals,
        doc_keys=doc_keys,
        chunks=chunks,
        readable=readable,
        private={key: _private_windows(chunks, readable[key]) for key in readable},
        description=description,
    )


def _private_windows(chunks: dict[str, list[str]], readable: frozenset[str]) -> list[str]:
    """Overlapping windows of the unreadable documents' chunks that occur nowhere in the
    readable ones (shared boilerplate such as the synthetic-marker line is not evidence)."""
    seen = "\n".join(metrics.normalize(text) for key in readable for text in chunks[key])
    windows: set[str] = set()
    for key, texts in chunks.items():
        if key in readable:
            continue
        for content in texts:
            norm = metrics.normalize(content)
            for start in range(0, max(len(norm) - PRIVATE_WINDOW, 0) + 1, PRIVATE_WINDOW // 2):
                window = norm[start : start + PRIVATE_WINDOW]
                if window not in seen:
                    windows.add(window)
    return sorted(windows)


# --- Retrieval --------------------------------------------------------------------------------


def evaluate_retrieval(index: Index, cases: Sequence[Case], settings: Settings) -> dict[str, Any]:
    """Each configuration over every case: ranks of the evidence needles in the top 10,
    the context the answer pipeline would build from the top RETRIEVAL_FINAL_TOP_K, and
    a leak check over every candidate returned (RETRIEVAL_FUSED_TOP_K of them)."""
    get_embedder().embed_query("warm up")  # model loading is not query latency
    reranker = get_reranker()
    if reranker is not None:
        reranker.score("warm up", ["warm up"])
    results: dict[str, Any] = {}
    for name, (mode, rerank) in RETRIEVAL_CONFIGS.items():
        options = RetrievalOptions.from_settings(
            settings, mode=mode, rerank=rerank, final_top_k=settings.retrieval_fused_top_k
        )
        rows = []
        for number, case in enumerate(cases, 1):
            row, attempts = _retrying(
                case.id, partial(_retrieval_row, index, case, options, settings)
            )
            rows.append({**row, "attempts": attempts})
            _progress(f"retrieval {name}", number, len(cases), case.id)
        results[name] = {
            "options": _options(options),
            "summary": _retrieval_summary(rows),
            "cases": rows,
        }
    return results


def _retrieval_row(
    index: Index, case: Case, options: RetrievalOptions, settings: Settings
) -> dict[str, Any]:
    query = normalize_query(case.question, max_chars=settings.max_query_chars)
    search = query
    if case.history:
        history = [ChatTurn(turn.role, turn.content) for turn in case.history]
        search = rewrite_query(query, history, llm=None).query  # what the extractive path uses
    with index.session() as db:
        result = retrieve(
            db,
            index.scope(case),
            query,
            options=options,
            embedder=get_embedder(),
            reranker=get_reranker(),
            search_query=search,
        )
    started = time.perf_counter()
    passages = build_context(
        result.candidates[: settings.retrieval_final_top_k], max_tokens=settings.context_max_tokens
    )
    context_ms = (time.perf_counter() - started) * 1000
    top = result.candidates[: max(TOP_K)]
    needles = [item.needle for item in case.evidence]
    ranks = metrics.needle_ranks([c.content for c in top], needles) if needles else []
    passage_docs = [index.doc_keys[p.candidate.document_id] for p in passages]
    return {
        "id": case.id,
        "category": case.category,
        "expected": case.expected,
        "principal": case.principal,
        "search_query": result.search_query if result.search_query != result.query else None,
        "top": [f"{index.doc_keys[c.document_id]}#{c.chunk_index}" for c in top],
        "ranks": ranks,
        **{f"recall@{k}": metrics.recall_at(ranks, k) if needles else None for k in TOP_K},
        "rr": metrics.reciprocal_rank(ranks) if needles else None,
        "context_precision": (
            metrics.context_precision(passage_docs, case.evidence_docs) if needles else None
        ),
        "context_recall": metrics.context_recall([p.text for p in passages], needles),
        "context_relevance": metrics.context_relevance(
            [(p.text, p.token_count) for p in passages], needles
        ),
        "passages": len(passages),
        "context_tokens": sum(p.token_count for p in passages),
        **_access(index, case, result),
        "timings_ms": {**result.timings_ms, "context": context_ms},
    }


def _access(index: Index, case: Case, result: RetrievalResult) -> dict[str, list[str]]:
    docs = [index.doc_keys[c.document_id] for c in result.candidates]
    return {
        "leaked": sorted({d for d in docs if d not in index.readable[case.principal]}),
        "out_of_scope": sorted({d for d in docs if not index.in_scope(case, d)}),
    }


def _retrieval_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if row["ranks"]]
    by_category: dict[str, Any] = {}
    for category in dict.fromkeys(row["category"] for row in scored):
        group = [row for row in scored if row["category"] == category]
        by_category[category] = {
            "cases": len(group),
            **{f"recall@{k}": metrics.mean(row[f"recall@{k}"] for row in group) for k in (1, 5)},
            "mrr": metrics.mean(row["rr"] for row in group),
            "context_recall": metrics.mean(row["context_recall"] for row in group),
        }
    return {
        "cases": len(rows),
        "scored_cases": len(scored),
        **{f"recall@{k}": metrics.mean(row[f"recall@{k}"] for row in scored) for k in TOP_K},
        "mrr": metrics.mean(row["rr"] for row in scored),
        "context_precision": metrics.mean(row["context_precision"] for row in scored),
        "context_recall": metrics.mean(row["context_recall"] for row in scored),
        "context_relevance": metrics.mean(row["context_relevance"] for row in scored),
        "context_tokens_mean": metrics.mean(row["context_tokens"] for row in rows),
        "leak_rate": sum(bool(row["leaked"]) for row in rows) / len(rows),
        "out_of_scope_rate": sum(bool(row["out_of_scope"]) for row in rows) / len(rows),
        "latency_ms": _latency(row["timings_ms"] for row in rows),
        "by_category": by_category,
    }


# --- Generation -------------------------------------------------------------------------------


class RecordingLLM:
    """Keeps every request sent to the wrapped provider, so the run can show what reached
    the model, and that no text the asker may not read ever did."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.requests: list[LLMRequest] = []

    @property
    def provider_name(self) -> str:
        return self.inner.provider_name

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self.inner.generate(request)

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        self.requests.append(request)
        return self.inner.stream(request)


def evaluate_generation(
    index: Index,
    cases: Sequence[Case],
    settings: Settings,
    *,
    configs: Sequence[str],
    llm: RecordingLLM | None,
    label: str,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name in configs:
        mode, rerank = RETRIEVAL_CONFIGS[name]
        options = RetrievalOptions.from_settings(settings, mode=mode, rerank=rerank)
        rows = []
        for number, case in enumerate(cases, 1):
            if llm is not None:
                llm.requests.clear()  # once per case: a retried case's requests are all checked
            (result, seconds), attempts = _retrying(
                case.id, partial(_answer, index, case, options, llm)
            )
            requests = list(llm.requests) if llm is not None else []
            rows.append({**_answer_row(index, case, result, requests), "attempts": attempts})
            _progress(
                f"{label} {name}",
                number,
                len(cases),
                f"{case.id} {result.status.value} {seconds:.1f}s",
            )
        results[name] = {
            "options": _options(options),
            "summary": _generation_summary(rows),
            "cases": rows,
        }
    return results


def _answer(
    index: Index, case: Case, options: RetrievalOptions, llm: RecordingLLM | None
) -> tuple[AnswerResult, float]:
    started = time.perf_counter()
    with index.session() as db:
        result = answer_question(
            db,
            index.scope(case),
            case.question,
            history=[ChatTurn(turn.role, turn.content) for turn in case.history],
            options=options,
            embedder=get_embedder(),
            reranker=get_reranker(),
            llm=llm,
        )
    return result, time.perf_counter() - started


def _answer_row(
    index: Index, case: Case, result: AnswerResult, requests: Sequence[LLMRequest]
) -> dict[str, Any]:
    text = result.answer
    abstained = result.status in ABSTAINED
    judged = not abstained and result.status is not AnswerStatus.ERROR
    passage_text = {p.source_id: p.text for p in result.passages}
    passage_doc = {p.source_id: index.doc_keys[p.candidate.document_id] for p in result.passages}
    cited = [c.source_id for c in result.citations if c.verified]
    needles = [item.needle for item in case.evidence]
    rewrite = result.rewrite_usage
    return {
        "id": case.id,
        "category": case.category,
        "expected": case.expected,
        "principal": case.principal,
        "status": result.status.value,
        "abstained": abstained,
        "answer": text[:ANSWER_CHARS],
        "search_query": (
            result.retrieval.search_query
            if result.retrieval.search_query != result.retrieval.query
            else None
        ),
        "passages": [passage_doc[p.source_id] for p in result.passages],
        "citations": cited,
        "invalid_citations": result.invalid_citations,
        "unverified_quotes": len(result.unverified_quotes),
        "fact_recall": metrics.fact_recall(text, case.key_facts),
        "missing_facts": [fact for fact in case.key_facts if not metrics.matches(text, fact)],
        "forbidden_hits": [fact for fact in case.forbidden_facts if metrics.matches(text, fact)],
        "canary_hits": metrics.canary_hits(text, case.canaries, result.invalid_citations),
        "faithfulness": (
            metrics.faithfulness(text, result.question, passage_text) if judged else None
        ),
        "citation": (
            metrics.citation_scores(
                [(passage_doc[i], passage_text[i]) for i in cited], needles, case.evidence_docs
            )
            if judged and case.answerable
            else None
        ),
        **_access(index, case, result.retrieval),
        "prompt": _prompt_check(index, case, requests) if requests else None,
        "tokens": {
            "input": result.usage.input_tokens,
            "output": result.usage.output_tokens,
            "rewrite_input": rewrite.input_tokens if rewrite else 0,
            "rewrite_output": rewrite.output_tokens if rewrite else 0,
        },
        "truncated": bool(result.metadata.get("truncated")),
        "error": result.metadata.get("error"),
        "timings_ms": result.timings_ms,
    }


def _prompt_check(index: Index, case: Case, requests: Sequence[LLMRequest]) -> dict[str, Any]:
    """Every recorded request (system prompt and all messages) searched for text that
    only unreadable documents contain, and for a permission case's canaries."""
    prompts = [
        metrics.normalize("\n".join([r.system, *(m.content for m in r.messages)])) for r in requests
    ]
    windows = [w for w in index.private[case.principal] if any(w in p for p in prompts)]
    canaries = (
        [c for c in case.canaries if any(metrics.matches(p, c) for p in prompts)]
        if case.expected == "must_not_leak"
        else []
    )
    return {
        "requests": len(requests),
        "purposes": [r.purpose for r in requests],
        "private_windows": len(windows),
        "leak_canaries": canaries,
    }


def _generation_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [row for row in rows if row["status"] != AnswerStatus.ERROR.value]
    with_facts = [row for row in rows if row["fact_recall"] is not None]
    judged = [row for row in rows if row["faithfulness"] and row["faithfulness"]["claims"]]
    cited = [row for row in rows if row["citation"] is not None]
    pairs = [(row["expected"] in ("abstain", "must_not_leak"), row["abstained"]) for row in ok]
    unanswerable = [row for row in ok if row["expected"] == "abstain"]
    injection = [row for row in rows if row["expected"] == "must_not_follow_injection"]
    leaks = [row for row in rows if row["expected"] == "must_not_leak"]
    similar = [row for row in rows if row["category"] == "similar_pair"]
    prompted = [row for row in rows if row["prompt"] is not None]
    answered = [row for row in ok if not row["abstained"]]
    by_category: dict[str, Any] = {}
    for category in dict.fromkeys(row["category"] for row in rows):
        group = [row for row in rows if row["category"] == category]
        by_category[category] = {
            "cases": len(group),
            "fact_recall": metrics.mean(row["fact_recall"] for row in group),
            "abstained": metrics.mean(row["abstained"] for row in group),
            "faithfulness": metrics.mean(
                row["faithfulness"]["score"] for row in group if row["faithfulness"]
            ),
        }
    tokens = [row["tokens"] for row in rows]
    return {
        "cases": len(rows),
        "statuses": dict(Counter(row["status"] for row in rows)),
        "errors": len(rows) - len(ok),
        "correctness": {
            "cases": len(with_facts),
            "fact_recall": metrics.mean(row["fact_recall"] for row in with_facts),
            "all_facts": metrics.mean(row["fact_recall"] == 1 for row in with_facts),
        },
        "similar_pair_confusion": metrics.mean(bool(row["forbidden_hits"]) for row in similar),
        "faithfulness": {
            "answers": len(judged),
            "score": metrics.mean(row["faithfulness"]["score"] for row in judged),
            "fully_supported": metrics.mean(row["faithfulness"]["score"] == 1 for row in judged),
            "claims": sum(row["faithfulness"]["claims"] for row in judged),
            "supported": sum(row["faithfulness"]["supported"] for row in judged),
            "uncited": sum(row["faithfulness"]["uncited"] for row in judged),
        },
        "citations": {
            "answers": len(cited),
            "precision": metrics.mean(row["citation"]["precision"] for row in cited),
            "doc_precision": metrics.mean(row["citation"]["doc_precision"] for row in cited),
            "recall": metrics.mean(row["citation"]["recall"] for row in cited),
            "answers_with_invalid": metrics.mean(
                bool(row["invalid_citations"]) for row in answered
            ),
            "unverified_quotes": sum(row["unverified_quotes"] for row in rows),
        },
        "abstention": {
            **metrics.abstention(pairs),
            "unanswerable_recall": metrics.mean(row["abstained"] for row in unanswerable),
        },
        "injection": {
            "cases": len(injection),
            "resisted": metrics.mean(not row["canary_hits"] for row in injection),
            "hits": dict(Counter(hit for row in injection for hit in row["canary_hits"])),
        },
        "permission": {
            "cases": len(leaks),
            "answer_leaks": sum(bool(row["canary_hits"]) for row in leaks),
            "retrieval_leaks": sum(bool(row["leaked"]) for row in rows),
            "out_of_scope": sum(bool(row["out_of_scope"]) for row in rows),
            "prompts_checked": sum(row["prompt"]["requests"] for row in prompted),
            "prompt_leaks": (
                sum(
                    bool(row["prompt"]["private_windows"] or row["prompt"]["leak_canaries"])
                    for row in prompted
                )
                if prompted
                else None
            ),
        },
        "latency_ms": _latency(row["timings_ms"] for row in rows),
        "tokens": {
            "input_mean": metrics.mean(t["input"] for t in tokens),
            "input_max": max(t["input"] for t in tokens),
            "output_mean": metrics.mean(t["output"] for t in tokens),
            "output_max": max(t["output"] for t in tokens),
            "max_total": max(t["input"] + t["output"] for t in tokens),
            "input_total": sum(t["input"] for t in tokens),
            "output_total": sum(t["output"] for t in tokens),
            "rewrite_input_total": sum(t["rewrite_input"] for t in tokens),
            "rewrite_output_total": sum(t["rewrite_output"] for t in tokens),
        },
        "truncated": sum(row["truncated"] for row in rows),
        "by_category": by_category,
    }


# --- Run --------------------------------------------------------------------------------------


def run(
    *,
    name: str,
    dataset_version: str,
    cases: Sequence[Case],
    admin_url: str,
    retrieval: bool,
    extractive: bool,
    llm_configs: Sequence[str],
    command_line: str,
) -> Path:
    """Run what was asked, write one JSON file per part into reports/<name>/ and
    regenerate report.md from every JSON file there."""
    settings = get_settings()
    out_dir = report.REPORTS / name
    provision_database(
        admin_url=admin_url,
        owner_url=settings.migration_database_url or "",
        app_url=settings.database_url,
    )
    configure_logging(settings.log_level, settings.log_format)  # after alembic's fileConfig
    index = build_index(settings, cases)
    header = {
        "command": command_line,
        "dataset": dataset_version,
        "case_ids": [case.id for case in cases],
        "environment": environment(settings),
        "settings": pinned_settings(settings),
        "index": index.description,
    }
    if retrieval:
        _write(
            out_dir / "retrieval.json", header, lambda: evaluate_retrieval(index, cases, settings)
        )
    if extractive:
        _write(
            out_dir / "generation-extractive.json",
            header,
            lambda: evaluate_generation(
                index, cases, settings, configs=EXTRACTIVE_CONFIGS, llm=None, label="extractive"
            ),
        )
    if llm_configs:
        inner = get_llm()
        if inner is None:
            raise RuntimeError("LLM_PROVIDER is extractive; no LLM to evaluate")
        llm = RecordingLLM(inner)
        warm = time.perf_counter()
        inner.generate(
            LLMRequest(
                system="Reply with one word.",
                messages=[LLMMessage(role="user", content="Say OK.")],
                max_output_tokens=4,
                purpose="warmup",
            )
        )
        llm_header = {
            **header,
            "llm": {
                "provider": inner.provider_name,
                "model": inner.model_name,
                "warmup_seconds": time.perf_counter() - warm,
                "server": ollama_info(settings.llm_base_url or "", inner.model_name),
            },
        }
        slug = re.sub(r"[^a-z0-9.]+", "-", inner.model_name.lower()).strip("-")
        _write(
            out_dir / f"generation-{slug}.json",
            llm_header,
            lambda: evaluate_generation(
                index, cases, settings, configs=llm_configs, llm=llm, label=inner.model_name
            ),
        )
    return report.write_report(out_dir)


def _write(path: Path, header: dict[str, Any], evaluate: Callable[[], dict[str, Any]]) -> None:
    started_at, load_before = datetime.now(UTC), list(os.getloadavg())
    started = time.perf_counter()
    configs = evaluate()
    data = {
        **header,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "load_average_before": load_before,
        "duration_seconds": time.perf_counter() - started,
        "load_average_after": list(os.getloadavg()),
        "configs": configs,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.dump(data), encoding="utf-8")


def _retrying[T](case_id: str, work: Callable[[], T]) -> tuple[T, int]:
    """`work()` and the attempts it took, retried while the database is unreachable."""
    for attempt in range(1, DB_ATTEMPTS):
        try:
            return work(), attempt
        except OperationalError:
            print(
                f"{case_id}: database unavailable, retrying in {DB_RETRY_SECONDS:.0f} s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(DB_RETRY_SECONDS)
    return work(), DB_ATTEMPTS


def _latency(timings: Iterable[dict[str, float]]) -> dict[str, dict[str, float | None]]:
    values: dict[str, list[float]] = {}
    for timing in timings:
        for stage, ms in timing.items():
            values.setdefault(stage, []).append(ms)
    return {
        stage: {"p50": metrics.percentile(ms, 50), "p95": metrics.percentile(ms, 95), "n": len(ms)}
        for stage, ms in values.items()
    }


def _options(options: RetrievalOptions) -> dict[str, Any]:
    return {
        "mode": options.mode,
        "rerank": options.rerank,
        "vector_top_k": options.vector_top_k,
        "keyword_top_k": options.keyword_top_k,
        "rrf_k": options.rrf_k,
        "fused_top_k": options.fused_top_k,
        "final_top_k": options.final_top_k,
        "min_rerank_score": options.min_rerank_score,
    }


def _progress(stage: str, number: int, total: int, detail: str) -> None:
    """Case ids, statuses and timings only: never question or answer text."""
    print(f"[{stage}] {number}/{total} {detail}", file=sys.stderr, flush=True)


# --- Environment ------------------------------------------------------------------------------

_PACKAGES = (
    "fastapi",
    "sqlalchemy",
    "psycopg",
    "pgvector",
    "alembic",
    "pypdf",
    "python-docx",
    "reportlab",
    "httpx",
    "sentence-transformers",
    "transformers",
    "torch",
)

# Settings that shape the results (never secrets or URLs with credentials).
_REPORTED_SETTINGS = (
    "embedding_provider",
    "embedding_model",
    "embedding_dimensions",
    "embedding_device",
    "embedding_query_prefix",
    "embedding_cache_ttl_seconds",
    "reranker_provider",
    "reranker_model",
    "reranker_device",
    "reranker_max_length",
    "retrieval_vector_top_k",
    "retrieval_keyword_top_k",
    "retrieval_rrf_k",
    "retrieval_fused_top_k",
    "retrieval_final_top_k",
    "retrieval_min_rerank_score",
    "context_max_tokens",
    "hnsw_ef_search",
    "fts_config",
    "query_rewrite_enabled",
    "chat_history_messages",
    "chunk_target_tokens",
    "chunk_max_tokens",
    "chunk_overlap_tokens",
    "llm_provider",
    "llm_model",
    "llm_base_url",
    "llm_max_output_tokens",
    "llm_temperature",
    "llm_timeout_seconds",
    "llm_max_retries",
)


def pinned_settings(settings: Settings) -> dict[str, Any]:
    values = {key: getattr(settings, key) for key in _REPORTED_SETTINGS}
    values["database"] = make_url(settings.database_url).database
    values["database_role"] = make_url(settings.database_url).username
    values["redis_db"] = settings.redis_url.rsplit("/", 1)[-1]
    return values


def environment(settings: Settings) -> dict[str, Any]:
    with get_sessionmaker()() as db:
        postgres = db.scalar(text("SHOW server_version"))
        pgvector = db.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": _command(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor(),
        "cpu_count": os.cpu_count(),
        "memory_gb": _memory_gb(),
        "packages": {name: _version(name) for name in _PACKAGES},
        "postgres": postgres,
        "pgvector": pgvector,
        "device": resolve_device(settings.embedding_device),
        "model_revisions": {
            model: _hf_revision(model)
            for model in (settings.embedding_model, settings.reranker_model)
        },
        "git_commit": _command(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"]),
        # The run's own result files do not make the code it measured dirty.
        "git_dirty": bool(
            _command(
                [
                    "git",
                    "-C",
                    str(REPO_ROOT),
                    "status",
                    "--porcelain",
                    "--",
                    ".",
                    ":(exclude)backend/eval/reports",
                ]
            )
        ),
    }


def ollama_info(base_url: str, model: str) -> dict[str, Any] | None:
    """Version, digest and context window of an Ollama server behind an OpenAI-compatible
    base URL ending in /v1; None for other servers."""
    root = base_url.rstrip("/")
    if not root.endswith("/v1"):
        return None
    root = root[: -len("/v1")]
    try:
        with httpx.Client(timeout=10.0) as client:
            version = client.get(f"{root}/api/version").json().get("version")
            tags = client.get(f"{root}/api/tags").json().get("models", [])
            show = client.post(f"{root}/api/show", json={"model": model}).json()
            running = client.get(f"{root}/api/ps").json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return {"error": "the Ollama API did not answer"}
    loaded: dict[str, Any] = next((m for m in running if m.get("name") == model), {})
    info = show.get("model_info") or {}
    return {
        "version": version,
        "digest": next((m.get("digest") for m in tags if m.get("name") == model), None),
        "details": show.get("details"),
        "trained_context_length": next(
            (value for key, value in info.items() if key.endswith(".context_length")), None
        ),
        "loaded_context_length": loaded.get("context_length"),
        "num_ctx_parameter": _num_ctx(show.get("parameters") or ""),
    }


def _num_ctx(parameters: str) -> int | None:
    match = re.search(r"^num_ctx\s+(\d+)", parameters, re.MULTILINE)
    return int(match.group(1)) if match else None


def _hf_revision(model: str) -> str | None:
    """The cached snapshot the model loads from (HF_HUB_OFFLINE=1: nothing is downloaded)."""
    from huggingface_hub.constants import HF_HUB_CACHE

    ref = Path(HF_HUB_CACHE) / f"models--{model.replace('/', '--')}" / "refs" / "main"
    return ref.read_text().strip() if ref.is_file() else None


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _memory_gb() -> float | None:
    raw = _command(["sysctl", "-n", "hw.memsize"])
    return int(raw) / 2**30 if raw and raw.isdigit() else None


def _command(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None
