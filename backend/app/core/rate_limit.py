"""Fixed-window rate limiting backed by Redis.

Fails open when Redis is unreachable (logged), so a cache outage degrades protection
rather than taking the API down.
"""

from __future__ import annotations

import logging
import time

import redis

from app.core.errors import RateLimitedError

logger = logging.getLogger(__name__)


def hit(
    client: redis.Redis, bucket: str, identity: str, *, limit: int, window_seconds: int
) -> None:
    """Count one request; raise RateLimitedError once `limit` is exceeded in the window."""
    window = int(time.time() // window_seconds)
    key = f"rl:{bucket}:{identity}:{window}"
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds + 1)
        count = int(pipe.execute()[0])
    except redis.RedisError:
        logger.warning("rate_limit.redis_unavailable", extra={"bucket": bucket})
        return
    if count > limit:
        retry_after = window_seconds - int(time.time() % window_seconds)
        raise RateLimitedError(headers={"Retry-After": str(max(retry_after, 1))})
