"""Uptime sampler + daily tier promotion/demotion engine.

Two background threads, both daemonized so they die with the
coordinator process. Both are written defensively — exceptions in a
tick are logged and swallowed so a malformed row can't kill the
thread.

UptimeSampler runs every ``sample_interval_minutes`` (default 5) and
credits each currently-heartbeating worker that many minutes of
``worker_uptime`` for today (UTC). The sample window matches the
interval so a worker that was online for *some* of the last 5
minutes gets full credit — fine-grained enough for tier decisions,
coarse enough that the DB load is trivial (one upsert per worker per
sample).

TierEngine runs once per day shortly after midnight UTC and:

- For every non-admin un-revoked member, computes the 7-day uptime
  summary across all owned workers.
- Compares to ``TIER_REQUIREMENTS`` to decide whether to promote
  (member meets next tier's bar) or to start/continue a demotion
  countdown (member misses current tier's bar). A single passing day
  cancels the countdown — demotion is for sustained underperformance,
  not vacations.
- Caps moves at one step per 24 hours per member.
- Hides workers offline > ``stale_hide_days`` from the account UI by
  setting last_seen-derived state (no DB delete) and prunes uptime
  rows older than 60 days.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from coordinator.tiers import (
    TIER_LADDER,
    meets_requirements,
    quota_for,
    tier_above,
    tier_below,
)
from shared.config import WORKER_HEARTBEATS

log = logging.getLogger("coordinator.tier_engine")


# Sampler default: every 5 minutes. The number must match the credit
# amount — a sampler running every 5 minutes credits 5 minutes_online
# per tick. Changing the interval without changing the credit would
# silently inflate or deflate everyone's uptime. Keep them paired via
# the constructor.
DEFAULT_SAMPLE_INTERVAL_MINUTES = 5

# Engine default: every 24 hours. Lifespan schedules the first run
# offset so a coordinator restarted at noon doesn't run the engine
# immediately — the offset puts the first tick shortly after the next
# UTC midnight.
DEFAULT_ENGINE_INTERVAL_SECONDS = 86_400.0

# Demotion grace — a member must miss their current tier's bar for at
# least this many days continuously before the engine actually drops
# them. Mirrors business.md → tier policy. 14 days = two weekly
# eval cycles, forgiving of one vacation week.
DEMOTION_GRACE_DAYS = 14

# Promotion cooldown — refuse to move a member who was already moved
# inside this window. Mirrors the "one move per day" rule. Slightly
# under 24h so a daily 00:05 tick doesn't perpetually miss eligible
# members by an exact-second tie.
TIER_MOVE_COOLDOWN_SECONDS = 23 * 3600.0

# UI hide threshold — owned workers with no heartbeat for this long
# are dropped from the account-page registered-workers table.
# Members can still see them via the future /me/workers/all endpoint
# if we add one. Manual "forget" deletes the row entirely.
STALE_WORKER_HIDE_DAYS = 30


class UptimeSampler(threading.Thread):
    """Heartbeat sampler — credits worker_uptime every N minutes for
    each worker seen heartbeating within the sample window. Cheap:
    one HGETALL on WORKER_HEARTBEATS plus one upsert per fresh
    worker, every sample_interval_minutes."""

    daemon = True

    def __init__(
        self,
        redis_client,
        db,
        sample_interval_minutes: int = DEFAULT_SAMPLE_INTERVAL_MINUTES,
    ):
        super().__init__(name="uptime-sampler")
        self.r = redis_client
        self.db = db
        self.interval_minutes = int(sample_interval_minutes)
        self.interval_seconds = float(self.interval_minutes * 60)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info(json.dumps({
            "event": "uptime_sampler_started",
            "interval_minutes": self.interval_minutes,
        }))
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # never let the thread die
                log.exception("uptime sampler tick failed: %s", e)
            self._stop.wait(self.interval_seconds)

    def _tick(self) -> None:
        now = time.time()
        cutoff = now - self.interval_seconds
        raw_map = self.r.hgetall(WORKER_HEARTBEATS) or {}
        credited = 0
        for worker_id, raw in raw_map.items():
            ts = _parse_heartbeat_ts(raw)
            if ts <= 0 or ts < cutoff:
                continue
            self.db.add_worker_uptime_minutes(
                worker_id, now, self.interval_minutes,
            )
            credited += 1
        if credited:
            log.info(json.dumps({
                "event": "uptime_sampled",
                "workers_credited": credited,
                "minutes_per_worker": self.interval_minutes,
            }))


def _parse_heartbeat_ts(raw) -> float:
    """Mirror of the parsing logic in scheduler._read_heartbeat. We
    only need the timestamp here; job_id is irrelevant for uptime.
    Tolerates the legacy bare-float shape."""
    if raw is None:
        return 0.0
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return float(data.get("ts", 0) or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


class TierEngine(threading.Thread):
    """Daily promote/demote evaluator. Runs forever in the background;
    each tick processes every non-admin un-revoked member exactly
    once. Tick latency is small (one DB join per member); we do not
    parallelize — the network is hundreds of members, not millions.
    """

    daemon = True

    def __init__(
        self,
        db,
        interval_seconds: float = DEFAULT_ENGINE_INTERVAL_SECONDS,
        first_run_delay_seconds: Optional[float] = None,
    ):
        super().__init__(name="tier-engine")
        self.db = db
        self.interval = float(interval_seconds)
        # First run defaults to "next UTC midnight + 5 minutes" so the
        # engine always evaluates against a clean day boundary even
        # if the coordinator just restarted at 14:37 UTC.
        if first_run_delay_seconds is None:
            now = time.time()
            seconds_into_day = now % 86400.0
            seconds_to_midnight = (86400.0 - seconds_into_day) % 86400.0
            first_run_delay_seconds = seconds_to_midnight + 300.0
        self.first_run_delay = float(first_run_delay_seconds)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info(json.dumps({
            "event": "tier_engine_started",
            "interval_s": self.interval,
            "first_run_in_s": self.first_run_delay,
        }))
        # Wait until the scheduled first run, then loop every interval.
        if self._stop.wait(self.first_run_delay):
            return
        while not self._stop.is_set():
            try:
                self.evaluate_all()
            except Exception as e:
                log.exception("tier engine tick failed: %s", e)
            self._stop.wait(self.interval)

    # ------------------------------------------------------------------
    # Core evaluation — also exposed publicly so tests can call it
    # directly without spinning up the thread.
    # ------------------------------------------------------------------
    def evaluate_all(self) -> dict:
        """Run one engine pass. Returns a stats dict for logging /
        observability: ``{promoted, demoted, grace_started,
        grace_cleared, no_change}``. Safe to call ad-hoc from an
        admin endpoint or test."""
        now = time.time()
        stats = {
            "promoted": 0,
            "demoted": 0,
            "grace_started": 0,
            "grace_cleared": 0,
            "no_change": 0,
        }
        members = self.db.list_members_for_tier_engine()
        for row in members:
            outcome = self._evaluate_one(row, now)
            stats[outcome] = stats.get(outcome, 0) + 1
        # Bounded retention for the uptime table — engine is the
        # daily checkpoint where this can run cheaply.
        try:
            pruned = self.db.prune_worker_uptime(older_than_days=60)
            if pruned:
                log.info(json.dumps({
                    "event": "uptime_pruned", "rows": pruned,
                }))
        except Exception as e:
            log.exception("uptime prune failed: %s", e)
        log.info(json.dumps({"event": "tier_engine_evaluated", **stats}))
        return stats

    def _evaluate_one(self, row, now: float) -> str:
        """Decide promote / demote / grace / no-change for one
        member. Returns the verdict tag for stats counting."""
        member_id = row["member_id"]
        current_tier = row["tier"]
        keys = row.keys()
        last_changed = (
            float(row["tier_last_changed_at"])
            if "tier_last_changed_at" in keys
            and row["tier_last_changed_at"] is not None
            else 0.0
        )
        below_since = (
            float(row["tier_below_threshold_since"])
            if "tier_below_threshold_since" in keys
            and row["tier_below_threshold_since"] is not None
            else None
        )

        # 1-move-per-day cap — skip outright if recently moved.
        if last_changed and (now - last_changed) < TIER_MOVE_COOLDOWN_SECONDS:
            return "no_change"

        summary = self.db.member_uptime_summary(
            member_id, window_days=7, now=now,
        )
        days_online = int(summary["days_online"])
        avg_hours = float(summary["avg_hours_per_active_day"])

        meets_current = meets_requirements(
            current_tier, days_online, avg_hours,
        )
        next_tier = tier_above(current_tier)
        meets_next = (
            next_tier is not None
            and meets_requirements(next_tier, days_online, avg_hours)
        )

        # Promotion path — clear any in-progress demotion since the
        # member is now over-performing.
        if meets_next and next_tier is not None:
            q = quota_for(next_tier)
            self.db.set_member_tier(
                member_id,
                next_tier,
                now,
                new_daily_quota_tokens=q["tokens"],
                new_daily_quota_images=q["images"],
                new_daily_quota_voice_minutes=q["voice_minutes"],
            )
            log.info(json.dumps({
                "event": "member_promoted",
                "member_id": member_id,
                "from_tier": current_tier,
                "to_tier": next_tier,
            }))
            return "promoted"

        # Member meets current bar — clear any in-progress demotion
        # streak. They're fine to stay where they are.
        if meets_current:
            if below_since is not None:
                self.db.clear_member_below_threshold(member_id)
                return "grace_cleared"
            return "no_change"

        # Below current bar. If they're already at the floor we can't
        # demote them further; just leave the below-threshold mark in
        # place (or set it) so /me/contributor-status can warn them.
        demote_to = tier_below(current_tier)
        if demote_to is None:
            if below_since is None:
                self.db.mark_member_below_threshold(member_id, now)
                return "grace_started"
            return "no_change"

        # Start or continue the demotion countdown. Demote only after
        # DEMOTION_GRACE_DAYS of continuous below-threshold.
        if below_since is None:
            self.db.mark_member_below_threshold(member_id, now)
            log.info(json.dumps({
                "event": "member_below_threshold",
                "member_id": member_id,
                "tier": current_tier,
            }))
            return "grace_started"
        if (now - below_since) < DEMOTION_GRACE_DAYS * 86400.0:
            return "no_change"
        q = quota_for(demote_to)
        self.db.set_member_tier(
            member_id,
            demote_to,
            now,
            new_daily_quota_tokens=q["tokens"],
            new_daily_quota_images=q["images"],
            new_daily_quota_voice_minutes=q["voice_minutes"],
        )
        log.info(json.dumps({
            "event": "member_demoted",
            "member_id": member_id,
            "from_tier": current_tier,
            "to_tier": demote_to,
            "days_below_threshold": (now - below_since) / 86400.0,
        }))
        return "demoted"


__all__ = [
    "DEFAULT_SAMPLE_INTERVAL_MINUTES",
    "DEFAULT_ENGINE_INTERVAL_SECONDS",
    "DEMOTION_GRACE_DAYS",
    "STALE_WORKER_HIDE_DAYS",
    "TIER_LADDER",
    "TierEngine",
    "UptimeSampler",
]
