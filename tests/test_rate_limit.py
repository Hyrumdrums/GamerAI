"""Tests for ``coordinator.rate_limit.RateLimiter``.

Uses a hand-rolled in-memory Redis fake plus a controllable clock so the
limiter's bucket boundaries are deterministic. Run with
``python -m unittest tests.test_rate_limit``.
"""
from __future__ import annotations

import unittest

from coordinator.rate_limit import RateLimiter


class FakeRedis:
    """Just enough Redis surface to exercise the limiter."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        self.expirations[key] = seconds


class RateLimiterDisabledTests(unittest.TestCase):
    def test_zero_disables(self):
        rl = RateLimiter(FakeRedis(), per_minute=0)
        self.assertFalse(rl.enabled)
        for _ in range(1_000):
            self.assertTrue(rl.allow("anyone"))

    def test_negative_treated_as_zero(self):
        rl = RateLimiter(FakeRedis(), per_minute=-5)
        self.assertFalse(rl.enabled)
        self.assertTrue(rl.allow("anyone"))


class RateLimiterEnabledTests(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()
        self.t = [0]
        self.rl = RateLimiter(self.r, per_minute=3, _now=lambda: self.t[0])

    def test_allows_up_to_limit(self):
        self.assertTrue(self.rl.allow("alice"))
        self.assertTrue(self.rl.allow("alice"))
        self.assertTrue(self.rl.allow("alice"))

    def test_rejects_when_limit_exceeded(self):
        for _ in range(3):
            self.assertTrue(self.rl.allow("alice"))
        self.assertFalse(self.rl.allow("alice"))
        self.assertFalse(self.rl.allow("alice"))

    def test_independent_keys_have_independent_buckets(self):
        for _ in range(3):
            self.rl.allow("alice")
        self.assertTrue(self.rl.allow("bob"))
        self.assertFalse(self.rl.allow("alice"))

    def test_first_request_sets_expiry(self):
        self.rl.allow("alice")
        # exactly one expire call, on the bucket key
        self.assertEqual(len(self.r.expirations), 1)
        ((key, ttl),) = self.r.expirations.items()
        self.assertTrue(key.startswith("rl:alice:"))
        self.assertGreaterEqual(ttl, 60)

    def test_rolls_over_at_minute_boundary(self):
        for _ in range(3):
            self.rl.allow("alice")
        self.assertFalse(self.rl.allow("alice"))
        # advance by exactly one bucket
        self.t[0] = 60
        self.assertTrue(self.rl.allow("alice"))


if __name__ == "__main__":
    unittest.main()
