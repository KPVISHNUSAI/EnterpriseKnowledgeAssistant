"""AWS S3 (or S3-compatible) object storage. Credentials come from the standard AWS
chain (instance/task role, environment, profile); nothing is configured here."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.storage.base import StorageError, validate_key

_ERRORS = (BotoCoreError, ClientError)
_MISSING = frozenset({"404", "NoSuchKey", "NotFound"})


def content_disposition(filename: str, *, inline: bool) -> str:
    """RFC 6266 value: an ASCII fallback for old clients plus the exact UTF-8 name."""
    fallback = "".join(c if " " <= c <= "~" and c not in '"\\' else "_" for c in filename)
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


class S3Storage:
    backend = "s3"

    def __init__(self, settings: Settings) -> None:
        if not settings.s3_bucket:
            raise StorageError("S3_BUCKET is not configured")
        self.bucket = settings.s3_bucket
        self.presign_ttl = settings.s3_presign_ttl_seconds
        self.client = boto3.client(
            "s3",
            region_name=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            config=Config(
                signature_version="s3v4",  # required for SSE-KMS objects and presigning
                retries={"mode": "standard", "max_attempts": 3},
                connect_timeout=5,
                read_timeout=60,
            ),
        )
        self._encryption: dict[str, str] = {}
        if settings.s3_sse != "none":
            self._encryption["ServerSideEncryption"] = settings.s3_sse
        if settings.s3_sse == "aws:kms" and settings.s3_kms_key_id:
            self._encryption["SSEKMSKeyId"] = settings.s3_kms_key_id

    def put(self, key: str, fileobj: BinaryIO, *, content_type: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=validate_key(key),
                Body=fileobj,
                ContentType=content_type,
                **self._encryption,
            )
        except _ERRORS as exc:
            raise StorageError(str(exc)) from exc

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=validate_key(key))
            return bytes(response["Body"].read())
        except _ERRORS as exc:
            raise StorageError(str(exc)) from exc

    def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=validate_key(key))["Body"]
        except _ERRORS as exc:
            raise StorageError(str(exc)) from exc
        try:
            yield from body.iter_chunks(chunk_size)
        except _ERRORS as exc:
            raise StorageError(str(exc)) from exc
        finally:
            body.close()

    def delete(self, key: str) -> None:
        try:  # S3 deletes are idempotent: a missing key is not an error
            self.client.delete_object(Bucket=self.bucket, Key=validate_key(key))
        except _ERRORS as exc:
            raise StorageError(str(exc)) from exc

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=validate_key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING:
                return False
            raise StorageError(str(exc)) from exc
        except BotoCoreError as exc:
            raise StorageError(str(exc)) from exc
        return True

    def presigned_download_url(
        self, key: str, *, filename: str, content_type: str, inline: bool
    ) -> str | None:
        try:
            return str(
                self.client.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": self.bucket,
                        "Key": validate_key(key),
                        "ResponseContentDisposition": content_disposition(filename, inline=inline),
                        "ResponseContentType": content_type,
                    },
                    ExpiresIn=self.presign_ttl,
                )
            )
        except _ERRORS as exc:
            raise StorageError(str(exc)) from exc

    def check(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except _ERRORS as exc:
            raise StorageError(str(exc)) from exc
