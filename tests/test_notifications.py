"""Tests for the Phase-6 push-notifications backend.

Covers:
  - GET /notifications/vapid-key  (public, returns null when unset)
  - POST/DELETE /notifications/subscribe  (member-scoped, upsert on
    duplicate endpoint)
  - GET /notifications  (paginated)
  - GET /notifications/unread-count
  - PUT /notifications/{id}/read
  - PUT /notifications/read-all
  - GET/PUT /notifications/preferences

Plus the ``send_to_member`` infrastructure:
  - VAPID-unconfigured path (in-app row persists, push count = 0)
  - Opt-out path (no row, no push)
  - Dead-subscription cleanup via mocked pywebpush

Run with ``python -m unittest tests.test_notifications``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# 1. Env BEFORE imports — auth on, fresh DB, no rate limit. VAPID kept
#    unset for the default test runs; individual tests toggle it on
#    via reload() helpers below.
_TMPDIR = tempfile.mkdtemp(prefix="gamerai-test-notif-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["API_TOKEN"] = "admin-seed-token-for-notif-tests"
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("STRICT_MODELS", None)
os.environ.pop("VAPID_PUBLIC_KEY", None)
os.environ.pop("VAPID_PRIVATE_KEY", None)

# 2. Drop cached modules so the new env is picked up.
for _mod in list(sys.modules):
    if _mod.split(".", 1)[0] in ("shared", "coordinator"):
        del sys.modules[_mod]

# 3. Patch the Redis factory.
import fakeredis  # noqa: E402

import coordinator.redis_client  # noqa: E402

_FAKE = fakeredis.FakeStrictRedis(decode_responses=True)
coordinator.redis_client.get_client = lambda: _FAKE  # type: ignore[assignment]

# 4. Import after env + Redis stub.
from fastapi.testclient import TestClient  # noqa: E402

from coordinator import admin as coordinator_admin  # noqa: E402
from coordinator import main as coordinator_main  # noqa: E402
from coordinator import notifications  # noqa: E402

ADMIN_TOKEN = os.environ["API_TOKEN"]


class NotificationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator_main.app)
        cls.r = _FAKE
        cls.db = coordinator_main.db
        coordinator_main.ensure_admin_seed()

    def setUp(self):
        # Each test starts from a clean push/notifications/preferences
        # slate but keeps the admin member row alive (it's the
        # API_TOKEN seed). A handful of tests also create a fresh
        # contributor to exercise non-admin paths.
        self.r.flushall()
        self.db._conn.executescript(
            "DELETE FROM push_subscriptions; "
            "DELETE FROM notifications; "
            "DELETE FROM notification_preferences; "
            "DELETE FROM members WHERE role <> 'admin';"
        )
        # admin member id is deterministic from API_TOKEN; look it up
        # for use in send_to_member assertions below.
        row = self.db._conn.execute(
            "SELECT member_id FROM members WHERE role='admin' LIMIT 1"
        ).fetchone()
        self.admin_member_id = row["member_id"]

    # ------------------------------------------------------------------
    # /notifications/vapid-key (public)
    # ------------------------------------------------------------------
    def test_vapid_key_endpoint_returns_null_when_unconfigured(self):
        # No bearer required — vapid-key is public.
        resp = self.client.get("/notifications/vapid-key")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"key": None})

    def test_vapid_key_endpoint_returns_configured_key(self):
        with mock.patch.object(
            notifications, "VAPID_PUBLIC_KEY", "BPublicKeyExampleHere",
        ):
            resp = self.client.get("/notifications/vapid-key")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"key": "BPublicKeyExampleHere"})

    # ------------------------------------------------------------------
    # subscribe / unsubscribe
    # ------------------------------------------------------------------
    def _subscribe(self, token, endpoint="https://push.example.com/abc"):
        return self.client.post(
            "/notifications/subscribe",
            json={
                "endpoint": endpoint,
                "keys": {"p256dh": "p256test", "auth": "authtest"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_subscribe_requires_auth(self):
        resp = self.client.post(
            "/notifications/subscribe",
            json={"endpoint": "https://e", "keys": {"p256dh": "p", "auth": "a"}},
        )
        self.assertEqual(resp.status_code, 401)

    def test_subscribe_creates_row(self):
        resp = self._subscribe(ADMIN_TOKEN)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["id"], int)
        rows = self.db.list_push_subscriptions(self.admin_member_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["endpoint"], "https://push.example.com/abc")
        self.assertEqual(rows[0]["p256dh"], "p256test")
        self.assertEqual(rows[0]["auth"], "authtest")

    def test_subscribe_twice_same_endpoint_upserts(self):
        self._subscribe(ADMIN_TOKEN)
        # Same endpoint, new keys (browser rotated them).
        resp = self.client.post(
            "/notifications/subscribe",
            json={
                "endpoint": "https://push.example.com/abc",
                "keys": {"p256dh": "newp256", "auth": "newauth"},
            },
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        rows = self.db.list_push_subscriptions(self.admin_member_id)
        self.assertEqual(len(rows), 1, "duplicate endpoint should upsert")
        self.assertEqual(rows[0]["p256dh"], "newp256")
        self.assertEqual(rows[0]["auth"], "newauth")

    def test_subscribe_rejects_empty_endpoint(self):
        resp = self.client.post(
            "/notifications/subscribe",
            json={"endpoint": "", "keys": {"p256dh": "p", "auth": "a"}},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_unsubscribe_removes_row(self):
        self._subscribe(ADMIN_TOKEN)
        resp = self.client.request(
            "DELETE",
            "/notifications/subscribe",
            json={"endpoint": "https://push.example.com/abc"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["removed"])
        self.assertEqual(
            len(self.db.list_push_subscriptions(self.admin_member_id)), 0,
        )

    def test_unsubscribe_unknown_endpoint_is_noop(self):
        resp = self.client.request(
            "DELETE",
            "/notifications/subscribe",
            json={"endpoint": "https://never-subscribed"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["removed"])

    # ------------------------------------------------------------------
    # list + unread-count + mark-read
    # ------------------------------------------------------------------
    def _seed_notifications(self, n=3):
        ids = []
        for i in range(n):
            nid = notifications._new_notification_id()
            self.db.insert_notification(
                notification_id=nid,
                member_id=self.admin_member_id,
                type_=notifications.CATEGORY_SYSTEM,
                title=f"title {i}",
                body=f"body {i}",
                data_json=json.dumps({"url": f"/conv/c{i}"}),
                pushed=False,
                now=1700000000.0 + i,
            )
            ids.append(nid)
        return ids

    def test_list_notifications_returns_newest_first(self):
        ids = self._seed_notifications(3)
        resp = self.client.get(
            "/notifications",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        out = resp.json()["notifications"]
        self.assertEqual(len(out), 3)
        # DESC by created_at — last inserted is first
        self.assertEqual(out[0]["notification_id"], ids[2])
        self.assertEqual(out[0]["data"], {"url": "/conv/c2"})
        self.assertEqual(out[0]["read"], False)

    def test_list_notifications_pagination(self):
        self._seed_notifications(5)
        resp = self.client.get(
            "/notifications?limit=2&offset=1",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["notifications"]), 2)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["offset"], 1)

    def test_list_notifications_caps_limit_at_200(self):
        resp = self.client.get(
            "/notifications?limit=999",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["limit"], 200)

    def test_unread_count(self):
        ids = self._seed_notifications(3)
        resp = self.client.get(
            "/notifications/unread-count",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.json(), {"count": 3})
        # Mark one read; count drops by one.
        self.db.mark_notification_read(self.admin_member_id, ids[0])
        resp = self.client.get(
            "/notifications/unread-count",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.json(), {"count": 2})

    def test_mark_read_single(self):
        ids = self._seed_notifications(2)
        resp = self.client.put(
            f"/notifications/{ids[0]}/read",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(
            self.db.unread_notification_count(self.admin_member_id), 1,
        )

    def test_mark_read_unknown_is_404(self):
        resp = self.client.put(
            "/notifications/notif_unknown/read",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_mark_read_other_members_notif_is_404(self):
        ids = self._seed_notifications(1)
        # Create a contributor and try to mark admin's notification as
        # read using their token — must 404, not silently succeed.
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            coordinator_admin.main(["create-member", "--role", "contributor"])
        other_token = None
        for line in buf.getvalue().splitlines():
            if line.startswith("token="):
                other_token = line.split("=", 1)[1].strip()
                break
        self.assertIsNotNone(other_token)
        resp = self.client.put(
            f"/notifications/{ids[0]}/read",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_mark_all_read(self):
        self._seed_notifications(4)
        resp = self.client.put(
            "/notifications/read-all",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"marked": 4})
        self.assertEqual(
            self.db.unread_notification_count(self.admin_member_id), 0,
        )

    # ------------------------------------------------------------------
    # preferences
    # ------------------------------------------------------------------
    def test_get_preferences_defaults_to_all_enabled(self):
        resp = self.client.get(
            "/notifications/preferences",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for cat in notifications.KNOWN_CATEGORIES:
            self.assertTrue(
                body["preferences"][cat],
                f"default should be enabled for {cat}",
            )
        self.assertEqual(
            sorted(body["categories"]),
            sorted(notifications.KNOWN_CATEGORIES),
        )

    def test_put_preferences_persists_then_reads_back(self):
        resp = self.client.put(
            "/notifications/preferences",
            json={"preferences": {
                notifications.CATEGORY_IMAGE_DONE: False,
                notifications.CATEGORY_VOICE_DONE: True,
                "unknown_category": False,  # silently ignored
            }},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(resp.status_code, 200)
        applied = resp.json()["applied"]
        self.assertEqual(applied[notifications.CATEGORY_IMAGE_DONE], False)
        self.assertEqual(applied[notifications.CATEGORY_VOICE_DONE], True)
        self.assertNotIn("unknown_category", applied)

        resp = self.client.get(
            "/notifications/preferences",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        prefs = resp.json()["preferences"]
        self.assertFalse(prefs[notifications.CATEGORY_IMAGE_DONE])
        self.assertTrue(prefs[notifications.CATEGORY_VOICE_DONE])
        # system stayed default-True (not touched)
        self.assertTrue(prefs[notifications.CATEGORY_SYSTEM])

    # ------------------------------------------------------------------
    # send_to_member (the actual delivery infrastructure)
    # ------------------------------------------------------------------
    def test_send_without_vapid_inserts_row_and_returns_zero(self):
        # No VAPID configured (the module-level env was popped at
        # setUpClass time). send_to_member should still persist the
        # in-app row, just skip the push fan-out.
        with mock.patch.object(notifications, "VAPID_PUBLIC_KEY", None), \
             mock.patch.object(notifications, "VAPID_PRIVATE_KEY", None):
            pushed = notifications.send_to_member(
                self.db,
                self.admin_member_id,
                notifications.CATEGORY_SYSTEM,
                "title",
                "body",
                {"url": "/x"},
            )
        self.assertEqual(pushed, 0)
        rows = self.db.list_notifications(self.admin_member_id, 10, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "title")

    def test_send_respects_opt_out(self):
        # Member opts out of image_done.
        self.db.set_notification_preference(
            self.admin_member_id,
            notifications.CATEGORY_IMAGE_DONE,
            False,
        )
        pushed = notifications.send_to_member(
            self.db,
            self.admin_member_id,
            notifications.CATEGORY_IMAGE_DONE,
            "title",
            "body",
        )
        self.assertEqual(pushed, 0)
        # No row inserted either.
        rows = self.db.list_notifications(self.admin_member_id, 10, 0)
        self.assertEqual(len(rows), 0)

    def test_send_unknown_category_bypasses_preferences(self):
        # An 'experimental_new_thing' category not in KNOWN_CATEGORIES
        # should always send (no preference toggle exists for it yet).
        self.db.set_notification_preference(
            self.admin_member_id, "experimental", False,
        )
        with mock.patch.object(notifications, "VAPID_PUBLIC_KEY", None):
            pushed = notifications.send_to_member(
                self.db, self.admin_member_id,
                "experimental", "t", "b",
            )
        # 0 because VAPID is off, but the row should still be there
        self.assertEqual(pushed, 0)
        rows = self.db.list_notifications(self.admin_member_id, 10, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "experimental")

    def test_send_deletes_subscription_on_410(self):
        # Seed a subscription, configure VAPID, and arrange a fake
        # pywebpush that raises a 410 WebPushException — the cleanup
        # path must delete the dead subscription row.
        self._subscribe(ADMIN_TOKEN)
        self.assertEqual(
            len(self.db.list_push_subscriptions(self.admin_member_id)), 1,
        )

        class FakeWebPushException(Exception):
            def __init__(self, response):
                self.response = response

        class FakeResp:
            def __init__(self, status):
                self.status_code = status

        def fake_webpush(**kwargs):
            raise FakeWebPushException(FakeResp(410))

        with mock.patch.object(
                notifications, "VAPID_PUBLIC_KEY", "BPub"), \
             mock.patch.object(
                notifications, "VAPID_PRIVATE_KEY", "Priv"), \
             mock.patch.object(
                notifications, "_import_webpush",
                return_value=(fake_webpush, FakeWebPushException)):
            pushed = notifications.send_to_member(
                self.db, self.admin_member_id,
                notifications.CATEGORY_SYSTEM, "t", "b",
            )
        self.assertEqual(pushed, 0)
        self.assertEqual(
            len(self.db.list_push_subscriptions(self.admin_member_id)),
            0,
            "410 should have triggered subscription cleanup",
        )

    # ------------------------------------------------------------------
    # /jobs/complete trigger — the actual integration point.
    # ------------------------------------------------------------------
    # 1x1 transparent PNG: mirrors the constant in test_coordinator_e2e
    # so the image-complete path passes its magic-header check.
    _MOCK_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def test_image_complete_fires_push_for_submitter(self):
        """Submitting an image, then completing it as a worker, must
        leave an 'image_done' notification row for the submitter."""
        import uuid
        # Use the admin token directly as the submitter — same auth
        # path as a real member token (admin is just a member with
        # role=admin), and self.admin_member_id is already cached.
        headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        worker_id = "wkr-notif-" + uuid.uuid4().hex[:6]
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"tools": ["chat", "image"]},
            },
            headers=headers,
        )
        gr = self.client.post(
            "/generate",
            json={"prompt": "draw a cat", "tool": "image"},
            headers=headers,
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        job_id = gr.json()["job_id"]
        nxt = self.client.post(
            "/jobs/next",
            json={"worker_id": worker_id, "tool": "image"},
            headers=headers,
        ).json()
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
                "duration_seconds": 1.0,
                "status": "complete",
                "image_b64": self._MOCK_PNG_B64,
                "claim_token": claim_token,
            },
            headers=headers,
        )
        self.assertEqual(comp.status_code, 200, comp.text)
        rows = self.db.list_notifications(self.admin_member_id, 10, 0)
        self.assertEqual(len(rows), 1, "expected one notification row")
        self.assertEqual(rows[0]["type"], notifications.CATEGORY_IMAGE_DONE)
        self.assertEqual(rows[0]["title"], "Your image is ready")
        # data should at least carry url so the click navigates back.
        data = json.loads(rows[0]["data"]) if rows[0]["data"] else None
        self.assertIsNotNone(data)
        self.assertEqual(data["url"], "/")
        self.assertEqual(data["job_id"], job_id)

    def test_image_error_does_not_fire_push(self):
        """Failed image completions don't deserve a push — the user
        already sees the error bubble. No notification row."""
        import uuid
        headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        worker_id = "wkr-notiferr-" + uuid.uuid4().hex[:6]
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"tools": ["chat", "image"]},
            },
            headers=headers,
        )
        gr = self.client.post(
            "/generate",
            json={"prompt": "doomed image", "tool": "image"},
            headers=headers,
        )
        job_id = gr.json()["job_id"]
        nxt = self.client.post(
            "/jobs/next",
            json={"worker_id": worker_id, "tool": "image"},
            headers=headers,
        ).json()
        claim_token = nxt["claim_token"]
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "text": "",
                "model": "dreamshaper8",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "duration_seconds": 0.5,
                "status": "error",
                "error": "worker bombed",
                "claim_token": claim_token,
            },
            headers=headers,
        )
        rows = self.db.list_notifications(self.admin_member_id, 10, 0)
        self.assertEqual(rows, [])

    def test_chat_complete_does_not_fire_push(self):
        """Chat is streamed live; pushing for a chat completion would
        be noise. Only image / voice trigger."""
        import uuid
        headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        worker_id = "wkr-notifchat-" + uuid.uuid4().hex[:6]
        self.client.post(
            "/register",
            json={"worker_id": worker_id},
            headers=headers,
        )
        gr = self.client.post(
            "/generate", json={"prompt": "hi"}, headers=headers,
        )
        job_id = gr.json()["job_id"]
        # /jobs/next defaults to chat; no need to pass tool.
        nxt = self.client.post(
            "/jobs/next", json={"worker_id": worker_id}, headers=headers,
        ).json()
        claim_token = nxt["claim_token"]
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "text": "hello",
                "model": "mock",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "duration_seconds": 0.1,
                "status": "complete",
                "claim_token": claim_token,
            },
            headers=headers,
        )
        rows = self.db.list_notifications(self.admin_member_id, 10, 0)
        self.assertEqual(rows, [])

    def test_send_succeeds_when_webpush_returns_normally(self):
        self._subscribe(ADMIN_TOKEN, endpoint="https://e1")
        self._subscribe(ADMIN_TOKEN, endpoint="https://e2")
        sent = []

        def fake_webpush(**kwargs):
            sent.append(kwargs["subscription_info"]["endpoint"])

        class _Exc(Exception):
            pass

        with mock.patch.object(
                notifications, "VAPID_PUBLIC_KEY", "BPub"), \
             mock.patch.object(
                notifications, "VAPID_PRIVATE_KEY", "Priv"), \
             mock.patch.object(
                notifications, "_import_webpush",
                return_value=(fake_webpush, _Exc)):
            pushed = notifications.send_to_member(
                self.db, self.admin_member_id,
                notifications.CATEGORY_SYSTEM, "Title", "Body",
                {"url": "/conv/abc"},
            )
        self.assertEqual(pushed, 2)
        self.assertEqual(sorted(sent), ["https://e1", "https://e2"])
        rows = self.db.list_notifications(self.admin_member_id, 10, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pushed"], 1)  # bool stored as 1


if __name__ == "__main__":
    unittest.main()
