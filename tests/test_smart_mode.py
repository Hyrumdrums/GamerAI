"""Smart-mode routing tests (coordinator side).

Smart mode = chat jobs targeting a smart-tier model (the 14B served by
a multi-machine llama.cpp RPC pipeline). The envelope keeps
``tool="chat"`` so every downstream chat path (partials, completion,
earnings, conversations) is untouched; only the QUEUE differs — jobs
ride ``job_queue:chat:smart`` and only workers advertising the
``chat:smart`` tool pick them up.

Covers: the registry tier plumbing, /generate's smart-flag resolution
and queue placement, the worker round-trip, and — the easy thing to
regress — that every requeue path (reaper, abandon) sends a smart job
back to the smart queue instead of letting a 3B chat worker grab it.

Same import bootstrap as test_coordinator_e2e (fakeredis patched in
before ``coordinator.main`` imports).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import uuid

# 1. Test-only env. Must be set BEFORE coordinator imports so module-level
#    code reads the right values.
_TMPDIR = tempfile.mkdtemp(prefix="gamerai-smart-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.pop("API_TOKEN", None)
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("STRICT_MODELS", None)

# 2. Drop any cached modules so they re-read the env above. Purge by
#    top-level package name (the bare "coordinator" too, not just its
#    submodules) — otherwise `from coordinator import main` returns the
#    PREVIOUS test module's main via the stale package attribute,
#    bound to that module's fakeredis instance.
for _mod in list(sys.modules):
    if _mod.split(".", 1)[0] in ("shared", "coordinator"):
        del sys.modules[_mod]

# 3. Patch the Redis factory before main imports it.
import fakeredis  # noqa: E402

import coordinator.redis_client  # noqa: E402

_FAKE = fakeredis.FakeStrictRedis(decode_responses=True)
coordinator.redis_client.get_client = lambda: _FAKE  # type: ignore[assignment]

# 4. Now safe to import main + scheduler.
from fastapi.testclient import TestClient  # noqa: E402

from coordinator import main as coordinator_main  # noqa: E402
from coordinator import model_registry  # noqa: E402
from coordinator.scheduler import Reaper  # noqa: E402
from shared.config import job_queue_for  # noqa: E402

SMART_QUEUE = "job_queue:chat:smart"
SMART_MODEL = model_registry.DEFAULT_SMART_MODEL


class RegistryTierTests(unittest.TestCase):
    def test_default_smart_model_is_registered_smart_chat(self):
        m = model_registry.get(SMART_MODEL)
        self.assertIsNotNone(m)
        self.assertEqual(m.kind, "chat")
        self.assertEqual(m.tier, "smart")

    def test_standard_models_are_standard_tier(self):
        self.assertEqual(model_registry.get("llama3.2:3b").tier, "standard")
        self.assertFalse(model_registry.is_smart_model("llama3.2:3b"))
        self.assertFalse(model_registry.is_smart_model(None))
        self.assertFalse(model_registry.is_smart_model("totally-fake"))

    def test_image_models_never_smart_even_if_mislabeled(self):
        # is_smart_model double-checks kind == "chat".
        self.assertFalse(model_registry.is_smart_model("sdxl"))

    def test_route_for(self):
        self.assertEqual(model_registry.route_for("chat", SMART_MODEL), "chat:smart")
        self.assertEqual(model_registry.route_for("chat", "llama3.2:3b"), "chat")
        self.assertEqual(model_registry.route_for("chat", None), "chat")
        # Non-chat tools never reroute, whatever the model says.
        self.assertEqual(model_registry.route_for("search", SMART_MODEL), "search")
        self.assertEqual(model_registry.route_for("image", SMART_MODEL), "image")

    def test_to_dict_carries_tier(self):
        d = model_registry.get(SMART_MODEL).to_dict()
        self.assertEqual(d["tier"], "smart")

    def test_queue_key(self):
        self.assertEqual(job_queue_for("chat:smart"), SMART_QUEUE)
        # And the standard mappings are untouched.
        self.assertEqual(job_queue_for("chat"), "job_queue")
        self.assertEqual(job_queue_for("image"), "job_queue:image")


class SmartRoutingE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.r = _FAKE
        cls.db = coordinator_main.db

    def setUp(self):
        self.r.flushall()
        self.db._conn.executescript(
            "DELETE FROM jobs; DELETE FROM workers; DELETE FROM earnings;"
        )

    # ---------- enqueue placement ----------
    def test_smart_flag_routes_to_smart_queue_with_default_model(self):
        resp = self.client.post(
            "/generate", json={"prompt": "deep question", "smart": True},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.r.llen("job_queue"), 0)
        raw = self.r.lpop(SMART_QUEUE)
        self.assertIsNotNone(raw)
        job = json.loads(raw)
        self.assertEqual(job["tool"], "chat")  # tool unchanged on purpose
        self.assertEqual(job["route"], "chat:smart")
        self.assertEqual(job["model"], SMART_MODEL)

    def test_explicit_smart_model_routes_without_flag(self):
        resp = self.client.post(
            "/generate", json={"prompt": "hi", "model": SMART_MODEL},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.r.llen(SMART_QUEUE), 1)

    def test_plain_chat_still_rides_legacy_queue(self):
        resp = self.client.post("/generate", json={"prompt": "hi"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.r.llen(SMART_QUEUE), 0)
        job = json.loads(self.r.lpop("job_queue"))
        self.assertNotIn("route", job)

    def test_smart_flag_ignored_for_non_chat_tools(self):
        resp = self.client.post(
            "/generate",
            json={"prompt": "kittens", "tool": "image", "smart": True},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.r.llen(SMART_QUEUE), 0)
        self.assertEqual(self.r.llen("job_queue:image"), 1)

    # ---------- worker round-trip ----------
    def _register(self, worker_id: str, tools):
        resp = self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"models": [SMART_MODEL], "tools": tools},
            },
        )
        self.assertEqual(resp.status_code, 200)

    def test_smart_worker_round_trip(self):
        job_id = self.client.post(
            "/generate", json={"prompt": "explain GPUs", "smart": True},
        ).json()["job_id"]

        head = "wkr-head-" + uuid.uuid4().hex[:6]
        self._register(head, ["chat:smart"])
        out = self.client.post(
            "/jobs/next", json={"worker_id": head, "tools": ["chat:smart"]},
        ).json()
        self.assertIsNotNone(out["job"])
        self.assertEqual(out["job"]["job_id"], job_id)

        done = self.client.post("/jobs/complete", json={
            "worker_id": head,
            "job_id": job_id,
            "text": "GPUs go brrr, at length",
            "model": SMART_MODEL,
            "prompt_tokens": 10,
            "completion_tokens": 50,
            "duration_seconds": 12.0,
            "status": "complete",
            "claim_token": out["claim_token"],
        })
        self.assertEqual(done.status_code, 200)
        result = self.client.get(f"/result/{job_id}").json()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["model"], SMART_MODEL)

    def test_standard_chat_worker_never_sees_smart_jobs(self):
        self.client.post("/generate", json={"prompt": "hi", "smart": True})
        plain = "wkr-plain-" + uuid.uuid4().hex[:6]
        self._register(plain, ["chat"])
        out = self.client.post(
            "/jobs/next", json={"worker_id": plain, "tools": ["chat"]},
        ).json()
        self.assertIsNone(out["job"])
        # And asking for the smart queue without advertising it is
        # filtered out by the coordinator-side capability gate.
        out2 = self.client.post(
            "/jobs/next", json={"worker_id": plain, "tools": ["chat:smart"]},
        ).json()
        self.assertIsNone(out2["job"])
        self.assertEqual(self.r.llen(SMART_QUEUE), 1)

    # ---------- requeue paths keep the smart queue ----------
    def test_abandon_requeues_to_smart_queue(self):
        self.client.post("/generate", json={"prompt": "hi", "smart": True})
        head = "wkr-head-" + uuid.uuid4().hex[:6]
        self._register(head, ["chat:smart"])
        out = self.client.post(
            "/jobs/next", json={"worker_id": head, "tools": ["chat:smart"]},
        ).json()
        job_id = out["job"]["job_id"]
        resp = self.client.post("/jobs/abandon", json={
            "worker_id": head,
            "job_id": job_id,
            "claim_token": out["claim_token"],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.r.llen("job_queue"), 0)
        requeued = json.loads(self.r.lpop(SMART_QUEUE))
        self.assertEqual(requeued["job_id"], job_id)
        self.assertEqual(requeued["route"], "chat:smart")

    def test_reaper_requeues_to_smart_queue(self):
        self.client.post("/generate", json={"prompt": "hi", "smart": True})
        head = "wkr-head-" + uuid.uuid4().hex[:6]
        self._register(head, ["chat:smart"])
        out = self.client.post(
            "/jobs/next", json={"worker_id": head, "tools": ["chat:smart"]},
        ).json()
        job_id = out["job"]["job_id"]
        # Expire the claim deadline, silence the worker, tick the reaper.
        meta = json.loads(self.r.hget("job_processing", job_id))
        meta["deadline"] = time.time() - 1
        self.r.hset("job_processing", job_id, json.dumps(meta))
        self.r.hdel("worker_heartbeats", head)
        Reaper(self.r, self.db)._tick()
        self.assertEqual(self.r.llen("job_queue"), 0)
        requeued = json.loads(self.r.lpop(SMART_QUEUE))
        self.assertEqual(requeued["job_id"], job_id)

    def test_job_row_envelope_rebuild_derives_smart_route(self):
        # The DB-row rebuild path (claim raced with reaper / abandon
        # before claim) must re-derive the route from the persisted
        # model, since the original envelope is gone.
        job_id = self.client.post(
            "/generate", json={"prompt": "hi", "smart": True},
        ).json()["job_id"]
        row = self.db.get_job(job_id)
        env = coordinator_main._job_row_to_envelope(row)
        self.assertEqual(env.get("route"), "chat:smart")
        plain_id = self.client.post(
            "/generate", json={"prompt": "hi"},
        ).json()["job_id"]
        env2 = coordinator_main._job_row_to_envelope(self.db.get_job(plain_id))
        self.assertNotIn("route", env2)

    # ---------- liveness gate ----------
    def test_live_worker_gate_distinguishes_smart_pool(self):
        prev = coordinator_main.REQUIRE_LIVE_WORKER
        coordinator_main.REQUIRE_LIVE_WORKER = True
        try:
            # A smart BACKEND (explicit empty tools) heartbeats — it
            # must satisfy NEITHER the chat pool nor the smart pool.
            backend = "wkr-backend-" + uuid.uuid4().hex[:6]
            self._register(backend, [])
            self.client.post("/heartbeat", json={
                "worker_id": backend, "status": "idle",
            })
            r1 = self.client.post(
                "/generate", json={"prompt": "hi", "smart": True},
            )
            self.assertEqual(r1.status_code, 503)
            r2 = self.client.post("/generate", json={"prompt": "hi"})
            self.assertEqual(r2.status_code, 503)

            # A head comes online → smart passes, plain chat still 503.
            head = "wkr-head-" + uuid.uuid4().hex[:6]
            self._register(head, ["chat:smart"])
            self.client.post("/heartbeat", json={
                "worker_id": head, "status": "idle",
            })
            r3 = self.client.post(
                "/generate", json={"prompt": "hi", "smart": True},
            )
            self.assertEqual(r3.status_code, 200)
            r4 = self.client.post("/generate", json={"prompt": "hi"})
            self.assertEqual(r4.status_code, 503)
        finally:
            coordinator_main.REQUIRE_LIVE_WORKER = prev


if __name__ == "__main__":
    unittest.main()
