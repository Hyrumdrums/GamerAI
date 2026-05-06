"""Optional per-client rate limiting for the coordinator.

A single environment variable, ``RATE_LIMIT_PER_MIN``, controls behaviour:

  * ``0`` / unset → disabled. ``allow()`` is a no-op that always returns
                    True. **This is the default**, so tests, local dev,
                    and CI never need to think about it.
  * ``N > 0``      → at most N requests per client identity per rolling
                     60-second bucket. Excess requests get rejected with
                     a 429 by the coordinator middleware.

The "client identity" is the Caddy-forwarded IP when present, falling
back to the direct peer address. When auth is on we could key on the
token instead, but with one shared token today that's identical to
"everyone." IPs are the right granularity for now.

The store is bucket-per-minute counters in Redis so the limiter is
stateless across coordinator restarts and works under multiple replicas.
Tests inject any object with ``incr`` + ``expire`` so the module is
unit-testable without Redis.
"""
from __future__ import annotations

import time
from typing import Protocol


class _RedisLike(Protocol):
    def incr(self, key: str): ...        # noqa: E704
    def expire(self, key: str, seconds: int): ...  # noqa: E704


_PREFIX = "rl:"
_BUCKET_SECONDS = 60
_KEY_TTL_SECONDS = 90  # >= bucket so the key disappears on its own


class RateLimiter:
    """Fixed-window per-key rate limiter. Disabled when ``per_minute`` is 0."""

    def __init__(self, redis_client: _RedisLike, per_minute: int, *, _now=time.time):
        self._r = redis_client
        self._limit = max(0, int(per_minute))
        self._now = _now

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def allow(self, identity: str) -> bool:
        """Return True if the request from *identity* is within the limit."""
        if not self.enabled:
            return True
        bucket = int(self._now() // _BUCKET_SECONDS)
        key = f"{_PREFIX}{identity}:{bucket}"
        count = int(self._r.incr(key))
        if count == 1:
            # first request in this bucket — set TTL so the key self-cleans.
            self._r.expire(key, _KEY_TTL_SECONDS)
        return count <= self._limit
