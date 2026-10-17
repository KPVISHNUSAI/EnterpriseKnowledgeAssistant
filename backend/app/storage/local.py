"""Filesystem storage for local development (no AWS credentials required)."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from app.storage.base import StorageError, validate_key


class LocalStorage:
    backend = "local"

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError("storage key escapes the storage root")
        return path

    def put(self, key: str, fileobj: BinaryIO, *, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
                shutil.copyfileobj(fileobj, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp.name, path)  # atomic: readers never see a partial file
        except OSError as exc:
            raise StorageError(str(exc)) from exc

    def get_bytes(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as exc:
            raise StorageError(str(exc)) from exc

    def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        path = self._path(key)
        try:
            with path.open("rb") as fh:
                while chunk := fh.read(chunk_size):
                    yield chunk
        except OSError as exc:
            raise StorageError(str(exc)) from exc

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(str(exc)) from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def presigned_download_url(
        self, key: str, *, filename: str, content_type: str, inline: bool
    ) -> str | None:
        return None

    def check(self) -> None:
        if not os.access(self.root, os.W_OK):
            raise StorageError(f"storage root {self.root} is not writable")
