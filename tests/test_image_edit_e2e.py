"""Coordinator-side tests for image alteration (img2img): /generate
validates and forwards init_image_b64/strength for tool=image jobs.

The real NSFW classifier (NudeNet) needs downloaded model weights this
sandbox doesn't have — tests/test_coordinator_e2e.py already treats
that as out of unit-test scope (see its own comment on the subject).
This file follows the same precedent: deterministic validation (size,
base64 shape, magic bytes) gets real assertions; the NSFW *wiring* is
tested by mocking the classifier call, not by trusting real model
inference to run in CI.

Run with ``python -m unittest tests.test_image_edit_e2e``.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
import uuid
from unittest import mock


_TINY_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n" + b"0" * 64
).decode("ascii")
_TINY_JPEG = base64.b64encode(
    b"\xff\xd8\xff" + b"0" * 64
).decode("ascii")


class _BaseE2E(unittest.TestCase):
    """Mirrors tests/test_conversations.py's _BaseE2E."""

    @classmethod
    def setUpClass(cls):
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
        os.environ["CANARY_INTERVAL_SECONDS"] = "0"

        for mod in list(sys.modules):
            if mod.split(".", 1)[0] in ("shared", "coordinator"):
                del sys.modules[mod]

        import fakeredis
        from fastapi.testclient import TestClient

        from coordinator import main as coord_main

        coord_main.r = fakeredis.FakeRedis(decode_responses=True)
        cls.r = coord_main.r
        cls.coord_main = coord_main
        cls.db = coord_main.db
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

    def setUp(self):
        self.r.delete("job_queue:image")

    def _admin_headers(self):
        return {"Authorization": "Bearer test-admin-token"}

    def _make_member(self, role: str = "contributor", email: str | None = None):
        from coordinator import member_auth
        mem_id = "mem_" + uuid.uuid4().hex[:12]
        raw = member_auth.generate_token()
        self.db.create_member(
            member_id=mem_id,
            email=email,
            role=role,
            parent_member_id=None,
            token_hash=member_auth.hash_token(raw),
            tier="BRONZE",
            daily_quota_tokens=None,
        )
        return mem_id, raw

    def _register_image_worker(self):
        self.client.post(
            "/register",
            json={
                "worker_id": "img-worker-1",
                "capabilities": {"tools": ["image"]},
            },
            headers=self._admin_headers(),
        )
        self.client.post(
            "/heartbeat",
            json={"worker_id": "img-worker-1", "status": "idle"},
            headers=self._admin_headers(),
        )


class ImageEditGenerateTests(_BaseE2E):
    def test_init_image_forwarded_in_job_envelope(self):
        self._register_image_worker()
        _, t = self._make_member(email="ie1@x.com")
        r = self.client.post(
            "/generate",
            json={
                "prompt": "make it blue",
                "tool": "image",
                "image": {"init_image_b64": _TINY_PNG, "strength": 0.4},
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        envelope = json.loads(self.r.lpop("job_queue:image"))
        self.assertEqual(envelope["image"]["init_image_b64"], _TINY_PNG)
        self.assertEqual(envelope["image"]["strength"], 0.4)

    def test_jpeg_init_image_accepted(self):
        self._register_image_worker()
        _, t = self._make_member(email="ie2@x.com")
        r = self.client.post(
            "/generate",
            json={
                "prompt": "make it blue",
                "tool": "image",
                "image": {"init_image_b64": _TINY_JPEG},
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_txt2img_unaffected_no_init_image_key(self):
        self._register_image_worker()
        _, t = self._make_member(email="ie3@x.com")
        r = self.client.post(
            "/generate",
            json={"prompt": "a cat", "tool": "image"},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        envelope = json.loads(self.r.lpop("job_queue:image"))
        self.assertNotIn("init_image_b64", envelope["image"])

    def test_strength_clamped_to_valid_range(self):
        self._register_image_worker()
        _, t = self._make_member(email="ie4@x.com")
        r = self.client.post(
            "/generate",
            json={
                "prompt": "x", "tool": "image",
                "image": {"init_image_b64": _TINY_PNG, "strength": 5.0},
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        envelope = json.loads(self.r.lpop("job_queue:image"))
        self.assertEqual(envelope["image"]["strength"], 1.0)

    def test_invalid_base64_rejected(self):
        self._register_image_worker()
        _, t = self._make_member(email="ie5@x.com")
        r = self.client.post(
            "/generate",
            json={
                "prompt": "x", "tool": "image",
                "image": {"init_image_b64": "not-valid-base64!!!"},
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.r.llen("job_queue:image"), 0)

    def test_bad_magic_bytes_rejected(self):
        self._register_image_worker()
        _, t = self._make_member(email="ie6@x.com")
        garbage = base64.b64encode(b"not an image at all").decode("ascii")
        r = self.client.post(
            "/generate",
            json={
                "prompt": "x", "tool": "image",
                "image": {"init_image_b64": garbage},
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.r.llen("job_queue:image"), 0)

    def test_oversized_init_image_rejected(self):
        self._register_image_worker()
        old = self.coord_main.MAX_IMAGE_BYTES
        self.coord_main.MAX_IMAGE_BYTES = 32
        try:
            _, t = self._make_member(email="ie7@x.com")
            r = self.client.post(
                "/generate",
                json={
                    "prompt": "x", "tool": "image",
                    "image": {"init_image_b64": _TINY_PNG},
                },
                headers={"Authorization": f"Bearer {t}"},
            )
            self.assertEqual(r.status_code, 413)
            self.assertEqual(self.r.llen("job_queue:image"), 0)
        finally:
            self.coord_main.MAX_IMAGE_BYTES = old

    def test_nsfw_rejection_wiring(self):
        """Not testing NudeNet's real detection — mocking the
        classifier call to confirm /generate turns a positive hit into
        a clean 400 with no job queued, i.e. that MY integration code
        (not NudeNet's model accuracy) is wired correctly."""
        self._register_image_worker()
        _, t = self._make_member(email="ie8@x.com")
        with mock.patch.object(
            self.coord_main, "_classify_image_or_raise",
            side_effect=self.coord_main._NSFWFilteredError("blocked"),
        ):
            r = self.client.post(
                "/generate",
                json={
                    "prompt": "x", "tool": "image",
                    "image": {"init_image_b64": _TINY_PNG},
                },
                headers={"Authorization": f"Bearer {t}"},
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.r.llen("job_queue:image"), 0)


if __name__ == "__main__":
    unittest.main()
