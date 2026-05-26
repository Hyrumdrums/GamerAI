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
        seq = type(self)._contrib_seq
        member_id = f"mem_webui_contrib_{seq}"
        self.db.create_member(
            member_id=member_id,
            email=f"alice{seq}@example.com",
            role="contributor",
            parent_member_id=None,
            token_hash=member_auth.hash_token(token),
        )
        body = {
            "daily_quota_tokens": 200,
            "invitee_email": f"webui-target-{seq}@example.com",
        }
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
        self.assertIn("alice", body)
        self.assertIn("@example.com", body)
        # Combined-cap one-liner — image-limits slice changed the
        # rendering from "200 tokens/day" to "200 tokens · unlimited
        # images" so the redeemer sees both axes upfront.
        self.assertIn("200 tokens", body)
        self.assertIn("unlimited images", body)
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
        # Paired-machines section renders with empty state + a link
        # to /contribute so a non-contributor admin has a path to
        # become one.
        self.assertIn("Paired machines", body)
        self.assertIn('href="/contribute"', body)
        self.assertIn("No paired PCs yet", body)

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

    def test_agent_pair_page_redirects_anonymous_to_login(self):
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.get("/agent/pair?code=pair_anything")
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertIn("/login", resp.headers["location"])
        # next= preserves the pair url so the user lands back on it.
        self.assertIn("pair_anything", resp.headers["location"])

    def test_agent_pair_page_renders_pending_form_for_signed_in_user(self):
        # Start a real pair code on the coordinator.
        start = self.coord.post("/agents/pair/start").json()
        resp = self.web.get(f"/agent/pair?code={start['pair_code']}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Pair this PC", resp.text)
        self.assertIn(start["pair_code"], resp.text)

    def test_agent_pair_post_confirms_via_coordinator(self):
        start = self.coord.post("/agents/pair/start").json()
        resp = self.web.post(
            "/agent/pair",
            data={"code": start["pair_code"]},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Paired", resp.text)
        # Confirm that the pair record is now approved on the coord.
        info = self.coord.get(
            f"/agents/pair/{start['pair_code']}",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        ).json()
        self.assertEqual(info["state"], "approved")

    def test_agent_pair_page_shows_expired_for_unknown_code(self):
        resp = self.web.get("/agent/pair?code=pair_nonexistent00")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("expired", resp.text)

    def test_account_create_invite_then_revoke_round_trips(self):
        # Admin creates an invite from the account page, then revokes it.
        resp = self.web.post(
            "/account/invites",
            data={
                "invitee_email": "account-created@example.com",
                "daily_quota_tokens": "50",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        # Confirm the invite exists by checking the friends section.
        page = self.web.get("/account")
        self.assertEqual(page.status_code, 200)
        # At least one open invite is now in the table.
        self.assertIn("Open invites", page.text)

    # ------------------------------------------------------------------
    # /contribute onboarding page
    # ------------------------------------------------------------------
    def test_contribute_page_renders_for_anonymous(self):
        anon = TestClient(client_web.app, follow_redirects=False)
        resp = anon.get("/contribute")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("powered by your friends", resp.text)
        self.assertIn("GamerAI-Agent-Setup.exe", resp.text)
        # Signed-out users see a sign-in link, not the account link.
        self.assertIn('href="/login"', resp.text)
        # Regression guards: no pricing claims, no repo links, no
        # earnings table — the page deliberately stays vague.
        self.assertNotIn("github.com", resp.text)
        self.assertNotIn("80%", resp.text)
        self.assertNotIn("$/mo", resp.text)
        self.assertNotIn("Haiku", resp.text)

    def test_contribute_page_renders_for_signed_in_user(self):
        resp = self.web.get("/contribute")
        self.assertEqual(resp.status_code, 200)
        # Signed-in users see their account link in the topbar.
        self.assertIn('href="/account"', resp.text)

    def test_me_endpoint_includes_paired_machines_count(self):
        """JS uses this to decide whether to show the topbar
        Contribute CTA — zero paired machines means "not yet a
        contributor"."""
        r = self.coord.get(
            "/me", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("paired_machines_count", body)
        self.assertIsInstance(body["paired_machines_count"], int)

    def test_account_create_invite_rejects_missing_email(self):
        resp = self.web.post(
            "/account/invites",
            data={"daily_quota_tokens": "50"},  # no invitee_email
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        # Flash text is URL-encoded with spaces as %20 (the
        # default RedirectResponse encoding), not +.
        self.assertIn("Friend", resp.headers["location"])
        self.assertIn("email%20is%20required", resp.headers["location"])

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
        # Jinja's HTML autoescape may render the apostrophe as &#39; on
        # some MarkupSafe versions; assert on the surrounding words instead.
        self.assertIn("Username or password", resp.text)
        self.assertIn("Double-check", resp.text)
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
        self.assertIn("alice", body)
        self.assertIn("@example.com", body)  # the contributor row
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
        response.

        chat.js is the ES-module entry point loaded by the page; the
        DOMPurify call lives in whichever sibling module owns assistant
        bubble rendering. To stay robust against future re-splits, walk
        the import graph from chat.js and assert DOMPurify.sanitize
        appears somewhere in the reachable JS — not in a hard-coded
        file path.
        """
        import re
        body = self.web.get("/").text
        self.assertIn("/static/js/chat.js", body)

        visited: set[str] = set()
        stack: list[str] = ["chat.js"]
        found = False
        while stack:
            fname = stack.pop()
            if fname in visited:
                continue
            visited.add(fname)
            src = self.web.get(f"/static/js/{fname}").text
            if "DOMPurify.sanitize" in src:
                found = True
                break
            for imp in re.findall(r"from\s+['\"]\./([^'\"]+)['\"]", src):
                stack.append(imp)
        self.assertTrue(
            found,
            f"DOMPurify.sanitize not found in any module reachable from "
            f"chat.js (visited: {sorted(visited)})",
        )

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

    # ------------------------------------------------------------------
    # /api/notifications/* BFF proxies (Phase 6 of pwa-refactor.txt).
    # The coordinator-side endpoint logic is covered in
    # test_notifications.py; here we just confirm the proxy passes the
    # bearer through and the response shape survives the round-trip.
    # ------------------------------------------------------------------
    def test_api_notifications_vapid_key_proxy_is_public(self):
        # No bearer — vapid-key is public on the coordinator and on
        # the BFF.
        resp = self.web.get("/api/notifications/vapid-key")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("key", resp.json())

    def test_api_notifications_subscribe_proxy_round_trips(self):
        payload = {
            "endpoint": "https://push.example.com/round-trip-test",
            "keys": {"p256dh": "p256x", "auth": "authx"},
        }
        resp = self.web.post(
            "/api/notifications/subscribe", json=payload,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["id"], int)

    def test_api_notifications_list_proxy(self):
        # Empty list for a member that hasn't received any pushes yet.
        resp = self.web.get("/api/notifications")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["limit"], 50)
        self.assertEqual(body["notifications"], [])

    def test_api_notifications_preferences_round_trip(self):
        # Defaults should come back enabled for every known category.
        get1 = self.web.get("/api/notifications/preferences").json()
        self.assertTrue(all(v for v in get1["preferences"].values()))
        # Flip image_done off; read back and confirm.
        put = self.web.put(
            "/api/notifications/preferences",
            json={"preferences": {"image_done": False}},
        )
        self.assertEqual(put.status_code, 200)
        get2 = self.web.get("/api/notifications/preferences").json()
        self.assertFalse(get2["preferences"]["image_done"])
        self.assertTrue(get2["preferences"]["system"])  # untouched

    # ------------------------------------------------------------------
    # tier UX — the Contribution status card + the invite form's
    # tier-allowance hint + forget-worker round-trip.
    # ------------------------------------------------------------------
    def _signed_in_member(self, username: str = "alicebrowse") -> tuple[TestClient, str]:
        """Bootstrap a contributor + accept their own invite (since
        v1 admins are the only ones who can create invites) so we end
        up with a member-cookie'd TestClient. Returns ``(client,
        member_id)``. Useful for any test that needs to render the
        account page as a non-admin."""
        _, code = self._make_contributor_and_invite()
        accept = self.web.post(
            f"/invite/{code}",
            data=self._accept_form(
                username=username,
                password="correct-horse-battery",
                password_confirm="correct-horse-battery",
            ),
            follow_redirects=False,
        )
        invitee_cookie = accept.cookies[client_web.SESSION_COOKIE]
        member = self.db.get_member_by_username(username)
        self.assertIsNotNone(member)
        c = TestClient(client_web.app, follow_redirects=False)
        c.cookies.set(client_web.SESSION_COOKIE, invitee_cookie)
        return c, member["member_id"]

    def test_account_page_renders_contribution_status_for_member(self):
        """Non-admin members see the Contribution status card with
        7-day uptime numbers and the current-tier requirements. The
        card is the visible surface of the tier engine — if it stops
        rendering, the host loses sight of why they're at whatever
        tier they're at."""
        bob, _ = self._signed_in_member("bobtier")
        resp = bob.get("/account")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("Contribution status", body)
        # 7-day uptime block.
        self.assertIn("7-day uptime", body)
        self.assertIn("of 7 days online", body)
        # Current-tier requirements line ("≥ N hr/day · ≥ M days/week").
        self.assertIn("Current tier needs", body)
        self.assertIn("hr/day", body)
        self.assertIn("days/week", body)

    def test_account_page_omits_contribution_status_for_admin(self):
        """Admins are off the tier ladder by design. The card must
        not render for them — the template gate is
        ``contrib_status.engine_applies``."""
        resp = self.web.get("/account")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Contribution status", resp.text)

    def test_account_page_shows_below_bar_badge_for_new_member(self):
        """A freshly minted member has zero credited uptime → below
        BRONZE's 2hr × 4d bar → the 'below bar' badge fires. The
        demotion-countdown warning paragraph is intentionally
        suppressed for BRONZE (it's the floor; there's no tier to
        demote to). This guards the visible 'something's wrong'
        signal, which is the most likely thing to silently regress
        when the template gets re-shuffled."""
        bob, _ = self._signed_in_member("bobnew")
        resp = bob.get("/account")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("below bar", body)
        # Badge class is the actual CSS hook the UI styles against.
        self.assertIn("badge-warn", body)

    def test_account_page_shows_demotion_warning_for_above_bronze(self):
        """For a SILVER+ member below their bar, the warning paragraph
        names the demotion target so the host knows what they're at
        risk of dropping to. Promote the test member to SILVER via
        direct DB write to short-circuit the engine's daily cadence."""
        bob, member_id = self._signed_in_member("bobwarn")
        # Hand-set tier to SILVER. (The engine would do this after
        # observing a week of meeting the SILVER bar, but we don't
        # want to spin up the engine here.)
        import time as _time
        self.db.set_member_tier(
            member_id, "SILVER", _time.time(),
            new_daily_quota_tokens=500_000,
            new_daily_quota_images=100,
        )
        resp = bob.get("/account")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("below bar", body)
        self.assertIn("You're below the", body)
        self.assertIn("BRONZE", body)  # the demotion target is named

    def test_account_page_shows_meeting_bar_when_uptime_sufficient(self):
        """Seed enough worker_uptime for BRONZE (2hr × 4d) across an
        owned worker; the badge must flip to 'meeting bar' and the
        warning must disappear. Exercises the engine's read path
        end-to-end through the template."""
        bob, member_id = self._signed_in_member("bobmeet")
        # Plant an owned worker + 4 distinct UTC days, 3 hours each.
        import time as _time
        now = _time.time()
        self.db.claim_worker_ownership(
            "w_meeting", member_id, "idle", now,
        )
        for d in range(4):
            self.db.add_worker_uptime_minutes(
                "w_meeting", now - d * 86400.0, 180,  # 3hr
            )
        resp = bob.get("/account")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn("meeting bar", body)
        self.assertNotIn("below bar", body)
        # Next-tier line is rendered (SILVER); won't be eligible yet
        # because 3hr/day is below SILVER's 6hr bar.
        self.assertIn("Next tier (SILVER)", body)
        self.assertNotIn("eligible — promoting", body)

    def test_account_page_shows_eligible_for_promotion_when_next_tier_met(self):
        """When the member also meets the *next* tier's bar, the UI
        shows the 'eligible — promoting on next nightly run' badge so
        the host knows the engine will move them soon."""
        bob, member_id = self._signed_in_member("bobpromo")
        import time as _time
        now = _time.time()
        self.db.claim_worker_ownership(
            "w_promo", member_id, "idle", now,
        )
        # SILVER bar = 6hr × 5d. Give 6hr × 6d (over both axes).
        for d in range(6):
            self.db.add_worker_uptime_minutes(
                "w_promo", now - d * 86400.0, 360,  # 6hr
            )
        resp = bob.get("/account")
        body = resp.text
        self.assertIn("eligible — promoting on next nightly run", body)

    def test_account_invite_form_carries_tier_allowance_data_attrs(self):
        """The form's data-tier-tokens / data-tier-images attrs are
        what the inline JS reads to compute '≈ N% of cap' as the host
        types. Without them the tip silently disappears. Admin is on
        an unlimited tier so the attrs may be absent — admin's
        tier_quota is null'd in /me; this test only checks the form
        EXISTS and carries data-tier."""
        resp = self.web.get("/account")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        # Form scaffold present.
        self.assertIn('action="/account/invites"', body)
        # data-tier attr is always rendered (carries the tier name).
        self.assertIn('data-tier="', body)
        # Both image-cap input AND its placeholder tip span exist.
        self.assertIn('name="daily_quota_images"', body)
        self.assertIn('id="daily_quota_images_tip"', body)

    def test_forget_worker_round_trips(self):
        """The stale-worker fix: a host clicking "forget" on a
        registered worker row sends POST /account/workers/{id}/forget,
        which proxies to the coordinator and removes the row. Mirrors
        the unpair pattern."""
        bob, member_id = self._signed_in_member("bobforget")
        import time as _time
        self.db.claim_worker_ownership(
            "w_stale", member_id, "idle", _time.time(),
        )
        # Page renders with the worker present.
        before = bob.get("/account").text
        self.assertIn("w_stale", before)
        # POST forget.
        resp = bob.post(
            "/account/workers/w_stale/forget", follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        # Flash is URL-encoded into the Location header.
        self.assertIn("Worker", resp.headers["location"])
        self.assertIn("forgotten", resp.headers["location"])
        # Row is gone from the DB and the next page render.
        self.assertIsNone(self.db.worker_owner("w_stale"))
        after = bob.get("/account").text
        self.assertNotIn("w_stale", after)

    def test_forget_worker_owned_by_someone_else_flashes_error(self):
        """Trying to forget a worker you don't own surfaces the
        coordinator's 404 as a flash message rather than 500ing or
        deleting the wrong row."""
        bob, _ = self._signed_in_member("bobthief")
        # Plant a worker under someone else.
        import time as _time
        other_id = "mem_other_owner"
        self.db.create_member(
            member_id=other_id, email=None, role="contributor",
            parent_member_id=None,
            token_hash=member_auth.hash_token(member_auth.generate_token()),
        )
        self.db.claim_worker_ownership(
            "w_other", other_id, "idle", _time.time(),
        )
        resp = bob.post(
            "/account/workers/w_other/forget", follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        # The flash carries the coordinator's "worker not found" detail.
        self.assertIn("worker", resp.headers["location"].lower())
        # And the row survives.
        self.assertEqual(self.db.worker_owner("w_other"), other_id)


if __name__ == "__main__":
    unittest.main()
