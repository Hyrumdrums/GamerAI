"""Tests for the daily tier promotion/demotion engine + uptime sampler
data layer. DB-only; no HTTP, no Redis-roundtrip background thread —
the engine's ``evaluate_all()`` is called directly in-process so the
test can control the clock and skip the 24-hour wait.

Run with ``python -m unittest tests.test_tier_engine``.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

from coordinator import member_auth
from coordinator.db import DB
from coordinator.tier_engine import (
    DEMOTION_GRACE_DAYS,
    TIER_LADDER,
    TierEngine,
)
from coordinator.tiers import (
    TIER_QUOTAS,
    TIER_REQUIREMENTS,
    meets_requirements,
    tier_above,
    tier_below,
)


def _fresh_db() -> DB:
    fd, path = tempfile.mkstemp(prefix="gamerai-tiers-", suffix=".db")
    os.close(fd)
    return DB(path=path)


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class TierMathTests(unittest.TestCase):
    """Pure-Python sanity checks for the constants — guard against
    typos that would silently break the engine."""

    def test_ladder_matches_quota_keys(self):
        self.assertEqual(set(TIER_LADDER), set(TIER_QUOTAS.keys()))
        self.assertEqual(set(TIER_LADDER), set(TIER_REQUIREMENTS.keys()))

    def test_quotas_increase_monotonically(self):
        prior_tokens = -1
        for tier in TIER_LADDER:
            t = TIER_QUOTAS[tier]["tokens"]
            self.assertGreater(t, prior_tokens, f"tokens regressed at {tier}")
            prior_tokens = t

    def test_platinum_is_finite(self):
        """User explicitly asked for a cap so an API-key holder at
        PLATINUM can't abuse 'unlimited'."""
        self.assertIsNotNone(TIER_QUOTAS["PLATINUM"]["tokens"])
        self.assertIsNotNone(TIER_QUOTAS["PLATINUM"]["images"])

    def test_requirements_increase_monotonically(self):
        for axis in ("min_hours_per_day", "min_days_per_week"):
            prior = -1
            for tier in TIER_LADDER:
                v = TIER_REQUIREMENTS[tier][axis]
                self.assertGreaterEqual(
                    v, prior, f"{axis} regressed at {tier}",
                )
                prior = v

    def test_meets_requirements_threshold_logic(self):
        # BRONZE is 2hr × 4d. Exactly meeting both = True.
        self.assertTrue(meets_requirements("BRONZE", 4, 2.0))
        # One axis short = False.
        self.assertFalse(meets_requirements("BRONZE", 3, 2.0))
        self.assertFalse(meets_requirements("BRONZE", 4, 1.9))

    def test_tier_above_and_below(self):
        self.assertEqual(tier_above("BRONZE"), "SILVER")
        self.assertIsNone(tier_above("PLATINUM"))
        self.assertEqual(tier_below("SILVER"), "BRONZE")
        self.assertIsNone(tier_below("BRONZE"))


class UptimeRollupTests(unittest.TestCase):
    """worker_uptime accumulation + per-member 7-day aggregation."""

    def setUp(self):
        self.db = _fresh_db()
        self.member_id = "mem_uptime"
        self.db.create_member(
            member_id=self.member_id,
            email=None, role="contributor",
            parent_member_id=None,
            token_hash=member_auth.hash_token(member_auth.generate_token()),
        )
        now = time.time()
        self.db.claim_worker_ownership("w_one", self.member_id, "idle", now)
        self.db.claim_worker_ownership("w_two", self.member_id, "idle", now)

    def test_credit_accumulates_within_day(self):
        now = time.time()
        self.db.add_worker_uptime_minutes("w_one", now, 5)
        self.db.add_worker_uptime_minutes("w_one", now, 5)
        summary = self.db.member_uptime_summary(
            self.member_id, window_days=7, now=now,
        )
        # 10 min total < 60 min "online day" threshold — counts toward
        # total_minutes but not days_online.
        self.assertEqual(summary["total_minutes"], 10)
        self.assertEqual(summary["days_online"], 0)

    def test_active_day_threshold_is_one_hour(self):
        now = time.time()
        # Exactly 60 min — counts as one active day.
        for _ in range(12):
            self.db.add_worker_uptime_minutes("w_one", now, 5)
        summary = self.db.member_uptime_summary(
            self.member_id, window_days=7, now=now,
        )
        self.assertEqual(summary["days_online"], 1)
        self.assertEqual(summary["avg_hours_per_active_day"], 1.0)

    def test_sums_across_owned_workers_per_day(self):
        """Two PCs each online 4hr today should count as 8 worker-
        hours on the day — the network sees them as 8h of capacity."""
        now = time.time()
        # 240 min each → 480 min combined for the day.
        for _ in range(48):
            self.db.add_worker_uptime_minutes("w_one", now, 5)
            self.db.add_worker_uptime_minutes("w_two", now, 5)
        summary = self.db.member_uptime_summary(
            self.member_id, window_days=7, now=now,
        )
        self.assertEqual(summary["days_online"], 1)
        self.assertAlmostEqual(summary["avg_hours_per_active_day"], 8.0)

    def test_window_excludes_old_days(self):
        now = time.time()
        # Today: 1 hour.
        for _ in range(12):
            self.db.add_worker_uptime_minutes("w_one", now, 5)
        # 10 days ago: also 1 hour.
        ten_days_ago = now - 10 * 86400.0
        for _ in range(12):
            self.db.add_worker_uptime_minutes("w_one", ten_days_ago, 5)
        summary = self.db.member_uptime_summary(
            self.member_id, window_days=7, now=now,
        )
        # Only today inside 7-day window.
        self.assertEqual(summary["days_online"], 1)
        self.assertEqual(summary["total_minutes"], 60)

    def test_stranger_uptime_not_credited(self):
        """A worker owned by someone else must not pollute our
        member's summary — the JOIN through workers.owner_member_id
        is the security boundary here."""
        now = time.time()
        self.db.claim_worker_ownership("w_stranger", "mem_other", "idle", now)
        for _ in range(48):
            self.db.add_worker_uptime_minutes("w_stranger", now, 5)
        summary = self.db.member_uptime_summary(
            self.member_id, window_days=7, now=now,
        )
        self.assertEqual(summary["total_minutes"], 0)


class TierEngineDecisionTests(unittest.TestCase):
    """End-to-end decision logic — populate uptime + member rows, run
    the engine in-process, assert tier moves."""

    def setUp(self):
        self.db = _fresh_db()
        self.engine = TierEngine(self.db, interval_seconds=86400.0)

    def _make_member(self, tier: str = "BRONZE") -> str:
        member_id = f"mem_{tier.lower()}_{int(time.time()*1e6) % 10_000_000}"
        self.db.create_member(
            member_id=member_id,
            email=None, role="contributor",
            parent_member_id=None,
            token_hash=member_auth.hash_token(member_auth.generate_token()),
            tier=tier,
            daily_quota_tokens=TIER_QUOTAS[tier]["tokens"],
            daily_quota_images=TIER_QUOTAS[tier]["images"],
        )
        return member_id

    def _make_worker_for(self, member_id: str, worker_id: str) -> None:
        self.db.claim_worker_ownership(worker_id, member_id, "idle", time.time())

    def _fill_uptime_days(
        self,
        worker_id: str,
        days_of_uptime: int,
        hours_each_day: float,
        now: float,
    ) -> None:
        """Backfill ``days_of_uptime`` distinct UTC days, each with
        ``hours_each_day`` of credited minutes. Fills the most recent
        days first so they land inside the 7-day window."""
        minutes = int(hours_each_day * 60)
        for d in range(days_of_uptime):
            day_ts = now - d * 86400.0
            # One bulk insert per day — bypass the +1 minute API.
            self.db.add_worker_uptime_minutes(worker_id, day_ts, minutes)

    def test_promotes_from_bronze_to_silver_when_meeting_silver_bar(self):
        member_id = self._make_member("BRONZE")
        self._make_worker_for(member_id, "w_p1")
        now = time.time()
        # SILVER needs 6 hr/day × 5 days. Give 6 days × 6 hr.
        self._fill_uptime_days("w_p1", days_of_uptime=6, hours_each_day=6.0, now=now)
        stats = self.engine.evaluate_all()
        self.assertEqual(stats["promoted"], 1)
        row = self.db.get_member(member_id)
        self.assertEqual(row["tier"], "SILVER")
        self.assertEqual(row["daily_quota_tokens"], TIER_QUOTAS["SILVER"]["tokens"])
        self.assertEqual(row["daily_quota_images"], TIER_QUOTAS["SILVER"]["images"])

    def test_one_move_per_day_cap_blocks_double_promotion(self):
        """A member who'd qualify for two jumps gets only one — the
        engine refuses to act again within 23h of the prior move."""
        member_id = self._make_member("BRONZE")
        self._make_worker_for(member_id, "w_cap")
        now = time.time()
        # Way over GOLD's bar — 7 days × 14 hr.
        self._fill_uptime_days("w_cap", 7, 14.0, now)
        self.engine.evaluate_all()
        first = self.db.get_member(member_id)["tier"]
        # Run again immediately — must NOT jump further.
        self.engine.evaluate_all()
        second = self.db.get_member(member_id)["tier"]
        self.assertEqual(first, second)
        self.assertEqual(first, "SILVER")

    def test_below_threshold_starts_grace_but_no_demotion_yet(self):
        member_id = self._make_member("SILVER")
        self._make_worker_for(member_id, "w_grace")
        now = time.time()
        # SILVER needs 6h × 5d; give 2h × 4d (BRONZE-level).
        self._fill_uptime_days("w_grace", 4, 2.0, now)
        stats = self.engine.evaluate_all()
        self.assertEqual(stats["grace_started"], 1)
        row = self.db.get_member(member_id)
        self.assertEqual(row["tier"], "SILVER")
        self.assertIsNotNone(row["tier_below_threshold_since"])

    def test_demotion_fires_after_grace_window(self):
        member_id = self._make_member("SILVER")
        self._make_worker_for(member_id, "w_demote")
        now = time.time()
        self._fill_uptime_days("w_demote", 4, 2.0, now)
        # Backdate below_threshold_since to >14 days ago.
        backdated = now - (DEMOTION_GRACE_DAYS + 1) * 86400.0
        with self.db._lock:
            self.db._conn.execute(
                "UPDATE members SET tier_below_threshold_since=? "
                "WHERE member_id=?",
                (backdated, member_id),
            )
        stats = self.engine.evaluate_all()
        self.assertEqual(stats["demoted"], 1)
        row = self.db.get_member(member_id)
        self.assertEqual(row["tier"], "BRONZE")
        # Demotion clears the grace stamp.
        self.assertIsNone(row["tier_below_threshold_since"])

    def test_meeting_bar_clears_grace_stamp(self):
        """A previously-below-threshold member who recovers gets the
        countdown cancelled — one passing day forgives."""
        member_id = self._make_member("SILVER")
        self._make_worker_for(member_id, "w_clear")
        now = time.time()
        # Mark them below threshold first.
        self.db.mark_member_below_threshold(member_id, now - 5 * 86400.0)
        # Now backfill uptime that meets SILVER's bar.
        self._fill_uptime_days("w_clear", 6, 7.0, now)
        stats = self.engine.evaluate_all()
        # They might also qualify for GOLD (12h × 6d) — 7h × 6d is
        # below GOLD's hours-per-day bar, so they stay SILVER but
        # get the grace cleared.
        row = self.db.get_member(member_id)
        self.assertIsNone(row["tier_below_threshold_since"])
        self.assertEqual(stats["grace_cleared"], 1)

    def test_bronze_below_threshold_does_not_demote_no_floor(self):
        """BRONZE is the floor — a below-threshold BRONZE member just
        sits there until they recover. They get the grace mark for
        UI visibility but the engine never deletes them."""
        member_id = self._make_member("BRONZE")
        self._make_worker_for(member_id, "w_floor")
        # No uptime at all — well below BRONZE's 2h × 4d.
        stats = self.engine.evaluate_all()
        self.assertEqual(stats["grace_started"], 1)
        row = self.db.get_member(member_id)
        self.assertEqual(row["tier"], "BRONZE")

    def test_admin_member_is_skipped(self):
        """Admins aren't on the ladder — engine must not touch them."""
        admin_id = "mem_admin"
        self.db.create_member(
            member_id=admin_id, email=None, role="admin",
            parent_member_id=None,
            token_hash=member_auth.hash_token(member_auth.generate_token()),
            tier="BRONZE", daily_quota_tokens=None,
        )
        self._make_worker_for(admin_id, "w_admin")
        # Give them eligible-for-promotion uptime.
        now = time.time()
        self._fill_uptime_days("w_admin", 7, 14.0, now)
        self.engine.evaluate_all()
        row = self.db.get_member(admin_id)
        self.assertEqual(row["tier"], "BRONZE")  # untouched


class WorkerForgetTests(unittest.TestCase):
    """db.delete_worker — used by the account-page forget button."""

    def setUp(self):
        self.db = _fresh_db()
        now = time.time()
        self.db.claim_worker_ownership("w_keepme", "mem_a", "idle", now)
        self.db.claim_worker_ownership("w_gone", "mem_a", "idle", now)

    def test_delete_removes_only_target(self):
        ok = self.db.delete_worker("w_gone")
        self.assertTrue(ok)
        remaining = {r["worker_id"] for r in self.db.list_workers()}
        self.assertEqual(remaining, {"w_keepme"})

    def test_delete_returns_false_for_missing(self):
        self.assertFalse(self.db.delete_worker("w_does_not_exist"))


if __name__ == "__main__":
    unittest.main()
