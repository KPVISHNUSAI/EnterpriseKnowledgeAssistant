from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import BinaryIO, Protocol

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.-]{0,1023}$")


class StorageError(Exception):
    """Raised when the storage backend fails; callers treat it as retryable."""


class ObjectStorage(Protocol):
    backend: str

    def put(self, key: str, fileobj: BinaryIO, *, content_type: str) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]: ...

    def delete(self, key: str) -> None:
        """Idempotent: deleting a missing key is not an error."""
        ...

    def exists(self, key: str) -> bool: ...

    def presigned_download_url(
        self, key: str, *, filename: str, content_type: str, inline: bool
    ) -> str | None:
        """Short-lived direct download URL, or None if the API must stream the file."""
        ...

    def check(self) -> None:
        """Readiness probe; raises StorageError if the backend is unusable."""
        ...


def validate_key(key: str) -> str:
    if not _KEY.match(key) or ".." in key.split("/"):
        raise StorageError("invalid storage key")
    return key


def build_storage_key(
    prefix: str,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    version: int,
    extension: str,
) -> str:
    """Keys are built only from server-generated ids plus a whitelisted extension, so a
    user-supplied filename can never influence the storage path."""
    prefix = prefix.strip("/")
    key = (
        f"orgs/{organization_id}/documents/{document_id}/v{version}/"
        f"{uuid.uuid4().hex}{extension.lower()}"
    )
    return validate_key(f"{prefix}/{key}" if prefix else key)
