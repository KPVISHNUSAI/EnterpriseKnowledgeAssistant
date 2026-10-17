"""Data model shared by the parsers, the cleaner and the chunker."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

HEADING_SEPARATOR = " > "


class BlockKind(StrEnum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"  # structure only: its title is carried by the heading_path that follows
    CODE = "code"  # verbatim, never reflowed
    TABLE = "table"  # Markdown pipe table, never reflowed
    LIST = "list"  # one or more items, one per line


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    text: str
    heading_path: tuple[str, ...] = ()  # enclosing headings, outermost first
    page_number: int | None = None  # 1-based; None for formats without pages


@dataclass
class ParsedDocument:
    blocks: list[Block]
    page_count: int | None = None
    # Small format-specific counters for the job metrics (never document text).
    stats: dict[str, int] = field(default_factory=dict)


@dataclass
class Chunk:
    content: str  # exact passage text shown to users and the LLM
    token_count: int
    heading_path: tuple[str, ...]
    page_number: int | None
    page_end: int | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def section_title(self) -> str | None:
        return self.heading_path[-1] if self.heading_path else None

    @property
    def heading_path_text(self) -> str | None:
        return HEADING_SEPARATOR.join(self.heading_path) or None


class IngestionError(Exception):
    """Permanent failure caused by the document itself. `message` is shown to users, so
    it must be safe and actionable; retrying cannot help."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
