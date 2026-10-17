"""Password hashing, opaque session tokens and CSRF tokens.

Session tokens are random 256-bit values; only their SHA-256 hash is stored, so a
database leak does not yield usable sessions. The CSRF token is an HMAC of the
session token (double-submit pattern bound to the session), so it needs no storage.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings


@lru_cache
def _hasher() -> PasswordHasher:
    s = get_settings()
    return PasswordHasher(
        time_cost=s.argon2_time_cost,
        memory_cost=s.argon2_memory_kib,
        parallelism=s.argon2_parallelism,
    )


@lru_cache
def _dummy_hash() -> str:
    return _hasher().hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant-work verification: unknown users still pay for one hash check."""
    try:
        return _hasher().verify(stored_hash or _dummy_hash(), password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(stored_hash: str) -> bool:
    return _hasher().check_needs_rehash(stored_hash)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token_for(session_token: str) -> str:
    key = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, b"csrf:" + session_token.encode(), hashlib.sha256).hexdigest()


def verify_csrf(session_token: str, provided: str | None) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(csrf_token_for(session_token), provided)
