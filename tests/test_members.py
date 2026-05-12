"""Tests for the member-identity data layer (coordinator/db.py + member_auth).

DB-layer only — no HTTP, no Redis. Each test gets a fresh tempfile DB.
Run with ``python -m unittest tests.test_members``.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest

from coordinator import member_auth
from coordinator.db import DB


def _fresh_db() -> DB:
    fd, path = tempfile.mkstemp(prefix="gamerai-members-", suffix=".db")
    os.close(fd)
    return DB(path=path)


class TokenHelpersTests(unittest.TestCase):
    def test_generate_token_has_prefix_and_length(self):
        t = member_auth.generate_token()
        self.assertTrue(t.startswith("gai_"))
        # 64 hex chars after the prefix
        self.assertEqual(len(t), 4 + 64)

    def test_generate_token_is_random(self):
        a = member_auth.generate_token()
        b = member_auth.generate_token()
        self.assertNotEqual(a, b)

    def test_hash_token_is_stable(self):
        t = "gai_" + "a" * 64
        self.assertEqual(member_auth.hash_token(t), member_auth.hash_token(t))

    def test_hash_token_ignores_surrounding_whitespace(self):
        t = "gai_" + "a" * 64
        self.assertEqual(
            member_auth.hash_token(t),
            member_auth.hash_token(f"  {t}\n"),
        )

    def test_parse_bearer_extracts_token(self):
        self.assertEqual(
            member_auth.parse_bearer("Bearer gai_abc"), "gai_abc"
        )
        self.assertEqual(
            member_auth.parse_bearer("bearer gai_abc"), "gai_abc"
        )

    def test_parse_bearer_returns_none_for_malformed(self):
        for bad in (None, "", "gai_abc", "Basic gai_abc", "Bearer", "Bearer "):
            self.assertIsNone(member_auth.parse_bearer(bad), bad)


class MemberCrudTests(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    def _seed(self, role="contributor", parent=None, quota=None, email=None):
        token = member_auth.generate_token()
        member_id = "mem_" + token[4:16]
        self.db.create_member(
            member_id=member_id,
            email=email,
            role=role,
            parent_member_id=parent,
            token_hash=member_auth.hash_token(token),
            tier="BRONZE",
            daily_quota_tokens=quota,
        )
        return member_id, token

    def test_lookup_by_token_resolves_member(self):
        mid, token = self._seed(email="alice@example.com")
        m = member_auth.lookup_member_by_token(self.db, token)
        self.assertIsNotNone(m)
        self.assertEqual(m.member_id, mid)
        self.assertEqual(m.email, "alice@example.com")
        self.assertEqual(m.role, "contributor")
        self.assertEqual(m.tier, "BRONZE")
        self.assertIsNone(m.daily_quota_tokens)

    def test_lookup_returns_none_for_unknown_token(self):
        self._seed()
        self.assertIsNone(
            member_auth.lookup_member_by_token(self.db, "gai_" + "0" * 64)
        )

    def test_revoked_member_does_not_resolve(self):
        _mid, token = self._seed()
        self.db.revoke_member_by_token_hash(
            member_auth.hash_token(token), time.time()
        )
        self.assertIsNone(
            member_auth.lookup_member_by_token(self.db, token)
        )

    def test_revoke_returns_false_for_unknown_token(self):
        self.assertFalse(
            self.db.revoke_member_by_token_hash(
                member_auth.hash_token("gai_unknown"), time.time()
            )
        )

    def test_parent_link_persists(self):
        parent_id, _ = self._seed(role="contributor")
        invitee_id, invitee_token = self._seed(role="invitee", parent=parent_id)
        m = member_auth.lookup_member_by_token(self.db, invitee_token)
        self.assertEqual(m.parent_member_id, parent_id)
        self.assertEqual(m.role, "invitee")

    def test_list_members_orders_by_creation(self):
        a, _ = self._seed(email="a@example.com")
        b, _ = self._seed(email="b@example.com")
        rows = self.db.list_members()
        self.assertEqual([r["member_id"] for r in rows], [a, b])

    def test_quota_round_trip(self):
        _mid, token = self._seed(quota=1000)
        m = member_auth.lookup_member_by_token(self.db, token)
        self.assertEqual(m.daily_quota_tokens, 1000)

    def test_touch_member_updates_last_active(self):
        mid, _ = self._seed()
        self.db.touch_member(mid, 12345.6)
        row = self.db.get_member(mid)
        self.assertAlmostEqual(float(row["last_active_at"]), 12345.6, places=2)


class MemberUsageTests(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        token = member_auth.generate_token()
        self.member_id = "mem_usage_test"
        self.db.create_member(
            member_id=self.member_id,
            email=None,
            role="invitee",
            parent_member_id=None,
            token_hash=member_auth.hash_token(token),
        )

    def test_usage_today_starts_empty(self):
        today = self.db.member_usage_today(self.member_id)
        self.assertEqual(today["tokens_in"], 0)
        self.assertEqual(today["tokens_out"], 0)
        self.assertEqual(today["jobs"], 0)

    def test_add_usage_accumulates_within_a_day(self):
        now = time.time()
        self.db.add_member_usage(self.member_id, now, tokens_in=10, tokens_out=20)
        self.db.add_member_usage(self.member_id, now, tokens_in=3, tokens_out=7)
        today = self.db.member_usage_today(self.member_id, now=now)
        self.assertEqual(today["tokens_in"], 13)
        self.assertEqual(today["tokens_out"], 27)
        self.assertEqual(today["jobs"], 2)

    def test_usage_does_not_leak_between_members(self):
        other = "mem_other"
        self.db.create_member(
            member_id=other,
            email=None,
            role="invitee",
            parent_member_id=None,
            token_hash=member_auth.hash_token(member_auth.generate_token()),
        )
        now = time.time()
        self.db.add_member_usage(self.member_id, now, tokens_in=5, tokens_out=5)
        self.assertEqual(
            self.db.member_usage_today(other, now=now)["tokens_out"], 0
        )


if __name__ == "__main__":
    unittest.main()
