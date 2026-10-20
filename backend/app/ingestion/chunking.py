"""Structure-aware chunking.

A section is a run of blocks with the same heading path; a chunk never spans two
sections, so it never spans two top-level sections either. Within a section, blocks are
packed in order up to CHUNK_TARGET_TOKENS. Prose is cut only at sentence or line ends
(words, then characters, only for a "sentence" that alone exceeds the target), and a
block that fits in a fresh chunk is moved there whole rather than split. Code blocks and
tables are atomic up to CHUNK_MAX_TOKENS - so one may extend a chunk past the target to
stay with the text that introduces it - and larger ones are split on line boundaries,
re-fenced (code) or repeating the header row (tables). Consecutive chunks of a section
share about CHUNK_OVERLAP_TOKENS of whole trailing sentences; `metadata.overlap_chars`
says how many leading characters repeat the previous chunk. Text addressed to AI
assistants (app/ingestion/instructions.py) is found per block and kept as character
ranges in `metadata.flags`. Token counts come from app/ingestion/tokens.py.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import groupby
from typing import Any

from app.ingestion.instructions import Flag, find_instructions
from app.ingestion.tokens import count_tokens
from app.ingestion.types import HEADING_SEPARATOR, Block, BlockKind, Chunk

_VERBATIM = (BlockKind.CODE, BlockKind.TABLE)
# Line breaks; ., ! or ? (optionally closed by a quote or bracket) followed by whitespace
# and something other than a lowercase letter ("e.g. this" is not a sentence end); CJK
# sentence ends.
_SEGMENT_END = re.compile(
    r"\n+|(?:(?<=[.!?])|(?<=[.!?][\"')\]]))[ \t]+(?=[^\sa-z])|(?<=[\u3002\uff01\uff1f])"
)
_WORD_END = re.compile(r"\s+")
_FENCE_LINE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_TABLE_DELIMITER = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
HEADER_MAX_CHARS = 300


@dataclass(frozen=True)
class _Unit:
    text: str  # includes the whitespace that followed it inside its block
    tokens: int
    block: int  # index of the source block; a block's units are contiguous
    kind: BlockKind
    page: int | None
    flags: tuple[Flag, ...] = ()  # ranges of `text`

    @property
    def prose(self) -> bool:
        return self.kind not in _VERBATIM


def chunk_blocks(
    blocks: Sequence[Block], *, target_tokens: int, max_tokens: int, overlap_tokens: int
) -> list[Chunk]:
    chunks: list[Chunk] = []
    body = [block for block in blocks if block.kind is not BlockKind.HEADING]
    for path, section in groupby(enumerate(body), key=lambda item: item[1].heading_path):
        packer = _Packer(target_tokens, max_tokens, overlap_tokens)
        for index, block in section:
            units = _units(block, index, target_tokens, max_tokens)
            if units:
                packer.add_block(units)
        chunks.extend(_make_chunk(units, carried, path) for units, carried in packer.finish())
    return chunks


def flagged_pages(chunks: Iterable[tuple[dict[str, Any], int | None]]) -> list[int]:
    """Pages holding text or a heading flagged as addressed to AI assistants, from each
    chunk's (metadata, page_number)."""
    pages: set[object] = set()
    for metadata, page in chunks:
        flags = metadata.get("flags")
        for flag in flags if isinstance(flags, list) else []:
            pages.add(flag.get("page") if isinstance(flag, dict) else None)
        if metadata.get("section_flag"):
            pages.add(page)
    return sorted(page for page in pages if isinstance(page, int))


def embedding_text(document_title: str, chunk: Chunk) -> str:
    """What the embedding model sees: a contextual header (document title + heading path)
    and the passage, so a short passage keeps its context. Only the passage is stored."""
    path = list(chunk.heading_path)
    if path and path[0].casefold() == document_title.strip().casefold():
        path = path[1:]  # a document's first heading often repeats its title
    header = HEADING_SEPARATOR.join(part for part in (document_title.strip(), *path) if part)
    return f"{header[:HEADER_MAX_CHARS]}\n\n{chunk.content}" if header else chunk.content


# --- Units --------------------------------------------------------------------------------


def _units(block: Block, index: int, target: int, max_tokens: int) -> list[_Unit]:
    # PDF extraction has already rejoined wrapped lines, so its line breaks end paragraphs.
    by_line = block.kind is not BlockKind.PARAGRAPH
    wrapped = not by_line and block.page_number is not None
    if block.kind in _VERBATIM and count_tokens(block.text) > max_tokens:
        # The pieces repeat header lines, so they are no longer slices of the block.
        pieces = _split_verbatim(block, max_tokens)
        flags = [tuple(find_instructions(piece, by_line=True)) for piece in pieces]
    else:
        if block.kind in _VERBATIM:
            pieces = [block.text]
        else:
            pieces = [
                piece
                for segment in _split_after(block.text, _SEGMENT_END)
                for piece in _fit(segment, target)
            ]
        flags = _slice_flags(
            find_instructions(block.text, by_line=by_line, wrapped=wrapped), pieces
        )
    return [
        _Unit(p, count_tokens(p), index, block.kind, block.page_number, f)
        for p, f in zip(pieces, flags, strict=True)
        if p.strip()
    ]


def _slice_flags(flags: Sequence[Flag], pieces: Sequence[str]) -> list[tuple[Flag, ...]]:
    """`flags` (ranges of the pieces joined) as ranges of each piece."""
    sliced = []
    start = 0
    for piece in pieces:
        end = start + len(piece)
        sliced.append(
            tuple(
                Flag(max(f.start, start) - start, min(f.end, end) - start, f.rule)
                for f in flags
                if f.start < end and f.end > start
            )
        )
        start = end
    return sliced


def _split_after(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Split after each separator match, keeping the separator on the left piece."""
    pieces, start = [], 0
    for match in pattern.finditer(text):
        if match.start() > start:
            pieces.append(text[start : match.end()])
            start = match.end()
    if start < len(text):
        pieces.append(text[start:])
    return pieces


def _fit(text: str, limit: int) -> list[str]:
    """`text` as pieces of at most `limit` tokens: whole, else words, else characters
    (no character counts as more than one token)."""
    if count_tokens(text) <= limit:
        return [text]
    pieces = []
    for word in _split_after(text, _WORD_END):
        if count_tokens(word) <= limit:
            pieces.append(word)
        else:
            pieces.extend(word[i : i + limit] for i in range(0, len(word), limit))
    return pieces


def _split_verbatim(block: Block, limit: int) -> list[str]:
    """Split an oversized code block or table on line boundaries."""
    lines = block.text.split("\n")
    head: list[str] = []
    tail: list[str] = []
    if block.kind is BlockKind.CODE and len(lines) > 2 and _FENCE_LINE.match(lines[0]):
        if _FENCE_LINE.match(lines[-1]):
            head, tail, lines = lines[:1], lines[-1:], lines[1:-1]
    elif block.kind is BlockKind.TABLE and len(lines) > 2 and _TABLE_DELIMITER.match(lines[1]):
        head, lines = lines[:2], lines[2:]  # every piece repeats the header row
    budget = limit - count_tokens("\n".join(head + tail))
    if budget < limit // 2:  # not worth repeating an enormous header
        lines, head, tail, budget = head + lines + tail, [], [], limit
    pieces: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        # A single over-long line is broken at words; within a piece its words stay on one
        # line, so the piece's lines can still be checked one by one for instructions.
        continued = False
        for part in _fit(line, budget):
            tokens = count_tokens(part)
            if current and size + tokens > budget:
                pieces.append(current)
                current, size, continued = [], 0, False
            if continued:
                current[-1] += part
            else:
                current.append(part)
            continued = True
            size += tokens
    if current:
        pieces.append(current)
    return [
        "\n".join([*head, *(line.rstrip() if line.strip() else line for line in piece), *tail])
        + "\n"
        for piece in pieces
    ]


# --- Packing ------------------------------------------------------------------------------


class _Packer:
    """Greedy packing of one section's units into chunks with sentence-aligned overlap."""

    def __init__(self, target: int, max_tokens: int, overlap: int) -> None:
        self.target, self.max_tokens, self.overlap = target, max_tokens, overlap
        self.done: list[tuple[list[_Unit], int]] = []  # (units, leading overlap units)
        self.units: list[_Unit] = []
        self.carried = 0
        self.size = 0

    def _limit(self, unit: _Unit) -> int:
        return self.target if unit.prose else self.max_tokens

    def _has_new(self) -> bool:
        return len(self.units) > self.carried

    def _close(self, next_tokens: int, next_limit: int) -> None:
        self.done.append((self.units, self.carried))
        tail: list[_Unit] = []
        size = 0
        for unit in reversed(self.units[1:]):  # never repeat a whole chunk
            if not unit.prose or size + unit.tokens > self.overlap:
                break
            tail.insert(0, unit)
            size += unit.tokens
        if size + next_tokens > next_limit:
            tail, size = [], 0
        self.units, self.carried, self.size = tail, len(tail), size

    def add_block(self, units: list[_Unit]) -> None:
        limit = self._limit(units[0])
        total = sum(unit.tokens for unit in units)
        if self._has_new() and self.size + total > limit and total <= limit:
            self._close(total, limit)  # the block fits a fresh chunk: keep it whole
        for unit in units:
            limit = self._limit(unit)
            if self.size + unit.tokens > limit:
                if self._has_new():
                    self._close(unit.tokens, limit)
                else:  # only carried overlap is in the way
                    self.units, self.carried, self.size = [], 0, 0
            self.units.append(unit)
            self.size += unit.tokens

    def finish(self) -> list[tuple[list[_Unit], int]]:
        if self._has_new():
            self.done.append((self.units, self.carried))
        return self.done


def _join(units: Sequence[_Unit]) -> tuple[str, list[Flag]]:
    """The units' text, and their flags as ranges of it (touching ranges merged)."""
    out = ""
    flags: list[Flag] = []
    previous: _Unit | None = None
    for unit in units:
        if previous is not None and unit.block != previous.block:
            lists = unit.kind is BlockKind.LIST and previous.kind is BlockKind.LIST
            out = out.rstrip()
            flags = [f._replace(end=min(f.end, len(out))) for f in flags]
            out += "\n" if lists else "\n\n"
        for flag in unit.flags:
            start, end = len(out) + flag.start, len(out) + flag.end
            if flags and flags[-1].end >= start:
                flags[-1] = flags[-1]._replace(end=max(flags[-1].end, end))
            else:
                flags.append(Flag(start, end, flag.rule, unit.page))
        out += unit.text
        previous = unit
    text = out.strip()
    shift = len(out) - len(out.lstrip())
    flags = [
        f._replace(start=max(f.start - shift, 0), end=min(f.end - shift, len(text))) for f in flags
    ]
    return text, [f for f in flags if f.start < f.end]


def _make_chunk(units: list[_Unit], carried: int, path: tuple[str, ...]) -> Chunk:
    content, flags = _join(units)
    pages = [unit.page for unit in units if unit.page is not None]
    metadata: dict[str, Any] = {"kinds": sorted({unit.kind.value for unit in units})}
    if carried:
        metadata["overlap_chars"] = len(_join(units[:carried])[0])
    if flags:
        metadata["flags"] = [
            {key: value for key, value in flag._asdict().items() if value is not None}
            for flag in flags
        ]
    # The heading is not part of the text but names the passage to the model
    # (app/rag/prompts.py checks it again), so readers are told about it too.
    if path and (heading := find_instructions(path[-1])):
        metadata["section_flag"] = heading[0].rule.value
    return Chunk(
        content=content,
        token_count=count_tokens(content),
        heading_path=path,
        page_number=min(pages, default=None),
        page_end=max(pages, default=None),
        metadata=metadata,
    )
