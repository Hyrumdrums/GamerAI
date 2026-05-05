"""Tests for ``coordinator.idempotency.IdempotencyStore``.

Uses a hand-rolled in-memory fake for the Redis surface so the module is
unit-testable without spinning up a real Redis. Run with
``python -m unittest tests.test_idempotency``.
"""
from __future__ import annotations

import unittest

from coordinator.idempotency import IdempotencyStore


class FakeRedis:
    """The minimal subset of redis.Redis we exercise."""

    def __init__(self):
        self.store: dict[str, tuple[bytes, int]] = {}
        self.now = 0

    def get(self, key):
        item = self.store.get(key)
        if item is None:
            return None
        value, expires = item
        if expires <= self.now:
            del self.store[key]
            return None
        return value

    def setex(self, key, ttl, value):
        encoded = value.encode() if isinstance(value, str) else value
        self.store[key] = (encoded, self.now + int(ttl))


class IdempotencyStoreTests(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()
        self.s = IdempotencyStore(self.r, ttl_seconds=60)

    def test_lookup_returns_none_for_unseen_key(self):
        self.assertIsNone(self.s.lookup("nope"))

    def test_remember_then_lookup_round_trips(self):
        self.s.remember("abc-123", "job-xyz")
        self.assertEqual(self.s.lookup("abc-123"), "job-xyz")

    def test_remember_handles_bytes_and_strings(self):
        # mimic both possible Redis return shapes
        self.s.remember("k", "job1")
        self.r.store["idem:k"] = (b"job-bytes", self.r.now + 60)
        self.assertEqual(self.s.lookup("k"), "job-bytes")

    def test_empty_or_missing_key_is_no_op(self):
        self.s.remember(None, "job")
        self.s.remember("", "job")
        self.s.remember("   ", "job")
        self.assertIsNone(self.s.lookup(None))
        self.assertIsNone(self.s.lookup(""))
        self.assertIsNone(self.s.lookup("   "))
        self.assertEqual(self.r.store, {})  # nothing was written

    def test_keys_are_namespaced(self):
        self.s.remember("xyz", "job1")
        self.assertIn("idem:xyz", self.r.store)

    def test_ttl_floors_at_one_second(self):
        s = IdempotencyStore(self.r, ttl_seconds=0)
        s.remember("k", "j")
        # entry should be present immediately and expire after 1s
        self.assertEqual(s.lookup("k"), "j")
        self.r.now = 2
        self.assertIsNone(s.lookup("k"))

    def test_keys_expire(self):
        self.s.remember("k", "j")
        self.r.now = 61
        self.assertIsNone(self.s.lookup("k"))


if __name__ == "__main__":
    unittest.main()
