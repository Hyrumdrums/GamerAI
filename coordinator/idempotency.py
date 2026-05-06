"""Optional idempotency-key storage for ``POST /generate``.

Customers that want safe retries (network blip, client timeout, mid-flight
restart) send an ``Idempotency-Key`` header on ``/generate``. The
coordinator caches ``key -> job_id`` for a configurable TTL and returns
the same ``job_id`` on retries with the same key, so the request never
double-charges and never duplicates work.

Behaviour summary:
  - no header on the request          -> behave exactly as before (no-op).
  - header present, key unseen        -> create the job; remember the key.
  - header present, key seen          -> return the previously-issued
                                          job_id; do not create a new job.

The store is intentionally tiny — one Redis key per Idempotency-Key with
a TTL — so it adds zero state to the SQLite ledger and disappears on its
own.

Tests inject a stub Redis-like object (anything with ``get`` and ``setex``)
so the module is unit-testable without a live Redis.
"""
from __future__ import annotations

from typing import Optional, Protocol


class _RedisLike(Protocol):
    def get(self, key: str): ...                # noqa: E704
    def setex(self, key: str, time: int, value): ...  # noqa: E704


_PREFIX = "idem:"


class IdempotencyStore:
    """Tiny ``key -> job_id`` cache with a TTL, backed by Redis."""

    def __init__(self, redis_client: _RedisLike, ttl_seconds: int):
        self._r = redis_client
        self._ttl = max(1, int(ttl_seconds))

    @staticmethod
    def _norm(key: str) -> str:
        return _PREFIX + key.strip()

    def lookup(self, key: Optional[str]) -> Optional[str]:
        """Return the job_id previously stored for *key*, or None."""
        if not key or not key.strip():
            return None
        v = self._r.get(self._norm(key))
        if v is None:
            return None
        return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)

    def remember(self, key: Optional[str], job_id: str) -> None:
        """Bind *key* to *job_id* for the configured TTL. No-op if *key*
        is empty/None."""
        if not key or not key.strip():
            return
        self._r.setex(self._norm(key), self._ttl, job_id)
