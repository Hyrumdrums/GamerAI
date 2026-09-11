"""Self-serve API keys + the OpenAI-compatible chat endpoint.

Mirrors the structure of test_pairing.py: auth ON, fakeredis, TestClient
against the coordinator app. Exercises:

- POST/GET/POST-revoke /me/api-keys round trip; raw key shown only once
- kind='agent' vs kind='api_key' rows don't leak into each other's list
- Generation-scoped keys 403 on account-management routes, 200 on
  /generate, /result, /me
- Full-access tokens (primary web token, agent-paired token) are
  unaffected by the scope refactor
- POST /v1/chat/completions non-streaming + streaming happy path
- Daily quota exceeded surfaces as an OpenAI-shaped 429 through
  /v1/chat/completions
- GET /v1/models only lists chat-kind models

Run with ``python -m unittest tests.test_api_keys``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid

_TMPDIR = tempfile.mkdtemp(prefix="gamerai-test-apikeys-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["API_TOKEN"] = "admin-seed-token-for-apikey-tests"
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("STRICT_MODELS", None)
os.environ["PUBLIC_BASE_URL"] = "https://example.invalid"
# Bound worst-case test hang time well below the 240s prod default — a
# real completion always lands in well under a second below.
os.environ["V1_CHAT_TIMEOUT_SECONDS"] = "5"

for _mod in list(sys.modules):
    if _mod.split(".", 1)[0] in ("shared", "coordinator"):
        del sys.modules[_mod]

import fakeredis  # noqa: E402

import coordinator.redis_client  # noqa: E402

_FAKE = fakeredis.FakeStrictRedis(decode_responses=True)
coordinator.redis_client.get_client = lambda: _FAKE  # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402

from coordinator import main as coordinator_main  # noqa: E402
from coordinator import member_auth  # noqa: E402
from coordinator import openai_compat  # noqa: E402

ADMIN_TOKEN = os.environ["API_TOKEN"]

# Speed up the internal poll loop so tests don't wait 200ms per tick.
openai_compat._POLL_INTERVAL_SECONDS = 0.02


class ApiKeysTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.db = coordinator_main.db
        coordinator_main.ensure_admin_seed()

    def setUp(self):
        _FAKE.flushall()
        self.db._conn.executescript(
            "DELETE FROM jobs; "
            "DELETE FROM workers; "
            "DELETE FROM earnings; "
            "DELETE FROM member_usage; "
            "DELETE FROM invites; "
            "DELETE FROM members; "
            "DELETE FROM member_tokens;"
        )
        coordinator_main.ensure_admin_seed()

    # ---- helpers -----------------------------------------------------
    def _make_member(self, role: str = "contributor", **quota) -> tuple[str, dict]:
        """Create a plain member with a known primary bearer. Returns
        (member_id, auth_headers)."""
        member_id = "mem_" + uuid.uuid4().hex[:12]
        raw_token = member_auth.generate_token()
        self.db.create_member(
            member_id=member_id,
            email=f"{member_id}@example.invalid",
            role=role,
            parent_member_id=None,
            token_hash=member_auth.hash_token(raw_token),
            **quota,
        )
        return member_id, {"Authorization": f"Bearer {raw_token}"}

    def _mint_api_key(self, headers: dict, label: str | None = None) -> dict:
        resp = self.client.post(
            "/me/api-keys", json={"label": label}, headers=headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _admin_headers(self) -> dict:
        return {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def _register_chat_worker(self, worker_id: str) -> None:
        # Ownership attribution doesn't matter for these tests (job
        # crediting keys off the *submitting* member, not the worker's
        # owner) — register under admin so this doesn't depend on which
        # member's token the test happens to be using elsewhere.
        resp = self.client.post(
            "/register", json={"worker_id": worker_id}, headers=self._admin_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _complete_next_chat_job_soon(self, worker_id: str, text: str = "hi there", delay: float = 0.05) -> None:
        """Background thread: shortly after the caller starts a blocking
        or streaming /v1/chat/completions call, pop the job a mock worker
        would see and complete it — same shape test_coordinator_e2e.py
        uses for /jobs/next + /jobs/complete."""
        def _worker():
            time.sleep(delay)
            admin_headers = self._admin_headers()
            nxt = self.client.post(
                "/jobs/next", json={"worker_id": worker_id, "tool": "chat"},
                headers=admin_headers,
            ).json()
            job = nxt.get("job")
            if not job:
                return
            self.client.post(
                "/jobs/complete",
                json={
                    "worker_id": worker_id,
                    "job_id": job["job_id"],
                    "claim_token": nxt.get("claim_token"),
                    "text": text,
                    "model": "mock",
                    "prompt_tokens": 5,
                    "completion_tokens": 7,
                    "duration_seconds": 0.1,
                    "status": "complete",
                },
                headers=admin_headers,
            )
        threading.Thread(target=_worker, daemon=True).start()


class ApiKeyLifecycleTests(ApiKeysTestBase):
    def test_create_list_revoke_round_trip(self):
        _, headers = self._make_member()
        created = self._mint_api_key(headers, label="my home assistant")
        self.assertTrue(created["api_key"].startswith(member_auth.API_KEY_TOKEN_PREFIX))
        self.assertEqual(created["label"], "my home assistant")

        listed = self.client.get("/me/api-keys", headers=headers).json()["api_keys"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], created["id"])
        self.assertEqual(listed[0]["label"], "my home assistant")
        # Raw key is never echoed back on list.
        self.assertNotIn("api_key", listed[0])

        # The minted key itself authenticates.
        key_headers = {"Authorization": f"Bearer {created['api_key']}"}
        self.assertEqual(self.client.get("/me", headers=key_headers).status_code, 200)

        revoke = self.client.post(
            f"/me/api-keys/{created['id']}/revoke", headers=headers,
        )
        self.assertEqual(revoke.status_code, 200)
        self.assertTrue(revoke.json()["deleted"])

        # Revoked key stops authenticating immediately.
        self.assertEqual(self.client.get("/me", headers=key_headers).status_code, 401)
        self.assertEqual(self.client.get("/me/api-keys", headers=headers).json()["api_keys"], [])

    def test_kind_filter_keeps_machines_and_api_keys_separate(self):
        member_id, headers = self._make_member()
        self._mint_api_key(headers, label="script")
        # Simulate a paired machine directly at the DB layer — same shape
        # /agents/pair/confirm writes, without driving the full pairing
        # flow (that's covered by test_pairing.py already).
        self.db.add_member_token(
            token_hash=member_auth.hash_token(member_auth.generate_token()),
            member_id=member_id,
            label="agent (paired)",
            when=time.time(),
        )

        machines = self.client.get("/me/machines", headers=headers).json()["machines"]
        self.assertEqual(len(machines), 1)
        self.assertEqual(machines[0]["label"], "agent (paired)")

        api_keys = self.client.get("/me/api-keys", headers=headers).json()["api_keys"]
        self.assertEqual(len(api_keys), 1)
        self.assertEqual(api_keys[0]["label"], "script")

    def test_revoke_scoped_to_owner_and_kind(self):
        _, headers_a = self._make_member()
        _, headers_b = self._make_member()
        created = self._mint_api_key(headers_a)
        # Another member can't revoke it.
        resp = self.client.post(
            f"/me/api-keys/{created['id']}/revoke", headers=headers_b,
        )
        self.assertEqual(resp.status_code, 404)
        # Still authenticates — the cross-member revoke was a no-op.
        key_headers = {"Authorization": f"Bearer {created['api_key']}"}
        self.assertEqual(self.client.get("/me", headers=key_headers).status_code, 200)


class ScopeEnforcementTests(ApiKeysTestBase):
    def test_generation_scoped_key_blocked_from_account_routes(self):
        _, headers = self._make_member()
        created = self._mint_api_key(headers)
        key_headers = {"Authorization": f"Bearer {created['api_key']}"}

        for method, path, body in (
            ("post", "/invites", {"invitee_email": "x@example.invalid"}),
            ("post", "/me/password", {"current_password": "x", "new_password": "y" * 10}),
            ("post", "/me/machines/abc123456789/unpair", None),
        ):
            resp = getattr(self.client, method)(path, json=body, headers=key_headers)
            self.assertEqual(
                resp.status_code, 403,
                f"{method.upper()} {path} should be scope-blocked, got {resp.status_code}: {resp.text}",
            )

    def test_generation_scoped_key_allowed_on_generation_routes(self):
        _, headers = self._make_member()
        created = self._mint_api_key(headers)
        key_headers = {"Authorization": f"Bearer {created['api_key']}"}

        self.assertEqual(self.client.get("/me", headers=key_headers).status_code, 200)
        gen = self.client.post(
            "/generate", json={"prompt": "hello"}, headers=key_headers,
        )
        self.assertEqual(gen.status_code, 200, gen.text)
        job_id = gen.json()["job_id"]
        self.assertEqual(
            self.client.get(f"/result/{job_id}", headers=key_headers).status_code, 200,
        )

    def test_primary_and_agent_tokens_unaffected_by_scope_refactor(self):
        member_id, headers = self._make_member()
        self.assertEqual(self.client.get("/me/machines", headers=headers).status_code, 200)
        self.assertNotEqual(
            self.client.post(
                "/me/password",
                json={"current_password": "x", "new_password": "y" * 10},
                headers=headers,
            ).status_code,
            403,
        )

        agent_raw = member_auth.generate_token()
        self.db.add_member_token(
            token_hash=member_auth.hash_token(agent_raw),
            member_id=member_id,
            label="agent (paired)",
            when=time.time(),
        )
        agent_headers = {"Authorization": f"Bearer {agent_raw}"}
        self.assertEqual(self.client.get("/me/machines", headers=agent_headers).status_code, 200)
        self.assertNotEqual(
            self.client.post(
                "/me/password",
                json={"current_password": "x", "new_password": "y" * 10},
                headers=agent_headers,
            ).status_code,
            403,
        )


class OpenAICompatTests(ApiKeysTestBase):
    def _key_headers(self) -> dict:
        _, headers = self._make_member()
        created = self._mint_api_key(headers)
        return {"Authorization": f"Bearer {created['api_key']}"}

    def test_chat_completions_non_streaming_happy_path(self):
        key_headers = self._key_headers()
        self._register_chat_worker("wkr-openai-nonstream")
        self._complete_next_chat_job_soon("wkr-openai-nonstream", text="hello from gamerai")

        resp = self.client.post(
            "/v1/chat/completions",
            json={
                "model": "llama3.2:3b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
            headers=key_headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "hello from gamerai")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["usage"]["completion_tokens"], 7)
        self.assertEqual(
            body["usage"]["total_tokens"],
            body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"],
        )

    def test_chat_completions_streaming_happy_path(self):
        key_headers = self._key_headers()
        self._register_chat_worker("wkr-openai-stream")
        self._complete_next_chat_job_soon("wkr-openai-stream", text="streamed answer")

        with self.client.stream(
            "POST", "/v1/chat/completions",
            json={
                "model": "llama3.2:3b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=key_headers,
        ) as resp:
            self.assertEqual(resp.status_code, 200)
            chunks = []
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                payload = line[len("data: "):]
                chunks.append(payload)

        self.assertEqual(chunks[-1], "[DONE]")
        deltas = []
        finish_reasons = []
        for raw in chunks[:-1]:
            evt = json.loads(raw)
            self.assertEqual(evt["object"], "chat.completion.chunk")
            delta = evt["choices"][0]["delta"]
            if "content" in delta:
                deltas.append(delta["content"])
            fr = evt["choices"][0].get("finish_reason")
            if fr:
                finish_reasons.append(fr)
        self.assertEqual("".join(deltas), "streamed answer")
        self.assertEqual(finish_reasons, ["stop"])

    def test_quota_exceeded_surfaces_as_openai_rate_limit_error(self):
        member_id, headers = self._make_member(daily_quota_tokens=1)
        created = self._mint_api_key(headers)
        key_headers = {"Authorization": f"Bearer {created['api_key']}"}
        # Seed today's usage already over the 1-token cap so /generate's
        # quota gate trips before any job is even enqueued — no worker
        # interaction needed for this one.
        self.db.add_member_usage(member_id, time.time(), tokens_in=0, tokens_out=100)

        resp = self.client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=key_headers,
        )
        self.assertEqual(resp.status_code, 429, resp.text)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "rate_limit_error")
        self.assertEqual(body["error"]["code"], 429)

    def test_v1_models_lists_only_chat_kind(self):
        _, headers = self._make_member()
        resp = self.client.get("/v1/models", headers=headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["object"], "list")
        self.assertTrue(body["data"])
        for entry in body["data"]:
            self.assertEqual(entry["object"], "model")
            self.assertEqual(entry["owned_by"], "gamerai")
        # A couple of known non-chat models must never appear here.
        ids = {entry["id"] for entry in body["data"]}
        self.assertNotIn("dreamshaperXL-lightning", ids)


if __name__ == "__main__":
    unittest.main()
