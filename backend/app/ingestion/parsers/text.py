"""Plain text: paragraphs separated by blank lines, no headings."""

from __future__ import annotations

import codecs
import re

from app.ingestion.cleaning import normalize_newlines
from app.ingestion.types import Block, BlockKind, IngestionError, ParsedDocument

_PARAGRAPH_BREAK = re.compile(r"\n(?:[ \t]*\n)+")
_INDENTED = re.compile(r"^(?: {4}|\t)")


def decode_text(data: bytes) -> str:
    """UTF-8 (with or without BOM), UTF-16 with BOM, else Windows-1252."""
    encodings: tuple[str, ...]
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "cp1252")
    for encoding in encodings:
        try:
            return normalize_newlines(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise IngestionError(
        "unreadable_text", "The file is not readable text. Save it as UTF-8 and upload it again."
    )


def parse_text(text: str) -> ParsedDocument:
    blocks = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        lines = [line for line in paragraph.split("\n") if line.strip()]
        if not lines:
            continue
        # A paragraph indented throughout is preformatted (code, ASCII tables): keep it.
        kind = (
            BlockKind.CODE if all(_INDENTED.match(line) for line in lines) else BlockKind.PARAGRAPH
        )
        blocks.append(Block(kind, paragraph))
    return ParsedDocument(blocks=blocks)
