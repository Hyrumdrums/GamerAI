"""Tests for the community-trust slice: ToS endpoint + acceptance
enforcement, plus the canary injection + verification + scoring loop."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import uuid

import fakeredis


class _BaseE2E(unittest.TestCase):
    """Boot a coordinator instance with a clean SQLite DB + fakeredis,
    auth ON, and a known API_TOKEN so the admin seed lands as expected.
    """

    @classmethod
    def setUpClass(cls):
        # Tear down any stale shared.X / coordinator.X modules so import
        # order doesn't leak state from a sibling test module.
        for mod in list(sys.modules):
            if mod.split(".", 1)[0] in ("shared", "coordinator"):
                del sys.modules[mod]

        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._db_path = os.path.join(cls._tmpdir.name, "gamerai.db")
        cls._old_env = {
            "DB_PATH": os.environ.get("DB_PATH"),
            "API_TOKEN": os.environ.get("API_TOKEN"),
            "CANARY_INTERVAL_SECONDS": os.environ.get("CANARY_INTERVAL_SECONDS"),
        }
        os.environ["DB_PATH"] = cls._db_path
        os.environ["API_TOKEN"] = "test-admin-token"
        # Disable the auto-injector so tests can control timing.
        os.environ["CANARY_INTERVAL_SECONDS"] = "0"

        # Force re-import after env mutation.
        for mod in list(sys.modules):
            if mod.split(".", 1)[0] in ("shared", "coordinator"):
                del sys.modules[mod]

        from fastapi.testclient import TestClient

        from coordinator import main as coord_main

        # Substitute fakeredis for the live client. Reset between class setups
        # so we don't carry queue state across test files.
        coord_main.r = fakeredis.FakeRedis(decode_responses=True)
        cls.r = coord_main.r
        cls.coord_main = coord_main
        cls.db = coord_main.db
        # The admin seed runs inside the FastAPI lifespan; trigger it manually
        # since TestClient's lifespan-on-enter happens once and we want it now.
        coord_main.ensure_admin_seed()

        cls.client = TestClient(coord_main.app)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls._tmpdir.cleanup()

    def _admin_headers(self):
        return {"Authorization": "Bearer test-admin-token"}

    def _make_contributor(self, email: str = "alice@example.com") -> tuple[str, str]:
        from coordinator import member_auth
        mem_id = "mem_" + uuid.uuid4().hex[:12]
        raw = member_auth.generate_token()
        self.db.create_member(
            member_id=mem_id,
            email=email,
            role="contributor",
            parent_member_id=None,
            token_hash=member_auth.hash_token(raw),
            tier="BRONZE",
            daily_quota_tokens=None,
        )
        return mem_id, raw

    _invite_seq = 0

    def _create_invite(self, contributor_token: str, daily_quota_tokens: int = 100) -> str:
        type(self)._invite_seq += 1
        resp = self.client.post(
            "/invites",
            json={
                "daily_quota_tokens": daily_quota_tokens,
                "invitee_email": f"tos-target-{type(self)._invite_seq:04d}@example.com",
            },
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["code"]


class TosEndpointTests(_BaseE2E):
    def test_tos_html_is_public(self):
        resp = self.client.get("/tos")
        self.assertEqual(resp.status_code, 200)
        # Markdown body is fetched client-side from /tos/raw and
        # rendered with marked.js; the static HTML shell carries the
        # title + version stamp.
        self.assertIn("Community ToS", resp.text)
        self.assertIn("Version", resp.text)
        self.assertIn("/tos/raw", resp.text)
        self.assertIn("marked", resp.text)

    def test_tos_raw_returns_markdown_with_version_header(self):
        resp = self.client.get("/tos/raw")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("# GamerAI Community Terms of Service", resp.text)
        self.assertIn("X-Tos-Version", resp.headers)
        self.assertEqual(resp.headers["X-Tos-Version"], self.coord_main.TOS_VERSION)

    def test_admin_seed_has_tos_accepted(self):
        me = self.client.get("/me", headers=self._admin_headers()).json()
        self.assertIsNotNone(me["tos"]["accepted_at"])
        self.assertEqual(me["tos"]["version"], self.coord_main.TOS_VERSION)
        self.assertFalse(me["tos"]["needs_reaccept"])


class InviteAcceptRequiresTosTests(_BaseE2E):
    _accept_seq = 0

    @classmethod
    def _payload(cls, **overrides) -> dict:
        cls._accept_seq += 1
        body = {
            "username": f"tosbob{cls._accept_seq:04d}",
            "password": "correct-horse-battery",
            "invitee_email": "bob@x.com",
            "tos_accepted": True,
        }
        body.update(overrides)
        return body

    def test_accept_without_tos_is_rejected(self):
        _, ctoken = self._make_contributor()
        code = self._create_invite(ctoken)
        resp = self.client.post(
            f"/invites/{code}/accept",
            json=self._payload(tos_accepted=False),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ToS", resp.json()["detail"])

    def test_accept_with_tos_records_version(self):
        _, ctoken = self._make_contributor(email="alice+tos@example.com")
        code = self._create_invite(ctoken)
        resp = self.client.post(
            f"/invites/{code}/accept", json=self._payload(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["tos_version"], self.coord_main.TOS_VERSION)
        me = self.client.get(
            "/me", headers={"Authorization": f"Bearer {body['token']}"}
        ).json()
        self.assertIsNotNone(me["tos"]["accepted_at"])
        self.assertEqual(me["tos"]["version"], self.coord_main.TOS_VERSION)


class CanaryVerifyResponseTests(unittest.TestCase):
    """Pure-function tests on the matcher — no DB or HTTP."""

    def _row(self, required):
        return {"required_tokens": json.dumps(required)}

    def test_all_tokens_present_lowercase(self):
        from coordinator.canaries import verify_response
        self.assertTrue(verify_response(self._row(["earth"]), "We live on Earth."))

    def test_case_insensitive(self):
        from coordinator.canaries import verify_response
        self.assertTrue(verify_response(self._row(["PACIFIC"]), "the pacific ocean"))

    def test_missing_token_fails(self):
        from coordinator.canaries import verify_response
        self.assertFalse(
            verify_response(self._row(["1969"]), "humans landed in nineteen sixty-nine")
        )

    def test_empty_response_fails(self):
        from coordinator.canaries import verify_response
        self.assertFalse(verify_response(self._row(["earth"]), ""))

    def test_malformed_required_tokens_is_treated_as_no_constraint(self):
        from coordinator.canaries import verify_response
        # A row whose required_tokens column is not JSON shouldn't crash
        # and should pass-through (no constraints to check).
        self.assertTrue(
            verify_response({"required_tokens": "not-json"}, "anything goes")
        )


class CanaryEndToEndTests(_BaseE2E):
    """Inject a canary directly into the queue, then have a 'worker'
    complete it via /jobs/complete and verify the score lands."""

    def _seed_canary(self, prompt: str, required_tokens: list[str], model: str = "llama3.2:1b") -> str:
        canary_id = "can_" + uuid.uuid4().hex[:12]
        self.db.create_canary(
            canary_id=canary_id,
            prompt=prompt,
            required_tokens_json=json.dumps(required_tokens),
            model=model,
            active=True,
        )
        return canary_id

    def _inject(self, canary_id: str) -> str:
        from shared.config import CANARY_PENDING
        canary = self.db.get_canary(canary_id)
        job_id = str(uuid.uuid4())
        self.r.hset(CANARY_PENDING, job_id, canary_id)
        self.db.insert_job(
            job_id=job_id,
            prompt=canary["prompt"],
            model=canary["model"],
            submitted_at=time.time(),
            submitted_by_member_id=None,
        )
        return job_id

    def _complete(self, worker_id: str, job_id: str, text: str):
        return self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "text": text,
                "model": "llama3.2:1b",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "duration_seconds": 1.0,
                "status": "complete",
            },
            headers=self._admin_headers(),
        )

    def test_matching_response_passes(self):
        cid = self._seed_canary("planet?", ["earth"])
        jid = self._inject(cid)
        wid = f"worker-pass-{uuid.uuid4().hex[:6]}"
        resp = self._complete(wid, jid, "We live on Earth.")
        self.assertEqual(resp.status_code, 200)
        # Earnings are zero for canaries (no real customer).
        self.assertEqual(resp.json()["earnings"], 0.0)
        score = self.db.canary_score_for_worker(wid)
        self.assertEqual(score, {"passed": 1, "total": 1, "score": 1.0})

    def test_non_matching_response_fails(self):
        cid = self._seed_canary("symbol for water?", ["h2o"])
        jid = self._inject(cid)
        wid = f"worker-fail-{uuid.uuid4().hex[:6]}"
        self._complete(wid, jid, "Water is wet and useful.")
        score = self.db.canary_score_for_worker(wid)
        self.assertEqual(score, {"passed": 0, "total": 1, "score": 0.0})

    def test_canary_does_not_credit_earnings(self):
        # Real /jobs/complete with non-canary job earns; canary does not.
        cid = self._seed_canary("planet?", ["earth"])
        jid = self._inject(cid)
        wid = f"worker-earnings-{uuid.uuid4().hex[:6]}"
        before = self.db.earnings_for(wid)
        self._complete(wid, jid, "Earth")
        after = self.db.earnings_for(wid)
        # No earnings row should have been created/updated for this worker.
        self.assertEqual(before, after)

    def test_worker_score_appears_on_workers_endpoint(self):
        cid = self._seed_canary("planet?", ["earth"])
        wid = f"worker-vis-{uuid.uuid4().hex[:6]}"
        # Worker has to exist for /workers to return it.
        self.db.upsert_worker(wid, "idle", time.time())
        jid = self._inject(cid)
        self._complete(wid, jid, "Earth")
        resp = self.client.get("/workers", headers=self._admin_headers())
        self.assertEqual(resp.status_code, 200)
        found = next(w for w in resp.json()["workers"] if w["worker_id"] == wid)
        self.assertIsNotNone(found["canary_score"])
        self.assertEqual(found["canary_score"]["score"], 1.0)


class CanaryTrafficGateTests(_BaseE2E):
    """A canary tick should be a no-op on an idle network — no real
    jobs submitted since the last injection — regardless of how many
    active canaries are seeded, and should fire once enough real
    traffic has accumulated."""

    def _seed_canary(self, prompt: str = "planet?", tokens=None, model: str = "llama3.2:1b") -> str:
        canary_id = "can_" + uuid.uuid4().hex[:12]
        self.db.create_canary(
            canary_id=canary_id,
            prompt=prompt,
            required_tokens_json=json.dumps(tokens or ["earth"]),
            model=model,
            active=True,
        )
        return canary_id

    def test_tick_skips_when_no_real_traffic(self):
        from shared.config import CANARY_PENDING, CANARY_REAL_JOBS_SINCE
        from coordinator.canaries import CanaryInjector

        self._seed_canary()
        self.r.delete(CANARY_REAL_JOBS_SINCE)
        before = self.r.hlen(CANARY_PENDING)
        injector = CanaryInjector(self.r, self.db, min_real_jobs=3)
        injector._tick()
        self.assertEqual(self.r.hlen(CANARY_PENDING), before)

    def test_tick_skips_below_threshold(self):
        from shared.config import CANARY_PENDING, CANARY_REAL_JOBS_SINCE
        from coordinator.canaries import CanaryInjector

        self._seed_canary()
        self.r.set(CANARY_REAL_JOBS_SINCE, 2)
        before = self.r.hlen(CANARY_PENDING)
        injector = CanaryInjector(self.r, self.db, min_real_jobs=3)
        injector._tick()
        self.assertEqual(self.r.hlen(CANARY_PENDING), before)

    def test_tick_fires_once_threshold_met_and_resets_counter(self):
        from shared.config import CANARY_PENDING, CANARY_REAL_JOBS_SINCE
        from coordinator.canaries import CanaryInjector

        self._seed_canary()
        # A live chat worker must be online for the tool-liveness gate
        # to allow firing — see test_tick_skips_when_no_live_chat_worker.
        _, t = self._make_contributor(email="canary-fire-worker@x.com")
        wid = f"wkr-canary-{uuid.uuid4().hex[:6]}"
        self.client.post(
            "/register", json={"worker_id": wid},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.r.set(CANARY_REAL_JOBS_SINCE, 3)
        before = self.r.hlen(CANARY_PENDING)
        injector = CanaryInjector(self.r, self.db, min_real_jobs=3)
        injector._tick()
        self.assertEqual(self.r.hlen(CANARY_PENDING), before + 1)
        self.assertEqual(int(self.r.get(CANARY_REAL_JOBS_SINCE)), 0)

    def test_tick_skips_when_no_live_chat_worker(self):
        """Reproduces the 2026-06-13 prod incident: real traffic on
        other tools (chat:smart, image, ...) satisfies the traffic
        gate, but no worker actually serves plain "chat" — the tool
        every canary envelope targets. A canary must not fire into a
        queue nobody is going to drain."""
        from shared.config import CANARY_PENDING, CANARY_REAL_JOBS_SINCE, WORKER_HEARTBEATS
        from coordinator.canaries import CanaryInjector

        self._seed_canary()
        self.r.set(CANARY_REAL_JOBS_SINCE, 3)
        # No worker registered — simulates a chat:smart-only network.
        # Explicit clear: this class shares one fakeredis instance across
        # all its tests (no per-test reset), and a sibling test registers
        # a worker of its own.
        self.r.delete(WORKER_HEARTBEATS)
        before = self.r.hlen(CANARY_PENDING)
        injector = CanaryInjector(self.r, self.db, min_real_jobs=3)
        injector._tick()
        self.assertEqual(self.r.hlen(CANARY_PENDING), before)
        # Counter stays put — a real job once a chat worker actually
        # comes online should still be able to trip the gate.
        self.assertEqual(int(self.r.get(CANARY_REAL_JOBS_SINCE)), 3)

    def test_generate_increments_real_job_counter(self):
        from shared.config import CANARY_REAL_JOBS_SINCE

        self.r.delete(CANARY_REAL_JOBS_SINCE)
        before = int(self.r.get(CANARY_REAL_JOBS_SINCE) or 0)
        resp = self.client.post(
            "/generate",
            json={"prompt": "hello canary gate", "tool": "chat"},
            headers=self._admin_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        after = int(self.r.get(CANARY_REAL_JOBS_SINCE) or 0)
        self.assertEqual(after, before + 1)


class WorkerOwnershipTests(_BaseE2E):
    """Worker→member binding (2026-05-13 security slice). A
    non-admin member cannot act on a worker_id owned by someone else.
    """

    def _post(self, path: str, token: str, json_body: dict | None = None):
        return self.client.post(
            path,
            json=json_body or {},
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_register_stamps_owner(self):
        _, t = self._make_contributor(email="reg-owner@x.com")
        wid = f"wkr-own-{uuid.uuid4().hex[:6]}"
        resp = self._post("/register", t, {"worker_id": wid})
        self.assertEqual(resp.status_code, 200, resp.text)
        # DB now records this member as the owner.
        owner = self.db.worker_owner(wid)
        self.assertIsNotNone(owner)
        # Resolve their member_id via the token to compare.
        from coordinator import member_auth
        their_id = member_auth.lookup_member_by_token(self.db, t).member_id
        self.assertEqual(owner, their_id)

    def test_register_rejects_when_owned_by_different_member(self):
        _, alice = self._make_contributor(email="alice-own@x.com")
        _, bob = self._make_contributor(email="bob-own@x.com")
        wid = f"wkr-conflict-{uuid.uuid4().hex[:6]}"
        self.assertEqual(self._post("/register", alice, {"worker_id": wid}).status_code, 200)
        # Bob tries to register the same worker_id — should be rejected.
        resp = self._post("/register", bob, {"worker_id": wid})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("different member", resp.json()["detail"])

    def test_re_register_by_same_owner_succeeds(self):
        _, t = self._make_contributor(email="re-reg@x.com")
        wid = f"wkr-re-{uuid.uuid4().hex[:6]}"
        self.assertEqual(self._post("/register", t, {"worker_id": wid}).status_code, 200)
        self.assertEqual(self._post("/register", t, {"worker_id": wid}).status_code, 200)

    def test_legacy_unowned_worker_is_adopted_by_first_caller(self):
        # Simulate a pre-migration row: worker exists but owner is NULL.
        wid = f"wkr-legacy-{uuid.uuid4().hex[:6]}"
        self.db.upsert_worker(wid, "idle", time.time())
        self.assertIsNone(self.db.worker_owner(wid))
        _, t = self._make_contributor(email="adopt@x.com")
        resp = self._post("/register", t, {"worker_id": wid})
        self.assertEqual(resp.status_code, 200)
        from coordinator import member_auth
        their_id = member_auth.lookup_member_by_token(self.db, t).member_id
        self.assertEqual(self.db.worker_owner(wid), their_id)

    def test_jobs_complete_rejects_non_owner(self):
        _, alice = self._make_contributor(email="alice-jobs@x.com")
        _, bob = self._make_contributor(email="bob-jobs@x.com")
        # Alice registers her worker.
        wid = f"wkr-spoof-{uuid.uuid4().hex[:6]}"
        self.assertEqual(self._post("/register", alice, {"worker_id": wid}).status_code, 200)
        # Bob tries to complete a job AS Alice's worker.
        resp = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": wid,
                "job_id": "fakejob",
                "text": "spoofed",
                "model": "llama3.2:1b",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "duration_seconds": 0.0,
                "status": "complete",
            },
            headers={"Authorization": f"Bearer {bob}"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("not your worker", resp.json()["detail"])

    def test_admin_can_act_on_any_worker(self):
        # Admin operational override — admins can complete jobs on
        # any worker for incident response.
        _, alice = self._make_contributor(email="alice-adminbypass@x.com")
        wid = f"wkr-admin-{uuid.uuid4().hex[:6]}"
        self.assertEqual(self._post("/register", alice, {"worker_id": wid}).status_code, 200)
        # Admin completes a "job" with Alice's worker_id — should pass
        # the ownership gate (admin bypass), and fall through to the
        # normal complete logic.
        resp = self.client.post(
            "/heartbeat",
            json={"worker_id": wid, "status": "idle"},
            headers=self._admin_headers(),
        )
        self.assertEqual(resp.status_code, 200)

    def test_jobs_complete_by_owner_works(self):
        _, t = self._make_contributor(email="happy@x.com")
        wid = f"wkr-happy-{uuid.uuid4().hex[:6]}"
        self.assertEqual(self._post("/register", t, {"worker_id": wid}).status_code, 200)
        # Submit a job (as admin, since members have quota; admin doesn't).
        sub = self.client.post(
            "/generate", json={"prompt": "hi"}, headers=self._admin_headers(),
        )
        jid = sub.json()["job_id"]
        # Owner must hold a current claim to complete — mint one.
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": wid, "job_id": jid},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        claim_token = claim.json()["claim_token"]
        # Complete it as the worker's owner.
        resp = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": wid,
                "job_id": jid,
                "text": "hello",
                "model": "llama3.2:1b",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "duration_seconds": 0.0,
                "status": "complete",
                "claim_token": claim_token,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class CanaryEnvelopeLeakTests(_BaseE2E):
    """Regression guard: the worker-facing job envelope must NOT
    contain submitted_by_member_id. Including it lets a malicious
    worker recognize canaries (submitted_by_member_id is NULL for
    canaries; non-null for real prompts) and selectively cheat."""

    def test_generated_job_envelope_omits_submitter(self):
        # Submit a real prompt (has a submitter — the admin).
        resp = self.client.post(
            "/generate",
            json={"prompt": "hello"},
            headers=self._admin_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        # Pop the queue and inspect the envelope a worker would see.
        raw = self.r.lpop("job_queue")
        self.assertIsNotNone(raw)
        envelope = json.loads(raw)
        self.assertNotIn(
            "submitted_by_member_id", envelope,
            "submitter leaked into worker envelope — canary detection vuln",
        )
        # Sanity: the fields the worker DOES need are present.
        self.assertIn("job_id", envelope)
        self.assertIn("prompt", envelope)

    def test_canary_envelope_indistinguishable_from_real_envelope(self):
        # Seed and inject a canary. Real prompt right after.
        canary_id = "can_" + uuid.uuid4().hex[:12]
        self.db.create_canary(
            canary_id=canary_id,
            prompt="planet?",
            required_tokens_json=json.dumps(["earth"]),
            model="llama3.2:1b",
            active=True,
        )
        from shared.config import CANARY_PENDING
        cjid = str(uuid.uuid4())
        self.r.hset(CANARY_PENDING, cjid, canary_id)
        self.db.insert_job(
            job_id=cjid, prompt="planet?", model="llama3.2:1b",
            submitted_at=time.time(), submitted_by_member_id=None,
        )
        # Use the actual production injector so the test exercises
        # the real envelope shape (not a hand-built one).
        from coordinator.canaries import CanaryInjector
        CanaryInjector(self.r, self.db)._inject(self.db.get_canary(canary_id))
        # Submit a real prompt to compare envelope shape.
        self.client.post("/generate", json={"prompt": "real"}, headers=self._admin_headers())
        # Both queue entries should have the SAME set of keys; if the
        # canary envelope has a missing key the real envelope has
        # (or vice versa) the worker can distinguish them.
        envelopes = []
        while True:
            raw = self.r.lpop("job_queue")
            if not raw:
                break
            envelopes.append(json.loads(raw))
        self.assertEqual(len(envelopes), 2)
        keys_a = set(envelopes[0].keys())
        keys_b = set(envelopes[1].keys())
        self.assertEqual(
            keys_a, keys_b,
            "canary vs real envelopes have different keys — worker can fingerprint",
        )


if __name__ == "__main__":
    unittest.main()
