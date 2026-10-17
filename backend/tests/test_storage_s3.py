"""S3Storage against moto's in-process S3 (never real AWS)."""

from __future__ import annotations

import io
from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from moto import mock_aws

from app.core.config import get_settings
from app.storage import StorageError
from app.storage.s3 import S3Storage, content_disposition

BUCKET = "eka-test-documents"
KEY = "orgs/1/documents/2/v1/0123abcd.txt"


def use_fake_aws_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Fake credentials only; a developer's real AWS config is never read."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    for name in ("AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    use_fake_aws_credentials(monkeypatch, tmp_path)


def s3_settings(**overrides):
    values = {
        "storage_backend": "s3",
        "s3_bucket": BUCKET,
        "s3_region": "us-east-1",
        "s3_sse": "AES256",
        **overrides,
    }
    return get_settings().model_copy(update=values)


@pytest.fixture
def s3(aws_env) -> Iterator[S3Storage]:
    with mock_aws():
        storage = S3Storage(s3_settings())
        storage.client.create_bucket(Bucket=BUCKET)
        yield storage


def test_put_get_stream_exists_delete(s3):
    s3.put(KEY, io.BytesIO(b"hello world"), content_type="text/plain")
    assert s3.exists(KEY)
    assert s3.get_bytes(KEY) == b"hello world"
    assert list(s3.iter_bytes(KEY, chunk_size=4)) == [b"hell", b"o wo", b"rld"]
    head = s3.client.head_object(Bucket=BUCKET, Key=KEY)
    assert head["ContentType"] == "text/plain"
    assert head["ServerSideEncryption"] == "AES256"

    s3.delete(KEY)
    assert not s3.exists(KEY)
    s3.delete(KEY)  # idempotent


def test_put_uses_kms_key_when_configured(aws_env):
    with mock_aws():
        storage = S3Storage(s3_settings(s3_sse="aws:kms", s3_kms_key_id="alias/eka-test"))
        storage.client.create_bucket(Bucket=BUCKET)
        storage.put(KEY, io.BytesIO(b"secret"), content_type="text/plain")
        head = storage.client.head_object(Bucket=BUCKET, Key=KEY)
        assert head["ServerSideEncryption"] == "aws:kms"
        assert head["SSEKMSKeyId"].endswith("alias/eka-test")


def test_put_without_encryption_when_disabled(aws_env):
    with mock_aws():
        storage = S3Storage(s3_settings(s3_sse="none"))
        storage.client.create_bucket(Bucket=BUCKET)
        storage.put(KEY, io.BytesIO(b"x"), content_type="text/plain")
        assert "ServerSideEncryption" not in storage.client.head_object(Bucket=BUCKET, Key=KEY)


def test_presigned_url_sets_disposition_type_and_ttl(s3):
    s3.put(KEY, io.BytesIO(b"%PDF-1.7"), content_type="application/pdf")
    url = s3.presigned_download_url(
        KEY, filename="Résumé 2026.pdf", content_type="application/pdf", inline=True
    )
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.path.endswith(KEY)
    assert query["response-content-type"] == ["application/pdf"]
    assert query["response-content-disposition"] == [
        "inline; filename=\"R_sum_ 2026.pdf\"; filename*=UTF-8''R%C3%A9sum%C3%A9%202026.pdf"
    ]
    assert query["X-Amz-Expires"] == [str(get_settings().s3_presign_ttl_seconds)]
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]


def test_check_reports_a_missing_bucket(aws_env):
    with mock_aws():
        storage = S3Storage(s3_settings())
        with pytest.raises(StorageError):
            storage.check()
        storage.client.create_bucket(Bucket=BUCKET)
        storage.check()


def test_botocore_errors_become_storage_errors(s3):
    with pytest.raises(StorageError):
        s3.get_bytes("orgs/missing.txt")
    with pytest.raises(StorageError):
        next(s3.iter_bytes("orgs/missing.txt"))
    assert not s3.exists("orgs/missing.txt")
    with pytest.raises(StorageError):
        S3Storage(s3_settings(s3_bucket="no-such-bucket")).put(
            KEY, io.BytesIO(b"x"), content_type="text/plain"
        )


@pytest.mark.parametrize("key", ["../etc/passwd", "orgs/../../x", "/absolute", ""])
def test_rejects_invalid_keys(s3, key):
    with pytest.raises(StorageError):
        s3.put(key, io.BytesIO(b"x"), content_type="text/plain")
    with pytest.raises(StorageError):
        s3.presigned_download_url(key, filename="x.txt", content_type="text/plain", inline=False)


def test_requires_a_bucket(aws_env):
    with pytest.raises(StorageError):
        S3Storage(s3_settings(s3_bucket=None))


@pytest.mark.parametrize(
    ("filename", "inline", "expected"),
    [
        ("report.pdf", False, "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"),
        ("a b.txt", True, "inline; filename=\"a b.txt\"; filename*=UTF-8''a%20b.txt"),
        (
            'quote"back\\slash.md',
            False,
            'attachment; filename="quote_back_slash.md"; '
            "filename*=UTF-8''quote%22back%5Cslash.md",
        ),
        (
            "日本語.pdf",
            False,
            "attachment; filename=\"___.pdf\"; filename*=UTF-8''%E6%97%A5%E6%9C%AC%E8%AA%9E.pdf",
        ),
    ],
)
def test_content_disposition(filename, inline, expected):
    assert content_disposition(filename, inline=inline) == expected
