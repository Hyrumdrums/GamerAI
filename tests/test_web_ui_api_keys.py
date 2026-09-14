"""Web UI coverage for the self-serve API-keys page (client/routes/api_keys.py),
which had zero test coverage before this file. Same harness as
test_web_ui_smoke.py: the web app's outbound httpx calls are routed through
an ASGI transport straight at the in-process coordinator app.

Run with ``python -m unittest tests.test_web_ui_api_keys``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# 1. Env BEFORE imports — auth on, fresh DB, no rate limit.
_TMPDIR = tempfile.mkdtemp(prefix="gamerai-test-webui-apikeys-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["API_TOKEN"] = "admin-seed-token-for-webui-apikey-tests"
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("STRICT_MODELS", None)

# 2. Drop cached modules (including the ``client`` package).
for _mod in list(sys.modules):
    if _mod.split(".", 1)[0] in ("shared", "coordinator", "client"):
        del sys.modules[_mod]

# 3. Patch the coordinator's Redis factory before main imports.
import fakeredis  # noqa: E402

import coordinator.redis_client  # noqa: E402

_FAKE = fakeredis.FakeStrictRedis(decode_responses=True)
coordinator.redis_client.get_client = lambda: _FAKE  # type: ignore[assignment]

# 4. Import the coordinator + web UI.
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from client import web as client_web  # noqa: E402
from client.services import coordinator_client as _coord_client  # noqa: E402
from coordinator import main as coordinator_main  # noqa: E402
from coordinator import member_auth  # noqa: E402

ADMIN_TOKEN = os.environ["API_TOKEN"]


def _patched_admin_client(bearer: str | None = None) -> httpx.AsyncClient:
    token = bearer or ADMIN_TOKEN
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=coordinator_main.app),
        base_url="http://coordinator",
        headers={"Authorization": f"Bearer {token}"},
    )


def _patched_public_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=coordinator_main.app),
        base_url="http://coordinator",
    )


_coord_client._client = _patched_admin_client  # type: ignore[assignment]
_coord_client._public_client = _patched_public_client  # type: ignore[assignment]


class ApiKeysPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = TestClient(client_web.app, follow_redirects=False)
        cls.coord = TestClient(coordinator_main.app)
        cls.db = coordinator_main.db
        coordinator_main.ensure_admin_seed()
        cls.web.cookies.set(client_web.SESSION_COOKIE, ADMIN_TOKEN)

    def setUp(self):
        _FAKE.flushall()
        self.db._conn.executescript(
            "DELETE FROM jobs; "
            "DELETE FROM workers; "
            "DELETE FROM earnings; "
            "DELETE FROM member_usage; "
            "DELETE FROM invites; "
            "DELETE FROM member_tokens; "
            "DELETE FROM members WHERE role NOT IN ('admin', 'guest');"
        )
        self.web.cookies.clear()
        self.web.cookies.set(client_web.SESSION_COOKIE, ADMIN_TOKEN)

    # ---- helpers -----------------------------------------------------
    _member_seq = 0

    def _make_member_and_cookie(self) -> str:
        """Create a plain member and return a web TestClient authenticated
        as them (session cookie is literally the bearer token)."""
        type(self)._member_seq += 1
        seq = type(self)._member_seq
        member_id = f"mem_webui_apikeys_{seq}"
        raw_token = member_auth.generate_token()
        self.db.create_member(
            member_id=member_id,
            email=f"apikeys{seq}@example.invalid",
            role="contributor",
            parent_member_id=None,
            token_hash=member_auth.hash_token(raw_token),
        )
        member = TestClient(client_web.app, follow_redirects=False)
        member.cookies.set(client_web.SESSION_COOKIE, raw_token)
        return member

    def _mint_key(self, raw_token: str, label: str | None = None) -> dict:
        resp = self.coord.post(
            "/me/api-keys",
            json={"label": label},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    # ------------------------------------------------------------------
    def test_page_redirects_anonymous_to_login(self):
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.get("/api-keys")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/login", resp.headers["location"])
        self.assertIn("next=/api-keys", resp.headers["location"])

    def test_page_renders_empty_state_for_signed_in_member(self):
        member = self._make_member_and_cookie()
        resp = member.get("/api-keys")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No API keys yet.", resp.text)

    def test_create_round_trips_and_shows_raw_key_once(self):
        member = self._make_member_and_cookie()
        resp = member.post("/api-keys", data={"label": "home assistant"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.text
        self.assertIn(member_auth.API_KEY_TOKEN_PREFIX, body)
        self.assertIn("Label: home assistant", body)

        # A raw key value present in this response.
        import re
        m = re.search(rf"{member_auth.API_KEY_TOKEN_PREFIX}[A-Za-z0-9_\-]+", body)
        self.assertIsNotNone(m)
        raw_key = m.group(0)

        # A subsequent GET never shows the raw key again, only the label.
        again = member.get("/api-keys")
        self.assertEqual(again.status_code, 200)
        self.assertNotIn(raw_key, again.text)
        self.assertIn("home assistant", again.text)

    def test_revoke_redirects_with_flash(self):
        member = self._make_member_and_cookie()
        raw_token = member.cookies.get(client_web.SESSION_COOKIE)
        created = self._mint_key(raw_token, label="to be revoked")

        resp = member.post(f"/api-keys/{created['id']}/revoke")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/api-keys", resp.headers["location"])
        self.assertIn("flash=", resp.headers["location"])

        listing = member.get("/api-keys")
        self.assertNotIn("to be revoked", listing.text)
        self.assertIn("No API keys yet.", listing.text)

    def test_revoke_unknown_key_shows_error_flash(self):
        member = self._make_member_and_cookie()
        resp = member.post("/api-keys/doesnotexist/revoke")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/api-keys", resp.headers["location"])
        self.assertIn("flash=", resp.headers["location"])


if __name__ == "__main__":
    unittest.main()
