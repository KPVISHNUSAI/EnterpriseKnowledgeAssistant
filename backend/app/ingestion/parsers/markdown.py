"""Markdown by line structure (CommonMark/GFM subset): ATX and Setext headings build the
heading path, fenced and indented code is kept verbatim, pipe tables and lists are kept as
written, YAML/TOML front matter is skipped. Inline markup in body text is left as is."""

from __future__ import annotations

import re

from app.ingestion.types import Block, BlockKind, ParsedDocument

_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_TABLE_DELIMITER = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)")
_THEMATIC_BREAK = re.compile(r"^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
_INDENTED = re.compile(r"^(?: {4}|\t)")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_EMPHASIS = re.compile(r"(?<!\w)(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)")
_FRONT_MATTER_SCAN = 200


def parse_markdown(text: str) -> ParsedDocument:
    lines = text.split("\n")
    blocks: list[Block] = []
    headings: list[tuple[int, str]] = []
    paragraph: list[str] = []

    def path() -> tuple[str, ...]:
        return tuple(title for _, title in headings)

    def flush() -> None:
        if paragraph:
            blocks.append(Block(BlockKind.PARAGRAPH, _join_paragraph(paragraph), path()))
            paragraph.clear()

    def heading(level: int, title: str) -> None:
        flush()
        if title:
            headings[:] = [(lvl, t) for lvl, t in headings if lvl < level] + [(level, title)]
            blocks.append(Block(BlockKind.HEADING, title, path()))

    i = _front_matter_end(lines)
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
            flush()
            end = _closing_fence(lines, i + 1, fence.group(1))
            if end < len(lines):
                body = "\n".join(lines[i : end + 1])
            else:  # unclosed: the block runs to the end of the document
                body = "\n".join(lines[i:]).rstrip() + "\n" + fence.group(1)
            blocks.append(Block(BlockKind.CODE, body, path()))
            i = end + 1
            continue
        atx = _ATX.match(line)
        if atx:
            heading(len(atx.group(1)), _inline(atx.group(2) or ""))
            i += 1
            continue
        if paragraph and _SETEXT.match(line):
            title = _inline(" ".join(part.strip() for part in paragraph))
            paragraph.clear()
            heading(1 if line.strip()[0] == "=" else 2, title)
            i += 1
            continue
        if not line.strip() or _THEMATIC_BREAK.match(line):
            flush()
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _is_delimiter_row(lines[i + 1]):
            flush()
            end = i + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            blocks.append(Block(BlockKind.TABLE, "\n".join(lines[i:end]), path()))
            i = end
            continue
        if _LIST_ITEM.match(line):
            flush()
            end = i + 1
            while end < len(lines) and _continues_list(lines[end]):
                end += 1
            blocks.append(Block(BlockKind.LIST, "\n".join(lines[i:end]), path()))
            i = end
            continue
        if (
            not paragraph
            and _INDENTED.match(line)
            and not (blocks and blocks[-1].kind is BlockKind.LIST)
        ):
            end = i
            while end < len(lines) and (_INDENTED.match(lines[end]) or not lines[end].strip()):
                end += 1
            blocks.append(Block(BlockKind.CODE, "\n".join(lines[i:end]).rstrip(), path()))
            i = end
            continue
        paragraph.append(line)
        i += 1
    flush()
    return ParsedDocument(blocks=blocks)


def _front_matter_end(lines: list[str]) -> int:
    """Index of the first line after YAML (---) or TOML (+++) front matter, else 0."""
    marker = lines[0].strip() if lines else ""
    if marker not in ("---", "+++"):
        return 0
    closers = ("---", "...") if marker == "---" else ("+++",)
    for i in range(1, min(len(lines), _FRONT_MATTER_SCAN)):
        if lines[i].strip() in closers:
            return i + 1
    return 0  # no closing marker: it was a thematic break


def _closing_fence(lines: list[str], start: int, opener: str) -> int:
    for i in range(start, len(lines)):
        stripped = lines[i].strip()
        if stripped and set(stripped) == {opener[0]} and len(stripped) >= len(opener):
            return i
    return len(lines)  # unclosed: the code block runs to the end of the document


def _is_delimiter_row(line: str) -> bool:
    return "|" in line and "-" in line and bool(_TABLE_DELIMITER.match(line))


def _continues_list(line: str) -> bool:
    """List items and their continuation lines run until a blank line or a new block."""
    stripped = line.strip()
    return bool(stripped) and not _FENCE.match(line) and not _ATX.match(line)


def _join_paragraph(lines: list[str]) -> str:
    """Soft line breaks render as spaces; hard breaks (two trailing spaces or a trailing
    backslash) stay line breaks."""
    parts = []
    for line in lines:
        parts.append(line.strip().removesuffix("\\").rstrip())
        parts.append("\n" if line.endswith(("  ", "\\")) else " ")
    return "".join(parts[:-1])


def _inline(text: str) -> str:
    """Heading text without link targets and emphasis/code markers."""
    text = _EMPHASIS.sub(r"\2", _LINK.sub(r"\1", text))
    return " ".join(text.replace("`", "").split())
