"""End-to-end pairing tests: agent → coordinator → browser → coordinator.

Mirrors the structure of test_member_auth_e2e: auth ON, fakeredis,
TestClient against the coordinator app. Exercises:

- POST /agents/pair/start emits a code + url
- GET /agents/pair/{code} returns state without revealing the token
- POST /agents/pair/{code}/confirm requires auth and approves
- POST /agents/pair/poll picks up the token exactly once
- The issued token authenticates as the confirming member
- Confirming does NOT rotate the member's primary token (web session
  on another device keeps working)

Run with ``python -m unittest tests.test_agent_pairing``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_TMPDIR = tempfile.mkdtemp(prefix="gamerai-test-pair-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["API_TOKEN"] = "admin-seed-token-for-pair-tests"
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("STRICT_MODELS", None)
os.environ["PUBLIC_BASE_URL"] = "https://example.invalid"

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

ADMIN_TOKEN = os.environ["API_TOKEN"]


class AgentPairingTests(unittest.TestCase):
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

    def _admin_headers(self) -> dict:
        return {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    # ------------------------------------------------------------------
    # /agents/pair/start
    # ------------------------------------------------------------------
    def test_start_is_public_and_returns_code_and_url(self):
        # No auth header — the agent has no token yet.
        r = self.client.post("/agents/pair/start")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["pair_code"].startswith("pair_"))
        self.assertIn("pair_url", body)
        self.assertTrue(
            body["pair_url"].startswith("https://example.invalid/agent/pair?code=")
        )

    # ------------------------------------------------------------------
    # /agents/pair/{code}
    # ------------------------------------------------------------------
    def test_info_returns_pending_for_fresh_code(self):
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        r = self.client.get(f"/agents/pair/{code}", headers=self._admin_headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "pending")

    def test_info_returns_404_for_unknown_code(self):
        r = self.client.get(
            "/agents/pair/pair_unknownsuffix12",
            headers=self._admin_headers(),
        )
        self.assertEqual(r.status_code, 404)

    def test_info_does_not_reveal_token_even_when_approved(self):
        # Approve a code, then re-read its info.
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        self.client.post(
            f"/agents/pair/{code}/confirm", headers=self._admin_headers()
        )
        r = self.client.get(f"/agents/pair/{code}", headers=self._admin_headers())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # State is exposed; the token is not.
        self.assertEqual(body["state"], "approved")
        self.assertNotIn("token", body)

    # ------------------------------------------------------------------
    # /agents/pair/{code}/confirm
    # ------------------------------------------------------------------
    def test_confirm_requires_auth(self):
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        r = self.client.post(f"/agents/pair/{code}/confirm")
        self.assertEqual(r.status_code, 401)

    def test_confirm_succeeds_for_authenticated_member(self):
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        r = self.client.post(
            f"/agents/pair/{code}/confirm", headers=self._admin_headers(),
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_confirm_410_after_second_confirm_attempt(self):
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        first = self.client.post(
            f"/agents/pair/{code}/confirm", headers=self._admin_headers(),
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            f"/agents/pair/{code}/confirm", headers=self._admin_headers(),
        )
        self.assertEqual(second.status_code, 410)

    def test_confirm_404_for_unknown_code(self):
        r = self.client.post(
            "/agents/pair/pair_unknownsuffix12/confirm",
            headers=self._admin_headers(),
        )
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------------------------
    # /agents/pair/poll
    # ------------------------------------------------------------------
    def test_poll_returns_pending_before_confirm(self):
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        r = self.client.post(
            "/agents/pair/poll", json={"pair_code": code},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "pending")

    def test_poll_returns_token_after_confirm(self):
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        self.client.post(
            f"/agents/pair/{code}/confirm", headers=self._admin_headers(),
        )
        r = self.client.post(
            "/agents/pair/poll", json={"pair_code": code},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["state"], "approved")
        self.assertTrue(body["token"].startswith("gai_"))
        self.assertEqual(body["member_id"], "mem_admin_seed")

    def test_poll_is_one_shot(self):
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        self.client.post(
            f"/agents/pair/{code}/confirm", headers=self._admin_headers(),
        )
        first = self.client.post("/agents/pair/poll", json={"pair_code": code})
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/agents/pair/poll", json={"pair_code": code})
        self.assertEqual(second.status_code, 404)

    def test_poll_404_for_unknown_code(self):
        r = self.client.post(
            "/agents/pair/poll", json={"pair_code": "pair_unknownsuffix12"},
        )
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------------------------
    # end-to-end: paired token authenticates as the right member
    # ------------------------------------------------------------------
    def test_paired_token_authenticates_as_confirming_member(self):
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        self.client.post(
            f"/agents/pair/{code}/confirm", headers=self._admin_headers(),
        )
        token = self.client.post(
            "/agents/pair/poll", json={"pair_code": code},
        ).json()["token"]
        me = self.client.get(
            "/me", headers={"Authorization": f"Bearer {token}"},
        ).json()
        self.assertEqual(me["member_id"], "mem_admin_seed")
        self.assertEqual(me["role"], "admin")

    def test_pairing_does_not_rotate_primary_token(self):
        """A web user signed in on another device must keep working
        after they pair an agent — we add to member_tokens rather than
        rotating members.token_hash."""
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        self.client.post(
            f"/agents/pair/{code}/confirm", headers=self._admin_headers(),
        )
        # Pick up the agent token (consumes the pair record).
        self.client.post("/agents/pair/poll", json={"pair_code": code})
        # Original ADMIN_TOKEN still works.
        me = self.client.get("/me", headers=self._admin_headers())
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["member_id"], "mem_admin_seed")

    # ------------------------------------------------------------------
    # /agents/pair/unpair
    # ------------------------------------------------------------------
    def _pair_and_get_token(self) -> str:
        code = self.client.post("/agents/pair/start").json()["pair_code"]
        self.client.post(
            f"/agents/pair/{code}/confirm", headers=self._admin_headers(),
        )
        return self.client.post(
            "/agents/pair/poll", json={"pair_code": code},
        ).json()["token"]

    def test_unpair_deletes_paired_token_and_revokes_access(self):
        token = self._pair_and_get_token()
        # Token works for /me beforehand.
        self.assertEqual(
            self.client.get(
                "/me", headers={"Authorization": f"Bearer {token}"},
            ).status_code,
            200,
        )
        r = self.client.post(
            "/agents/pair/unpair",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["deleted"])
        # Token no longer authenticates anything.
        self.assertEqual(
            self.client.get(
                "/me", headers={"Authorization": f"Bearer {token}"},
            ).status_code,
            401,
        )

    def test_unpair_is_idempotent_after_revocation(self):
        token = self._pair_and_get_token()
        first = self.client.post(
            "/agents/pair/unpair",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["deleted"])
        # Second call with the dead bearer — middleware rejects before
        # the handler runs, so we expect 401. The operational outcome
        # is the same either way: "the token doesn't work."
        second = self.client.post(
            "/agents/pair/unpair",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(second.status_code, 401)

    def test_unpair_leaves_primary_web_token_intact(self):
        """If someone manually pasted their /login token into the agent
        and then unpairs, the primary bearer in members.token_hash must
        stay valid — only member_tokens rows get removed."""
        r = self.client.post(
            "/agents/pair/unpair", headers=self._admin_headers(),
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["deleted"])
        # Admin web session still works.
        me = self.client.get("/me", headers=self._admin_headers())
        self.assertEqual(me.status_code, 200)

    def test_unpair_requires_auth(self):
        r = self.client.post("/agents/pair/unpair")
        self.assertEqual(r.status_code, 401)

    # ------------------------------------------------------------------
    # /me/machines + /me/machines/<prefix>/unpair
    # ------------------------------------------------------------------
    def test_machines_endpoint_lists_paired_agents(self):
        self._pair_and_get_token()
        r = self.client.get("/me/machines", headers=self._admin_headers())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["machines"]), 1)
        m = body["machines"][0]
        # 12-char prefix, never the raw bearer or full hash.
        self.assertEqual(len(m["id"]), 12)
        self.assertIn("agent (paired", m["label"])

    def test_machines_endpoint_empty_when_no_pairs(self):
        r = self.client.get("/me/machines", headers=self._admin_headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["machines"], [])

    def test_machines_unpair_revokes_credential(self):
        token = self._pair_and_get_token()
        m = self.client.get(
            "/me/machines", headers=self._admin_headers(),
        ).json()["machines"][0]
        r = self.client.post(
            f"/me/machines/{m['id']}/unpair",
            headers=self._admin_headers(),
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["deleted"])
        # The paired token no longer authenticates.
        self.assertEqual(
            self.client.get(
                "/me", headers={"Authorization": f"Bearer {token}"},
            ).status_code,
            401,
        )

    def test_machines_unpair_unknown_prefix_404s(self):
        r = self.client.post(
            "/me/machines/abc123def456/unpair",
            headers=self._admin_headers(),
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
