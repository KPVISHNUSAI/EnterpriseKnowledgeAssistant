"""Format dispatch: bytes of a stored original -> ParsedDocument (blocks in reading order)."""

from __future__ import annotations

from pathlib import PurePosixPath

from app.core.filetypes import EXTENSIONS, FileType, file_type_for_mime
from app.ingestion.parsers.docx import parse_docx
from app.ingestion.parsers.markdown import parse_markdown
from app.ingestion.parsers.pdf import parse_pdf
from app.ingestion.parsers.text import decode_text, parse_text
from app.ingestion.types import IngestionError, ParsedDocument


def detect_file_type(mime_type: str, filename: str) -> FileType:
    """The upload validated MIME type, extension and content against each other."""
    file_type = file_type_for_mime(mime_type) or EXTENSIONS.get(
        PurePosixPath(filename).suffix.lower()
    )
    if file_type is None:
        raise IngestionError("unsupported_file_type", "This file type is not supported.")
    return file_type


def parse(data: bytes, file_type: FileType) -> ParsedDocument:
    if file_type is FileType.PDF:
        return parse_pdf(data)
    if file_type is FileType.DOCX:
        return parse_docx(data)
    text = decode_text(data)
    return parse_markdown(text) if file_type is FileType.MD else parse_text(text)


__all__ = ["detect_file_type", "parse"]
