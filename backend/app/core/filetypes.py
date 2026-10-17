"""Supported upload formats (single source of truth for API, ingestion and search)."""

from __future__ import annotations

from enum import StrEnum


class FileType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    MD = "md"


MIME_TYPES: dict[FileType, str] = {
    FileType.PDF: "application/pdf",
    FileType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    FileType.TXT: "text/plain",
    FileType.MD: "text/markdown",
}

EXTENSIONS: dict[str, FileType] = {
    ".pdf": FileType.PDF,
    ".docx": FileType.DOCX,
    ".txt": FileType.TXT,
    ".md": FileType.MD,
    ".markdown": FileType.MD,
}

FILE_TYPE_BY_MIME: dict[str, FileType] = {mime: ft for ft, mime in MIME_TYPES.items()}


def file_type_for_mime(mime_type: str) -> FileType | None:
    return FILE_TYPE_BY_MIME.get(mime_type)
