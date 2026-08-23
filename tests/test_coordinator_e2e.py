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
from typing import Optional

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
from coordinator import member_auth  # noqa: E402
from coordinator import model_registry  # noqa: E402
from coordinator.scheduler import Reaper  # noqa: E402


def _job_complete_payload(
    worker_id: str,
    job_id: str,
    *,
    tokens: int = 7,
    claim_token: Optional[str] = None,
) -> dict:
    payload: dict = {
        "worker_id": worker_id,
        "job_id": job_id,
        "text": "[mock] hello",
        "model": "mock",
        "prompt_tokens": 5,
        "completion_tokens": tokens,
        "duration_seconds": 0.1,
        "status": "complete",
    }
    if claim_token is not None:
        payload["claim_token"] = claim_token
    return payload


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

        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": job_id},
        )
        self.assertEqual(claim.status_code, 200)
        claim_token = claim.json()["claim_token"]
        complete = self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(
                worker_id, job_id, tokens=11, claim_token=claim_token,
            ),
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
    # /jobs — admin job-listing feed (dashboard drill-down page)
    # ------------------------------------------------------------------
    def test_jobs_listing_sorted_most_recent_first(self):
        job_a = self.client.post("/generate", json={"prompt": "first"}).json()["job_id"]
        job_b = self.client.post("/generate", json={"prompt": "second"}).json()["job_id"]
        listing = self.client.get("/jobs").json()["jobs"]
        ids = [j["job_id"] for j in listing]
        # Most-recent-first: job_b was submitted after job_a.
        self.assertLess(ids.index(job_b), ids.index(job_a))

    def test_jobs_listing_filters_by_worker_id(self):
        job_id = self.client.post("/generate", json={"prompt": "hi"}).json()["job_id"]
        other_worker = "wkr-" + uuid.uuid4().hex[:6]
        target_worker = "wkr-" + uuid.uuid4().hex[:6]
        self.client.post("/register", json={"worker_id": other_worker})
        self.client.post("/register", json={"worker_id": target_worker})
        self.r.lpop("job_queue")
        claim_token = self.client.post(
            "/jobs/claim",
            json={"worker_id": target_worker, "job_id": job_id},
        ).json()["claim_token"]
        self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(target_worker, job_id, claim_token=claim_token),
        )

        matched = self.client.get(f"/jobs?worker_id={target_worker}").json()
        self.assertEqual([j["job_id"] for j in matched["jobs"]], [job_id])
        self.assertEqual(matched["filter_worker"]["worker_id"], target_worker)

        unmatched = self.client.get(f"/jobs?worker_id={other_worker}").json()
        self.assertEqual(unmatched["jobs"], [])

    def test_jobs_listing_filters_by_member_id(self):
        # This suite runs with AUTH disabled, so /generate never stamps
        # a submitted_by_member_id (member auth is exercised in
        # test_member_auth_e2e.py instead). What's testable here: an
        # unmatched member_id filters down to an empty list rather than
        # erroring, and the filter-context block reflects the id even
        # with no owned workers.
        self.client.post("/generate", json={"prompt": "anonymous submit"})
        empty = self.client.get("/jobs?member_id=mem_nobody").json()
        self.assertEqual(empty["jobs"], [])
        self.assertEqual(empty["filter_member"]["member_id"], "mem_nobody")
        self.assertEqual(empty["filter_member"]["workers"], [])

    def test_jobs_listing_status_filter(self):
        pending_job = self.client.post("/generate", json={"prompt": "stays pending"}).json()["job_id"]
        complete_job = self.client.post("/generate", json={"prompt": "will finish"}).json()["job_id"]
        worker_id = "wkr-" + uuid.uuid4().hex[:6]
        self.client.post("/register", json={"worker_id": worker_id})
        self.r.lpop("job_queue")
        claim_token = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": complete_job},
        ).json()["claim_token"]
        self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(worker_id, complete_job, claim_token=claim_token),
        )

        pending = self.client.get("/jobs?status=pending").json()["jobs"]
        self.assertEqual([j["job_id"] for j in pending], [pending_job])
        complete = self.client.get("/jobs?status=complete").json()["jobs"]
        self.assertEqual([j["job_id"] for j in complete], [complete_job])

    def test_jobs_listing_filter_worker_surfaces_gpu_hardware(self):
        worker_id = "wkr-" + uuid.uuid4().hex[:6]
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"gpu_model": "RTX 4090", "vram_gb": 24.0},
            },
        )
        body = self.client.get(f"/jobs?worker_id={worker_id}").json()
        self.assertEqual(body["filter_worker"]["gpu_model"], "RTX 4090")
        self.assertEqual(body["filter_worker"]["vram_gb"], 24.0)

    def test_jobs_listing_rows_surface_gpu_attempts_and_queue_time(self):
        job_id = self.client.post("/generate", json={"prompt": "hi"}).json()["job_id"]
        worker_id = "wkr-" + uuid.uuid4().hex[:6]
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"gpu_model": "RTX 4090", "vram_gb": 24.0},
            },
        )
        self.assertIsNotNone(self.r.lpop("job_queue"))
        claim_token = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": job_id},
        ).json()["claim_token"]
        self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(worker_id, job_id, claim_token=claim_token),
        )

        row = self.client.get(f"/jobs?worker_id={worker_id}").json()["jobs"][0]
        self.assertEqual(row["worker_gpu"], "RTX 4090 (24.0 GB)")
        self.assertEqual(row["attempts"], 1)
        self.assertIsNotNone(row["queue_seconds"])
        self.assertGreaterEqual(row["queue_seconds"], 0)

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

    def test_register_with_display_name_surfaces_in_workers(self):
        # display_name replaces the old hostname-embedded-in-worker_id
        # trick — the agent now sends a chosen (or random-defaulted)
        # name explicitly instead of the coordinator inferring one from
        # the id.
        worker_id = "win-" + uuid.uuid4().hex[:20]
        resp = self.client.post(
            "/register",
            json={"worker_id": worker_id, "display_name": "Living Room PC"},
        )
        self.assertEqual(resp.status_code, 200)

        listing = self.client.get("/workers").json()["workers"]
        rec = next(w for w in listing if w["worker_id"] == worker_id)
        self.assertEqual(rec["display_name"], "Living Room PC")

    def test_register_without_display_name_falls_back_to_generic_label(self):
        # An opaque worker_id (no embeddable hostname, no display_name)
        # has nothing to show but "agent" — this documents that
        # behavior rather than silently degrading.
        worker_id = "win-" + uuid.uuid4().hex[:20]
        self.assertEqual(
            self.client.post("/register", json={"worker_id": worker_id}).status_code,
            200,
        )
        listing = self.client.get("/workers").json()["workers"]
        rec = next(w for w in listing if w["worker_id"] == worker_id)
        self.assertEqual(rec["display_name"], "agent")

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


class ImageCostMultiplierTests(unittest.TestCase):
    """Pixel-area → quota-multiplier table. The composer's small /
    medium / large radios map to 512² / 768² / 1024² and should bill
    1× / 2× / 4× image-units against the daily image quota. Edge
    cases worth pinning: zero/negative dims fall through to 1× (no
    silent 4× surprise on a malformed worker output), and the
    boundaries are inclusive of the named bucket (a 512² image costs
    1×, a 513² image costs 2×)."""

    def test_small_bucket_at_and_below_512_squared(self):
        from coordinator.main import image_cost_multiplier
        self.assertEqual(image_cost_multiplier(512, 512), 1.0)
        self.assertEqual(image_cost_multiplier(256, 256), 1.0)
        self.assertEqual(image_cost_multiplier(1, 1), 1.0)

    def test_medium_bucket_between_513_and_768_squared(self):
        from coordinator.main import image_cost_multiplier
        # First step above the small boundary.
        self.assertEqual(image_cost_multiplier(513, 513), 2.0)
        self.assertEqual(image_cost_multiplier(768, 768), 2.0)

    def test_large_bucket_above_768_squared(self):
        from coordinator.main import image_cost_multiplier
        self.assertEqual(image_cost_multiplier(769, 769), 4.0)
        self.assertEqual(image_cost_multiplier(1024, 1024), 4.0)
        self.assertEqual(image_cost_multiplier(1536, 1536), 4.0)

    def test_unknown_dims_default_to_small(self):
        # A worker output we couldn't parse (PNG too short, dims=0)
        # should bill 1× rather than 4× — better to undercount the
        # edge case than blindside a BRONZE user.
        from coordinator.main import image_cost_multiplier
        self.assertEqual(image_cost_multiplier(0, 0), 1.0)
        self.assertEqual(image_cost_multiplier(-1, 1024), 1.0)

    def test_asymmetric_dims_use_area_not_max_side(self):
        # A 256x1024 image (262,144 px) is still small-bucket area
        # even though its long side is large. Confirms we're billing
        # by compute (area) and not by max-dim.
        from coordinator.main import image_cost_multiplier
        self.assertEqual(image_cost_multiplier(256, 1024), 1.0)


class ImageGenerationTests(unittest.TestCase):
    """Smoke tests for the image-tool slice. KISS coverage: queue +
    routing + PNG-storage path. Heavier image fidelity (real sd.cpp,
    canary parity, NSFW classifier) lives outside the unit suite."""

    # 1x1 transparent PNG — enough bytes to satisfy the magic-header
    # check; small enough to fit in a test fixture without binary blobs.
    _MOCK_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.r = _FAKE
        cls.db = coordinator_main.db

    def setUp(self):
        self.r.flushall()
        self.db._conn.executescript(
            "DELETE FROM jobs; DELETE FROM workers; DELETE FROM earnings; "
            "DELETE FROM messages; DELETE FROM conversations;"
        )

    def test_image_generate_routes_to_image_queue(self):
        resp = self.client.post(
            "/generate", json={"prompt": "a cat", "tool": "image"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # Chat queue is empty; image queue has the job.
        self.assertEqual(self.r.llen("job_queue"), 0)
        self.assertEqual(self.r.llen("job_queue:image"), 1)
        envelope = json.loads(self.r.lpop("job_queue:image"))
        self.assertEqual(envelope["tool"], "image")
        # When the UI doesn't pin per-job knobs, the coordinator no
        # longer bakes hard defaults into the envelope — the worker
        # falls through to the model's sidecar (Lightning-tuned for
        # the current default at steps≈6). steps/width/height should
        # NOT be in the envelope at all.
        self.assertNotIn("width", envelope["image"])
        self.assertNotIn("height", envelope["image"])
        self.assertNotIn("steps", envelope["image"])
        # Default image model auto-selected. Read it from the registry
        # so this test survives a default swap without churn.
        self.assertEqual(envelope["model"], model_registry.DEFAULT_IMAGE_MODEL)

    def test_image_generate_passes_through_explicit_overrides(self):
        # When the UI pins values, the coordinator DOES emit them so
        # the worker uses the user's choice — and clamps to sane
        # bounds.
        resp = self.client.post(
            "/generate",
            json={
                "prompt": "explicit",
                "tool": "image",
                "image": {"width": 768, "height": 768, "steps": 12},
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        envelope = json.loads(self.r.lpop("job_queue:image"))
        self.assertEqual(envelope["image"]["width"], 768)
        self.assertEqual(envelope["image"]["height"], 768)
        self.assertEqual(envelope["image"]["steps"], 12)

    def test_image_steps_clamped_when_overridden(self):
        # A buggy client sending steps=9999 is still capped to 50 so a
        # contributor's machine doesn't burn ten minutes on one image.
        resp = self.client.post(
            "/generate",
            json={
                "prompt": "huge",
                "tool": "image",
                "image": {"steps": 9999, "width": 99999, "height": 99999},
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        envelope = json.loads(self.r.lpop("job_queue:image"))
        self.assertEqual(envelope["image"]["steps"], 50)
        self.assertLessEqual(envelope["image"]["width"], 1536)
        self.assertLessEqual(envelope["image"]["height"], 1536)

    def test_image_envelope_carries_forced_negative_prompt(self):
        # The coordinator prepends a forced SFW negative onto every
        # image job — the layer-1 mitigation that catches "obese cow"
        # → topless woman drift on DreamShaper-class models.
        resp = self.client.post(
            "/generate",
            json={"prompt": "an obese cow", "tool": "image"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        envelope = json.loads(self.r.lpop("job_queue:image"))
        neg = envelope["image"]["negative_prompt"] or ""
        # Phrases from the default FORCED_NEGATIVE_PROMPT — assert
        # presence rather than exact match so the prompt text can be
        # tuned without breaking the test.
        for term in ("nsfw", "nude", "topless"):
            self.assertIn(term, neg.lower())

    def test_image_envelope_combines_forced_and_user_negative(self):
        # When the user supplies their own negative prompt, the forced
        # SFW prefix goes BEFORE it (full weight to the safety
        # phrases). Both halves must survive.
        resp = self.client.post(
            "/generate",
            json={
                "prompt": "a dog",
                "tool": "image",
                "image": {"negative_prompt": "blurry, lowres"},
            },
        )
        envelope = json.loads(self.r.lpop("job_queue:image"))
        neg = envelope["image"]["negative_prompt"] or ""
        self.assertIn("nsfw", neg.lower())
        self.assertIn("blurry", neg.lower())
        self.assertIn("lowres", neg.lower())
        # Forced phrases come first so the sampler sees them with
        # full weight, then the user's terms.
        self.assertLess(neg.lower().index("nsfw"), neg.lower().index("blurry"))

    def test_image_generate_rejects_denylist(self):
        # Picks a denylist phrase the unit test asserts continues to
        # reject. Concrete phrasing is uncomfortable to type but
        # required to verify the gate actually fires.
        resp = self.client.post(
            "/generate",
            json={"prompt": "loli scene", "tool": "image"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("denylist", resp.json()["detail"])

    def test_image_complete_persists_png_and_credits_earnings(self):
        worker_id = "wkr-img-" + uuid.uuid4().hex[:6]
        # Advertise image capability so /jobs/next routes correctly.
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"tools": ["chat", "image"]},
            },
        )
        gr = self.client.post(
            "/generate", json={"prompt": "a cat", "tool": "image"},
        )
        job_id = gr.json()["job_id"]
        # Worker pulls explicitly from the image queue.
        nxt = self.client.post(
            "/jobs/next",
            json={"worker_id": worker_id, "tool": "image"},
        ).json()
        self.assertIsNotNone(nxt["job"])
        self.assertEqual(nxt["job"]["job_id"], job_id)
        # /jobs/next atomically claims now — its response carries the
        # claim_token directly. (A separate /jobs/claim would just
        # overwrite this with a fresh token.)
        claim_token = nxt["claim_token"]
        comp = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "text": "a cat",
                "model": "dreamshaper8",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "duration_seconds": 1.2,
                "status": "complete",
                "image_b64": self._MOCK_PNG_B64,
                "claim_token": claim_token,
            },
        )
        self.assertEqual(comp.status_code, 200, comp.text)
        # Earnings recorded against image work (flat rate).
        self.assertGreater(comp.json()["earnings"], 0)
        # /result surfaces the image_path so polling clients can render.
        result = self.client.get(f"/result/{job_id}").json()
        self.assertEqual(result["status"], "complete")
        # /result reads JOB_RESULTS first; that path carries image_path
        # only when the redis copy isn't evicted. Image_path may live
        # only on the message row — accept either, just confirm at
        # least one surface has it.
        image_path = result.get("image_path")
        if not image_path:
            from coordinator.main import IMAGE_DIR
            self.assertTrue((IMAGE_DIR / f"{job_id}.png").exists())

    def test_image_complete_rejects_non_png(self):
        worker_id = "wkr-bad-" + uuid.uuid4().hex[:6]
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"tools": ["chat", "image"]},
            },
        )
        gr = self.client.post(
            "/generate", json={"prompt": "a cat", "tool": "image"},
        )
        job_id = gr.json()["job_id"]
        nxt = self.client.post(
            "/jobs/next",
            json={"worker_id": worker_id, "tool": "image"},
        ).json()
        claim_token = nxt["claim_token"]
        # Send bytes that aren't a PNG (the magic header is missing).
        import base64
        not_a_png = base64.b64encode(b"<html>nope</html>").decode("ascii")
        comp = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "text": "",
                "model": "dreamshaper8",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "duration_seconds": 1.0,
                "status": "complete",
                "image_b64": not_a_png,
                "claim_token": claim_token,
            },
        )
        self.assertEqual(comp.status_code, 400)
        self.assertIn("PNG", comp.json()["detail"])

    def test_chat_model_rejected_with_image_tool(self):
        resp = self.client.post(
            "/generate",
            json={"prompt": "hi", "tool": "image", "model": "llama3.2:1b"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("chat model", resp.json()["detail"])


class ClaimTokenEnforcementTests(unittest.TestCase):
    """The token-gated complete/partial path is the load-bearing
    correctness guarantee added in v1.1.23 — it stops a stale (reaper-
    requeued) worker from clobbering whoever the coordinator has now
    handed the job to. These tests pin the exact 410 contract."""

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

    def _submit_and_pop(self, worker_id: str) -> str:
        job_id = self.client.post(
            "/generate", json={"prompt": "claim me"},
        ).json()["job_id"]
        self.client.post("/register", json={"worker_id": worker_id})
        return job_id

    def test_jobs_next_returns_claim_token_atomically(self):
        worker_id = "wkr-atomic"
        job_id = self._submit_and_pop(worker_id)
        nxt = self.client.post(
            "/jobs/next", json={"worker_id": worker_id, "tool": "chat"},
        ).json()
        self.assertEqual(nxt["job"]["job_id"], job_id)
        self.assertIn("claim_token", nxt)
        self.assertTrue(nxt["claim_token"])
        # JOB_PROCESSING records the token so /complete can verify.
        meta = json.loads(self.r.hget("job_processing", job_id))
        self.assertEqual(meta["claim_token"], nxt["claim_token"])
        self.assertEqual(meta["worker_id"], worker_id)

    def test_complete_without_token_after_claim_returns_410(self):
        worker_id = "wkr-notoken"
        job_id = self._submit_and_pop(worker_id)
        # Claim via the legacy path so the processing record carries a
        # token, then submit a /complete that doesn't echo it.
        self.client.post(
            "/jobs/claim", json={"worker_id": worker_id, "job_id": job_id},
        )
        resp = self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(worker_id, job_id),
        )
        self.assertEqual(resp.status_code, 410, resp.text)
        # 410 detail carries the structured reason so the agent can
        # distinguish this from a transport failure.
        detail = resp.json()["detail"]
        self.assertEqual(detail["reason"], "claim_token_mismatch")

    def test_complete_with_wrong_token_returns_410(self):
        worker_id = "wkr-wrong"
        job_id = self._submit_and_pop(worker_id)
        self.client.post(
            "/jobs/claim", json={"worker_id": worker_id, "job_id": job_id},
        )
        resp = self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(
                worker_id, job_id, claim_token="not-the-real-token",
            ),
        )
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.json()["detail"]["reason"], "claim_token_mismatch")

    def test_complete_for_unclaimed_job_returns_410(self):
        # No /jobs/next, no /jobs/claim — the job is on the queue but
        # nobody owns it. /complete must refuse rather than silently
        # accept a forged result.
        worker_id = "wkr-orphan"
        job_id = self._submit_and_pop(worker_id)
        resp = self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(worker_id, job_id),
        )
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.json()["detail"]["reason"], "no_active_claim")

    def test_complete_from_non_claimant_returns_410(self):
        # wA owns the claim; wB tries to complete pretending to be wA.
        # _require_worker_owner is dev-mode-permissive (no auth here),
        # so we test only the claim-ownership gate.
        worker_a = "wkr-a"
        worker_b = "wkr-b"
        job_id = self._submit_and_pop(worker_a)
        self.client.post("/register", json={"worker_id": worker_b})
        claim = self.client.post(
            "/jobs/claim", json={"worker_id": worker_a, "job_id": job_id},
        ).json()
        # wB attempts complete with wA's token. _verify_claim_or_410
        # spots the worker_id mismatch before the token check.
        resp = self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(
                worker_b, job_id, claim_token=claim["claim_token"],
            ),
        )
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.json()["detail"]["reason"], "claim_owned_by_other_worker")

    def test_jobs_next_immediately_returns_job_with_wait_zero(self):
        # Legacy behavior preserved: zero wait, single tool, returns
        # whatever's already on the queue or null.
        worker_id = "wkr-zero"
        job_id = self._submit_and_pop(worker_id)
        out = self.client.post(
            "/jobs/next",
            json={"worker_id": worker_id, "tool": "chat", "wait": 0},
        ).json()
        self.assertEqual(out["job"]["job_id"], job_id)
        self.assertIn("claim_token", out)

    def test_jobs_next_long_poll_returns_null_when_no_work(self):
        # Long-poll over an empty queue returns null after the wait
        # window — verifies the timeout path. Uses a short wait (1s)
        # so the test doesn't actually hang.
        worker_id = "wkr-empty"
        self.client.post("/register", json={"worker_id": worker_id})
        start = time.time()
        out = self.client.post(
            "/jobs/next",
            json={
                "worker_id": worker_id,
                "tools": ["chat", "image"],
                "wait": 1,
            },
        ).json()
        elapsed = time.time() - start
        self.assertIsNone(out["job"])
        # Sanity: we actually waited (not instantly returning). Some
        # fakeredis BLPOP implementations short-circuit on empty —
        # tolerate both <1s and ~1s as long as the response shape
        # is correct.
        self.assertLess(elapsed, 3.0, f"long-poll overshot, took {elapsed:.1f}s")

    def test_jobs_next_long_poll_blpop_across_tool_queues(self):
        # tools=[chat, image] BLPOPs across both queues. With a chat
        # job pre-queued, the worker gets it on the long-poll.
        worker_id = "wkr-multi"
        job_id = self._submit_and_pop(worker_id)
        out = self.client.post(
            "/jobs/next",
            json={
                "worker_id": worker_id,
                "tools": ["chat", "image"],
                "wait": 1,
            },
        ).json()
        self.assertEqual(out["job"]["job_id"], job_id)
        self.assertIn("claim_token", out)

    def test_jobs_next_filters_to_advertised_tools(self):
        # Regression for the 2026-05-26 prod incident: an image-only
        # worker (tools=["image"] at /register) was BLPOP'ing the chat
        # queue too, picking up chat jobs and answering them with
        # [mock] responses. Coordinator now filters the requested
        # tools list to the intersection with what /register declared
        # — image-only worker asking for chat gets null, not a chat
        # job in mock mode.
        worker_id = "wkr-image-only"
        # Put a chat job on the queue so we can prove image-only
        # doesn't claim it.
        chat_job_id = self.client.post(
            "/generate", json={"prompt": "chat-only please"},
        ).json()["job_id"]
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"tools": ["image"]},
            },
        )
        # Ask for both — image-only should drop chat from the BLPOP set.
        out = self.client.post(
            "/jobs/next",
            json={
                "worker_id": worker_id,
                "tools": ["chat", "image"],
                "wait": 0,
            },
        ).json()
        self.assertIsNone(
            out["job"],
            "image-only worker should not have received the chat job",
        )
        # And the chat job is still on the queue waiting for a chat-
        # capable worker.
        chat_capable = "wkr-chat-capable"
        self.client.post("/register", json={"worker_id": chat_capable})
        out2 = self.client.post(
            "/jobs/next",
            json={"worker_id": chat_capable, "tool": "chat"},
        ).json()
        self.assertEqual(out2["job"]["job_id"], chat_job_id)

    def test_jobs_next_legacy_worker_with_no_capabilities_still_gets_chat(self):
        # Legacy contract: a worker that never advertised capabilities
        # (tools_json IS NULL) is treated as chat-only. /jobs/next with
        # tool="chat" still works for those workers — important so a
        # contributor on a pre-tools-column agent isn't silently locked
        # out of the chat queue.
        worker_id = "wkr-legacy"
        job_id = self._submit_and_pop(worker_id)  # registers w/o caps
        out = self.client.post(
            "/jobs/next",
            json={"worker_id": worker_id, "tool": "chat"},
        ).json()
        self.assertEqual(out["job"]["job_id"], job_id)

    def test_jobs_next_wait_capped_at_server_max(self):
        # A worker requesting an absurdly long wait gets capped at
        # MAX_LONGPOLL_SECONDS rather than holding a coordinator
        # thread for hours.
        from coordinator import main as coord_main
        self.assertLessEqual(coord_main.MAX_LONGPOLL_SECONDS, 60.0)

    def test_atomic_claim_complete_round_trip(self):
        # Happy path via the new atomic /jobs/next: the returned
        # claim_token is sufficient on /complete, no extra /claim
        # round-trip needed.
        worker_id = "wkr-roundtrip"
        job_id = self._submit_and_pop(worker_id)
        nxt = self.client.post(
            "/jobs/next", json={"worker_id": worker_id, "tool": "chat"},
        ).json()
        comp = self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(
                worker_id, job_id, tokens=4, claim_token=nxt["claim_token"],
            ),
        )
        self.assertEqual(comp.status_code, 200, comp.text)
        self.assertGreater(comp.json()["earnings"], 0)


class ReaperExtensionTests(unittest.TestCase):
    """The reaper used to unconditionally requeue any job whose
    deadline had passed. With v1.1.23 the contract is: extend if the
    claimant is still heartbeating with the matching job_id, requeue
    only on actual silence or wrong-job. This is what lets a 60s
    DreamShaper image generation finish without being yanked."""

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

    def _claimed_job(self, worker_id: str) -> str:
        job_id = self.client.post(
            "/generate", json={"prompt": "reap me"},
        ).json()["job_id"]
        self.client.post("/register", json={"worker_id": worker_id})
        self.r.lpop("job_queue")  # mimic worker pop
        self.client.post(
            "/jobs/claim", json={"worker_id": worker_id, "job_id": job_id},
        )
        # Force the deadline into the past so the reaper has something
        # to decide on.
        meta = json.loads(self.r.hget("job_processing", job_id))
        meta["deadline"] = time.time() - 60
        self.r.hset("job_processing", job_id, json.dumps(meta))
        return job_id

    def test_extends_deadline_when_claimant_heartbeating_matching_job(self):
        worker_id = "wkr-busy"
        job_id = self._claimed_job(worker_id)
        # Fresh heartbeat carrying the right job_id — simulates a
        # healthy long-running inference.
        self.client.post(
            "/heartbeat",
            json={"worker_id": worker_id, "status": "busy", "job_id": job_id},
        )
        Reaper(self.r, self.db)._tick()
        # Job stays in flight, deadline pushed forward.
        self.assertEqual(self.r.llen("job_queue"), 0)
        self.assertEqual(self.r.hlen("job_processing"), 1)
        meta = json.loads(self.r.hget("job_processing", job_id))
        self.assertGreater(meta["deadline"], time.time())

    def test_requeues_when_claimant_silent(self):
        worker_id = "wkr-silent"
        job_id = self._claimed_job(worker_id)
        # No heartbeat from this worker, or only a very old one. Write
        # a stale heartbeat directly so the reaper sees it as expired.
        self.r.hset(
            "worker_heartbeats",
            worker_id,
            json.dumps({"ts": time.time() - 3600, "job_id": job_id}),
        )
        Reaper(self.r, self.db)._tick()
        # Job back on the queue, processing hash cleared.
        self.assertEqual(self.r.llen("job_queue"), 1)
        self.assertEqual(self.r.hlen("job_processing"), 0)

    def test_requeues_when_heartbeat_on_different_job(self):
        # Worker is alive but moved on to another job (e.g., crashed
        # and restarted, then picked something else up). The stale
        # job_id in JOB_PROCESSING should requeue, not extend.
        worker_id = "wkr-wandered"
        job_id = self._claimed_job(worker_id)
        self.client.post(
            "/heartbeat",
            json={
                "worker_id": worker_id,
                "status": "busy",
                "job_id": "some-other-job-id",
            },
        )
        Reaper(self.r, self.db)._tick()
        self.assertEqual(self.r.llen("job_queue"), 1)
        self.assertEqual(self.r.hlen("job_processing"), 0)

    def test_requeue_routes_to_correct_tool_queue(self):
        # An image-tool job that times out must land back on the
        # image queue, not the chat queue — otherwise chat-only
        # workers would pick it up and 503 the user immediately.
        worker_id = "wkr-img-reap"
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"tools": ["chat", "image"]},
            },
        )
        job_id = self.client.post(
            "/generate", json={"prompt": "a cat", "tool": "image"},
        ).json()["job_id"]
        self.r.lpop("job_queue:image")
        self.client.post(
            "/jobs/claim", json={"worker_id": worker_id, "job_id": job_id},
        )
        meta = json.loads(self.r.hget("job_processing", job_id))
        meta["deadline"] = time.time() - 60
        self.r.hset("job_processing", job_id, json.dumps(meta))
        # Worker is silent — no heartbeat written.
        Reaper(self.r, self.db)._tick()
        self.assertEqual(self.r.llen("job_queue:image"), 1)
        self.assertEqual(self.r.llen("job_queue"), 0)


class CancelEndpointTests(unittest.TestCase):
    """/jobs/cancel is the soft-cancel sibling to the new UI button.
    The contract: mark the job terminal, drop JOB_PROCESSING so the
    worker's eventual /complete 410s, idempotent for already-terminal
    jobs."""

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

    def test_cancel_marks_job_cancelled_and_pops_processing(self):
        worker_id = "wkr-cancel-1"
        job_id = self.client.post(
            "/generate", json={"prompt": "cancel me"},
        ).json()["job_id"]
        self.client.post("/register", json={"worker_id": worker_id})
        self.client.post(
            "/jobs/claim", json={"worker_id": worker_id, "job_id": job_id},
        )
        # Sanity: in-flight before cancel.
        self.assertEqual(self.r.hlen("job_processing"), 1)
        cancel = self.client.post("/jobs/cancel", json={"job_id": job_id})
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual(cancel.json()["status"], "cancelled")
        # Processing hash cleared; result shows cancelled status.
        self.assertEqual(self.r.hlen("job_processing"), 0)
        result = self.client.get(f"/result/{job_id}").json()
        self.assertEqual(result["status"], "cancelled")

    def test_workers_late_complete_after_cancel_is_410(self):
        # Mimic the race the agent has to handle: worker finishes
        # inference seconds after the user clicked Cancel. The
        # coordinator must reject so the worker skips local credit.
        worker_id = "wkr-late"
        job_id = self.client.post(
            "/generate", json={"prompt": "late"},
        ).json()["job_id"]
        self.client.post("/register", json={"worker_id": worker_id})
        claim = self.client.post(
            "/jobs/claim", json={"worker_id": worker_id, "job_id": job_id},
        ).json()
        self.client.post("/jobs/cancel", json={"job_id": job_id})
        # Worker's /complete carries a token that USED to be valid
        # but the processing record is gone.
        resp = self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(
                worker_id, job_id, claim_token=claim["claim_token"],
            ),
        )
        self.assertEqual(resp.status_code, 410, resp.text)
        self.assertEqual(resp.json()["detail"]["reason"], "no_active_claim")

    def test_cancel_is_idempotent_on_already_terminal_job(self):
        # Cancelling a completed job is a 200 no-op, not 404 — so a
        # double-click from a flaky network doesn't surface an error.
        worker_id = "wkr-done"
        job_id = self.client.post(
            "/generate", json={"prompt": "finished"},
        ).json()["job_id"]
        self.client.post("/register", json={"worker_id": worker_id})
        claim = self.client.post(
            "/jobs/claim", json={"worker_id": worker_id, "job_id": job_id},
        ).json()
        self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(
                worker_id, job_id, claim_token=claim["claim_token"],
            ),
        )
        cancel = self.client.post("/jobs/cancel", json={"job_id": job_id})
        self.assertEqual(cancel.status_code, 200)
        self.assertTrue(cancel.json().get("already_terminal"))
        # Status stays 'complete' — cancel doesn't rewrite finished work.
        result = self.client.get(f"/result/{job_id}").json()
        self.assertEqual(result["status"], "complete")

    def test_cancel_unknown_job_returns_404(self):
        resp = self.client.post("/jobs/cancel", json={"job_id": "no-such-job"})
        self.assertEqual(resp.status_code, 404)


class PromptSizeLimitTests(unittest.TestCase):
    """MAX_PROMPT_BYTES on POST /generate. Monkeypatches the already-
    imported module constant directly rather than re-importing with a
    fresh env, since it's a plain int read once at request time (no
    startup wiring depends on its value)."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)

    def setUp(self):
        self._orig = coordinator_main.MAX_PROMPT_BYTES

    def tearDown(self):
        coordinator_main.MAX_PROMPT_BYTES = self._orig

    def test_disabled_by_default_accepts_any_size(self):
        self.assertEqual(coordinator_main.MAX_PROMPT_BYTES, 0)
        resp = self.client.post("/generate", json={"prompt": "x" * 5000})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_under_cap_accepted(self):
        coordinator_main.MAX_PROMPT_BYTES = 100
        resp = self.client.post("/generate", json={"prompt": "hello"})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_over_cap_rejected_with_413(self):
        coordinator_main.MAX_PROMPT_BYTES = 100
        resp = self.client.post("/generate", json={"prompt": "x" * 101})
        self.assertEqual(resp.status_code, 413, resp.text)
        self.assertIn("MAX_PROMPT_BYTES", resp.json()["detail"])

    def test_cap_counts_utf8_bytes_not_characters(self):
        # Multi-byte characters should count by encoded size, not by
        # Python string length, so the cap can't be undercounted with
        # non-ASCII input.
        coordinator_main.MAX_PROMPT_BYTES = 10
        # "e"-acute is 1 char but 2 UTF-8 bytes: 6 chars == 6 (would
        # pass a char-length check) but 12 bytes (fails a 10-byte cap).
        resp = self.client.post("/generate", json={"prompt": "é" * 6})
        self.assertEqual(resp.status_code, 413, resp.text)


class CapacityCeilingTests(unittest.TestCase):
    """CAPACITY_JOBS_PER_WORKER on POST /generate — the ceiling scales
    with live worker count, so tests register real workers via
    /register (which also stamps a heartbeat) rather than faking
    liveness."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.r = _FAKE

    def setUp(self):
        self.r.flushall()
        self._orig = coordinator_main.CAPACITY_JOBS_PER_WORKER

    def tearDown(self):
        coordinator_main.CAPACITY_JOBS_PER_WORKER = self._orig

    def test_disabled_by_default_allows_unbounded_queueing(self):
        self.assertEqual(coordinator_main.CAPACITY_JOBS_PER_WORKER, 0)
        for _ in range(10):
            resp = self.client.post("/generate", json={"prompt": "hi"})
            self.assertEqual(resp.status_code, 200, resp.text)

    def test_blocks_once_ceiling_reached_with_one_worker(self):
        coordinator_main.CAPACITY_JOBS_PER_WORKER = 1
        self.client.post("/register", json={"worker_id": "wkr-cap-1"})
        # First job fills the ceiling (1 worker * 1 = 1).
        first = self.client.post("/generate", json={"prompt": "one"})
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post("/generate", json={"prompt": "two"})
        self.assertEqual(second.status_code, 503, second.text)
        self.assertIn("at capacity", second.json()["detail"])

    def test_ceiling_scales_with_live_worker_count(self):
        coordinator_main.CAPACITY_JOBS_PER_WORKER = 1
        self.client.post("/register", json={"worker_id": "wkr-cap-a"})
        self.client.post("/register", json={"worker_id": "wkr-cap-b"})
        # Ceiling is now 2 workers * 1 = 2.
        self.assertEqual(
            self.client.post("/generate", json={"prompt": "one"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/generate", json={"prompt": "two"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/generate", json={"prompt": "three"}).status_code,
            503,
        )

    def test_completing_a_job_frees_up_capacity(self):
        coordinator_main.CAPACITY_JOBS_PER_WORKER = 1
        self.client.post("/register", json={"worker_id": "wkr-cap-c"})
        job_id = self.client.post(
            "/generate", json={"prompt": "one"},
        ).json()["job_id"]
        self.assertEqual(
            self.client.post("/generate", json={"prompt": "two"}).status_code,
            503,
        )
        # Mimic the real worker: BLPOP off the queue before claiming,
        # same as CoordinatorE2ETests does — otherwise the job stays
        # double-counted (still on the list AND in JOB_PROCESSING).
        self.r.lpop("job_queue")
        claim = self.client.post(
            "/jobs/claim", json={"worker_id": "wkr-cap-c", "job_id": job_id},
        ).json()
        self.client.post(
            "/jobs/complete",
            json=_job_complete_payload(
                "wkr-cap-c", job_id, claim_token=claim["claim_token"],
            ),
        )
        # The completed job is off the queue and out of JOB_PROCESSING,
        # so capacity is free again.
        self.assertEqual(
            self.client.post("/generate", json={"prompt": "three"}).status_code,
            200,
        )

    def test_zero_live_workers_still_allows_a_floor_of_one_batch(self):
        # No workers registered at all; ceiling uses max(live, 1) so a
        # cold-started network can still queue its first batch of jobs
        # rather than 503ing everyone before any worker has connected.
        coordinator_main.CAPACITY_JOBS_PER_WORKER = 2
        self.assertEqual(
            self.client.post("/generate", json={"prompt": "one"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/generate", json={"prompt": "two"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/generate", json={"prompt": "three"}).status_code,
            503,
        )


def _signup_payload(**overrides):
    payload = {
        "username": "newcontributor",
        "password": "correct horse battery staple",
        "email": "new@example.com",
        "tos_accepted": True,
    }
    payload.update(overrides)
    return payload


class SignupTests(unittest.TestCase):
    """Public, invite-free POST /signup — the task-5.1 gap the launch
    review flagged: today running the agent registers a *worker* but
    never creates a *member*, so nobody can join without an existing
    member vouching for them. These tests cover the new door in."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.db = coordinator_main.db

    def setUp(self):
        _FAKE.flushall()
        # Other test classes in this module never create members, so
        # nothing else in the suite depends on this table's contents —
        # safe (and necessary, since signups persist to SQLite, not
        # the per-test-flushed fake Redis) to reset it here.
        self.db._conn.execute("DELETE FROM members")
        self._orig_max = coordinator_main.SIGNUP_MAX_PER_IP

    def tearDown(self):
        coordinator_main.SIGNUP_MAX_PER_IP = self._orig_max

    def test_signup_creates_a_working_account(self):
        resp = self.client.post("/signup", json=_signup_payload())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["role"], "contributor")
        self.assertIsNone(body["parent_member_id"])
        self.assertTrue(body["token"])
        self.assertEqual(body["username"], "newcontributor")
        # RESEND_API_KEY is unset in this test env, so email_send is
        # unconfigured and signup auto-verifies rather than gating on
        # a link that can never be sent — see SignupEmailVerificationTests
        # in test_member_auth_e2e.py for the Resend-configured path.
        self.assertTrue(body["email_verified"])

        # The returned token actually resolves to this member. Checked
        # via member_auth directly (the same lookup the live bearer-auth
        # middleware calls) rather than a real HTTP round-trip, since
        # this test module runs with API_TOKEN unset (AUTH_ENABLED=False
        # module-wide) and the middleware no-ops entirely in that mode —
        # see the file's own docstring on the env setup.
        looked_up = member_auth.lookup_member_by_token(self.db, body["token"])
        self.assertIsNotNone(looked_up)
        self.assertEqual(looked_up.member_id, body["member_id"])

    def test_signup_creates_a_contributor_who_can_invite(self):
        # role="contributor" is what POST /invites' authorization check
        # actually tests for (member.role in ("admin", "contributor")) —
        # assert the signup path produces a role that satisfies it, so
        # the two sides can't silently drift apart.
        resp = self.client.post(
            "/signup", json=_signup_payload(username="hostuser"),
        )
        self.assertIn(resp.json()["role"], ("admin", "contributor"))

    def test_signup_requires_tos_acceptance(self):
        resp = self.client.post(
            "/signup", json=_signup_payload(tos_accepted=False),
        )
        self.assertEqual(resp.status_code, 400)

    def test_signup_rejects_duplicate_username(self):
        self.client.post("/signup", json=_signup_payload(email="a@example.com"))
        resp = self.client.post(
            "/signup", json=_signup_payload(email="b@example.com"),
        )
        self.assertEqual(resp.status_code, 409)

    def test_signup_rejects_duplicate_email(self):
        self.client.post("/signup", json=_signup_payload(username="userone"))
        resp = self.client.post(
            "/signup", json=_signup_payload(username="usertwo"),
        )
        self.assertEqual(resp.status_code, 409)

    def test_throttle_blocks_after_max_signups_from_one_ip(self):
        coordinator_main.SIGNUP_MAX_PER_IP = 2
        self.client.post("/signup", json=_signup_payload(
            username="capuser1", email="cap1@example.com"))
        self.client.post("/signup", json=_signup_payload(
            username="capuser2", email="cap2@example.com"))
        third = self.client.post("/signup", json=_signup_payload(
            username="capuser3", email="cap3@example.com"))
        self.assertEqual(third.status_code, 429, third.text)

    def test_throttle_does_not_count_failed_attempts(self):
        # A rejected (bad ToS) attempt shouldn't burn a real user's quota.
        coordinator_main.SIGNUP_MAX_PER_IP = 1
        rejected = self.client.post(
            "/signup", json=_signup_payload(tos_accepted=False),
        )
        self.assertEqual(rejected.status_code, 400)
        ok = self.client.post("/signup", json=_signup_payload())
        self.assertEqual(ok.status_code, 200, ok.text)


class DemoModeTests(unittest.TestCase):
    """The no-install public demo (task 6.1) moved to a fully
    client-side canned-response library — see demo.html.j2 — so a
    bot can no longer spam real inference cost via the public page.
    This just pins that the old coordinator surface stays gone."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)

    def test_demo_generate_endpoint_is_gone(self):
        resp = self.client.post("/demo/generate", json={"prompt": "hello"})
        self.assertEqual(resp.status_code, 404)

    def test_demo_result_endpoint_is_gone(self):
        resp = self.client.get("/demo/result/anything")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
