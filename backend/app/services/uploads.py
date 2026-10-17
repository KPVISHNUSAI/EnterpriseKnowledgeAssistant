"""Upload validation, done before anything is stored.

The file type comes from the extension, and the declared Content-Type and the content
itself must agree with it. Structure is checked without extracting anything to disk;
encryption, page limits and parseability are left to ingestion.
"""

from __future__ import annotations

import codecs
import hashlib
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO, cast

from fastapi import UploadFile

from app.core.config import Settings, get_settings
from app.core.errors import PayloadTooLargeError, UnsupportedMediaTypeError, ValidationFailedError
from app.core.filetypes import EXTENSIONS, MIME_TYPES, FileType

MAX_FILENAME_CHARS = 255
MAX_DOCX_ENTRIES = 10_000
_READ_CHUNK = 1024 * 1024
_SPOOL_IN_MEMORY = 1024 * 1024  # larger uploads roll over to a temporary file
# ZipFile parses the whole central directory up front, so its size is bounded first
# (real DOCX entries take ~100 bytes each, far below this per-entry allowance).
_MAX_CENTRAL_DIRECTORY_BYTES = MAX_DOCX_ENTRIES * 256
_MAX_CONTENT_TYPES_BYTES = 1024 * 1024
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Declared types accepted besides the canonical MIME type (browsers often send nothing
# specific for documents, and plain text for Markdown).
_GENERIC_TYPES = frozenset({"", "application/octet-stream"})
_ALIASES: dict[FileType, frozenset[str]] = {
    FileType.PDF: frozenset({"application/x-pdf"}),
    FileType.DOCX: frozenset(),
    FileType.TXT: frozenset(),
    FileType.MD: frozenset({"text/x-markdown", "text/plain"}),
}


@dataclass(frozen=True)
class ValidatedUpload:
    file: BinaryIO  # spooled copy, positioned at the start
    filename: str  # sanitized display name (metadata only, never part of a storage key)
    file_type: FileType
    size: int
    sha256: str

    @property
    def mime_type(self) -> str:
        return MIME_TYPES[self.file_type]

    @property
    def extension(self) -> str:
        return f".{self.file_type.value}"


def clean_text(value: str) -> str:
    """NFC-normalize, drop control/format characters (NUL, bidi overrides, ...) and
    collapse whitespace. Used for user-supplied names and titles."""
    value = unicodedata.normalize("NFC", value)
    kept = "".join(
        ch for ch in value if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )
    return " ".join(kept.split())


def sanitize_filename(raw: str | None) -> str:
    """Basename only (browsers may send a path, with either separator), cleaned."""
    return clean_text((raw or "").replace("\\", "/").rsplit("/", 1)[-1])


@contextmanager
def validated_upload(upload: UploadFile) -> Iterator[ValidatedUpload]:
    """Validate an upload and yield it spooled to a temporary file (closed on exit)."""
    settings = get_settings()
    filename = sanitize_filename(upload.filename)
    file_type = _file_type(filename)
    _check_declared_type(file_type, upload.content_type)
    with tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY) as spool:
        target = cast(BinaryIO, spool)
        size, sha256 = _copy(upload.file, target, settings)
        _check_content(file_type, target, settings)
        target.seek(0)
        yield ValidatedUpload(target, _truncate(filename), file_type, size, sha256)


def _file_type(filename: str) -> FileType:
    extension = f".{filename.rsplit('.', 1)[-1].lower()}" if "." in filename else ""
    file_type = EXTENSIONS.get(extension)
    if file_type is None:
        allowed = ", ".join(sorted(EXTENSIONS))
        raise UnsupportedMediaTypeError(f"Unsupported file type. Allowed: {allowed}")
    return file_type


def _check_declared_type(file_type: FileType, content_type: str | None) -> None:
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared not in _GENERIC_TYPES | _ALIASES[file_type] | {MIME_TYPES[file_type]}:
        raise UnsupportedMediaTypeError(
            f"The declared content type does not match a .{file_type.value} file"
        )


def _truncate(filename: str) -> str:
    if len(filename) <= MAX_FILENAME_CHARS:
        return filename
    stem, extension = filename.rsplit(".", 1)  # the extension was validated
    return f"{stem[: MAX_FILENAME_CHARS - len(extension) - 1].rstrip()}.{extension}"


def _copy(source: BinaryIO, target: BinaryIO, settings: Settings) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(_READ_CHUNK):
        size += len(chunk)
        if size > settings.max_upload_bytes:
            raise PayloadTooLargeError(
                f"The file exceeds the {settings.max_upload_mb} MB upload limit"
            )
        digest.update(chunk)
        target.write(chunk)
    if size == 0:
        raise ValidationFailedError("The file is empty", code="empty_file")
    target.seek(0)
    return size, digest.hexdigest()


def _check_content(file_type: FileType, fileobj: BinaryIO, settings: Settings) -> None:
    head = fileobj.read(1024)
    fileobj.seek(0)
    if file_type == FileType.PDF:
        if b"%PDF-" not in head:
            raise UnsupportedMediaTypeError("The file is not a valid PDF document")
    elif file_type == FileType.DOCX:
        if head.startswith(_OLE_SIGNATURE):  # Word wraps encrypted files in an OLE container
            raise UnsupportedMediaTypeError(
                "Password-protected and legacy Word (.doc) files are not supported"
            )
        if not head.startswith(b"PK\x03\x04"):
            raise UnsupportedMediaTypeError("The file is not a valid DOCX document")
        _check_docx(fileobj, settings.max_docx_uncompressed_mb * 1024 * 1024)
    else:
        _check_text(fileobj)


def _check_text(fileobj: BinaryIO) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()  # a BOM decodes as U+FEFF
    try:
        while chunk := fileobj.read(_READ_CHUNK):
            if b"\x00" in chunk:  # binary data, or UTF-16/32 text
                raise UnsupportedMediaTypeError(
                    "Text files must be UTF-8 encoded and must not contain NUL bytes"
                )
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise UnsupportedMediaTypeError("Text files must be UTF-8 encoded") from None


def _unsafe_archive(message: str) -> ValidationFailedError:
    return ValidationFailedError(message, code="unsafe_archive")


def _not_docx() -> UnsupportedMediaTypeError:
    return UnsupportedMediaTypeError("The file is not a valid DOCX document")


def _macro_enabled() -> UnsupportedMediaTypeError:
    return UnsupportedMediaTypeError(
        "Macro-enabled documents are not accepted", code="macro_enabled_document"
    )


def _unsafe_name(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    absolute = name.startswith(("/", "\\")) or name[1:2] == ":"  # also C:...
    return absolute or ".." in parts


def _check_docx(fileobj: BinaryIO, max_uncompressed: int) -> None:
    # The same end-of-central-directory reader ZipFile uses, so the bound applies to
    # exactly what it is about to parse.
    try:
        end_record = zipfile._EndRecData(fileobj)  # type: ignore[attr-defined]
    except Exception:
        end_record = None
    if end_record is None:
        raise _not_docx()
    if (
        end_record[zipfile._ECD_ENTRIES_TOTAL] > MAX_DOCX_ENTRIES  # type: ignore[attr-defined]
        or end_record[zipfile._ECD_SIZE] > _MAX_CENTRAL_DIRECTORY_BYTES  # type: ignore[attr-defined]
    ):
        raise _unsafe_archive(f"The document has more than {MAX_DOCX_ENTRIES} parts")
    try:
        archive = zipfile.ZipFile(fileobj)
    except Exception:  # any parse failure means it is not a DOCX we accept
        raise _not_docx() from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_DOCX_ENTRIES:
            raise _unsafe_archive(f"The document has more than {MAX_DOCX_ENTRIES} parts")
        names = {info.filename for info in infos}
        if any(_unsafe_name(name) for name in names):
            raise _unsafe_archive("The document contains unsafe part names")
        if sum(info.file_size for info in infos) > max_uncompressed:
            # ZipFile never inflates an entry beyond its declared size, so this bounds
            # what ingestion can decompress (zip-bomb guard).
            raise _unsafe_archive(
                f"The document expands to more than {max_uncompressed // (1024 * 1024)} MB"
            )
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise _not_docx()
        if any("vbaproject" in name.lower() for name in names):
            raise _macro_enabled()
        types_info = archive.getinfo("[Content_Types].xml")
        if types_info.file_size > _MAX_CONTENT_TYPES_BYTES:
            raise _not_docx()
        try:
            content_types = archive.read(types_info).lower()
        except Exception:  # encrypted, corrupt or unsupported compression
            raise _not_docx() from None
    # A VBA part can have any name; its declared content type gives it away.
    if b"vbaproject" in content_types or b"macroenabled" in content_types:
        raise _macro_enabled()
