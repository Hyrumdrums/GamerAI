"""Slice 1 tests for the per-machine uptime schedule.

Two layers:
  * ``ScheduleMathTests`` — the pure window math in ``coordinator.schedule``
    (wrap-around windows, DST, paused/disabled short-circuits).
  * ``MachineScheduleE2ETests`` — real HTTP through ``TestClient`` with
    auth on: pairing→register link stamping, the merged /me/machines
    shape, PATCH ownership + validation, and the /jobs/next gate.

Run with ``python -m unittest tests.test_machine_schedule``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone

# 1. Env BEFORE imports — auth on, fresh DB, no rate limit.
_TMPDIR = tempfile.mkdtemp(prefix="gamerai-test-sched-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["API_TOKEN"] = "admin-seed-token-for-tests"
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("STRICT_MODELS", None)

# 2. Drop cached coordinator/shared modules so they pick up the env above.
for _mod in list(sys.modules):
    if _mod.split(".", 1)[0] in ("shared", "coordinator"):
        del sys.modules[_mod]

# 3. Patch the Redis factory with fakeredis.
import fakeredis  # noqa: E402

import coordinator.redis_client  # noqa: E402

_FAKE = fakeredis.FakeStrictRedis(decode_responses=True)
coordinator.redis_client.get_client = lambda: _FAKE  # type: ignore[assignment]

# 4. Import the app + helpers.
from fastapi.testclient import TestClient  # noqa: E402

from coordinator import admin as coordinator_admin  # noqa: E402
from coordinator import main as coordinator_main  # noqa: E402
from coordinator import member_auth  # noqa: E402
from coordinator import schedule as sched  # noqa: E402
from shared.config import job_queue_for  # noqa: E402

ADMIN_TOKEN = os.environ["API_TOKEN"]


class ScheduleMathTests(unittest.TestCase):
    def test_paused_blocks_regardless_of_window(self):
        self.assertFalse(
            sched.allowed_now(
                paused=True, sched_enabled=False,
                start_min=None, end_min=None, tz_name=None,
            )
        )

    def test_disabled_is_always_allowed(self):
        self.assertTrue(
            sched.allowed_now(
                paused=False, sched_enabled=False,
                start_min=0, end_min=0, tz_name="UTC",
            )
        )

    def test_same_day_window(self):
        # 09:00-17:00 UTC; check 09:30 inside, 08:30 outside.
        inside = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)
        outside = datetime(2026, 1, 15, 8, 30, tzinfo=timezone.utc)
        kw = dict(paused=False, sched_enabled=True,
                  start_min=9 * 60, end_min=17 * 60, tz_name="UTC")
        self.assertTrue(sched.allowed_now(now=inside, **kw))
        self.assertFalse(sched.allowed_now(now=outside, **kw))

    def test_overnight_wrap_window(self):
        # 22:00-06:00 UTC overnight.
        kw = dict(paused=False, sched_enabled=True,
                  start_min=22 * 60, end_min=6 * 60, tz_name="UTC")
        self.assertTrue(  # 23:00
            sched.allowed_now(now=datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc), **kw))
        self.assertTrue(  # 02:00
            sched.allowed_now(now=datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc), **kw))
        self.assertFalse(  # 12:00
            sched.allowed_now(now=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc), **kw))

    def test_dst_uses_local_wall_clock(self):
        # Window 09:00-17:00 America/Denver. 15:30 UTC is 08:30 MST
        # (winter, UTC-7 → outside) but 09:30 MDT (summer, UTC-6 →
        # inside). Same UTC instant-of-day, different decision: proves
        # we interpret the window in DST-correct local wall-clock.
        kw = dict(paused=False, sched_enabled=True,
                  start_min=9 * 60, end_min=17 * 60, tz_name="America/Denver")
        winter = datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, 15, 30, tzinfo=timezone.utc)
        self.assertFalse(sched.allowed_now(now=winter, **kw))
        self.assertTrue(sched.allowed_now(now=summer, **kw))

    def test_next_open_local_formats_start(self):
        # Outside a 09:00-17:00 window → sleeping until 09:00.
        out = datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(
            sched.next_open_local(start_min=9 * 60, end_min=17 * 60,
                                  tz_name="UTC", now=out),
            "09:00",
        )

    def test_next_open_none_when_inside(self):
        inside = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            sched.next_open_local(start_min=9 * 60, end_min=17 * 60,
                                  tz_name="UTC", now=inside))

    def test_valid_tz(self):
        self.assertTrue(sched.valid_tz("America/Denver"))
        self.assertFalse(sched.valid_tz("Mars/Olympus"))


class MachineScheduleE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.r = _FAKE
        cls.db = coordinator_main.db
        coordinator_main.ensure_admin_seed()

    def setUp(self):
        self.r.flushall()
        self.db._conn.executescript(
            "DELETE FROM jobs; DELETE FROM workers; DELETE FROM earnings; "
            "DELETE FROM member_usage; DELETE FROM member_tokens; "
            "DELETE FROM members WHERE role <> 'admin';"
        )

    # ---- helpers ----
    def _make_member(self) -> str:
        from contextlib import redirect_stdout
        from io import StringIO
        buf = StringIO()
        with redirect_stdout(buf):
            coordinator_admin.main(["create-member", "--role", "contributor"])
        for line in buf.getvalue().splitlines():
            if line.startswith("token="):
                return line.split("=", 1)[1].strip()
        raise AssertionError("no token from CLI")

    def _pair_machine(self, member_id: str, label: str = "agent") -> str:
        """Mint an agent bearer + member_tokens row directly (skips the
        browser handoff). Returns the raw agent token."""
        raw = member_auth.generate_token()
        self.db.add_member_token(
            member_auth.hash_token(raw), member_id, label, time.time(),
        )
        return raw

    def _member_id_for(self, token: str) -> str:
        return self.client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        ).json()["member_id"]

    def _agent_headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _register(self, agent_token: str, worker_id: str) -> None:
        resp = self.client.post(
            "/register", json={"worker_id": worker_id},
            headers=self._agent_headers(agent_token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _enqueue_chat_job(self) -> str:
        job_id = str(uuid.uuid4())
        self.r.rpush(job_queue_for("chat"), json.dumps({
            "job_id": job_id, "prompt": "hi", "model": "llama3.2:3b",
            "tool": "chat", "submitted_at": time.time(),
        }))
        return job_id

    def _next_job(self, agent_token: str, worker_id: str):
        return self.client.post(
            "/jobs/next", json={"worker_id": worker_id},
            headers=self._agent_headers(agent_token),
        ).json()

    # ---- tests ----
    def test_register_stamps_worker_link_and_machines_merges(self):
        member_token = self._make_member()
        member_id = self._member_id_for(member_token)
        agent = self._pair_machine(member_id)
        wid = f"win-DESKTOP-ABC-{uuid.uuid4().hex[:8]}"
        self._register(agent, wid)

        body = self.client.get(
            "/me/machines", headers={"Authorization": f"Bearer {member_token}"}
        ).json()
        self.assertEqual(len(body["machines"]), 1)
        m = body["machines"][0]
        # One merged row: the pairing record now carries its worker_id +
        # a derived friendly name (hostname out of win-<host>-<rand>).
        self.assertEqual(m["worker_id"], wid)
        self.assertEqual(m["name"], "DESKTOP-ABC")
        self.assertIn("schedule", m)
        self.assertTrue(m["allowed_now"])  # no schedule yet → allowed

    def test_machines_surfaces_gpu_hardware_from_capabilities(self):
        member_token = self._make_member()
        member_id = self._member_id_for(member_token)
        agent = self._pair_machine(member_id)
        wid = f"win-DESKTOP-GPU-{uuid.uuid4().hex[:8]}"
        resp = self.client.post(
            "/register",
            json={
                "worker_id": wid,
                "capabilities": {"gpu_model": "RTX 4090", "vram_gb": 24.0},
            },
            headers=self._agent_headers(agent),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        m = self.client.get(
            "/me/machines", headers={"Authorization": f"Bearer {member_token}"}
        ).json()["machines"][0]
        self.assertEqual(m["gpu_model"], "RTX 4090")
        self.assertEqual(m["vram_gb"], 24.0)

    def test_patch_schedule_persists_and_round_trips(self):
        member_token = self._make_member()
        member_id = self._member_id_for(member_token)
        agent = self._pair_machine(member_id)
        wid = f"win-PC-{uuid.uuid4().hex[:8]}"
        self._register(agent, wid)
        mid = self.client.get(
            "/me/machines", headers={"Authorization": f"Bearer {member_token}"}
        ).json()["machines"][0]["id"]

        resp = self.client.patch(
            f"/me/machines/{mid}/schedule",
            json={"paused": False, "enabled": True,
                  "start_min": 60, "end_min": 540, "tz": "America/Denver"},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["schedule"]["enabled"])
        self.assertEqual(body["schedule"]["start_min"], 60)
        self.assertEqual(body["schedule"]["tz"], "America/Denver")

    def test_patch_rejects_bad_timezone(self):
        member_token = self._make_member()
        member_id = self._member_id_for(member_token)
        agent = self._pair_machine(member_id)
        self._register(agent, f"win-PC-{uuid.uuid4().hex[:8]}")
        mid = self.client.get(
            "/me/machines", headers={"Authorization": f"Bearer {member_token}"}
        ).json()["machines"][0]["id"]
        resp = self.client.patch(
            f"/me/machines/{mid}/schedule",
            json={"enabled": True, "start_min": 60, "end_min": 540,
                  "tz": "Mars/Olympus"},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_patch_rejects_enabled_without_window(self):
        member_token = self._make_member()
        member_id = self._member_id_for(member_token)
        agent = self._pair_machine(member_id)
        self._register(agent, f"win-PC-{uuid.uuid4().hex[:8]}")
        mid = self.client.get(
            "/me/machines", headers={"Authorization": f"Bearer {member_token}"}
        ).json()["machines"][0]["id"]
        resp = self.client.patch(
            f"/me/machines/{mid}/schedule",
            json={"enabled": True},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_patch_other_members_machine_is_404(self):
        owner_token = self._make_member()
        owner_id = self._member_id_for(owner_token)
        agent = self._pair_machine(owner_id)
        self._register(agent, f"win-PC-{uuid.uuid4().hex[:8]}")
        mid = self.client.get(
            "/me/machines", headers={"Authorization": f"Bearer {owner_token}"}
        ).json()["machines"][0]["id"]

        attacker_token = self._make_member()
        resp = self.client.patch(
            f"/me/machines/{mid}/schedule",
            json={"paused": True},
            headers={"Authorization": f"Bearer {attacker_token}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_jobs_next_gated_when_paused(self):
        member_token = self._make_member()
        member_id = self._member_id_for(member_token)
        agent = self._pair_machine(member_id)
        wid = f"win-PC-{uuid.uuid4().hex[:8]}"
        self._register(agent, wid)
        mid = self.client.get(
            "/me/machines", headers={"Authorization": f"Bearer {member_token}"}
        ).json()["machines"][0]["id"]

        self._enqueue_chat_job()
        # Pause → even with a job queued, the worker gets nothing.
        self.client.patch(
            f"/me/machines/{mid}/schedule", json={"paused": True},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertIsNone(self._next_job(agent, wid)["job"])

        # Unpause → the same queued job is now claimable.
        self.client.patch(
            f"/me/machines/{mid}/schedule", json={"paused": False},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertIsNotNone(self._next_job(agent, wid)["job"])

    def test_jobs_next_gated_outside_window(self):
        member_token = self._make_member()
        member_id = self._member_id_for(member_token)
        agent = self._pair_machine(member_id)
        wid = f"win-PC-{uuid.uuid4().hex[:8]}"
        self._register(agent, wid)
        mid = self.client.get(
            "/me/machines", headers={"Authorization": f"Bearer {member_token}"}
        ).json()["machines"][0]["id"]

        # Build a 1-hour UTC window starting one hour from now → "now"
        # is deterministically outside it.
        cur = datetime.now(timezone.utc)
        cur_min = cur.hour * 60 + cur.minute
        start = (cur_min + 60) % 1440
        end = (cur_min + 120) % 1440
        self._enqueue_chat_job()
        self.client.patch(
            f"/me/machines/{mid}/schedule",
            json={"enabled": True, "start_min": start, "end_min": end, "tz": "UTC"},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertIsNone(self._next_job(agent, wid)["job"])

    def test_jobs_next_tokenless_worker_always_allowed(self):
        # The in-VPS server worker authenticates with the admin token,
        # which isn't a member_tokens row → no schedule → never gated.
        wid = f"vps-worker-{uuid.uuid4().hex[:8]}"
        self.client.post(
            "/register", json={"worker_id": wid},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self._enqueue_chat_job()
        job = self.client.post(
            "/jobs/next", json={"worker_id": wid},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        ).json()
        self.assertIsNotNone(job["job"])


if __name__ == "__main__":
    unittest.main()
