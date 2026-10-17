"""Context construction: ranked candidates -> the labelled passages (S1..Sn) that are
given to the language model and that citations must refer to."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Sequence
from itertools import pairwise

from app.rag.types import Candidate, ContextPassage

# A passage whose word trigrams already appear in a kept passage at this rate adds
# nothing: exact copies, near copies (a word changed) and text inside a merged passage.
_NEAR_DUPLICATE = 0.9
_WORD = re.compile(r"\w+")


def build_context(candidates: Sequence[Candidate], *, max_tokens: int) -> list[ContextPassage]:
    """Merge each document's consecutive overlapping chunks into one passage, drop
    (near) duplicates, then fill the token budget in rank order, skipping passages that
    do not fit. Text is exact document text; the prompt layer is responsible for fencing."""
    passages: list[ContextPassage] = []
    kept: list[frozenset[tuple[str, ...]]] = []
    used = 0
    for primary, run in _runs(candidates):
        text, tokens = _merged(run)
        shingles = _shingles(text)
        if any(len(shingles & other) >= _NEAR_DUPLICATE * len(shingles) for other in kept):
            continue
        if used + tokens > max_tokens:
            continue  # a smaller, lower-ranked passage may still fit
        used += tokens
        kept.append(shingles)
        passages.append(
            ContextPassage(
                source_id=f"S{len(passages) + 1}",
                candidate=primary,
                text=text,
                token_count=tokens,
                chunk_ids=[c.chunk_id for c in run],
            )
        )
    return passages


def _runs(candidates: Sequence[Candidate]) -> list[tuple[Candidate, list[Candidate]]]:
    """Runs of a document's consecutive chunks that share overlap text (in document
    order), each with its best-ranked member, ordered by that member's rank."""
    rank: dict[uuid.UUID, int] = {}
    by_document: dict[uuid.UUID, list[Candidate]] = {}
    for position, candidate in enumerate(candidates):
        rank.setdefault(candidate.chunk_id, position)
        by_document.setdefault(candidate.document_id, []).append(candidate)
    runs: list[list[Candidate]] = []
    for chunks in by_document.values():
        chunks.sort(key=lambda c: c.chunk_index)
        runs.append([chunks[0]])
        for chunk in chunks[1:]:
            last = runs[-1][-1]
            if chunk.chunk_index == last.chunk_index + 1 and _overlap(last, chunk):
                runs[-1].append(chunk)
            else:
                runs.append([chunk])
    with_primary = [(min(run, key=lambda c: rank[c.chunk_id]), run) for run in runs]
    return sorted(with_primary, key=lambda item: rank[item[0].chunk_id])


def _merged(run: list[Candidate]) -> tuple[str, int]:
    """The run's text with each overlap included once, and its estimated token count."""
    text, tokens = run[0].content, run[0].token_count
    for previous, chunk in pairwise(run):
        overlap = _overlap(previous, chunk)
        text += chunk.content[overlap:]
        # Chunk token counts include the overlap; count only the chunk's new share.
        new_share = (len(chunk.content) - overlap) / len(chunk.content)
        tokens += math.ceil(chunk.token_count * new_share)
    return text, tokens


def _overlap(previous: Candidate, chunk: Candidate) -> int:
    """How many leading characters of `chunk` repeat the end of `previous`: the chunker's
    metadata.overlap_chars (app/ingestion/chunking.py), used only when the text confirms
    it. Guessing from the text instead would splice repetitive or coincidental text."""
    declared: object = chunk.metadata.get("overlap_chars")
    if not isinstance(declared, int) or not 0 < declared <= len(chunk.content):
        return 0
    return declared if previous.content.endswith(chunk.content[:declared]) else 0


def _shingles(text: str) -> frozenset[tuple[str, ...]]:
    words = _WORD.findall(text.casefold())
    return frozenset(zip(words, words[1:], words[2:], strict=False)) or frozenset({tuple(words)})
