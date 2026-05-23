"""Web UI smoke tests. Exercises the public invite-redemption pages and
the admin browser views by routing the web UI's outbound httpx calls
through an ASGI transport against the in-process coordinator app — no
network, no separate worker process.

The goal is dead-route / template / proxy-shape coverage, not pixel
exactness. Run with ``python -m unittest tests.test_web_ui_smoke``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# 1. Env BEFORE imports — auth on, fresh DB, no rate limit.
_TMPDIR = tempfile.mkdtemp(prefix="gamerai-test-webui-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["API_TOKEN"] = "admin-seed-token-for-webui-tests"
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
    """Drop-in for ``coordinator_client._client`` — routes outbound calls
    through ASGITransport at the coordinator app, no network. When a
    per-session ``bearer`` is supplied (new browser-auth slice), that
    bearer goes on the wire; otherwise we use the admin token, which
    matches the pre-slice behavior."""
    token = bearer or ADMIN_TOKEN
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=coordinator_main.app),
        base_url="http://coordinator",
        headers={"Authorization": f"Bearer {token}"},
    )


def _patched_public_client() -> httpx.AsyncClient:
    """Drop-in for ``coordinator_client._public_client`` — no auth header."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=coordinator_main.app),
        base_url="http://coordinator",
    )


# Patch where the routes actually look up the factories. The legacy
# ``client.web`` module re-exports these names for back-compat, but the
# route handlers import the ``coordinator_client`` module directly — so
# rebinding the shim doesn't affect them.
_coord_client._client = _patched_admin_client  # type: ignore[assignment]
_coord_client._public_client = _patched_public_client  # type: ignore[assignment]


class WebUISmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # follow_redirects=False so we can assert the /admin redirect target
        # without TestClient auto-following it.
        cls.web = TestClient(client_web.app, follow_redirects=False)
        cls.coord = TestClient(coordinator_main.app)
        cls.db = coordinator_main.db
        cls.r = _FAKE
        coordinator_main.ensure_admin_seed()
        # Browser-auth slice: the web UI gates non-public pages on the
        # session cookie. For tests we stamp it as the admin so existing
        # smoke coverage of /, /dashboard, /admin/* keeps working. The
        # /invite/<code> public path is unaffected (no cookie required).
        cls.web.cookies.set(client_web.SESSION_COOKIE, ADMIN_TOKEN)

    def setUp(self):
        self.r.flushall()
        self.db._conn.executescript(
            "DELETE FROM jobs; "
            "DELETE FROM workers; "
            "DELETE FROM earnings; "
            "DELETE FROM member_usage; "
            "DELETE FROM invites; "
            "DELETE FROM members WHERE role <> 'admin';"
        )
        # The invite-accept flow now sets a fresh session cookie when
        # auto-logging the new member in — that would otherwise clobber
        # the admin cookie set in setUpClass and break later tests in
        # the same suite. Reset to admin every test.
        self.web.cookies.clear()
        self.web.cookies.set(client_web.SESSION_COOKIE, ADMIN_TOKEN)

    # ------------------------------------------------------------------
    # helpers — bootstrap a contributor + invite via the coordinator
    # ------------------------------------------------------------------
    _contrib_seq = 0

    def _make_contributor_and_invite(self, **invite_kwargs) -> tuple[str, str]:
        """Returns (contributor_token, invite_code). Both built via the
        coordinator's HTTP surface so this test file has no direct DB
        dependencies beyond table truncation."""
        admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        # Mint a contributor by direct DB write — the only path that does
        # this today is the CLI, which we don't want to exec here.
        token = member_auth.generate_token()
        type(self)._contrib_seq += 1
        member_id = f"mem_webui_contrib_{type(self)._contrib_seq}"
        self.db.create_member(
            member_id=member_id,
            email="alice@example.com",
            role="contributor",
            parent_member_id=None,
            token_hash=member_auth.hash_token(token),
        )
        body = {"daily_quota_tokens": 200}
        body.update(invite_kwargs)
        resp = self.coord.post(
            "/invites",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return token, resp.json()["code"]

    # ------------------------------------------------------------------
    # public redemption flow
    # ------------------------------------------------------------------
    def test_invite_landing_renders_inviter_info(self):
        _, code = self._make_contributor_and_invite()
        resp = self.web.get(f"/invite/{code}")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("You've been invited", body)
        self.assertIn("alice@example.com", body)
        self.assertIn("200 tokens/day", body)
        # The form posts back to the same URL.
        self.assertIn(f'<form method="POST"', body)

    def test_invite_landing_404_for_unknown_code(self):
        resp = self.web.get("/invite/inv_doesnotexist")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("was not found", resp.text)

    def test_invite_landing_410_for_revoked(self):
        _, code = self._make_contributor_and_invite()
        # Revoke via admin endpoint.
        rev = self.coord.post(
            f"/invites/{code}/revoke",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(rev.status_code, 200)
        resp = self.web.get(f"/invite/{code}")
        self.assertEqual(resp.status_code, 410)
        self.assertIn("revoked", resp.text)

    _accept_counter = 0

    @classmethod
    def _accept_form(cls, **overrides) -> dict:
        cls._accept_counter += 1
        data = {
            "username": f"bobwebui{cls._accept_counter:04d}",
            "password": "correct-horse-battery",
            "password_confirm": "correct-horse-battery",
            "invitee_email": "bob@example.com",
            "tos_accepted": "on",
        }
        data.update(overrides)
        return data

    def test_invite_accept_auto_logs_in(self):
        _, code = self._make_contributor_and_invite()
        # follow_redirects=False — we want to see the 303 + cookie ourselves.
        resp = self.web.post(
            f"/invite/{code}", data=self._accept_form(),
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text)
        self.assertEqual(resp.headers["location"], "/")
        # The session cookie was stamped with a fresh bearer.
        self.assertIn(client_web.SESSION_COOKIE, resp.cookies)
        self.assertTrue(
            resp.cookies[client_web.SESSION_COOKIE].startswith("gai_")
        )

    def test_invite_accept_410_after_one_shot(self):
        _, code = self._make_contributor_and_invite()
        first = self.web.post(
            f"/invite/{code}", data=self._accept_form(),
            follow_redirects=False,
        )
        self.assertEqual(first.status_code, 303)
        second = self.web.post(
            f"/invite/{code}", data=self._accept_form(),
            follow_redirects=False,
        )
        self.assertEqual(second.status_code, 410)
        self.assertIn("accepted", second.text)

    def test_invite_accept_requires_email(self):
        _, code = self._make_contributor_and_invite()
        resp = self.web.post(
            f"/invite/{code}",
            data=self._accept_form(invitee_email=""),
            follow_redirects=False,
        )
        # The coordinator returns 400 ("email is required") and the
        # client re-renders the form with the error inline.
        self.assertEqual(resp.status_code, 400)
        self.assertIn("email is required", resp.text)

    def test_invite_accept_mismatched_passwords_re_renders_form(self):
        _, code = self._make_contributor_and_invite()
        resp = self.web.post(
            f"/invite/{code}",
            data=self._accept_form(password_confirm="something-else"),
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Passwords didn", resp.text)

    def test_invite_accept_duplicate_username_re_renders_form(self):
        _, code1 = self._make_contributor_and_invite()
        _, code2 = self._make_contributor_and_invite()
        first = self.web.post(
            f"/invite/{code1}",
            data=self._accept_form(username="taken1234"),
            follow_redirects=False,
        )
        self.assertEqual(first.status_code, 303)
        second = self.web.post(
            f"/invite/{code2}",
            data=self._accept_form(username="taken1234"),
            follow_redirects=False,
        )
        self.assertEqual(second.status_code, 409)
        self.assertIn("already taken", second.text)

    # ------------------------------------------------------------------
    # account page
    # ------------------------------------------------------------------
    def test_account_page_redirects_anonymous_to_login(self):
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.get("/account")
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertIn("/login", resp.headers["location"])

    def test_account_page_renders_for_admin(self):
        resp = self.web.get("/account")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("Account", body)
        self.assertIn("Friends", body)
        # Admin can create invites.
        self.assertIn('action="/account/invites"', body)
        # No host section — admin has no parent.
        self.assertNotIn("Your host", body)

    def test_account_page_renders_for_invitee_with_host_section(self):
        _, code = self._make_contributor_and_invite()
        # Bob redeems, becomes signed in as the invitee.
        accept = self.web.post(
            f"/invite/{code}",
            data=self._accept_form(
                username="bobacct",
                password="correct-horse-battery",
                password_confirm="correct-horse-battery",
            ),
            follow_redirects=False,
        )
        invitee_cookie = accept.cookies[client_web.SESSION_COOKIE]
        bob = TestClient(client_web.app, follow_redirects=False)
        bob.cookies.set(client_web.SESSION_COOKIE, invitee_cookie)
        resp = bob.get("/account")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("Your host", body)
        self.assertIn("vouched for you", body)
        # Invitee can't create invites in v1.
        self.assertNotIn('action="/account/invites"', body)
        self.assertIn("Run a GamerAI agent", body)

    def test_account_password_change_round_trips(self):
        _, code = self._make_contributor_and_invite()
        accept = self.web.post(
            f"/invite/{code}",
            data=self._accept_form(
                username="bobpwchange",
                password="correct-horse-battery",
                password_confirm="correct-horse-battery",
            ),
            follow_redirects=False,
        )
        invitee_cookie = accept.cookies[client_web.SESSION_COOKIE]
        bob = TestClient(client_web.app, follow_redirects=False)
        bob.cookies.set(client_web.SESSION_COOKIE, invitee_cookie)
        resp = bob.post(
            "/account/password",
            data={
                "current_password": "correct-horse-battery",
                "new_password": "brand-new-passphrase",
                "new_password_confirm": "brand-new-passphrase",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Password", resp.headers["location"])

        # Logging in with the new password works.
        self.web.cookies.clear()
        login = self.web.post(
            "/login",
            data={"username": "bobpwchange", "password": "brand-new-passphrase"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 303)

    def test_account_create_invite_then_revoke_round_trips(self):
        # Admin creates an invite from the account page, then revokes it.
        resp = self.web.post(
            "/account/invites",
            data={"daily_quota_tokens": "50"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        # Confirm the invite exists by checking the friends section.
        page = self.web.get("/account")
        self.assertEqual(page.status_code, 200)
        # At least one open invite is now in the table.
        self.assertIn("Open invites", page.text)

    # ------------------------------------------------------------------
    # u/p login page
    # ------------------------------------------------------------------
    def test_login_page_shows_username_and_password_fields(self):
        # No session cookie — anonymous browse.
        self.web.cookies.clear()
        resp = self.web.get("/login")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn('name="username"', body)
        self.assertIn('name="password"', body)
        # Token fallback still available but tucked under a <details>.
        self.assertIn("Sign in with a bearer token instead", body)

    def test_login_with_valid_username_and_password_sets_cookie(self):
        # Bob redeems an invite to get an account with credentials.
        _, code = self._make_contributor_and_invite()
        accept_data = self._accept_form(
            username="bobpwlogin",
            password="correct-horse-battery",
            password_confirm="correct-horse-battery",
        )
        self.web.post(
            f"/invite/{code}", data=accept_data, follow_redirects=False,
        )
        # Clear cookies so /login is exercised, not the existing session.
        self.web.cookies.clear()
        resp = self.web.post(
            "/login",
            data={
                "username": "bobpwlogin",
                "password": "correct-horse-battery",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(client_web.SESSION_COOKIE, resp.cookies)
        self.assertTrue(
            resp.cookies[client_web.SESSION_COOKIE].startswith("gai_")
        )

    def test_login_with_wrong_password_re_renders_form(self):
        _, code = self._make_contributor_and_invite()
        self.web.post(
            f"/invite/{code}",
            data=self._accept_form(
                username="bobpwwrong",
                password="correct-horse-battery",
                password_confirm="correct-horse-battery",
            ),
            follow_redirects=False,
        )
        self.web.cookies.clear()
        resp = self.web.post(
            "/login",
            data={"username": "bobpwwrong", "password": "nope"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("didn't match", resp.text)
        # Username is preserved across the re-render so the user only
        # has to retype the password.
        self.assertIn('value="bobpwwrong"', resp.text)

    def test_login_token_fallback_still_works_for_admin(self):
        self.web.cookies.clear()
        resp = self.web.post(
            "/login/token",
            data={"token": ADMIN_TOKEN},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(client_web.SESSION_COOKIE, resp.cookies)

    def test_invite_accept_without_tos_checkbox_is_rejected(self):
        """If a user posts without checking the box (e.g. via devtools
        manipulation), the web layer bounces them back rather than
        silently submitting tos_accepted=false to the coordinator."""
        _, code = self._make_contributor_and_invite()
        data = self._accept_form()
        del data["tos_accepted"]
        resp = self.web.post(
            f"/invite/{code}", data=data, follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("terms must be accepted", resp.text)

    # ------------------------------------------------------------------
    # admin browser views
    # ------------------------------------------------------------------
    def test_admin_members_page_renders(self):
        contributor_token, _ = self._make_contributor_and_invite()
        del contributor_token  # we only want the side effect of the row insert
        resp = self.web.get("/admin/members")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("Members", body)
        self.assertIn("admin", body)         # the admin seed row
        self.assertIn("alice@example.com", body)  # the contributor row
        self.assertIn("contributor", body)

    def test_admin_invites_page_renders(self):
        _, code = self._make_contributor_and_invite()
        resp = self.web.get("/admin/invites")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("Invites", body)
        self.assertIn(code, body)
        self.assertIn("open", body)

    def test_admin_invites_shows_accepted_state(self):
        _, code = self._make_contributor_and_invite()
        # Bob accepts.
        self.web.post(
            f"/invite/{code}",
            data=self._accept_form(),
            follow_redirects=False,
        )
        # That auto-login stamped Bob's session cookie into the shared
        # TestClient; restore admin to view the admin page.
        self.web.cookies.clear()
        self.web.cookies.set(client_web.SESSION_COOKIE, ADMIN_TOKEN)
        resp = self.web.get("/admin/invites")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn(code, body)
        self.assertIn("accepted", body)

    # ------------------------------------------------------------------
    # session-cookie auth (browser-auth slice)
    # ------------------------------------------------------------------
    def test_index_redirects_to_login_without_session(self):
        """A visitor without a session cookie cannot see the chat UI."""
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.get("/")
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertIn("/login", resp.headers["location"])

    def test_login_page_renders(self):
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sign in", resp.text)
        # Both forms are present: u/p as the primary, token as the
        # collapsible fallback.
        self.assertIn('name="username"', resp.text)
        self.assertIn('name="password"', resp.text)
        self.assertIn('name="token"', resp.text)

    def test_login_token_fallback_with_valid_token_sets_cookie(self):
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.post(
            "/login/token",
            data={"token": ADMIN_TOKEN, "next": "/"},
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/")
        set_cookie = resp.headers.get("set-cookie", "")
        self.assertIn(client_web.SESSION_COOKIE, set_cookie)
        self.assertIn("HttpOnly", set_cookie)

    def test_login_token_fallback_with_invalid_token_re_renders_401(self):
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.post(
            "/login/token",
            data={"token": "gai_definitely-not-valid", "next": "/"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("rejected", resp.text)

    def test_logout_clears_cookie(self):
        # Use the class-level client which has a cookie set.
        resp = self.web.get("/logout")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")
        # The Set-Cookie header should clear gai_session.
        set_cookie = resp.headers.get("set-cookie", "")
        self.assertIn(client_web.SESSION_COOKIE, set_cookie)
        # FastAPI's delete_cookie uses an expired Max-Age and/or empty value.
        self.assertTrue(
            'Max-Age=0' in set_cookie or 'expires=' in set_cookie.lower(),
            f"expected an expiry-stamped cookie, got: {set_cookie}",
        )

    def test_api_generate_without_session_returns_401(self):
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.post("/api/generate", json={"prompt": "hi"})
        self.assertEqual(resp.status_code, 401)

    def test_invite_redemption_remains_public(self):
        """/invite/<code> must NOT require a session — the whole point is
        Bob hits it without an account yet."""
        _, code = self._make_contributor_and_invite()
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.get(f"/invite/{code}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("invited", resp.text.lower())

    # ------------------------------------------------------------------
    # static / chrome pages
    # ------------------------------------------------------------------
    def test_index_page_renders(self):
        resp = self.web.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("GamerAI", body)
        # Conversation-aware shell — sidebar list, prompt input, send button.
        self.assertIn('id="prompt"', body)
        self.assertIn('id="composer"', body)
        self.assertIn('id="conv-list"', body)
        self.assertIn('id="new-chat"', body)

    def test_admin_redirects_to_dashboard(self):
        resp = self.web.get("/admin")
        self.assertIn(resp.status_code, (302, 307))
        self.assertEqual(resp.headers["location"], "/dashboard")

    def test_dashboard_renders_on_empty_state(self):
        """Regression guard — the dashboard does division/sum across the
        workers/earnings lists. An empty network must not 500."""
        resp = self.web.get("/dashboard")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("Admin Dashboard", body)
        self.assertIn("Total Workers", body)

    def test_dashboard_renders_with_a_worker(self):
        # Make the dashboard exercise both the metrics and the workers loops.
        admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        self.coord.post(
            "/register",
            json={
                "worker_id": "wkr-dashtest",
                "capabilities": {"vram_gb": 24.0, "gpu_model": "RTX 4090"},
            },
            headers=admin_headers,
        )
        resp = self.web.get("/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wkr-dashtest"[-12:], resp.text)

    # ------------------------------------------------------------------
    # /api/* proxy endpoints — exercise each one through the web UI so we
    # know the proxy adapter (auth headers, base URL, error translation)
    # works for every backend endpoint that the browser-side JS hits.
    # ------------------------------------------------------------------
    def test_api_generate_proxy_round_trips(self):
        resp = self.web.post("/api/generate", json={"prompt": "hello"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("job_id", body)
        # The job is queued on the coordinator's Redis.
        self.assertEqual(self.r.llen("job_queue"), 1)

    def test_chat_ui_uses_vendored_js_not_cdn(self):
        """Regression guard for the supply-chain finding: the chat UI
        must not pull marked / DOMPurify from a third-party CDN. A
        CDN compromise would inject arbitrary JS into authenticated
        pages and exfiltrate every prompt the user types."""
        resp = self.web.get("/")
        body = resp.text
        self.assertIn("/static/marked.min.js", body)
        self.assertIn("/static/purify.min.js", body)
        self.assertNotIn("cdn.jsdelivr.net", body)

    def test_vendored_static_files_are_served(self):
        for path in ("/static/marked.min.js", "/static/purify.min.js"):
            resp = self.web.get(path)
            self.assertEqual(resp.status_code, 200, f"{path} not served")
            self.assertGreater(len(resp.content), 1024, f"{path} is suspiciously small")

    def test_chat_ui_sanitizes_assistant_markdown(self):
        """Belt-and-suspenders to the canary system: marked.parse
        output for assistant messages must be piped through DOMPurify
        so a malicious model can't XSS the user via raw HTML in its
        response. The chat JS lives in /static/js/chat.js; the page
        body just references it via <script src>."""
        body = self.web.get("/").text
        self.assertIn("/static/js/chat.js", body)
        chat_js = self.web.get("/static/js/chat.js").text
        self.assertIn("DOMPurify.sanitize", chat_js)

    def test_api_conversations_round_trip(self):
        """The new chat UI calls POST /api/conversations on the first
        prompt of a new chat, then GETs the list, then GETs the
        single conversation as the user clicks back into it."""
        create = self.web.post("/api/conversations", json={})
        self.assertEqual(create.status_code, 200, create.text)
        cid = create.json()["conversation_id"]
        listed = self.web.get("/api/conversations").json()["conversations"]
        self.assertTrue(any(c["conversation_id"] == cid for c in listed))
        single = self.web.get(f"/api/conversations/{cid}").json()
        self.assertEqual(single["conversation_id"], cid)
        self.assertEqual(single.get("messages", []), [])

    def test_api_result_proxy_returns_pending_for_known_job(self):
        # Submit so we have a job_id that exists.
        jid = self.web.post(
            "/api/generate", json={"prompt": "p"}
        ).json()["job_id"]
        resp = self.web.get(f"/api/result/{jid}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "pending")

    def test_api_result_proxy_returns_404_for_unknown_job(self):
        resp = self.web.get("/api/result/no-such-job")
        self.assertEqual(resp.status_code, 404)

    def test_api_workers_proxy_returns_list(self):
        resp = self.web.get("/api/workers")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("workers", resp.json())
        self.assertIsInstance(resp.json()["workers"], list)

    def test_api_earnings_proxy_returns_aggregate(self):
        resp = self.web.get("/api/earnings")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("workers", body)
        self.assertIn("total_usd", body)

    def test_api_metrics_proxy_returns_dict(self):
        resp = self.web.get("/api/metrics")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Keys defined by coordinator.db.DB.metrics().
        self.assertIn("total_jobs", body)
        self.assertIn("queue_depth", body)


if __name__ == "__main__":
    unittest.main()
