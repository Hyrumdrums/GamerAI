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

    def _create_invite(self, contributor_token: str, daily_quota_tokens: int = 100) -> str:
        resp = self.client.post(
            "/invites",
            json={"daily_quota_tokens": daily_quota_tokens},
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["code"]


class TosEndpointTests(_BaseE2E):
    def test_tos_html_is_public(self):
        resp = self.client.get("/tos")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Community Terms", resp.text)
        self.assertIn("Version", resp.text)

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
    def test_accept_without_tos_is_rejected(self):
        _, ctoken = self._make_contributor()
        code = self._create_invite(ctoken)
        resp = self.client.post(f"/invites/{code}/accept", json={})  # tos_accepted defaults to False
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ToS", resp.json()["detail"])

    def test_accept_with_tos_records_version(self):
        _, ctoken = self._make_contributor(email="alice+tos@example.com")
        code = self._create_invite(ctoken)
        resp = self.client.post(
            f"/invites/{code}/accept",
            json={"tos_accepted": True, "invitee_email": "bob@x.com"},
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


if __name__ == "__main__":
    unittest.main()
