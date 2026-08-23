"""End-to-end member-auth tests: ``API_TOKEN`` set, admin seeded, real
HTTP through ``TestClient``. Parallels ``test_coordinator_e2e.py`` but
with auth on.

Run with ``python -m unittest tests.test_member_auth_e2e``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

# 1. Env BEFORE imports — auth on, fresh DB, no rate limit.
_TMPDIR = tempfile.mkdtemp(prefix="gamerai-test-mauth-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["API_TOKEN"] = "admin-seed-token-for-tests"
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("STRICT_MODELS", None)

# 2. Drop cached coordinator/shared modules so they pick up the env above.
#    Includes the package modules themselves — otherwise the stale package
#    objects retain attribute references to first-loaded submodules and
#    `from coordinator import main` returns the wrong (older) module.
for _mod in list(sys.modules):
    if _mod.split(".", 1)[0] in ("shared", "coordinator"):
        del sys.modules[_mod]

# 3. Patch the Redis factory.
import fakeredis  # noqa: E402

import coordinator.redis_client  # noqa: E402

_FAKE = fakeredis.FakeStrictRedis(decode_responses=True)
coordinator.redis_client.get_client = lambda: _FAKE  # type: ignore[assignment]

# 4. Import the app + admin CLI machinery.
from fastapi.testclient import TestClient  # noqa: E402

from coordinator import admin as coordinator_admin  # noqa: E402
from coordinator import main as coordinator_main  # noqa: E402
from coordinator import member_auth  # noqa: E402

ADMIN_TOKEN = os.environ["API_TOKEN"]


class MemberAuthE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.r = _FAKE
        cls.db = coordinator_main.db
        # Lifespan does not fire with TestClient as a plain instance, so the
        # admin seed must be invoked directly. Idempotent.
        coordinator_main.ensure_admin_seed()

    def setUp(self):
        # Wipe job/usage state between tests, but keep the admin member
        # (it's the API_TOKEN seed; recreating it is needless work).
        self.r.flushall()
        self.db._conn.executescript(
            "DELETE FROM jobs; "
            "DELETE FROM workers; "
            "DELETE FROM earnings; "
            "DELETE FROM member_usage; "
            "DELETE FROM invites; "
            "DELETE FROM members WHERE role <> 'admin';"
        )

    # ------------------------------------------------------------------
    # auth gate
    # ------------------------------------------------------------------
    def test_generate_without_token_is_rejected(self):
        resp = self.client.post("/generate", json={"prompt": "hi"})
        self.assertEqual(resp.status_code, 401)

    def test_generate_with_bogus_token_is_rejected(self):
        resp = self.client.post(
            "/generate",
            json={"prompt": "hi"},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_health_is_public_even_with_auth_on(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_admin_token_grants_access(self):
        resp = self.client.post(
            "/generate",
            json={"prompt": "hi"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------------
    # admin seeding
    # ------------------------------------------------------------------
    def test_admin_seed_is_idempotent(self):
        # Second call should be a no-op; no exception, member count unchanged.
        before = len(self.db.list_members())
        coordinator_main.ensure_admin_seed()
        coordinator_main.ensure_admin_seed()
        self.assertEqual(len(self.db.list_members()), before)

    def test_me_returns_admin_identity(self):
        resp = self.client.get(
            "/me", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["role"], "admin")
        self.assertIsNone(body["parent_member_id"])
        self.assertIsNone(body["daily_quota_tokens"])

    # ------------------------------------------------------------------
    # CLI: create-member
    # ------------------------------------------------------------------
    def _create_member_via_cli(self, *args) -> str:
        """Invoke the CLI in-process, scrape ``token=...`` off stdout."""
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            coordinator_admin.main(list(args))
        for line in buf.getvalue().splitlines():
            if line.startswith("token="):
                return line.split("=", 1)[1].strip()
        raise AssertionError(f"no token in CLI output:\n{buf.getvalue()}")

    def test_cli_creates_contributor_who_can_submit(self):
        token = self._create_member_via_cli(
            "create-member", "--role", "contributor", "--email", "alice@example.com"
        )
        resp = self.client.post(
            "/generate",
            json={"prompt": "hi from alice"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_cli_invitee_requires_valid_parent_token(self):
        with self.assertRaises(SystemExit):
            self._create_member_via_cli(
                "create-member",
                "--role", "invitee",
                "--parent-token", "gai_unknown",
            )

    def test_cli_invitee_under_contributor_links_correctly(self):
        contrib_token = self._create_member_via_cli(
            "create-member", "--role", "contributor"
        )
        invitee_token = self._create_member_via_cli(
            "create-member",
            "--role", "invitee",
            "--parent-token", contrib_token,
            "--daily-quota-tokens", "1000",
        )
        me = self.client.get(
            "/me", headers={"Authorization": f"Bearer {invitee_token}"}
        ).json()
        self.assertEqual(me["role"], "invitee")
        self.assertIsNotNone(me["parent_member_id"])
        self.assertEqual(me["daily_quota_tokens"], 1000)

    # ------------------------------------------------------------------
    # revoke
    # ------------------------------------------------------------------
    def test_revoked_token_is_rejected(self):
        from contextlib import redirect_stdout
        from io import StringIO

        token = self._create_member_via_cli(
            "create-member", "--role", "contributor"
        )
        # Confirm it works first.
        self.assertEqual(
            self.client.get(
                "/me", headers={"Authorization": f"Bearer {token}"}
            ).status_code,
            200,
        )
        # Revoke and confirm it stops working.
        with redirect_stdout(StringIO()):
            coordinator_admin.main(["revoke", "--token", token])
        self.assertEqual(
            self.client.get(
                "/me", headers={"Authorization": f"Bearer {token}"}
            ).status_code,
            401,
        )

    # ------------------------------------------------------------------
    # job attribution
    # ------------------------------------------------------------------
    def test_generate_records_submitter_on_job_row(self):
        token = self._create_member_via_cli(
            "create-member", "--role", "contributor"
        )
        member = member_auth.lookup_member_by_token(self.db, token)
        resp = self.client.post(
            "/generate",
            json={"prompt": "attribute me"},
            headers={"Authorization": f"Bearer {token}"},
        )
        job_id = resp.json()["job_id"]
        row = self.db.get_job(job_id)
        self.assertEqual(row["submitted_by_member_id"], member.member_id)

    def test_jobs_complete_credits_member_usage_for_known_submitter(self):
        token = self._create_member_via_cli(
            "create-member", "--role", "contributor"
        )
        member = member_auth.lookup_member_by_token(self.db, token)
        headers = {"Authorization": f"Bearer {token}"}
        admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

        job_id = self.client.post(
            "/generate", json={"prompt": "track usage"}, headers=headers
        ).json()["job_id"]

        # Register a worker as admin (workers don't authenticate as members yet —
        # slice 2 layers that on).
        worker_id = "wkr-" + uuid.uuid4().hex[:6]
        self.client.post(
            "/register", json={"worker_id": worker_id}, headers=admin_headers
        )
        # Pop the job (mimic worker BLPOP).
        self.assertIsNotNone(self.r.lpop("job_queue"))
        claim_resp = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": job_id},
            headers=admin_headers,
        )
        claim_token = claim_resp.json()["claim_token"]
        complete_resp = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "text": "[mock] reply",
                "model": "mock",
                "prompt_tokens": 4,
                "completion_tokens": 9,
                "duration_seconds": 0.1,
                "status": "complete",
                "claim_token": claim_token,
            },
            headers=admin_headers,
        )
        self.assertEqual(complete_resp.status_code, 200)

        # Member usage table should show 1 job for the submitter.
        usage = self.db.member_usage_today(member.member_id)
        self.assertEqual(usage["jobs"], 1)
        self.assertEqual(usage["tokens_in"], 4)
        self.assertEqual(usage["tokens_out"], 9)

    # ------------------------------------------------------------------
    # invites — slice 2
    # ------------------------------------------------------------------
    def _admin_headers(self):
        return {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    _accept_seq = 0

    @classmethod
    def _accept_payload(cls, **overrides) -> dict:
        """Build a redeem payload that satisfies the post-u/p contract
        (username + password + invitee_email all required) and yields a
        fresh username per call so concurrent tests in the suite can't
        collide on the unique index."""
        cls._accept_seq += 1
        body = {
            "username": f"invitee{cls._accept_seq:04d}",
            "password": "correct-horse-battery",
            "invitee_email": "bob@example.com",
            "tos_accepted": True,
        }
        body.update(overrides)
        return body

    def _make_contributor(self) -> tuple[str, str]:
        token = self._create_member_via_cli(
            "create-member", "--role", "contributor"
        )
        member = member_auth.lookup_member_by_token(self.db, token)
        return member.member_id, token

    _invite_seq = 0

    def _create_invite(self, contributor_token: str, **kwargs) -> str:
        # invitee_email is required on POST /invites; default to a
        # per-call unique address so two helper calls in one test
        # don't collide on the email unique index.
        type(self)._invite_seq += 1
        body = {
            "daily_quota_tokens": 100,
            "invitee_email": f"invite-target-{type(self)._invite_seq:04d}@example.com",
        }
        body.update(kwargs)
        resp = self.client.post(
            "/invites",
            json=body,
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["code"]

    def test_create_invite_returns_code(self):
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        self.assertTrue(code.startswith("inv_"))

    def test_operational_endpoints_are_admin_only(self):
        """/workers, /earnings, /metrics expose cross-member data and are
        reachable directly via the Caddy catch-all, so the coordinator —
        not just the web BFF — must reject non-admin members."""
        _, contributor_token = self._make_contributor()
        ct_headers = {"Authorization": f"Bearer {contributor_token}"}
        for path in (
            "/workers", "/earnings", "/metrics", "/earnings/some-worker",
        ):
            resp = self.client.get(path, headers=ct_headers)
            self.assertEqual(resp.status_code, 403, f"{path}: {resp.text}")
        for path in ("/workers", "/earnings", "/metrics"):
            self.assertEqual(
                self.client.get(path, headers=self._admin_headers())
                .status_code,
                200, path,
            )

    def test_result_is_owner_scoped(self):
        """A member must not read another member's job result by job_id
        (broken-access-control / IDOR). Owner + admin may."""
        _, alice = self._make_contributor()
        a_headers = {"Authorization": f"Bearer {alice}"}
        conv = self.client.post(
            "/conversations", json={}, headers=a_headers,
        ).json()
        job_id = self.client.post(
            "/generate",
            json={"prompt": "secret prompt",
                  "conversation_id": conv["conversation_id"]},
            headers=a_headers,
        ).json()["job_id"]
        _, bob = self._make_contributor()
        intruder = self.client.get(
            f"/result/{job_id}",
            headers={"Authorization": f"Bearer {bob}"},
        )
        self.assertEqual(intruder.status_code, 404, intruder.text)
        self.assertEqual(
            self.client.get(f"/result/{job_id}", headers=a_headers)
            .status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/result/{job_id}", headers=self._admin_headers(),
            ).status_code,
            200,
        )

    def test_invitee_role_cannot_create_invite(self):
        # An invitee tries to create an invite — should be forbidden.
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        resp = self.client.post(
            "/invites",
            json={
                "daily_quota_tokens": 50,
                "invitee_email": "would-be-invitee@example.com",
            },
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_invite_details_public_no_auth_required(self):
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        resp = self.client.get(f"/invites/{code}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["state"], "open")
        self.assertEqual(body["daily_quota_tokens"], 100)

    def test_invite_details_returns_404_for_unknown_code(self):
        self.assertEqual(
            self.client.get("/invites/inv_doesnotexist").status_code, 404
        )

    def test_accept_invite_mints_invitee_member(self):
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        payload = self._accept_payload()
        # Public — no auth header.
        resp = self.client.post(f"/invites/{code}/accept", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["token"].startswith("gai_"))
        self.assertEqual(body["role"], "invitee")
        self.assertEqual(body["username"], payload["username"])
        self.assertEqual(body["daily_quota_tokens"], 100)

        # The new token actually works.
        me = self.client.get(
            "/me", headers={"Authorization": f"Bearer {body['token']}"}
        ).json()
        self.assertEqual(me["role"], "invitee")
        self.assertEqual(me["email"], "bob@example.com")
        self.assertEqual(me["username"], payload["username"])
        self.assertTrue(me["has_password"])
        self.assertEqual(me["daily_quota_tokens"], 100)

        # The new member can also log in via u/p.
        login = self.client.post(
            "/login",
            json={
                "username": payload["username"],
                "password": payload["password"],
            },
        )
        self.assertEqual(login.status_code, 200, login.text)

    def test_accept_rejects_missing_username(self):
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        payload = self._accept_payload(username="ab")  # too short
        resp = self.client.post(f"/invites/{code}/accept", json=payload)
        self.assertEqual(resp.status_code, 400)

    def test_accept_rejects_weak_password(self):
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        payload = self._accept_payload(password="short")
        resp = self.client.post(f"/invites/{code}/accept", json=payload)
        self.assertEqual(resp.status_code, 400)

    def test_accept_rejects_duplicate_username(self):
        _, contributor_token = self._make_contributor()
        code1 = self._create_invite(contributor_token)
        code2 = self._create_invite(contributor_token)
        payload = self._accept_payload(username="sharedname")
        first = self.client.post(f"/invites/{code1}/accept", json=payload)
        self.assertEqual(first.status_code, 200)
        second_payload = self._accept_payload(username="sharedname")
        second = self.client.post(f"/invites/{code2}/accept", json=second_payload)
        self.assertEqual(second.status_code, 409)

    def test_accept_rejects_duplicate_email(self):
        """Email uniqueness is load-bearing for the future email-based
        password-reset path. Two members with the same email makes
        'reset to alice@example.com' ambiguous; the cheapest enforcement
        is at signup."""
        _, contributor_token = self._make_contributor()
        code1 = self._create_invite(contributor_token)
        code2 = self._create_invite(contributor_token)
        first = self.client.post(
            f"/invites/{code1}/accept",
            json=self._accept_payload(invitee_email="dup@example.com"),
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            f"/invites/{code2}/accept",
            json=self._accept_payload(invitee_email="dup@example.com"),
        )
        self.assertEqual(second.status_code, 409)
        self.assertIn("already claimed", second.json()["detail"])

    def test_revoking_a_member_frees_their_email_and_username(self):
        """Revoked rows must not squat on identity slots — otherwise a
        mistakenly-revoked account leaks its email forever (the footgun
        the founder hit on 2026-05-23 with a stray test contributor
        blocking their own admin email)."""
        _, contributor_token = self._make_contributor()
        code1 = self._create_invite(contributor_token)
        first = self.client.post(
            f"/invites/{code1}/accept",
            json=self._accept_payload(
                username="recycleme",
                invitee_email="recycle@example.com",
            ),
        ).json()
        # Now revoke the freshly-created invitee.
        ok = self.db.revoke_member_by_token_hash(
            member_auth.hash_token(first["token"]), time.time(),
        )
        self.assertTrue(ok)
        # A fresh invitee should be able to claim the same username + email.
        code2 = self._create_invite(contributor_token)
        second = self.client.post(
            f"/invites/{code2}/accept",
            json=self._accept_payload(
                username="recycleme",
                invitee_email="recycle@example.com",
            ),
        )
        self.assertEqual(second.status_code, 200, second.text)

    def test_accept_email_collision_is_case_insensitive(self):
        _, contributor_token = self._make_contributor()
        code1 = self._create_invite(contributor_token)
        code2 = self._create_invite(contributor_token)
        first = self.client.post(
            f"/invites/{code1}/accept",
            json=self._accept_payload(invitee_email="Mixed.Case@Example.COM"),
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            f"/invites/{code2}/accept",
            json=self._accept_payload(invitee_email="mixed.case@example.com"),
        )
        self.assertEqual(second.status_code, 409)

    def test_accept_is_one_shot(self):
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        first = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        )
        self.assertEqual(second.status_code, 410)
        self.assertIn("accepted", second.json()["detail"])

    def test_accept_after_revoke_is_rejected(self):
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        rev = self.client.post(
            f"/invites/{code}/revoke", headers=self._admin_headers()
        )
        self.assertEqual(rev.status_code, 200)
        resp = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        )
        self.assertEqual(resp.status_code, 410)

    def test_accept_after_expiry_is_rejected(self):
        _, contributor_token = self._make_contributor()
        # Create an invite that expired 1 hour ago via direct DB write.
        from coordinator import member_auth as ma
        code = ma.generate_invite_code()
        self.db.create_invite(
            invite_id="inv_id_test_expired",
            code=code,
            contributor_member_id=member_auth.lookup_member_by_token(
                self.db, contributor_token
            ).member_id,
            daily_quota_tokens=100,
            expires_at=time.time() - 3600,
        )
        resp = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        )
        self.assertEqual(resp.status_code, 410)
        self.assertIn("expired", resp.json()["detail"])

    def test_invitee_sees_self_in_list_only_if_admin(self):
        _, contributor_token = self._make_contributor()
        self._create_invite(contributor_token)
        # Contributor sees one — their own.
        own = self.client.get(
            "/invites",
            headers={"Authorization": f"Bearer {contributor_token}"},
        ).json()
        self.assertEqual(len(own["invites"]), 1)
        # Admin with ?all=true sees the same one.
        all_ = self.client.get(
            "/invites?all=true", headers=self._admin_headers()
        ).json()
        self.assertEqual(len(all_["invites"]), 1)

    def test_admin_members_requires_admin(self):
        _, contributor_token = self._make_contributor()
        forbidden = self.client.get(
            "/admin/members",
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(forbidden.status_code, 403)
        ok = self.client.get("/admin/members", headers=self._admin_headers())
        self.assertEqual(ok.status_code, 200)
        # Admin seed + one contributor created in this test.
        self.assertGreaterEqual(len(ok.json()["members"]), 2)

    # ------------------------------------------------------------------
    # quota enforcement — slice 2
    # ------------------------------------------------------------------
    def test_generate_under_quota_is_allowed(self):
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token, daily_quota_tokens=20)
        token = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()["token"]
        resp = self.client.post(
            "/generate",
            json={"prompt": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_generate_over_quota_returns_429(self):
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token, daily_quota_tokens=5)
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        invitee_member = member_auth.lookup_member_by_token(
            self.db, invitee_token
        )
        # Pre-populate usage above the cap by writing directly to the table.
        self.db.add_member_usage(
            invitee_member.member_id, time.time(), tokens_in=0, tokens_out=10
        )
        resp = self.client.post(
            "/generate",
            json={"prompt": "should be rejected"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(resp.status_code, 429)
        self.assertIn("daily quota", resp.json()["detail"])

    def test_admin_has_no_quota(self):
        # Admin has daily_quota_tokens=NULL → unbounded.
        for _ in range(3):
            resp = self.client.post(
                "/generate",
                json={"prompt": "again"},
                headers=self._admin_headers(),
            )
            self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------------
    # image-limits slice — two-dimensional quota gate
    # ------------------------------------------------------------------
    def test_image_over_quota_returns_429_and_chat_untouched(self):
        """The image gate must fire on image_units, not tokens_out —
        a member at their image cap can still submit chat (and vice
        versa)."""
        _, contributor_token = self._make_contributor()
        code = self._create_invite(
            contributor_token,
            daily_quota_tokens=100_000,  # plenty of chat
            daily_quota_images=1,
        )
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        invitee_member = member_auth.lookup_member_by_token(
            self.db, invitee_token
        )
        # Burn through the image cap directly.
        self.db.add_member_image_usage(
            invitee_member.member_id, time.time(), units=1.0,
        )
        # Image submit is rejected.
        img_resp = self.client.post(
            "/generate",
            json={"prompt": "draw a cat", "tool": "image"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(img_resp.status_code, 429)
        self.assertIn("image", img_resp.json()["detail"])
        # Chat is still fine — separate ledger.
        chat_resp = self.client.post(
            "/generate",
            json={"prompt": "hello"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(chat_resp.status_code, 200)

    def test_token_over_quota_does_not_block_images(self):
        """The chat gate fires on tokens_out only — a member exhausted
        on tokens can still submit images while under their image
        cap."""
        _, contributor_token = self._make_contributor()
        code = self._create_invite(
            contributor_token,
            daily_quota_tokens=5,
            daily_quota_images=10,
        )
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        invitee_member = member_auth.lookup_member_by_token(
            self.db, invitee_token
        )
        self.db.add_member_usage(
            invitee_member.member_id, time.time(), tokens_in=0, tokens_out=10,
        )
        # Chat 429.
        chat_resp = self.client.post(
            "/generate",
            json={"prompt": "hi"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(chat_resp.status_code, 429)
        # Image still accepted.
        img_resp = self.client.post(
            "/generate",
            json={"prompt": "draw something", "tool": "image"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(img_resp.status_code, 200)

    # ------------------------------------------------------------------
    # sponsored-user isolation — a contributor's own usage/quota must
    # stay a fully separate ledger from each invitee they sponsor, and
    # invitees under the same host must not share a ledger with each
    # other either. This is the actual "invite my family to use from
    # my quota" mechanic: the host sets each invitee's cap (see
    # business.md), but consumption is metered per-member, not pooled.
    # ------------------------------------------------------------------
    def _submit_and_complete(
        self, token: str, completion_tokens: int, prompt_tokens: int = 1,
    ) -> None:
        """Full round-trip through the real crediting path (submit ->
        worker claim -> complete), not a direct DB seed — this is what
        actually calls db.add_member_usage in production, so a bug in
        who gets credited would show up here and not in a shortcut
        test."""
        admin_headers = self._admin_headers()
        job_id = self.client.post(
            "/generate",
            json={"prompt": "usage isolation check"},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["job_id"]
        worker_id = "wkr-" + uuid.uuid4().hex[:8]
        self.client.post(
            "/register", json={"worker_id": worker_id}, headers=admin_headers,
        )
        self.assertIsNotNone(self.r.lpop("job_queue"))
        claim_token = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": job_id},
            headers=admin_headers,
        ).json()["claim_token"]
        complete_resp = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "text": "[mock] reply",
                "model": "mock",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "duration_seconds": 0.1,
                "status": "complete",
                "claim_token": claim_token,
            },
            headers=admin_headers,
        )
        self.assertEqual(complete_resp.status_code, 200, complete_resp.text)

    def test_invitee_usage_does_not_debit_hosts_quota(self):
        """The host runs their own agent and games on their own quota;
        their invited family member's consumption must not touch the
        host's own ledger, and vice versa."""
        host_token = self._create_member_via_cli(
            "create-member", "--role", "contributor",
            "--daily-quota-tokens", "5",
        )
        host_member = member_auth.lookup_member_by_token(self.db, host_token)
        code = self._create_invite(host_token, daily_quota_tokens=5)
        invitee_token = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()["token"]
        invitee_member = member_auth.lookup_member_by_token(self.db, invitee_token)

        # Invitee burns well past their own 5-token cap.
        self._submit_and_complete(invitee_token, completion_tokens=10)

        # Host's ledger is untouched by the invitee's usage — still
        # zero, and the host can still submit under their own cap.
        host_usage = self.db.member_usage_today(host_member.member_id)
        self.assertEqual(host_usage["tokens_out"], 0)
        host_resp = self.client.post(
            "/generate",
            json={"prompt": "host's own turn"},
            headers={"Authorization": f"Bearer {host_token}"},
        )
        self.assertEqual(host_resp.status_code, 200)

        # The invitee, having exceeded their own cap, is now blocked —
        # on their own ledger, independent of the host's fresh usage.
        invitee_resp = self.client.post(
            "/generate",
            json={"prompt": "invitee tries again"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(invitee_resp.status_code, 429)
        invitee_usage = self.db.member_usage_today(invitee_member.member_id)
        self.assertEqual(invitee_usage["tokens_out"], 10)

    def test_sibling_invitees_have_independent_quota_ledgers(self):
        """Two family members invited by the same host must not share
        a usage ledger — one sibling maxing out their cap must not
        block (or silently credit) the other."""
        host_token = self._create_member_via_cli(
            "create-member", "--role", "contributor",
        )
        code_a = self._create_invite(host_token, daily_quota_tokens=5)
        code_b = self._create_invite(host_token, daily_quota_tokens=5)
        token_a = self.client.post(
            f"/invites/{code_a}/accept",
            json=self._accept_payload(invitee_email="sibling-a@example.com"),
        ).json()["token"]
        token_b = self.client.post(
            f"/invites/{code_b}/accept",
            json=self._accept_payload(invitee_email="sibling-b@example.com"),
        ).json()["token"]
        member_b = member_auth.lookup_member_by_token(self.db, token_b)

        # Sibling A blows through their own cap.
        self._submit_and_complete(token_a, completion_tokens=10)
        resp_a = self.client.post(
            "/generate",
            json={"prompt": "a tries again"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        self.assertEqual(resp_a.status_code, 429)

        # Sibling B is unaffected — zero usage, still under cap.
        usage_b = self.db.member_usage_today(member_b.member_id)
        self.assertEqual(usage_b["tokens_out"], 0)
        resp_b = self.client.post(
            "/generate",
            json={"prompt": "b's first turn"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        self.assertEqual(resp_b.status_code, 200)

    def test_jobs_listing_resolves_worker_and_member_labels(self):
        """The admin job-listing page (GET /jobs) links each row to its
        worker and submitting member, and resolves the filter-context
        block (dashboard's 'jump to owner' / 'jump to machine' links)
        by real ownership, not just by echoing the query param back."""
        token = self._create_member_via_cli(
            "create-member", "--role", "contributor", "--email", "carol@example.com",
        )
        member = member_auth.lookup_member_by_token(self.db, token)
        headers = {"Authorization": f"Bearer {token}"}
        worker_id = "wkr-" + uuid.uuid4().hex[:8]

        self.assertEqual(
            self.client.post("/register", json={"worker_id": worker_id}, headers=headers).status_code,
            200,
        )
        job_id = self.client.post(
            "/generate", json={"prompt": "carol's own job"}, headers=headers,
        ).json()["job_id"]
        self.assertIsNotNone(self.r.lpop("job_queue"))
        claim_token = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": job_id},
            headers=headers,
        ).json()["claim_token"]
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "text": "[mock] reply",
                "model": "mock",
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "duration_seconds": 0.1,
                "status": "complete",
                "claim_token": claim_token,
            },
            headers=headers,
        )

        by_worker = self.client.get(
            f"/jobs?worker_id={worker_id}", headers=self._admin_headers(),
        ).json()
        self.assertEqual([j["job_id"] for j in by_worker["jobs"]], [job_id])
        self.assertEqual(by_worker["jobs"][0]["submitted_by_label"], "carol@example.com")
        self.assertEqual(by_worker["filter_worker"]["owner_member_id"], member.member_id)
        self.assertEqual(by_worker["filter_worker"]["owner_label"], "carol@example.com")

        by_member = self.client.get(
            f"/jobs?member_id={member.member_id}", headers=self._admin_headers(),
        ).json()
        self.assertEqual([j["job_id"] for j in by_member["jobs"]], [job_id])
        self.assertEqual(by_member["filter_member"]["label"], "carol@example.com")
        self.assertEqual(
            [w["worker_id"] for w in by_member["filter_member"]["workers"]], [worker_id],
        )

    def test_me_returns_tier_quota_and_earnings_envelope(self):
        """/me grows three new fields: tier_quota, daily_quota_images,
        earnings — the account-page activity card depends on all
        three being present, even when zero."""
        resp = self.client.get("/me", headers=self._admin_headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("tier_quota", body)
        self.assertIn("daily_quota_images", body)
        self.assertIn("earnings", body)
        self.assertEqual(
            set(body["earnings"].keys()),
            {"total_tokens", "total_jobs", "total_usd", "machine_count"},
        )
        # Admin → tier_quota nullified to "unlimited" regardless of tier.
        self.assertIsNone(body["tier_quota"]["tokens"])
        self.assertIsNone(body["tier_quota"]["images"])
        # usage_today now includes image_units.
        self.assertIn("image_units", body["usage_today"])

    def test_me_bronze_member_gets_concrete_tier_quota(self):
        """A non-admin member gets the BRONZE allowance baked into
        coordinator/tiers.py reflected back through /me."""
        _, contributor_token = self._make_contributor()
        resp = self.client.get(
            "/me",
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["tier"], "BRONZE")
        # Numbers come from TIER_QUOTAS; assert they're non-null
        # rather than pinning the magic constants here (so a tuning
        # change to the table doesn't break this test).
        self.assertIsNotNone(body["tier_quota"]["tokens"])
        self.assertIsNotNone(body["tier_quota"]["images"])

    # ------------------------------------------------------------------
    # worker forget endpoint + contributor-status
    # ------------------------------------------------------------------
    def test_forget_worker_deletes_owned_row(self):
        _, contributor_token = self._make_contributor()
        contributor = member_auth.lookup_member_by_token(
            self.db, contributor_token,
        )
        # Plant a worker owned by this contributor.
        self.db.claim_worker_ownership(
            "w_to_forget", contributor.member_id, "idle", time.time(),
        )
        resp = self.client.post(
            "/me/workers/w_to_forget/forget",
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["deleted"])
        # Worker row is gone.
        self.assertIsNone(self.db.worker_owner("w_to_forget"))

    def test_forget_worker_404_for_stranger(self):
        """A different member can't forget someone else's worker."""
        _, host_token = self._make_contributor()
        host = member_auth.lookup_member_by_token(self.db, host_token)
        self.db.claim_worker_ownership(
            "w_protected", host.member_id, "idle", time.time(),
        )
        _, stranger_token = self._make_contributor()
        resp = self.client.post(
            "/me/workers/w_protected/forget",
            headers={"Authorization": f"Bearer {stranger_token}"},
        )
        self.assertEqual(resp.status_code, 404)
        # Worker survived.
        self.assertEqual(self.db.worker_owner("w_protected"), host.member_id)

    def test_contributor_status_envelope_for_admin(self):
        resp = self.client.get(
            "/me/contributor-status", headers=self._admin_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["is_admin"])
        self.assertFalse(body["engine_applies"])

    def test_contributor_status_envelope_for_member(self):
        _, contributor_token = self._make_contributor()
        resp = self.client.get(
            "/me/contributor-status",
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["is_admin"])
        self.assertTrue(body["engine_applies"])
        self.assertIn("uptime_7d", body)
        self.assertIn("current_tier_requirements", body)
        self.assertEqual(body["tier"], "BRONZE")
        # Brand-new member has zero uptime → does not meet BRONZE bar.
        self.assertFalse(body["meets_current"])

    # ------------------------------------------------------------------
    # friend management — host edits / revokes accepted invitees
    # ------------------------------------------------------------------
    def _accept_invite_under(self, contributor_token: str) -> tuple[str, str]:
        """Create + redeem an invite under ``contributor_token`` and
        return ``(invitee_member_id, invitee_token)``. Shorthand used
        by the friend-management tests so each can build its own
        host→invitee pair without rewriting the dance."""
        code = self._create_invite(
            contributor_token,
            daily_quota_tokens=100,
            daily_quota_images=5,
        )
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        invitee = member_auth.lookup_member_by_token(self.db, invitee_token)
        return invitee.member_id, invitee_token

    def test_host_can_edit_friend_quota(self):
        _, contributor_token = self._make_contributor()
        invitee_id, invitee_token = self._accept_invite_under(contributor_token)
        resp = self.client.post(
            f"/me/friends/{invitee_id}/quota",
            json={"daily_quota_tokens": 250, "daily_quota_images": 12},
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # /me from the invitee's side reflects the new caps.
        me = self.client.get(
            "/me",
            headers={"Authorization": f"Bearer {invitee_token}"},
        ).json()
        self.assertEqual(me["daily_quota_tokens"], 250)
        self.assertEqual(me["daily_quota_images"], 12)

    def test_quota_update_to_null_means_unlimited(self):
        _, contributor_token = self._make_contributor()
        invitee_id, invitee_token = self._accept_invite_under(contributor_token)
        resp = self.client.post(
            f"/me/friends/{invitee_id}/quota",
            json={"daily_quota_tokens": None, "daily_quota_images": None},
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        me = self.client.get(
            "/me",
            headers={"Authorization": f"Bearer {invitee_token}"},
        ).json()
        self.assertIsNone(me["daily_quota_tokens"])
        self.assertIsNone(me["daily_quota_images"])

    def test_stranger_cannot_edit_other_hosts_friend(self):
        """A different contributor must not be able to mutate someone
        else's invitee's quota. 404 (not 403) so the endpoint doesn't
        reveal whose tree the member belongs to."""
        _, host_token = self._make_contributor()
        invitee_id, _ = self._accept_invite_under(host_token)
        _, stranger_token = self._make_contributor()
        resp = self.client.post(
            f"/me/friends/{invitee_id}/quota",
            json={"daily_quota_tokens": 999_999, "daily_quota_images": 999},
            headers={"Authorization": f"Bearer {stranger_token}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_admin_can_edit_any_friend(self):
        _, host_token = self._make_contributor()
        invitee_id, _ = self._accept_invite_under(host_token)
        resp = self.client.post(
            f"/me/friends/{invitee_id}/quota",
            json={"daily_quota_tokens": 42, "daily_quota_images": 3},
            headers=self._admin_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_host_can_revoke_friend(self):
        _, contributor_token = self._make_contributor()
        invitee_id, invitee_token = self._accept_invite_under(contributor_token)
        resp = self.client.post(
            f"/me/friends/{invitee_id}/revoke",
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["was_already_revoked"])
        # Invitee's bearer no longer authenticates.
        unauth = self.client.get(
            "/me",
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(unauth.status_code, 401)

    def test_revoke_is_idempotent(self):
        _, contributor_token = self._make_contributor()
        invitee_id, _ = self._accept_invite_under(contributor_token)
        first = self.client.post(
            f"/me/friends/{invitee_id}/revoke",
            headers={"Authorization": f"Bearer {contributor_token}"},
        ).json()
        second = self.client.post(
            f"/me/friends/{invitee_id}/revoke",
            headers={"Authorization": f"Bearer {contributor_token}"},
        ).json()
        self.assertFalse(first["was_already_revoked"])
        self.assertTrue(second["was_already_revoked"])

    def test_stranger_cannot_revoke_other_hosts_friend(self):
        _, host_token = self._make_contributor()
        invitee_id, invitee_token = self._accept_invite_under(host_token)
        _, stranger_token = self._make_contributor()
        resp = self.client.post(
            f"/me/friends/{invitee_id}/revoke",
            headers={"Authorization": f"Bearer {stranger_token}"},
        )
        self.assertEqual(resp.status_code, 404)
        # Invitee is still active.
        ok = self.client.get(
            "/me",
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(ok.status_code, 200)

    def test_invite_accepts_and_inherits_image_cap(self):
        """daily_quota_images must round-trip through /invites POST →
        redeem → the new invitee member's row."""
        _, contributor_token = self._make_contributor()
        code = self._create_invite(
            contributor_token,
            daily_quota_tokens=50_000,
            daily_quota_images=7,
        )
        # Public details endpoint surfaces both caps.
        details = self.client.get(f"/invites/{code}").json()
        self.assertEqual(details["daily_quota_images"], 7)
        # Redeem.
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        invitee_member = member_auth.lookup_member_by_token(
            self.db, invitee_token,
        )
        self.assertEqual(invitee_member.daily_quota_images, 7)
        self.assertEqual(invitee_member.daily_quota_tokens, 50_000)

    # ------------------------------------------------------------------
    # voice-phase1 slice — tts quota gate + audio crediting
    # ------------------------------------------------------------------
    def test_tts_gate_falls_back_to_tier_default_when_column_null(self):
        """An invitee whose daily_quota_voice_minutes is NULL must still
        be capped at their tier's voice_minutes from TIER_QUOTAS.
        Different precedence from tokens/images (NULL=unlimited) —
        voice is tier-driven by design (see voice-phase1)."""
        from coordinator.tiers import TIER_QUOTAS
        _, contributor_token = self._make_contributor()
        # Invite without setting voice cap → column stays NULL.
        code = self._create_invite(contributor_token)
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        invitee_member = member_auth.lookup_member_by_token(
            self.db, invitee_token,
        )
        self.assertIsNone(invitee_member.daily_quota_voice_minutes)
        # Burn through the BRONZE tier default cap by writing
        # voice_seconds directly to the rollup.
        bronze_cap = TIER_QUOTAS["BRONZE"]["voice_minutes"]
        self.db.add_member_voice_usage(
            invitee_member.member_id, time.time(),
            seconds=bronze_cap * 60,
        )
        tts_resp = self.client.post(
            "/generate",
            json={"prompt": "speak", "tool": "tts"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(tts_resp.status_code, 429)
        self.assertIn("voice", tts_resp.json()["detail"])
        # Chat still works — separate ledger.
        chat_resp = self.client.post(
            "/generate",
            json={"prompt": "hi"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(chat_resp.status_code, 200)

    def test_tts_gate_respects_per_member_override(self):
        """An explicit per-member voice cap overrides the tier default.
        Setting it to 1 minute means we're rejected after 60 seconds of
        usage regardless of the tier's larger allowance."""
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        invitee_member = member_auth.lookup_member_by_token(
            self.db, invitee_token,
        )
        self.db.update_member_quotas(
            invitee_member.member_id,
            daily_quota_tokens=invitee_member.daily_quota_tokens,
            daily_quota_images=invitee_member.daily_quota_images,
            daily_quota_voice_minutes=1,
        )
        self.db.add_member_voice_usage(
            invitee_member.member_id, time.time(), seconds=61.0,
        )
        resp = self.client.post(
            "/generate",
            json={"prompt": "speak", "tool": "tts"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(resp.status_code, 429)

    def test_admin_unlimited_voice(self):
        """Admin role bypasses the voice cap entirely, matching the
        existing tokens/images precedent."""
        admin_member = member_auth.lookup_member_by_token(
            self.db, ADMIN_TOKEN,
        )
        self.db.add_member_voice_usage(
            admin_member.member_id, time.time(), seconds=99_999.0,
        )
        resp = self.client.post(
            "/generate",
            json={"prompt": "speak", "tool": "tts"},
            headers=self._admin_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_me_returns_voice_envelope(self):
        """/me grows voice_minutes_today + daily_quota_voice_minutes —
        the account-page voice meter depends on both being present."""
        resp = self.client.get("/me", headers=self._admin_headers()).json()
        self.assertIn("voice_minutes_today", resp)
        self.assertIn("daily_quota_voice_minutes", resp)
        self.assertIn("voice_minutes", resp["tier_quota"])
        # Admin → tier_quota all-None including voice.
        self.assertIsNone(resp["tier_quota"]["voice_minutes"])

    def test_me_bronze_member_gets_voice_tier_quota(self):
        """Non-admin members see the TIER_QUOTAS voice_minutes value
        in /me.tier_quota — the UI's voice cap denominator."""
        from coordinator.tiers import TIER_QUOTAS
        _, contributor_token = self._make_contributor()
        resp = self.client.get(
            "/me", headers={"Authorization": f"Bearer {contributor_token}"},
        ).json()
        self.assertEqual(
            resp["tier_quota"]["voice_minutes"],
            TIER_QUOTAS["BRONZE"]["voice_minutes"],
        )

    def _claim_and_complete_tts(
        self,
        invitee_token: str,
        prompt: str,
        *,
        audio_seconds: float,
        duration_seconds: float = 0.5,
        audio_b64: str = "AAAA",
    ) -> str:
        """End-to-end submit + worker-claim + complete for a tts job.
        Mirrors the existing chat-completion test's claim flow:
        /generate enqueues → LPOP simulates the worker's BLPOP →
        /jobs/claim mints a claim_token → /jobs/complete posts the
        audio bill. Returns the job_id so the caller can poll
        /result."""
        submit = self.client.post(
            "/generate",
            json={"prompt": prompt, "tool": "tts"},
            headers={"Authorization": f"Bearer {invitee_token}"},
        )
        self.assertEqual(submit.status_code, 200, submit.text)
        job_id = submit.json()["job_id"]
        # Drain the per-tool queue (LPOP on job_queue:tts) — the
        # voice-phase1 routing change put tts on its own Redis list.
        self.assertIsNotNone(self.r.lpop("job_queue:tts"))
        worker_id = "wkr-" + uuid.uuid4().hex[:6]
        self.client.post(
            "/register",
            json={"worker_id": worker_id},
            headers=self._admin_headers(),
        )
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": job_id},
            headers=self._admin_headers(),
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        claim_token = claim.json()["claim_token"]
        complete = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "text": prompt[:200],
                "model": "piper:en_us-libritts-high",
                "prompt_tokens": 2,
                "completion_tokens": 0,
                "duration_seconds": duration_seconds,
                "status": "complete",
                "audio_b64": audio_b64,
                "audio_seconds": audio_seconds,
                "claim_token": claim_token,
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(complete.status_code, 200, complete.text)
        return job_id

    def test_tts_completion_credits_voice_seconds(self):
        """A successful tts /jobs/complete bumps the submitter's
        voice_seconds rollup AND stashes audio_b64 on /result so the
        polling client can play it."""
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        invitee_member = member_auth.lookup_member_by_token(
            self.db, invitee_token,
        )
        import base64 as _b64
        fake_audio = _b64.b64encode(b"RIFF1234WAVEdata...").decode("ascii")
        job_id = self._claim_and_complete_tts(
            invitee_token, "hello world",
            audio_seconds=7.5, duration_seconds=0.8, audio_b64=fake_audio,
        )
        usage = self.db.member_usage_today(invitee_member.member_id)
        self.assertAlmostEqual(usage["voice_seconds"], 7.5, places=2)
        # /result/{job_id} surfaces the audio for the polling client.
        result = self.client.get(
            f"/result/{job_id}",
            headers={"Authorization": f"Bearer {invitee_token}"},
        ).json()
        self.assertEqual(result["audio_b64"], fake_audio)
        self.assertAlmostEqual(result["audio_seconds"], 7.5, places=2)

    def test_tts_worker_cannot_overbill_via_audio_seconds(self):
        """The audio_seconds the worker reports IS what bills against
        the user — by design (see voice-phase1). This test pins the
        contract so a future refactor doesn't accidentally start
        crediting duration_seconds (synthesis wall-clock) instead,
        which would let a slow worker over-bill the submitter."""
        _, contributor_token = self._make_contributor()
        code = self._create_invite(contributor_token)
        accept = self.client.post(
            f"/invites/{code}/accept", json=self._accept_payload()
        ).json()
        invitee_token = accept["token"]
        invitee_member = member_auth.lookup_member_by_token(
            self.db, invitee_token,
        )
        # Pathologically slow synth (30s of compute), tiny clip (2s of
        # audio). Bill is 2s, not 30s.
        self._claim_and_complete_tts(
            invitee_token, "hi",
            audio_seconds=2.0, duration_seconds=30.0,
        )
        usage = self.db.member_usage_today(invitee_member.member_id)
        self.assertAlmostEqual(usage["voice_seconds"], 2.0, places=2)


def _signup_payload(**overrides):
    payload = {
        "username": "verifyuser-" + uuid.uuid4().hex[:8],
        "password": "correct horse battery staple",
        "email": "verify-" + uuid.uuid4().hex[:8] + "@example.com",
        "tos_accepted": True,
    }
    payload.update(overrides)
    return payload


class SignupEmailVerificationTests(unittest.TestCase):
    """POST /signup gates chat/image/voice on a confirmed email once
    Resend is configured (open, invite-free signup means an unverified
    throwaway address is the only thing standing between a stranger
    and free BRONZE quota — see todo.txt). Resend itself is never
    actually called: send_verification_email is mocked so these tests
    don't make network calls or need a real API key."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.r = _FAKE
        cls.db = coordinator_main.db
        coordinator_main.ensure_admin_seed()

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

    def _find_verify_code(self) -> str:
        keys = self.r.keys("email_verify:*")
        self.assertEqual(len(keys), 1, f"expected exactly one pending code, got {keys}")
        return keys[0].split(":", 1)[1]

    def test_signup_auto_verifies_when_resend_unconfigured(self):
        # Module-wide default: RESEND_API_KEY is unset in this test
        # env, so email_send.is_configured() is False and signup must
        # not lock the new account out of an email that can never
        # arrive.
        resp = self.client.post("/signup", json=_signup_payload())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["email_verified"])
        generate = self.client.post(
            "/generate",
            json={"prompt": "hi"},
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        self.assertEqual(generate.status_code, 200)

    @mock.patch.object(coordinator_main.email_send, "send_verification_email",
                        return_value=True)
    @mock.patch.object(coordinator_main.email_send, "is_configured",
                        return_value=True)
    def test_signup_gates_generate_until_verified(self, _is_cfg, _send):
        resp = self.client.post("/signup", json=_signup_payload())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["email_verified"])
        token = body["token"]

        blocked = self.client.post(
            "/generate",
            json={"prompt": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(blocked.status_code, 403)

        code = self._find_verify_code()
        verified = self.client.get(f"/verify-email?code={code}")
        self.assertEqual(verified.status_code, 200)

        allowed = self.client.post(
            "/generate",
            json={"prompt": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(allowed.status_code, 200)

        # Single-use: the same link again is a clean 400, not a repeat
        # 200 that could re-verify a different (reassigned) code.
        reused = self.client.get(f"/verify-email?code={code}")
        self.assertEqual(reused.status_code, 400)

    @mock.patch.object(coordinator_main.email_send, "send_verification_email",
                        return_value=True)
    @mock.patch.object(coordinator_main.email_send, "is_configured",
                        return_value=True)
    def test_verify_link_is_an_absolute_url(self, _is_cfg, send_mock):
        # PUBLIC_BASE_URL is unset in this test env, so the link must
        # fall back to the live request's own host (TestClient's
        # "http://testserver") rather than emailing a bare
        # "/verify-email?code=..." path with no host — a link like
        # that renders as dead text in a real inbox. Regression guard
        # for a shadowed-PUBLIC_BASE_URL bug caught before this shipped.
        self.client.post("/signup", json=_signup_payload())
        send_mock.assert_called_once()
        _to_email, verify_url = send_mock.call_args[0]
        self.assertTrue(
            verify_url.startswith("http://testserver/verify-email?code="),
            verify_url,
        )

    def test_verify_email_rejects_unknown_code(self):
        resp = self.client.get("/verify-email?code=not-a-real-code")
        self.assertEqual(resp.status_code, 400)

    @mock.patch.object(coordinator_main.email_send, "send_verification_email",
                        return_value=True)
    @mock.patch.object(coordinator_main.email_send, "is_configured",
                        return_value=True)
    def test_resend_verification_rejects_already_verified_member(self, _c, _s):
        resp = self.client.post(
            "/me/resend-verification",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 400)

    @mock.patch.object(coordinator_main.email_send, "send_verification_email",
                        return_value=True)
    @mock.patch.object(coordinator_main.email_send, "is_configured",
                        return_value=True)
    def test_resend_verification_issues_a_new_working_code(self, _c, _s):
        signup = self.client.post("/signup", json=_signup_payload()).json()
        token = signup["token"]
        # Burn the original code so only the resent one is live.
        self.r.delete(*self.r.keys("email_verify:*"))

        resend = self.client.post(
            "/me/resend-verification",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resend.status_code, 200)
        self.assertFalse(resend.json()["email_verified"])

        code = self._find_verify_code()
        self.assertEqual(
            self.client.get(f"/verify-email?code={code}").status_code, 200,
        )
        me = self.client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        ).json()
        self.assertTrue(me["email_verified"])


if __name__ == "__main__":
    unittest.main()
