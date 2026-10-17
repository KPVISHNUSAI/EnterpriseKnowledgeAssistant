"""Upload validation (app/services/uploads.py): no database needed.

Also home of the sample-file builders used by the document API tests.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from app.core.config import get_settings
from app.core.errors import (
    AppError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from app.core.filetypes import FileType
from app.services.uploads import MAX_DOCX_ENTRIES, clean_text, sanitize_filename, validated_upload

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# --- Sample files --------------------------------------------------------------------------


def pdf_bytes(pages: int = 3, text: str = "Quarterly security review") -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for number in range(1, pages + 1):
        pdf.drawString(72, 720, f"Page {number}: {text}")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def docx_bytes(text: str = "Employees must rotate credentials every 90 days.") -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Security policy", level=1)
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


CONTENT_TYPES_XML = (
    b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    b'content-types"><Override PartName="/word/document.xml" ContentType="application/'
    b'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
MINIMAL_DOCX = {"[Content_Types].xml": CONTENT_TYPES_XML, "word/document.xml": b"<w:document/>"}


def make_upload(data: bytes, filename: str | None, content_type: str | None = None) -> UploadFile:
    headers = Headers({"content-type": content_type} if content_type is not None else {})
    return UploadFile(io.BytesIO(data), filename=filename, headers=headers)


def validate(data: bytes, filename: str | None, content_type: str | None = None):
    with validated_upload(make_upload(data, filename, content_type)) as upload:
        return upload, upload.file.read()


def rejection(data: bytes, filename: str, content_type: str | None = None) -> AppError:
    with pytest.raises(AppError) as excinfo:
        validate(data, filename, content_type)
    return excinfo.value


# --- Accepted files ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "data", "content_type", "file_type"),
    [
        ("report.pdf", pdf_bytes(), "application/pdf", FileType.PDF),
        ("policy.docx", docx_bytes(), DOCX_MIME, FileType.DOCX),
        ("notes.txt", b"plain notes\n", "text/plain; charset=utf-8", FileType.TXT),
        ("guide.md", b"# Guide\n\nSome *markdown*.", "text/markdown", FileType.MD),
        ("guide.markdown", b"# Guide", "text/x-markdown", FileType.MD),
        ("readme.md", b"# Readme", "text/plain", FileType.MD),
        ("scan.PDF", pdf_bytes(pages=1), "application/x-pdf", FileType.PDF),
        ("policy.docx", docx_bytes(), "application/octet-stream", FileType.DOCX),
        ("notes.txt", "\ufeffBOM-prefixed UTF-8 \u2713".encode(), None, FileType.TXT),
    ],
)
def test_accepts_supported_files(filename, data, content_type, file_type):
    upload, stored = validate(data, filename, content_type)
    assert upload.file_type == file_type
    assert (
        upload.mime_type
        == {
            FileType.PDF: "application/pdf",
            FileType.DOCX: DOCX_MIME,
            FileType.TXT: "text/plain",
            FileType.MD: "text/markdown",
        }[file_type]
    )
    assert upload.extension == f".{file_type.value}"
    assert upload.size == len(data)
    assert upload.sha256 == hashlib.sha256(data).hexdigest()
    assert stored == data
    assert upload.filename == filename


def test_large_files_are_spooled_and_hashed_completely():
    data = b"0123456789abcdef\n" * 200_000  # ~3.4 MB, beyond the in-memory spool size
    upload, stored = validate(data, "big.txt", "text/plain")
    assert stored == data
    assert upload.sha256 == hashlib.sha256(data).hexdigest()


# --- Type agreement ------------------------------------------------------------------------


@pytest.mark.parametrize("filename", ["malware.exe", "README", "archive.tar.gz", "photo.png", ""])
def test_rejects_unsupported_extensions(filename):
    error = rejection(b"data", filename)
    assert isinstance(error, UnsupportedMediaTypeError)
    assert error.code == "unsupported_media_type"


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    [
        ("report.pdf", pdf_bytes(pages=1), "image/png"),
        ("notes.txt", b"hello", "text/html"),
        ("notes.txt", b"hello", "text/markdown"),
        ("policy.docx", docx_bytes(), "application/pdf"),
    ],
)
def test_rejects_declared_type_that_contradicts_the_extension(filename, data, content_type):
    error = rejection(data, filename, content_type)
    assert isinstance(error, UnsupportedMediaTypeError)
    assert error.code == "unsupported_media_type"


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("report.pdf", b"just some text pretending to be a PDF"),
        ("report.pdf", b"x" * 1024 + b"%PDF-1.7"),  # signature beyond the first 1024 bytes
        ("policy.docx", pdf_bytes(pages=1)),
        ("policy.docx", b"PK\x03\x04 but not really a zip archive"),
        ("policy.docx", b"\x00" * 8 + zip_bytes(MINIMAL_DOCX)),  # zip not at offset 0
        ("sheet.docx", zip_bytes({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b""})),
        ("notes.txt", b"caf\xe9 latin-1 is not UTF-8"),
        ("notes.txt", b"binary\x00data"),
        ("notes.md", b"# Title\n" + b"\x00"),
    ],
)
def test_rejects_content_that_does_not_match_the_type(filename, data):
    error = rejection(data, filename)
    assert isinstance(error, UnsupportedMediaTypeError)
    assert error.code == "unsupported_media_type"


def test_explains_password_protected_word_files():
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504  # OLE header, as Word writes
    error = rejection(ole, "protected.docx", DOCX_MIME)
    assert isinstance(error, UnsupportedMediaTypeError)
    assert "Password-protected" in error.message


def test_utf16_text_is_rejected_with_an_encoding_hint():
    error = rejection("hello".encode("utf-16"), "notes.txt", "text/plain")
    assert error.code == "unsupported_media_type"
    assert "UTF-8" in error.message


def test_accepts_pdf_signature_after_leading_bytes():
    upload, _ = validate(b"\n\n%PDF-1.4 minimal", "lenient.pdf")
    assert upload.file_type == FileType.PDF


# --- Size ----------------------------------------------------------------------------------


def test_rejects_empty_files():
    error = rejection(b"", "empty.txt", "text/plain")
    assert isinstance(error, ValidationFailedError)
    assert error.status_code == 422
    assert error.code == "empty_file"


def test_rejects_files_over_the_upload_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    validate(b"a" * (1024 * 1024), "limit.txt")  # exactly at the limit is fine
    error = rejection(b"a" * (1024 * 1024 + 1), "big.txt")
    assert isinstance(error, PayloadTooLargeError)
    assert error.status_code == 413
    assert error.code == "payload_too_large"


# --- DOCX structure ------------------------------------------------------------------------


def test_rejects_macro_enabled_docx_by_part_name():
    error = rejection(
        zip_bytes({**MINIMAL_DOCX, "word/vbaProject.bin": b"\xd0\xcf\x11\xe0"}), "macro.docx"
    )
    assert isinstance(error, UnsupportedMediaTypeError)
    assert error.code == "macro_enabled_document"


@pytest.mark.parametrize(
    "declaration",
    [
        b'<Override PartName="/word/x.bin" ContentType="application/vnd.ms-office.vbaProject"/>',
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>',
    ],
)
def test_rejects_macro_enabled_docx_by_declared_content_type(declaration):
    content_types = CONTENT_TYPES_XML.replace(b"</Types>", declaration + b"</Types>")
    data = zip_bytes({**MINIMAL_DOCX, "[Content_Types].xml": content_types, "word/x.bin": b"vba"})
    error = rejection(data, "renamed.docx")
    assert error.code == "macro_enabled_document"


def test_rejects_docx_zip_bomb(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_docx_uncompressed_mb", 1)
    bomb = zip_bytes({**MINIMAL_DOCX, "word/media/padding.bin": b"\x00" * (2 * 1024 * 1024)})
    assert len(bomb) < 20_000  # tiny on the wire, 2 MB when inflated
    error = rejection(bomb, "bomb.docx")
    assert isinstance(error, ValidationFailedError)
    assert error.code == "unsafe_archive"


@pytest.mark.parametrize("name", ["../evil.xml", "word/../../evil.xml", "/etc/evil", "C:\\evil"])
def test_rejects_docx_with_unsafe_part_names(name):
    error = rejection(zip_bytes({**MINIMAL_DOCX, name: b"x"}), "traversal.docx")
    assert error.code == "unsafe_archive"


def test_rejects_docx_with_too_many_parts():
    entries = {**MINIMAL_DOCX, **{f"word/p{i}.xml": b"" for i in range(MAX_DOCX_ENTRIES)}}
    error = rejection(zip_bytes(entries), "parts.docx")
    assert error.code == "unsafe_archive"


def test_rejects_docx_that_understates_its_part_count():
    entries = {**MINIMAL_DOCX, **{f"word/p{i}.xml": b"" for i in range(MAX_DOCX_ENTRIES)}}
    data = bytearray(zip_bytes(entries))
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, end + 8, 2, 2)  # entries on this disk / in total
    error = rejection(bytes(data), "liar.docx")
    assert error.code == "unsafe_archive"


def test_huge_central_directory_is_rejected_before_parsing(monkeypatch):
    # ~3 MB of directory entries while claiming two: refused without building ZipInfos.
    entries = {**MINIMAL_DOCX, **{f"word/p{i:05}.xml": b"" for i in range(50_000)}}
    data = bytearray(zip_bytes(entries))
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, end + 8, 2, 2)

    def must_not_parse(*args, **kwargs):
        raise AssertionError("the central directory was parsed")

    monkeypatch.setattr(zipfile, "ZipFile", must_not_parse)
    error = rejection(bytes(data), "huge.docx")
    assert error.code == "unsafe_archive"


def test_accepts_real_docx_with_images_and_tables():
    import docx

    document = docx.Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Owner"
    document.add_paragraph("See table above.")
    buffer = io.BytesIO()
    document.save(buffer)
    upload, _ = validate(buffer.getvalue(), "table.docx", DOCX_MIME)
    assert upload.file_type == FileType.DOCX


# --- Filenames -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.txt", "passwd.txt"),
        ("C:\\Users\\me\\Desktop\\report.pdf", "report.pdf"),
        ("/var/tmp/../x/notes.md", "notes.md"),
        ("  quarterly \t report\n final .pdf  ", "quarterly report final .pdf"),
        ("in\x00voice\x07.pdf", "invoice.pdf"),
        ("invoice\u202efdp.txt", "invoicefdp.txt"),  # right-to-left override is dropped
        ("Re\u0301sume\u0301.pdf", "R\u00e9sum\u00e9.pdf"),  # NFC
        (None, ""),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_long_filenames_are_truncated_keeping_the_extension():
    upload, _ = validate(b"text", "a" * 300 + ".markdown")
    assert len(upload.filename) == 255
    assert upload.filename.endswith("a.markdown")
    assert upload.file_type == FileType.MD


def test_clean_text_collapses_whitespace_and_drops_control_characters():
    assert clean_text("  Board\u00a0 minutes\x00 \n 2026\u200b ") == "Board minutes 2026"
