from __future__ import annotations

from functools import lru_cache

import redis

from app.core.config import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    """Shared client with short timeouts: Redis is used for rate limits and caches,
    and callers fail open when it is unavailable."""
    return redis.Redis.from_url(
        get_settings().redis_url,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
        health_check_interval=30,
    )
