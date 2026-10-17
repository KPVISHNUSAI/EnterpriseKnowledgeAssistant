"""Word documents with python-docx, in body order. Title/Heading N styles build the
heading path, tables become Markdown pipe tables, list paragraphs become "- " items and
code-styled or monospace paragraphs become fenced code blocks. DOCX has no page numbers."""

from __future__ import annotations

import io
import re
import zipfile

import docx
from docx.document import Document
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from app.core.config import get_settings
from app.ingestion.types import Block, BlockKind, IngestionError, ParsedDocument

_HEADING_STYLE = re.compile(r"^heading ([1-9])$")
_MONOSPACE = re.compile(r"mono|courier|consolas|menlo|monaco|console|inconsolata|code")
_CODE_STYLES = ("html preformatted", "macro text", "plain text")
_TRAILING_LEVEL = re.compile(r"(\d)$")


def _corrupt() -> IngestionError:
    return IngestionError(
        "corrupt_file", "The Word document could not be read. It may be damaged; save it again."
    )


def parse_docx(data: bytes) -> ParsedDocument:
    _check_unpacked_size(data)
    try:
        return _parse(docx.Document(io.BytesIO(data)))
    except IngestionError:
        raise
    except Exception as exc:  # python-docx/lxml raise many types on malformed packages
        raise _corrupt() from exc


def _check_unpacked_size(data: bytes) -> None:
    """Refuse zip bombs before python-docx inflates the XML parts into memory."""
    limit_mb = get_settings().max_docx_uncompressed_mb
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            unpacked = sum(info.file_size for info in archive.infolist())
    except (zipfile.BadZipFile, OSError) as exc:
        raise _corrupt() from exc
    if unpacked > limit_mb * 1024 * 1024:
        raise IngestionError(
            "file_too_large", f"The document expands to more than {limit_mb} MB when unpacked."
        )


def _parse(document: Document) -> ParsedDocument:
    blocks: list[Block] = []
    headings: list[tuple[int, str]] = []
    code: list[str] = []
    items: list[str] = []

    def path() -> tuple[str, ...]:
        return tuple(title for _, title in headings)

    def flush() -> None:
        body = "\n".join(code).strip("\n")
        if body.strip():
            blocks.append(Block(BlockKind.CODE, f"```\n{body}\n```", path()))
        code.clear()
        if items:
            blocks.append(Block(BlockKind.LIST, "\n".join(items), path()))
            items.clear()

    for item in document.iter_inner_content():
        if isinstance(item, Table):
            flush()
            table = _table_markdown(item)
            if table:
                blocks.append(Block(BlockKind.TABLE, table, path()))
            continue
        style = (item.style.name if item.style is not None else "") or ""
        level = _heading_level(style)
        list_level = _list_level(item, style)
        text = item.text
        if level is not None:
            flush()
            title = " ".join(text.split())
            if title:
                headings[:] = [(lvl, t) for lvl, t in headings if lvl < level] + [(level, title)]
                blocks.append(Block(BlockKind.HEADING, title, path()))
        elif _is_code(item, style):
            if items:
                flush()
            code.append(text)
        elif list_level is not None:
            if code:
                flush()
            if text.strip():
                items.append("  " * list_level + "- " + text.strip())
        else:
            flush()
            if text.strip():
                blocks.append(Block(BlockKind.PARAGRAPH, text, path()))
    flush()
    return ParsedDocument(blocks=blocks)


def _heading_level(style: str) -> int | None:
    name = style.casefold()
    if name == "title":
        return 0
    match = _HEADING_STYLE.match(name)
    return int(match.group(1)) if match else None


def _is_code(paragraph: Paragraph, style: str) -> bool:
    name = style.casefold()
    if "code" in name or name in _CODE_STYLES:
        return True
    style_font = paragraph.style.font.name if paragraph.style is not None else None
    fonts = {
        (run.font.name or style_font or "").casefold() for run in paragraph.runs if run.text.strip()
    }
    return bool(fonts) and all(_MONOSPACE.search(font) for font in fonts)


def _list_level(paragraph: Paragraph, style: str) -> int | None:
    """Nesting level (0-based) of a list paragraph, None for other paragraphs."""
    if paragraph._p.xpath("./w:pPr/w:numPr"):  # Word's own bullets and numbering
        level = paragraph._p.xpath("./w:pPr/w:numPr/w:ilvl/@w:val")
        return int(level[0]) if level else 0
    if style.casefold().startswith("list"):  # "List Bullet 2" -> level 1
        match = _TRAILING_LEVEL.search(style)
        return int(match.group(1)) - 1 if match else 0
    return None


def _cell_text(cell: _Cell) -> str:
    parts: list[str] = []
    for item in cell.iter_inner_content():
        if isinstance(item, Table):  # nested table: flatten its cells into this one
            parts.extend(_cell_text(inner) for row in item.rows for inner in row.cells)
        else:
            parts.append(item.text)
    return " ".join(" ".join(parts).split())


def _table_markdown(table: Table) -> str:
    rows = [[_cell_text(cell).replace("|", "\\|") for cell in row.cells] for row in table.rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)
