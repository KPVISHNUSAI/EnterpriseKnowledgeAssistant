"""Permission Proof: the leak test.

For every access group in an organization, ask the assistant about every document the
group may not read, and check what would reach the model. A group is the members who
share an organization role, workspace roles and readable documents; one of them is asked
for all. Probes are
the documents' own titles and excerpts, the queries most likely to pull a document into
retrieval. Each probe runs the chat's retrieval and context building and assembles the
exact model request, with no model call. Two checks, against app/proof/oracle.py rather
than the access code under test:

* every retrieved passage comes from a document the group may read (this covers titles
  and section names, which the request carries too);
* the request, minus the probe's own question and the fixed system prompt, holds no run
  of WINDOW characters that is found only in documents the group may not read (titles and
  section names count as a document's text).

Before probing, the application's readable set of every member must equal the oracle's.
Nothing here writes to the database, and reports hold ids and counts, never text.
"""

from __future__ import annotations

import html
import logging
import time
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import DocumentChunk, ProofVerdict, WorkspaceRole
from app.proof import oracle
from app.providers.embeddings import EmbeddingProvider
from app.rag.context import build_context
from app.rag.prompts import build_messages
from app.rag.query import normalize_query
from app.rag.retrieval import retrieve
from app.rag.types import RetrievalOptions, SearchScope
from app.services.access import Principal, readable_document_ids

logger = logging.getLogger(__name__)

WINDOW = 50  # characters: the shortest copied run the text check is sure to find...
STRIDE = 25  # ...is WINDOW + STRIDE - 1, since documents are indexed every STRIDE characters
PROBE_CHARS = 300
_KINDS = ("title", "first", "middle")  # probe order in a capped run


@dataclass(frozen=True)
class Simulation:
    """Test one person as if the application had wrongly let them read a workspace."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID


@dataclass
class Report:
    verdict: ProofVerdict
    summary: dict[str, Any]
    groups: list[dict[str, Any]]
    findings: list[dict[str, Any]]


@dataclass
class _Group:
    members: list[oracle.Member]
    readable: frozenset[uuid.UUID]  # by the oracle, from the real records
    principal: Principal  # what the application is asked as (simulation adds a workspace)
    simulated_workspace_id: uuid.UUID | None = None
    stats: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def run(
    db: Session,
    organization_id: uuid.UUID,
    *,
    embedder: EmbeddingProvider,
    max_probes: int,
    simulation: Simulation | None = None,
) -> Report:
    """Run the leak test over the organization's current documents and permissions. Pass a
    session whose transaction sees one snapshot (REPEATABLE READ), so a change made while
    the test runs cannot raise a false alarm."""
    settings = get_settings()
    started = time.perf_counter()
    snapshot = oracle.load_snapshot(db, organization_id)
    groups = _groups(snapshot, simulation)
    texts = _chunk_texts(db, organization_id)
    probes = _probes(snapshot, texts)
    labels = _labels(db, snapshot)
    index = TextIndex({doc: [*labels[doc], *chunks] for doc, chunks in texts.items()})

    plans = [[p for doc in _unreadable(g, probes) for p in probes[doc]] for g in groups]
    plans = [_round_robin(plan) for plan in plans]
    quotas = allocate([len(plan) for plan in plans], max_probes)
    options = RetrievalOptions.from_settings(settings)
    # Reranking only reorders and filters the fused candidates, so all of them are checked.
    probe_options = replace(options, rerank=False, final_top_k=options.fused_top_k)
    memo = _MemoEmbedder(embedder)
    findings: dict[tuple[int, str, uuid.UUID], int] = defaultdict(int)

    for number, group in enumerate(groups):
        # Every member, not only the one asked: the rule also reads per-person records
        # (uploads, personal grants) that a group can share without sharing their effect.
        # ponytail: one query per member; batch it if organizations reach many thousands.
        for principal in _principals(snapshot, group):
            allowed = readable_document_ids(db, principal, snapshot.documents)
            for doc in sorted(allowed - group.readable):
                findings[(number, "access_extra", doc)] += 1
            for doc in sorted(group.readable - allowed):
                findings[(number, "access_missing", doc)] += 1
        stats = group.stats
        stats["probes_planned"] = len(plans[number])
        stats["documents_unreadable"] = len(set(_unreadable(group, probes)))
        stats["documents_probed"] = len({p.document_id for p in plans[number][: quotas[number]]})
        scope = SearchScope(principal=group.principal)
        cache: dict[str, bool] = {}
        for probe in plans[number][: quotas[number]]:
            query = normalize_query(probe.text, max_chars=settings.max_query_chars)
            result = retrieve(db, scope, query, options=probe_options, embedder=memo, reranker=None)
            stats["probes_run"] += 1
            stats["passages_checked"] += len(result.candidates)
            for candidate in result.candidates:
                if candidate.document_id not in group.readable:
                    findings[(number, "retrieved", candidate.document_id)] += 1
            passages = build_context(
                result.candidates[: options.final_top_k], max_tokens=settings.context_max_tokens
            )
            if not passages:
                continue  # the pipeline answers with a fixed message and calls no model
            # Built without the question: the probe is copied from a document under test,
            # and cutting it out of the request afterwards would cut it out of a leaked
            # passage too. The system prompt is fixed text and is left out as well.
            request = "\n".join(m.content for m in build_messages("", passages, []))
            stats["prompts_checked"] += 1
            for doc in index.private(request, group.readable, cache):
                findings[(number, "prompt_text", doc)] += 1

    rows = [
        {"group": number, "kind": kind, "document_id": str(doc), "count": count}
        for (number, kind, doc), count in sorted(findings.items(), key=lambda item: item[0])
    ]
    for number, _, _ in findings:
        groups[number].stats["findings"] += 1
    probes_run = sum(g.stats["probes_run"] for g in groups)
    verdict = (
        ProofVerdict.FAILED
        if rows
        else ProofVerdict.PASSED
        if probes_run
        else ProofVerdict.NOTHING_TO_TEST
    )
    summary = {
        "groups": len(groups),
        "members": sum(len(g.members) for g in groups),
        "documents": len(snapshot.documents),
        "indexed_documents": len(probes),
        "probes_planned": sum(len(plan) for plan in plans),
        "probes_run": probes_run,
        "prompts_checked": sum(g.stats["prompts_checked"] for g in groups),
        "passages_checked": sum(g.stats["passages_checked"] for g in groups),
        "findings": len(rows),
        "max_probes": max_probes,
        "retrieval_mode": options.mode,
        "candidates_per_probe": options.fused_top_k,
        "window_characters": WINDOW,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }
    logger.info("proof.completed", extra={"verdict": verdict.value, **summary})
    return Report(verdict, summary, [_group_out(g) for g in groups], rows)


# --- Groups ----------------------------------------------------------------------------------


def _groups(snapshot: oracle.Snapshot, simulation: Simulation | None) -> list[_Group]:
    if simulation is not None:
        member = next((m for m in snapshot.members if m.user_id == simulation.user_id), None)
        if member is None:
            raise ValueError("the person to simulate is no longer an active member")
        roles = {**member.workspace_roles, simulation.workspace_id: WorkspaceRole.VIEWER}
        return [
            _Group(
                [member],
                oracle.readable(snapshot, member.user_id, member.workspace_roles),
                _principal(snapshot, member, roles),
                simulated_workspace_id=simulation.workspace_id,
            )
        ]
    groups: dict[tuple[Any, ...], _Group] = {}
    for member in snapshot.members:
        readable = oracle.readable(snapshot, member.user_id, member.workspace_roles)
        key = (member.org_role, frozenset(member.workspace_roles.items()), readable)
        if key in groups:
            groups[key].members.append(member)
        else:
            principal = _principal(snapshot, member, member.workspace_roles)
            groups[key] = _Group([member], readable, principal)
    return list(groups.values())


def _principals(snapshot: oracle.Snapshot, group: _Group) -> list[Principal]:
    if group.simulated_workspace_id is not None:
        return [group.principal]
    return [_principal(snapshot, m, m.workspace_roles) for m in group.members]


def _principal(
    snapshot: oracle.Snapshot, member: oracle.Member, roles: Mapping[uuid.UUID, WorkspaceRole]
) -> Principal:
    return Principal(
        user_id=member.user_id,
        email=member.email,
        full_name=member.full_name,
        organization_id=snapshot.organization_id,
        org_role=member.org_role,
        workspace_roles=dict(roles),
    )


def _group_out(group: _Group) -> dict[str, Any]:
    return {
        "members": [str(m.user_id) for m in group.members],
        "representative": str(group.principal.user_id),
        "workspaces": [
            {"workspace_id": str(ws), "role": role.value}
            for ws, role in sorted(group.members[0].workspace_roles.items())
        ],
        "simulated_workspace_id": (
            str(group.simulated_workspace_id) if group.simulated_workspace_id else None
        ),
        "readable_documents": len(group.readable),
        **{
            key: group.stats[key]
            for key in (
                "documents_unreadable",
                "documents_probed",
                "probes_planned",
                "probes_run",
                "prompts_checked",
                "passages_checked",
                "findings",
            )
        },
    }


# --- Probes ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    document_id: uuid.UUID
    kind: str  # title, first, middle
    text: str


def _chunk_texts(db: Session, organization_id: uuid.UUID) -> dict[uuid.UUID, list[str]]:
    texts: dict[uuid.UUID, list[str]] = defaultdict(list)
    for document_id, content in db.execute(
        select(DocumentChunk.document_id, DocumentChunk.content)
        .where(DocumentChunk.organization_id == organization_id)
        .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
    ):
        texts[document_id].append(content)
    return texts


def _labels(db: Session, snapshot: oracle.Snapshot) -> dict[uuid.UUID, list[str]]:
    """What the model request carries of a document besides its passages: its title and
    section names."""
    labels: dict[uuid.UUID, list[str]] = defaultdict(list)
    for doc in snapshot.documents.values():
        labels[doc.id].append(doc.title)
    for document_id, section in db.execute(
        select(DocumentChunk.document_id, DocumentChunk.section_title)
        .where(DocumentChunk.organization_id == snapshot.organization_id)
        .distinct()
    ):
        if section:
            labels[document_id].append(section)
    return labels


def _probes(
    snapshot: oracle.Snapshot, texts: Mapping[uuid.UUID, Sequence[str]]
) -> dict[uuid.UUID, list[Probe]]:
    """Up to three probes per indexed document: its title, and an excerpt of its first and
    of its middle chunk."""
    probes: dict[uuid.UUID, list[Probe]] = {}
    for doc_id in sorted(texts):
        doc = snapshot.documents.get(doc_id)
        if doc is None or not doc.indexed:
            continue
        chunks = texts[doc_id]
        candidates = [
            ("title", doc.title),
            ("first", _excerpt(chunks[0])),
            ("middle", _excerpt(chunks[len(chunks) // 2])),
        ]
        seen: set[str] = set()
        probes[doc_id] = []
        for kind, text in candidates:
            if text.strip() and text not in seen:
                seen.add(text)
                probes[doc_id].append(Probe(doc_id, kind, text))
    return probes


def _excerpt(content: str) -> str:
    words = content.split()
    text = ""
    for word in words:
        if len(text) + len(word) + 1 > PROBE_CHARS:
            break
        text = f"{text} {word}" if text else word
    return text or content[:PROBE_CHARS]


def _unreadable(group: _Group, probes: Mapping[uuid.UUID, list[Probe]]) -> list[uuid.UUID]:
    return [doc for doc in probes if doc not in group.readable]


def _round_robin(plan: list[Probe]) -> list[Probe]:
    """Every document's title first, then the first excerpts, then the middle ones, so a
    capped run still touches as many documents as it can."""
    return sorted(plan, key=lambda p: (_KINDS.index(p.kind), p.document_id))


def allocate(sizes: Sequence[int], budget: int) -> list[int]:
    """Split `budget` probe runs over groups as evenly as their sizes allow."""
    quotas = [0] * len(sizes)
    remaining = max(budget, 0)
    open_groups = [i for i, size in enumerate(sizes) if size]
    while remaining and open_groups:
        share = max(remaining // len(open_groups), 1)
        for i in list(open_groups):
            take = min(share, sizes[i] - quotas[i], remaining)
            quotas[i] += take
            remaining -= take
            if quotas[i] == sizes[i]:
                open_groups.remove(i)
            if not remaining:
                break
    return quotas


class _MemoEmbedder:
    """Each probe is embedded once, however many groups ask it."""

    def __init__(self, inner: EmbeddingProvider) -> None:
        self.inner = inner
        self._vectors: dict[str, list[float]] = {}

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    @property
    def dimensions(self) -> int:
        return self.inner.dimensions

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self.inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        if text not in self._vectors:
            self._vectors[text] = self.inner.embed_query(text)
        return self._vectors[text]


# --- Text check -------------------------------------------------------------------------------


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", html.unescape(text)).casefold().split())


class TextIndex:
    """Which documents hold each WINDOW-character run of their text, sampled every STRIDE
    characters. A prompt is searched at every position, so any copied run of
    WINDOW + STRIDE - 1 characters contains a sampled one.

    ponytail: one dict entry per STRIDE characters of the organization's text, built per
    run; a persistent index if tenants grow to gigabytes of text.
    """

    def __init__(self, texts: Mapping[uuid.UUID, Sequence[str]]) -> None:
        self._text = {doc: " ".join(normalize(t) for t in chunks) for doc, chunks in texts.items()}
        self._windows: dict[int, set[uuid.UUID]] = defaultdict(set)
        for doc, chunks in texts.items():
            for content in chunks:
                norm = normalize(content)
                for start in range(0, len(norm) - WINDOW + 1, STRIDE):
                    self._windows[hash(norm[start : start + WINDOW])].add(doc)

    def private(
        self, text: str, readable: AbstractSet[uuid.UUID], cache: dict[str, bool]
    ) -> set[uuid.UUID]:
        """Documents outside `readable` whose text `text` holds a run of, where that run
        is found in no readable document. `cache` (one per reader) remembers the runs
        already looked up in the readable documents."""
        norm = normalize(text)
        found: set[uuid.UUID] = set()
        for start in range(len(norm) - WINDOW + 1):
            run = norm[start : start + WINDOW]
            docs = self._windows.get(hash(run))
            if not docs or docs & readable:
                continue
            holders = {doc for doc in docs if run in self._text[doc]}  # not a hash collision
            # A run's edge spaces may come from the prompt's markup, not from the text.
            if holders and not self._readable_has(run.strip(), readable, cache):
                found |= holders
        return found

    def _readable_has(
        self, run: str, readable: AbstractSet[uuid.UUID], cache: dict[str, bool]
    ) -> bool:
        if run not in cache:
            cache[run] = any(run in self._text.get(doc, "") for doc in readable)
        return cache[run]
