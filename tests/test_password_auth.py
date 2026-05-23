"""u/p sign-in end-to-end: schema migration, CLI credentials, /login,
/me/password. Auth is ON throughout so the public-path allowance for
``POST /login`` is also exercised.

Run with ``python -m unittest tests.test_password_auth``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# 1. Env BEFORE imports — auth on, fresh DB, no rate limit.
_TMPDIR = tempfile.mkdtemp(prefix="gamerai-test-pwauth-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["API_TOKEN"] = "admin-seed-token-for-pwauth-tests"
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("STRICT_MODELS", None)

for _mod in list(sys.modules):
    if _mod.split(".", 1)[0] in ("shared", "coordinator"):
        del sys.modules[_mod]

import fakeredis  # noqa: E402

import coordinator.redis_client  # noqa: E402

_FAKE = fakeredis.FakeStrictRedis(decode_responses=True)
coordinator.redis_client.get_client = lambda: _FAKE  # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402

from coordinator import admin as coordinator_admin  # noqa: E402
from coordinator import main as coordinator_main  # noqa: E402
from coordinator import member_auth  # noqa: E402

ADMIN_TOKEN = os.environ["API_TOKEN"]


def _make_contributor(client: TestClient) -> str:
    from contextlib import redirect_stdout
    from io import StringIO

    buf = StringIO()
    with redirect_stdout(buf):
        coordinator_admin.main(["create-member", "--role", "contributor"])
    for line in buf.getvalue().splitlines():
        if line.startswith("token="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"no token in CLI output:\n{buf.getvalue()}")


class PasswordAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.db = coordinator_main.db
        coordinator_main.ensure_admin_seed()

    def setUp(self):
        # /login rotates the admin's token_hash, so tests can't share a
        # single seeded admin row across cases. Drop EVERYTHING and re-seed
        # from API_TOKEN so each test starts with the original bearer intact.
        _FAKE.flushall()
        self.db._conn.executescript(
            "DELETE FROM jobs; "
            "DELETE FROM workers; "
            "DELETE FROM earnings; "
            "DELETE FROM member_usage; "
            "DELETE FROM invites; "
            "DELETE FROM members;"
        )
        coordinator_main.ensure_admin_seed()

    # ------------------------------------------------------------------
    # validators
    # ------------------------------------------------------------------
    def test_validate_username_strips_and_accepts_handle(self):
        self.assertEqual(member_auth.validate_username("  alice "), "alice")

    def test_validate_username_rejects_too_short(self):
        with self.assertRaises(ValueError):
            member_auth.validate_username("ab")

    def test_validate_username_rejects_punctuation(self):
        with self.assertRaises(ValueError):
            member_auth.validate_username("alice!")

    def test_validate_password_rejects_too_short(self):
        with self.assertRaises(ValueError):
            member_auth.validate_password("short")

    def test_hash_and_verify_password_roundtrip(self):
        h = member_auth.hash_password("hunter2hunter")
        self.assertTrue(member_auth.verify_password("hunter2hunter", h))
        self.assertFalse(member_auth.verify_password("wrong-password", h))
        self.assertFalse(member_auth.verify_password("anything", None))

    # ------------------------------------------------------------------
    # admin CLI: set-credentials
    # ------------------------------------------------------------------
    def test_set_credentials_lets_admin_log_in(self):
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "rootadmin",
            "--password", "correct-horse-battery",
        ])
        r = self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "correct-horse-battery"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["token"].startswith("gai_"))
        self.assertEqual(body["role"], "admin")
        self.assertEqual(body["username"], "rootadmin")

    def test_set_credentials_username_case_insensitive_login(self):
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "MixedCase",
            "--password", "correct-horse-battery",
        ])
        r = self.client.post(
            "/login",
            json={"username": "mixedcase", "password": "correct-horse-battery"},
        )
        self.assertEqual(r.status_code, 200)

    def test_set_credentials_rejects_invalid_password(self):
        with self.assertRaises(SystemExit):
            coordinator_admin.main([
                "set-credentials",
                "--token", ADMIN_TOKEN,
                "--username", "rootadmin",
                "--password", "short",
            ])

    def test_set_credentials_rejects_unknown_token(self):
        with self.assertRaises(SystemExit):
            coordinator_admin.main([
                "set-credentials",
                "--token", "gai_unknown",
                "--username", "ghost",
                "--password", "correct-horse-battery",
            ])

    def test_set_credentials_rejects_duplicate_username(self):
        # Admin claims the name first.
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "shared",
            "--password", "correct-horse-battery",
        ])
        contributor_token = _make_contributor(self.client)
        with self.assertRaises(SystemExit):
            coordinator_admin.main([
                "set-credentials",
                "--token", contributor_token,
                "--username", "shared",
                "--password", "different-password",
            ])

    # ------------------------------------------------------------------
    # POST /login
    # ------------------------------------------------------------------
    def test_login_is_public_no_bearer_required(self):
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "rootadmin",
            "--password", "correct-horse-battery",
        ])
        # Notably: NO Authorization header on this request.
        r = self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "correct-horse-battery"},
        )
        self.assertEqual(r.status_code, 200)

    def test_login_with_unknown_username_returns_401(self):
        r = self.client.post(
            "/login",
            json={"username": "nobody", "password": "whatever"},
        )
        self.assertEqual(r.status_code, 401)

    def test_login_with_wrong_password_returns_401(self):
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "rootadmin",
            "--password", "correct-horse-battery",
        ])
        r = self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "WRONG"},
        )
        self.assertEqual(r.status_code, 401)

    def test_login_returns_same_detail_for_unknown_and_bad_password(self):
        """No account-enumeration via 401 detail strings."""
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "rootadmin",
            "--password", "correct-horse-battery",
        ])
        unknown = self.client.post(
            "/login", json={"username": "ghost", "password": "x"},
        ).json()
        bad_pw = self.client.post(
            "/login", json={"username": "rootadmin", "password": "x"},
        ).json()
        self.assertEqual(unknown["detail"], bad_pw["detail"])

    def test_login_rotates_token_and_old_token_stops_working(self):
        # Admin claims credentials.
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "rootadmin",
            "--password", "correct-horse-battery",
        ])
        # The original API_TOKEN bearer still works pre-login.
        pre = self.client.get(
            "/me", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        self.assertEqual(pre.status_code, 200)
        # u/p login mints a fresh token and rotates the prior hash.
        login = self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "correct-horse-battery"},
        ).json()
        new_token = login["token"]
        # Old bearer is dead.
        post = self.client.get(
            "/me", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        self.assertEqual(post.status_code, 401)
        # New bearer works.
        me = self.client.get(
            "/me", headers={"Authorization": f"Bearer {new_token}"}
        )
        self.assertEqual(me.status_code, 200)
        body = me.json()
        self.assertEqual(body["username"], "rootadmin")
        self.assertTrue(body["has_password"])

    # ------------------------------------------------------------------
    # POST /me/password
    # ------------------------------------------------------------------
    def test_change_password_rejects_wrong_current(self):
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "rootadmin",
            "--password", "correct-horse-battery",
        ])
        token = self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "correct-horse-battery"},
        ).json()["token"]
        r = self.client.post(
            "/me/password",
            json={
                "current_password": "nope-not-it",
                "new_password": "another-strong-one",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 401)

    def test_change_password_rejects_weak_new(self):
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "rootadmin",
            "--password", "correct-horse-battery",
        ])
        token = self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "correct-horse-battery"},
        ).json()["token"]
        r = self.client.post(
            "/me/password",
            json={
                "current_password": "correct-horse-battery",
                "new_password": "weak",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 400)

    def test_change_password_lets_new_password_log_in(self):
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "rootadmin",
            "--password", "correct-horse-battery",
        ])
        token = self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "correct-horse-battery"},
        ).json()["token"]
        r = self.client.post(
            "/me/password",
            json={
                "current_password": "correct-horse-battery",
                "new_password": "brand-new-passphrase",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200)
        # Same session keeps working — we only rotate the wire token on /login.
        me = self.client.get(
            "/me", headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(me.status_code, 200)
        # Old password no longer logs in.
        old = self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "correct-horse-battery"},
        )
        self.assertEqual(old.status_code, 401)
        # New password does.
        new = self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "brand-new-passphrase"},
        )
        self.assertEqual(new.status_code, 200)

    def test_admin_seed_skips_when_other_admin_exists(self):
        """Once the founding admin claims u/p their primary token has
        been rotated, so a coordinator restart would otherwise re-seed
        a duplicate admin from API_TOKEN. has_active_admin() short-
        circuits that path."""
        # Claim u/p — this rotates the API_TOKEN bearer out.
        coordinator_admin.main([
            "set-credentials",
            "--token", ADMIN_TOKEN,
            "--username", "rootadmin",
            "--password", "correct-horse-battery",
        ])
        self.client.post(
            "/login",
            json={"username": "rootadmin", "password": "correct-horse-battery"},
        )
        admins_before = [
            m for m in self.db.list_members() if m["role"] == "admin"
        ]
        self.assertEqual(len(admins_before), 1)
        # Restart-style reseed.
        coordinator_main.ensure_admin_seed()
        coordinator_main.ensure_admin_seed()
        admins_after = [
            m for m in self.db.list_members() if m["role"] == "admin"
        ]
        # Still exactly one admin — no duplicate from the env token.
        self.assertEqual(len(admins_after), 1)

    def test_change_password_first_set_works_without_current(self):
        """A member who has never set a password (legacy invitee, raw
        token only) can use /me/password to set their first one without
        knowing a current password — there isn't one to know."""
        contributor_token = _make_contributor(self.client)
        r = self.client.post(
            "/me/password",
            json={"current_password": "", "new_password": "fresh-passphrase"},
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
