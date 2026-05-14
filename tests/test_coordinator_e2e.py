"""End-to-end-ish tests that drive the FastAPI coordinator over HTTP, with
``fakeredis`` standing in for Redis and a tempfile-backed SQLite DB.

The key trick: ``coordinator.main`` runs Redis-touching code at import
time (``r = get_client()``), so we patch
``coordinator.redis_client.get_client`` *before* importing main, and we
clear ``shared.*`` and ``coordinator.*`` modules out of ``sys.modules``
first so they pick up the test-only env vars (DB_PATH, no auth, no rate
limit, etc.). After that the test is just a normal HTTP client driving
the real app.

Lifespan (and therefore the reaper thread) does not run when ``TestClient``
is used as a plain instance, so the reaper is exercised by calling
``Reaper._tick()`` directly — no flaky time-dependent waits.

Run with ``python -m unittest tests.test_coordinator_e2e``.
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
_TMPDIR = tempfile.mkdtemp(prefix="gamerai-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.pop("API_TOKEN", None)
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("STRICT_MODELS", None)

# 2. Drop any cached modules so they re-read the env above.
for _mod in list(sys.modules):
    if _mod.startswith(("shared.", "coordinator.")):
        del sys.modules[_mod]

# 3. Patch the Redis factory before main imports it.
import fakeredis  # noqa: E402

import coordinator.redis_client  # noqa: E402

_FAKE = fakeredis.FakeStrictRedis(decode_responses=True)
coordinator.redis_client.get_client = lambda: _FAKE  # type: ignore[assignment]

# 4. Now safe to import main + scheduler.
from fastapi.testclient import TestClient  # noqa: E402

from coordinator import main as coordinator_main  # noqa: E402
from coordinator.scheduler import Reaper  # noqa: E402


def _job_complete_payload(worker_id: str, job_id: str, *, tokens: int = 7) -> dict:
    return {
        "worker_id": worker_id,
        "job_id": job_id,
        "text": "[mock] hello",
        "model": "mock",
        "prompt_tokens": 5,
        "completion_tokens": tokens,
        "duration_seconds": 0.1,
        "status": "complete",
    }


class CoordinatorE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.r = _FAKE
        cls.db = coordinator_main.db

    def setUp(self):
        # Fresh state per test: wipe Redis and SQLite tables.
        self.r.flushall()
        self.db._conn.executescript(
            "DELETE FROM jobs; DELETE FROM workers; DELETE FROM earnings;"
        )

    # ------------------------------------------------------------------
    # smoke
    # ------------------------------------------------------------------
    def test_health_open_to_anyone(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_models_endpoint_lists_catalog(self):
        resp = self.client.get("/models")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["strict"])
        names = [m["name"] for m in body["models"]]
        self.assertIn("mock", names)
        self.assertIn("llama3.1:70b", names)

    # ------------------------------------------------------------------
    # job round-trip
    # ------------------------------------------------------------------
    def test_generate_then_worker_loop_credits_earnings(self):
        # Customer submits a job.
        resp = self.client.post("/generate", json={"prompt": "hello"})
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()["job_id"]

        # Pending state visible to /result.
        first = self.client.get(f"/result/{job_id}").json()
        self.assertEqual(first["status"], "pending")

        # Simulate a worker: register, claim, complete.
        worker_id = "wkr-" + uuid.uuid4().hex[:6]
        self.assertEqual(self.client.post("/register",
            json={"worker_id": worker_id}).status_code, 200)

        # The job is on the queue; pop it (mimics worker BLPOP).
        raw = self.r.lpop("job_queue")
        self.assertIsNotNone(raw)
        job = json.loads(raw)
        self.assertEqual(job["job_id"], job_id)

        self.assertEqual(self.client.post("/jobs/claim",
            json={"worker_id": worker_id, "job_id": job_id}).status_code, 200)
        complete = self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(worker_id, job_id, tokens=11),
        )
        self.assertEqual(complete.status_code, 200)
        self.assertGreater(complete.json()["earnings"], 0)

        # Result endpoint now shows complete with positive token count.
        final = self.client.get(f"/result/{job_id}").json()
        self.assertEqual(final["status"], "complete")
        self.assertEqual(final["worker_id"], worker_id)
        self.assertEqual(final["completion_tokens"], 11)

        # Earnings ledger reflects the job.
        earnings = self.client.get(f"/earnings/{worker_id}").json()
        self.assertEqual(earnings["total_tokens"], 11)
        self.assertGreater(earnings["total_usd"], 0)

    def test_generate_rejects_empty_prompt(self):
        resp = self.client.post("/generate", json={"prompt": "   "})
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------------
    # no-workers-available gate (REQUIRE_LIVE_WORKER)
    # ------------------------------------------------------------------
    def test_generate_503s_when_required_and_no_workers(self):
        # Toggle the live-worker gate via the module-level binding —
        # the helper reads the name out of coordinator.main globals,
        # so a patch here is sufficient without re-importing config.
        original = coordinator_main.REQUIRE_LIVE_WORKER
        coordinator_main.REQUIRE_LIVE_WORKER = True
        try:
            resp = self.client.post("/generate", json={"prompt": "hi"})
            self.assertEqual(resp.status_code, 503)
            self.assertIn(
                "No community members are available",
                resp.json()["detail"],
            )
            # No job should have been queued or persisted.
            self.assertEqual(self.r.llen("job_queue"), 0)
        finally:
            coordinator_main.REQUIRE_LIVE_WORKER = original

    def test_generate_succeeds_when_required_and_worker_is_live(self):
        original = coordinator_main.REQUIRE_LIVE_WORKER
        coordinator_main.REQUIRE_LIVE_WORKER = True
        try:
            self.assertEqual(
                self.client.post(
                    "/register", json={"worker_id": "wkr-live"}
                ).status_code,
                200,
            )
            resp = self.client.post("/generate", json={"prompt": "hi"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(self.r.llen("job_queue"), 1)
        finally:
            coordinator_main.REQUIRE_LIVE_WORKER = original

    def test_generate_skips_gate_when_disabled(self):
        # Default config: gate off, no workers, /generate still works.
        self.assertFalse(coordinator_main.REQUIRE_LIVE_WORKER)
        resp = self.client.post("/generate", json={"prompt": "hi"})
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------------
    # idempotency
    # ------------------------------------------------------------------
    def test_idempotency_key_returns_same_job_id(self):
        key = "idem-" + uuid.uuid4().hex
        first = self.client.post(
            "/generate", json={"prompt": "hi"}, headers={"Idempotency-Key": key}
        ).json()["job_id"]
        second = self.client.post(
            "/generate", json={"prompt": "different prompt"},
            headers={"Idempotency-Key": key},
        ).json()["job_id"]
        self.assertEqual(first, second)

    def test_idempotency_only_queues_one_job(self):
        key = "idem-" + uuid.uuid4().hex
        for _ in range(3):
            self.client.post(
                "/generate", json={"prompt": "x"},
                headers={"Idempotency-Key": key},
            )
        self.assertEqual(self.r.llen("job_queue"), 1)

    def test_no_idempotency_header_creates_distinct_jobs(self):
        a = self.client.post("/generate", json={"prompt": "p"}).json()["job_id"]
        b = self.client.post("/generate", json={"prompt": "p"}).json()["job_id"]
        self.assertNotEqual(a, b)

    # ------------------------------------------------------------------
    # worker capabilities
    # ------------------------------------------------------------------
    def test_register_with_capabilities_surfaces_in_workers(self):
        worker_id = "wkr-cap"
        body = {
            "worker_id": worker_id,
            "capabilities": {
                "vram_gb": 24.0,
                "gpu_model": "RTX 4090",
                "bandwidth_class": "high",
                "models": ["llama3.1:8b", "mistral:7b"],
            },
        }
        self.assertEqual(self.client.post("/register", json=body).status_code, 200)

        listing = self.client.get("/workers").json()["workers"]
        rec = next(w for w in listing if w["worker_id"] == worker_id)
        self.assertIsNotNone(rec["capabilities"])
        self.assertEqual(rec["capabilities"]["gpu_model"], "RTX 4090")
        self.assertEqual(rec["capabilities"]["models"], ["llama3.1:8b", "mistral:7b"])

    def test_register_without_capabilities_still_works(self):
        # legacy worker.py just sends {"worker_id": "..."}
        self.assertEqual(
            self.client.post("/register", json={"worker_id": "legacy-wkr"}).status_code,
            200,
        )
        listing = self.client.get("/workers").json()["workers"]
        rec = next(w for w in listing if w["worker_id"] == "legacy-wkr")
        self.assertIsNone(rec["capabilities"])

    # ------------------------------------------------------------------
    # abandon (voluntary requeue when contributor's user becomes active)
    # ------------------------------------------------------------------
    def test_abandon_returns_job_to_queue(self):
        # Submit + claim by worker A, then have A abandon mid-flight.
        job_id = self.client.post(
            "/generate", json={"prompt": "drain me"}
        ).json()["job_id"]
        worker_id = "wkr-abandoning"
        self.client.post("/register", json={"worker_id": worker_id})
        self.r.lpop("job_queue")  # mimic worker pop
        self.client.post(
            "/jobs/claim", json={"worker_id": worker_id, "job_id": job_id}
        )

        # Sanity: job is in-flight, queue is empty.
        self.assertEqual(self.r.llen("job_queue"), 0)
        self.assertEqual(self.r.hlen("job_processing"), 1)

        resp = self.client.post(
            "/jobs/abandon", json={"worker_id": worker_id, "job_id": job_id}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["requeued"])

        # Job back on the queue, out of in-flight, SQLite row reset to pending.
        self.assertEqual(self.r.llen("job_queue"), 1)
        self.assertEqual(self.r.hlen("job_processing"), 0)
        row = self.client.get(f"/result/{job_id}").json()
        self.assertEqual(row["status"], "pending")

    def test_abandon_unknown_job_is_noop(self):
        resp = self.client.post(
            "/jobs/abandon", json={"worker_id": "wkr-x", "job_id": "no-such"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["requeued"])

    # ------------------------------------------------------------------
    # reaper
    # ------------------------------------------------------------------
    def test_reaper_requeues_jobs_with_expired_deadlines(self):
        # Submit a job, claim it as worker A, then expire the deadline by
        # rewriting it directly.
        job_id = self.client.post(
            "/generate", json={"prompt": "stale"}
        ).json()["job_id"]
        worker_id = "wkr-stale"
        self.client.post("/register", json={"worker_id": worker_id})
        self.r.lpop("job_queue")  # mimic worker pop
        self.client.post(
            "/jobs/claim", json={"worker_id": worker_id, "job_id": job_id}
        )

        # Force the deadline into the past.
        meta = json.loads(self.r.hget("job_processing", job_id))
        meta["deadline"] = time.time() - 60
        self.r.hset("job_processing", job_id, json.dumps(meta))

        # Run one reaper tick.
        Reaper(self.r, self.db)._tick()

        # Job is back on the queue and out of the in-flight hash.
        self.assertEqual(self.r.llen("job_queue"), 1)
        self.assertEqual(self.r.hlen("job_processing"), 0)
        # SQLite row reset to pending.
        row = self.client.get(f"/result/{job_id}").json()
        self.assertEqual(row["status"], "pending")


if __name__ == "__main__":
    unittest.main()
