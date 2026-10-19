"""PDF text per page with pypdf. Bookmarks (the outline) supply section titles: a
section starts at its heading line when that line can be found on the page, otherwise at
the top of the page. Scanned pages have no text layer; OCR is out of scope."""

from __future__ import annotations

import io
import logging
import re
import unicodedata
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import pypdf.filters
from pypdf import PdfReader

from app.core.config import get_settings
from app.ingestion.cleaning import remove_repeated_edge_lines, typical_line_width, unwrap_lines
from app.ingestion.types import Block, BlockKind, IngestionError, ParsedDocument

logger = logging.getLogger(__name__)

_LETTER = re.compile(r"[^\W\d_]")


def _corrupt() -> IngestionError:
    return IngestionError(
        "corrupt_file", "The PDF could not be read. It may be damaged; try exporting it again."
    )


def _encrypted() -> IngestionError:
    return IngestionError(
        "encrypted_pdf",
        "The PDF is password-protected or encrypted. Upload a copy without a password.",
    )


@dataclass
class _DecodeBudget:
    limit_mb: int
    remaining: int  # bytes

    @property
    def exhausted(self) -> bool:
        return self.remaining < 0

    def error(self) -> IngestionError:
        return IngestionError(
            "file_too_large",
            f"The PDF expands to more than {self.limit_mb} MB of content. Split it into "
            "smaller files and upload them again.",
        )


_budget: ContextVar[_DecodeBudget | None] = ContextVar("pdf_decode_budget", default=None)
_decode_stream_data = pypdf.filters.decode_stream_data


def _budgeted_decode(stream: Any) -> bytes:
    """pypdf inflates every stream (page contents, forms, fonts, object streams) through
    `decode_stream_data` and keeps the result for the reader's lifetime. Its limits are
    per stream, so a small file of many highly compressed streams could otherwise fill the
    worker's memory (zlib reaches ~1000:1). Charging each decode to the document's budget
    bounds what one PDF can inflate, as MAX_DOCX_UNCOMPRESSED_MB does for Word files."""
    budget = _budget.get()
    if budget is None:
        return _decode_stream_data(stream)
    if budget.exhausted:
        raise budget.error()
    data = _decode_stream_data(stream)
    budget.remaining -= len(data)
    if budget.exhausted:
        raise budget.error()
    return data


# EncodedStreamObject.get_data imports this name from pypdf.filters on every call.
# tests/test_parsers.py::test_pdf_decompression_bomb_is_rejected fails if that changes.
pypdf.filters.decode_stream_data = _budgeted_decode


def parse_pdf(data: bytes) -> ParsedDocument:
    limit_mb = get_settings().max_pdf_uncompressed_mb
    budget = _DecodeBudget(limit_mb, limit_mb * 1024 * 1024)
    token = _budget.set(budget)
    try:
        parsed = _parse_pdf(data, budget)
    except IngestionError:
        if budget.exhausted:  # an over-budget decode can surface as any other failure
            raise budget.error() from None
        raise
    finally:
        _budget.reset(token)
    if budget.exhausted:  # pypdf skipped the part it could not decode (e.g. a form)
        raise budget.error()
    return parsed


def _parse_pdf(data: bytes, budget: _DecodeBudget) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        encrypted = reader.is_encrypted
    except Exception as exc:  # pypdf raises many exception types on malformed files
        raise _corrupt() from exc
    if encrypted:
        try:
            unlocked = bool(reader.decrypt(""))  # owner-password-only files open this way
        except Exception as exc:  # e.g. AES without the optional cryptography package
            raise _encrypted() from exc
        if not unlocked:
            raise _encrypted()
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        raise _corrupt() from exc
    limit = get_settings().max_pdf_pages
    if page_count > limit:
        raise IngestionError(
            "too_many_pages", f"The PDF has {page_count} pages; at most {limit} are supported."
        )

    pages: list[list[str]] = []
    failed = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # one damaged page should not lose the rest of the document
            if budget.exhausted:
                raise budget.error() from None
            failed += 1
            text = ""
        pages.append(text.splitlines())
    if page_count == 0 or failed == page_count:
        raise _corrupt()
    pages = remove_repeated_edge_lines(pages)
    if not any(_LETTER.search(line) for lines in pages for line in lines):
        raise IngestionError(
            "no_extractable_text",
            "No text could be extracted from this PDF. It is probably scanned images; text "
            "recognition (OCR) is not supported, so upload a PDF with a text layer.",
        )

    width = typical_line_width(pages)
    sections = _outline_by_page(reader)
    blocks: list[Block] = []
    path: tuple[str, ...] = ()
    empty_pages = 0
    for index, lines in enumerate(pages):
        empty_pages += not any(line.strip() for line in lines)
        path = _page_blocks(lines, index + 1, path, sections.get(index, []), width, blocks)
    stats = {"pages_failed": failed, "pages_without_text": empty_pages}
    return ParsedDocument(blocks=blocks, page_count=page_count, stats=stats)


def _page_blocks(
    lines: list[str],
    page_number: int,
    path: tuple[str, ...],
    entries: list[tuple[str, ...]],
    width: int,
    blocks: list[Block],
) -> tuple[str, ...]:
    """Append the page's blocks; returns the heading path in effect at the page end.
    Heading lines are located before unwrapping, so a heading is never joined to the
    paragraph above or below it."""
    starts: dict[int, list[tuple[tuple[str, ...], bool]]] = defaultdict(list)
    cursor = 0
    for entry in entries:  # sections beginning on this page, in reading order
        found = _find_heading(lines, entry[-1], cursor)
        cursor = cursor if found is None else found
        starts[cursor].append((entry, found is not None))

    segment: list[str] = []

    def flush() -> None:
        paragraph: list[str] = []
        for line in [*unwrap_lines(segment, width), ""]:
            if line:
                paragraph.append(line)
            elif paragraph:
                blocks.append(Block(BlockKind.PARAGRAPH, "\n".join(paragraph), path, page_number))
                paragraph = []
        segment.clear()

    for i, line in enumerate([*lines, ""]):  # the sentinel applies an empty page's entries
        is_heading = False
        for entry, found in starts.get(i, ()):
            flush()
            path = entry
            is_heading = is_heading or found
        if is_heading:
            blocks.append(Block(BlockKind.HEADING, " ".join(line.split()), path, page_number))
        else:
            segment.append(line)
    flush()
    return path


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _find_heading(lines: list[str], title: str, start: int) -> int | None:
    """Index of the line showing `title` (allowing a numbering prefix such as "2.1")."""
    target = _normalize(title)
    for i in range(start, len(lines)):
        line = _normalize(lines[i])
        if target and target in line and len(line) <= len(target) + 12:
            return i
    return None


def _outline_by_page(reader: PdfReader) -> dict[int, list[tuple[str, ...]]]:
    """0-based page index -> heading paths of the bookmarks pointing at that page."""
    entries: list[tuple[int, tuple[str, ...]]] = []

    def walk(items: list[Any], parents: tuple[str, ...]) -> None:
        last = parents
        for item in items:
            if isinstance(item, list):  # children of the preceding bookmark
                walk(item, last)
                continue
            title = " ".join(str(getattr(item, "title", "") or "").split())
            last = (*parents, title) if title else parents
            try:
                page = reader.get_destination_page_number(item)
            except Exception:  # dangling destination: keep the title for its children
                page = None
            if title and page is not None and page >= 0:
                entries.append((page, last))

    try:
        walk(reader.outline, ())
    except Exception:  # a broken outline costs section titles, never the text
        logger.warning("ingestion.pdf_outline_unreadable")
        return {}
    by_page: dict[int, list[tuple[str, ...]]] = defaultdict(list)
    for page, path in sorted(entries, key=lambda entry: entry[0]):  # stable sort
        by_page[page].append(path)
    return by_page
