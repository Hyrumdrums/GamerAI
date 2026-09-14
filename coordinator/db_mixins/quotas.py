"""``DB`` member-usage / worker-uptime / tier-engine-state methods —
coordinator/db.py god-file split. Mixin composition (not delegation):
every method keeps referencing ``self._conn``/``self._lock`` exactly
as before, since ``self`` still resolves to the composed ``DB``
instance via MRO — a pure cut/paste, no call-site changes needed
anywhere else in the coordinator. See coordinator/db.py for the full
``class DB(...)`` composition.
"""
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class QuotasMixin:
    # ---------- member usage ----------
    def add_member_usage(
        self,
        member_id: str,
        when: float,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        day = _utc_day(when)
        with self._lock:
            self._conn.execute(
                "INSERT INTO member_usage (member_id, day, tokens_in, tokens_out, jobs) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(member_id, day) DO UPDATE SET "
                "tokens_in  = tokens_in  + excluded.tokens_in, "
                "tokens_out = tokens_out + excluded.tokens_out, "
                "jobs       = jobs       + 1",
                (member_id, day, tokens_in, tokens_out),
            )

    def add_member_image_usage(
        self,
        member_id: str,
        when: float,
        units: float,
    ) -> None:
        """Credit ``units`` to the submitter's image-units rollup. Called
        from /jobs/complete on successful image jobs in place of the
        old synthetic 200-token credit, so the token ledger only tracks
        real chat tokens and the image quota is enforced against its
        own column. Always increments ``jobs`` by 1 — the rollup counts
        every credited job regardless of tool, mirroring the chat
        path."""
        day = _utc_day(when)
        with self._lock:
            self._conn.execute(
                "INSERT INTO member_usage "
                "(member_id, day, tokens_in, tokens_out, image_units, jobs) "
                "VALUES (?, ?, 0, 0, ?, 1) "
                "ON CONFLICT(member_id, day) DO UPDATE SET "
                "image_units = image_units + excluded.image_units, "
                "jobs        = jobs        + 1",
                (member_id, day, units),
            )

    def add_member_voice_usage(
        self,
        member_id: str,
        when: float,
        seconds: float,
    ) -> None:
        """Credit ``seconds`` of synthesised (or future: transcribed)
        audio to the submitter's voice rollup. Same shape as
        add_member_image_usage: own column on member_usage, jobs+1 per
        call. Stored as seconds (REAL) — the /generate gate divides by
        60 to compare against the daily-minute cap. Sub-second
        sentences from client-side segmentation must accumulate
        accurately, so int-minutes would round away most of a chatty
        conversation's usage."""
        day = _utc_day(when)
        with self._lock:
            self._conn.execute(
                "INSERT INTO member_usage "
                "(member_id, day, tokens_in, tokens_out, voice_seconds, jobs) "
                "VALUES (?, ?, 0, 0, ?, 1) "
                "ON CONFLICT(member_id, day) DO UPDATE SET "
                "voice_seconds = voice_seconds + excluded.voice_seconds, "
                "jobs          = jobs          + 1",
                (member_id, day, seconds),
            )

    def member_usage_today(self, member_id: str, now: Optional[float] = None) -> dict:
        when = now if now is not None else time.time()
        day = _utc_day(when)
        with self._lock:
            cur = self._conn.execute(
                "SELECT tokens_in, tokens_out, image_units, voice_seconds, jobs "
                "FROM member_usage WHERE member_id=? AND day=?",
                (member_id, day),
            )
            row = cur.fetchone()
        if row is None:
            return {
                "day": day,
                "tokens_in": 0,
                "tokens_out": 0,
                "image_units": 0.0,
                "voice_seconds": 0.0,
                "jobs": 0,
            }
        # image_units / voice_seconds may be missing on a row predating
        # their respective migrations — SELECT against a freshly migrated
        # DB always returns the columns, but the row keys() check is
        # cheaper than a try/except and keeps test fixtures happy.
        keys = row.keys()
        return {
            "day": day,
            "tokens_in": int(row["tokens_in"]),
            "tokens_out": int(row["tokens_out"]),
            "image_units": (
                float(row["image_units"]) if "image_units" in keys else 0.0
            ),
            "voice_seconds": (
                float(row["voice_seconds"]) if "voice_seconds" in keys else 0.0
            ),
            "jobs": int(row["jobs"]),
        }

    def member_earnings(self, member_id: str) -> dict:
        """Aggregate earnings across every worker owned by this member.
        Returns ``{total_tokens, total_jobs, total_usd, machine_count}``.

        Bridges the structurally split ledgers: ``earnings`` is keyed
        on ``worker_id``, ``workers.owner_member_id`` ties each machine
        back to its owner. Aggregated at read time so a member adding
        or unpairing a machine is reflected immediately without any
        backfill pass. ``machine_count`` is the count of distinct owned
        workers that have any earnings rows (zero on the common
        "paired-but-never-earned" path)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT "
                "COALESCE(SUM(e.total_tokens), 0) AS total_tokens, "
                "COALESCE(SUM(e.total_jobs),   0) AS total_jobs, "
                "COALESCE(SUM(e.total_usd),    0) AS total_usd, "
                "COUNT(*)                       AS machine_count "
                "FROM earnings e "
                "JOIN workers w ON w.worker_id = e.worker_id "
                "WHERE w.owner_member_id = ?",
                (member_id,),
            )
            row = cur.fetchone()
        if row is None:
            return {
                "total_tokens": 0,
                "total_jobs": 0,
                "total_usd": 0.0,
                "machine_count": 0,
            }
        return {
            "total_tokens": int(row["total_tokens"]),
            "total_jobs": int(row["total_jobs"]),
            "total_usd": round(float(row["total_usd"]), 8),
            "machine_count": int(row["machine_count"]),
        }

    # ---------- worker uptime ----------
    def add_worker_uptime_minutes(
        self,
        worker_id: str,
        when: float,
        minutes: int,
    ) -> None:
        """Credit ``minutes`` of online time to ``worker_id`` for the
        UTC day containing ``when``. Called by the UptimeSampler every
        N minutes for each worker that heartbeated within the window.
        Idempotent INSERT-OR-UPDATE so a sampler restart can't double-
        count or skip — the worst case is a sample missed during the
        downtime window, which is acceptable for tier-promotion
        granularity."""
        day = _utc_day(when)
        with self._lock:
            self._conn.execute(
                "INSERT INTO worker_uptime (worker_id, day, minutes_online) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(worker_id, day) DO UPDATE SET "
                "minutes_online = minutes_online + excluded.minutes_online",
                (worker_id, day, int(minutes)),
            )

    def member_uptime_summary(
        self,
        member_id: str,
        window_days: int = 7,
        now: Optional[float] = None,
    ) -> dict:
        """Aggregate the last ``window_days`` of uptime across every
        worker the member owns. Returns:

        - ``days_online``: number of distinct UTC days in the window
          where at least one owned worker logged ≥60 minutes (1 hour).
          The hour threshold filters out flaky 15-min bursts that
          shouldn't count as "online for the day."
        - ``avg_hours_per_active_day``: average hours of total owned-
          worker uptime on the days that counted as online. A member
          with two PCs each online 4 hours scores 8 hours that day —
          the network sees them as 8 worker-hours of capacity.
        - ``total_minutes``: raw sum across the window (for display).

        Returns zeroes for a member with no owned workers — the engine
        treats them as "not meeting any tier" and keeps them at
        BRONZE without demotion (BRONZE is the floor; can't go
        lower)."""
        when = now if now is not None else time.time()
        # Build the inclusive list of UTC days in the window.
        days = []
        for i in range(window_days):
            ts = when - i * 86400.0
            days.append(_utc_day(ts))
        placeholders = ",".join(["?"] * len(days))
        with self._lock:
            cur = self._conn.execute(
                f"SELECT u.day AS day, SUM(u.minutes_online) AS total_min "
                f"FROM worker_uptime u "
                f"JOIN workers w ON w.worker_id = u.worker_id "
                f"WHERE w.owner_member_id = ? AND u.day IN ({placeholders}) "
                f"GROUP BY u.day",
                (member_id, *days),
            )
            rows = cur.fetchall()
        if not rows:
            return {
                "window_days": window_days,
                "days_online": 0,
                "avg_hours_per_active_day": 0.0,
                "total_minutes": 0,
            }
        # An "online day" is any day where the member's combined
        # owned-worker uptime is ≥ 60 minutes. Below that we treat
        # the day as a fluke and don't credit it.
        ONLINE_DAY_THRESHOLD_MIN = 60
        active_days = [r for r in rows if int(r["total_min"] or 0) >= ONLINE_DAY_THRESHOLD_MIN]
        total_minutes = sum(int(r["total_min"] or 0) for r in rows)
        if not active_days:
            return {
                "window_days": window_days,
                "days_online": 0,
                "avg_hours_per_active_day": 0.0,
                "total_minutes": total_minutes,
            }
        avg_min = sum(int(r["total_min"] or 0) for r in active_days) / len(active_days)
        return {
            "window_days": window_days,
            "days_online": len(active_days),
            "avg_hours_per_active_day": round(avg_min / 60.0, 2),
            "total_minutes": total_minutes,
        }

    def prune_worker_uptime(self, older_than_days: int = 60) -> int:
        """Drop uptime rows older than ``older_than_days``. Called by
        the daily engine to keep the table bounded — we only ever
        look back 7 days, so 60-day retention gives plenty of
        headroom for debugging. Returns the row count deleted."""
        cutoff_ts = time.time() - older_than_days * 86400.0
        cutoff_day = _utc_day(cutoff_ts)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM worker_uptime WHERE day < ?", (cutoff_day,),
            )
            return cur.rowcount

    # ---------- tier engine state ----------
    def set_member_tier(
        self,
        member_id: str,
        new_tier: str,
        when: float,
        new_daily_quota_tokens: Optional[int] = None,
        new_daily_quota_images: Optional[int] = None,
        new_daily_quota_voice_minutes: Optional[int] = None,
    ) -> None:
        """Set a member's tier and stamp ``tier_last_changed_at`` so the
        next engine pass enforces the 1-move-per-day cap. The caller
        passes the new tier's quota defaults so the quotas track the
        tier change atomically — no risk of a half-moved member with
        a new tier but stale quotas. Clears
        ``tier_below_threshold_since`` because the situation changed
        (either promoted out of below-threshold, or demoted into a
        looser bar)."""
        with self._lock:
            self._conn.execute(
                "UPDATE members SET "
                "tier=?, "
                "daily_quota_tokens=?, "
                "daily_quota_images=?, "
                "daily_quota_voice_minutes=?, "
                "tier_last_changed_at=?, "
                "tier_below_threshold_since=NULL "
                "WHERE member_id=?",
                (
                    new_tier,
                    new_daily_quota_tokens,
                    new_daily_quota_images,
                    new_daily_quota_voice_minutes,
                    when,
                    member_id,
                ),
            )

    def mark_member_below_threshold(self, member_id: str, when: float) -> None:
        """Start the demotion countdown for ``member_id``. Only writes
        if ``tier_below_threshold_since`` is currently NULL — the
        countdown is a single sustained streak, not a rolling clock,
        so repeated calls during the grace window don't restart it."""
        with self._lock:
            self._conn.execute(
                "UPDATE members SET tier_below_threshold_since=? "
                "WHERE member_id=? AND tier_below_threshold_since IS NULL",
                (when, member_id),
            )

    def clear_member_below_threshold(self, member_id: str) -> None:
        """Cancel an in-progress demotion countdown. Called by the
        engine when a previously below-threshold member is observed
        meeting their tier's bar again — a single passing day forgives
        the streak."""
        with self._lock:
            self._conn.execute(
                "UPDATE members SET tier_below_threshold_since=NULL "
                "WHERE member_id=?",
                (member_id,),
            )

    def list_members_for_tier_engine(self) -> list[sqlite3.Row]:
        """Members the tier engine should evaluate: non-admin, not
        revoked. Admins are unbounded by design and shouldn't be
        re-tiered. Revoked members are already barred from consuming;
        no need to spend cycles on them."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM members "
                "WHERE role <> 'admin' AND revoked_at IS NULL "
                "ORDER BY member_id"
            )
            return cur.fetchall()
