from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.storage.base import ObjectStorage, StorageError, build_storage_key, validate_key


@lru_cache
def get_storage() -> ObjectStorage:
    settings = get_settings()
    if settings.storage_backend == "s3":
        from app.storage.s3 import S3Storage

        return S3Storage(settings)
    from app.storage.local import LocalStorage

    return LocalStorage(settings.local_storage_path)


__all__ = ["ObjectStorage", "StorageError", "build_storage_key", "get_storage", "validate_key"]
