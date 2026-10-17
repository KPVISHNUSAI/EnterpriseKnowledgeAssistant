"""Text cleaning. Prose is normalized (control characters, NFKC, whitespace); code and
tables keep their layout verbatim. The PDF helpers repair what text extraction does to
running text: repeated page headers/footers and lines broken at the column edge."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace

from app.ingestion.types import Block, BlockKind

# C0/C1 controls except tab and newline, zero-width space, word joiner, BOM, soft hyphen.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b\u2060\ufeff\u00ad]")
_NEWLINES = str.maketrans({"\r": "\n", "\u2028": "\n", "\v": "\n", "\f": "\n\n", "\u2029": "\n\n"})
_BLANK_RUNS = re.compile(r"\n{3,}")
_DIGITS = re.compile(r"\d+")
_LIST_ITEM = re.compile(r"^(?:[-*•‣◦▪●○■□\u2013]|\(?\d{1,3}[.)]|\(?[a-z][.)])\s")
_WORD_HYPHENS = ("-", "\u2010", "\u00ad")
# A line this close to the typical line width was most likely wrapped by the layout.
WRAP_RATIO = 0.75


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").translate(_NEWLINES)


def strip_control(text: str) -> str:
    return _CONTROL.sub("", normalize_newlines(text))


def clean_prose(text: str, *, keep_indent: bool = False) -> str:
    """NFKC and collapsed whitespace; `keep_indent` keeps leading indentation (the
    nesting of list items)."""
    lines = []
    for line in unicodedata.normalize("NFKC", strip_control(text)).split("\n"):
        words = " ".join(line.split())
        indent = line[: len(line) - len(line.lstrip())] if keep_indent and words else ""
        lines.append(indent + words)
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip("\n")


def clean_verbatim(text: str) -> str:
    """Code and tables: only control characters and trailing whitespace are removed."""
    lines = [line.rstrip() for line in strip_control(text).split("\n")]
    return "\n".join(lines).strip("\n")


def clean_blocks(blocks: Iterable[Block]) -> list[Block]:
    cleaned = []
    for block in blocks:
        if block.kind in (BlockKind.CODE, BlockKind.TABLE):
            text = clean_verbatim(block.text)
        else:
            text = clean_prose(block.text, keep_indent=block.kind is BlockKind.LIST)
        if text.strip():
            path = tuple(title for title in map(clean_prose, block.heading_path) if title)
            cleaned.append(replace(block, text=text, heading_path=path))
    return cleaned


# --- PDF text repair --------------------------------------------------------------------


def _line_key(line: str) -> str:
    # "Page 3 of 10" and "Page 4 of 10" are the same footer.
    return _DIGITS.sub("#", " ".join(line.split()).casefold())


def _edge_indexes(lines: Sequence[str], edge: int) -> set[int]:
    filled = [i for i, line in enumerate(lines) if line.strip()]
    return set(filled[:edge] + filled[-edge:])


def remove_repeated_edge_lines(pages: list[list[str]], edge: int = 3) -> list[list[str]]:
    """Drop running headers and footers: lines among the first/last `edge` non-blank
    lines of a page that recur there (ignoring digits) on most pages. Extraction order
    follows the content stream, so a footer can appear at the top of the page text."""
    if len(pages) < 3:
        return pages
    edges = [_edge_indexes(lines, edge) for lines in pages]
    counts = Counter(
        key
        for lines, idx in zip(pages, edges, strict=True)
        for key in {_line_key(lines[i]) for i in idx}
    )
    threshold = max(3, len(pages) // 2 + 1)
    repeated = {key for key, count in counts.items() if count >= threshold}
    return [
        [line for i, line in enumerate(lines) if i not in idx or _line_key(line) not in repeated]
        for lines, idx in zip(pages, edges, strict=True)
    ]


def typical_line_width(pages: Iterable[Iterable[str]]) -> int:
    """90th percentile of non-blank line lengths across the document (the text width)."""
    lengths = sorted(
        len(" ".join(line.split())) for lines in pages for line in lines if line.strip()
    )
    return lengths[int(0.9 * (len(lengths) - 1))] if lengths else 0


def unwrap_lines(lines: Iterable[str], width: int) -> list[str]:
    """Rejoin lines the layout wrapped and words hyphenated across a line break. A line
    at least WRAP_RATIO of the typical width continues on the next line; shorter lines
    (paragraph ends, headings, table rows, list items) keep their break. Blank lines are
    returned as "" (paragraph boundaries)."""
    out: list[str] = []
    previous_length = 0
    for raw in lines:
        line = " ".join(raw.split())
        if not line:
            out.append("")
            previous_length = 0
            continue
        wrapped = previous_length >= WRAP_RATIO * width and not _LIST_ITEM.match(line)
        if out and out[-1] and wrapped:
            last = out[-1]
            if last.endswith(_WORD_HYPHENS) and last[-2:-1].isalpha():
                # "inter-" + "national" was one word; "Hewlett-" + "Packard" keeps its hyphen.
                out[-1] = (last[:-1] if line[0].islower() else last) + line
            else:
                out[-1] = f"{last} {line}"
        else:
            out.append(line)
        previous_length = len(line)
    return out
