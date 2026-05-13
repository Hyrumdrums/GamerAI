"""Tests for the multi-turn conversation surface.

Covers: create + list + get + archive, ownership, the conversation-
aware /generate path, and the auto-append behavior on /jobs/complete
that turns a single job into two new message rows (user + assistant).
"""
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
    """Boot a coordinator instance with auth ON, clean SQLite, fake
    Redis, and a known admin token. Mirrors test_tos_and_canaries.py."""

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


class ConversationCrudTests(_BaseE2E):
    def test_create_returns_id(self):
        _, t = self._make_member(email="a@x.com")
        resp = self.client.post(
            "/conversations",
            json={"title": "test", "model": "llama3.2:1b"},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["conversation_id"].startswith("conv_"))
        self.assertEqual(body["title"], "test")
        self.assertEqual(body["model"], "llama3.2:1b")

    def test_list_shows_only_my_conversations(self):
        _, alice = self._make_member(email="alice@x.com")
        _, bob = self._make_member(email="bob@x.com")
        # Alice creates two
        for title in ("a1", "a2"):
            self.client.post(
                "/conversations",
                json={"title": title},
                headers={"Authorization": f"Bearer {alice}"},
            )
        # Bob creates one
        self.client.post(
            "/conversations",
            json={"title": "bobs"},
            headers={"Authorization": f"Bearer {bob}"},
        )
        bob_list = self.client.get(
            "/conversations", headers={"Authorization": f"Bearer {bob}"}
        ).json()
        self.assertEqual(len(bob_list["conversations"]), 1)
        self.assertEqual(bob_list["conversations"][0]["title"], "bobs")
        alice_list = self.client.get(
            "/conversations", headers={"Authorization": f"Bearer {alice}"}
        ).json()
        self.assertEqual(len(alice_list["conversations"]), 2)

    def test_get_returns_messages_in_order(self):
        _, t = self._make_member(email="c@x.com")
        conv = self.client.post(
            "/conversations", json={"title": "ordered"},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        # Append messages directly to verify seq ordering.
        for i, role, text in [
            (0, "user", "first user"),
            (1, "assistant", "first reply"),
            (2, "user", "second user"),
            (3, "assistant", "second reply"),
        ]:
            self.db.append_message(
                message_id=f"msg_seq{i}",
                conversation_id=cid,
                seq=i,
                role=role,
                text=text,
            )
        full = self.client.get(
            f"/conversations/{cid}", headers={"Authorization": f"Bearer {t}"}
        ).json()
        seqs = [m["seq"] for m in full["messages"]]
        self.assertEqual(seqs, [0, 1, 2, 3])
        roles = [m["role"] for m in full["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])

    def test_get_others_conversation_returns_404(self):
        _, alice = self._make_member(email="alice2@x.com")
        _, bob = self._make_member(email="bob2@x.com")
        conv = self.client.post(
            "/conversations", json={"title": "private"},
            headers={"Authorization": f"Bearer {alice}"},
        ).json()
        # Bob tries to read it.
        resp = self.client.get(
            f"/conversations/{conv['conversation_id']}",
            headers={"Authorization": f"Bearer {bob}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_admin_can_read_any_conversation(self):
        _, alice = self._make_member(email="alice3@x.com")
        conv = self.client.post(
            "/conversations", json={"title": "alices"},
            headers={"Authorization": f"Bearer {alice}"},
        ).json()
        resp = self.client.get(
            f"/conversations/{conv['conversation_id']}",
            headers=self._admin_headers(),
        )
        self.assertEqual(resp.status_code, 200)

    def test_delete_archives(self):
        _, t = self._make_member(email="d@x.com")
        conv = self.client.post(
            "/conversations", json={"title": "todelete"},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        # Default list excludes archived.
        d = self.client.delete(
            f"/conversations/{cid}", headers={"Authorization": f"Bearer {t}"}
        )
        self.assertEqual(d.status_code, 200)
        listed = self.client.get(
            "/conversations", headers={"Authorization": f"Bearer {t}"}
        ).json()["conversations"]
        ids = [c["conversation_id"] for c in listed]
        self.assertNotIn(cid, ids)
        # include_archived=true brings it back.
        listed_all = self.client.get(
            "/conversations?include_archived=true",
            headers={"Authorization": f"Bearer {t}"},
        ).json()["conversations"]
        ids_all = [c["conversation_id"] for c in listed_all]
        self.assertIn(cid, ids_all)


class GenerateConversationContextTests(_BaseE2E):
    def test_generate_without_conversation_unchanged(self):
        resp = self.client.post(
            "/generate",
            json={"prompt": "hello"},
            headers=self._admin_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()["job_id"]
        row = self.db.get_job(job_id)
        # No conversation linkage when not specified.
        self.assertIsNone(row["conversation_id"])
        # Prompt stored as-is.
        self.assertEqual(row["prompt"], "hello")

    def test_generate_with_conversation_stores_link_and_original_prompt(self):
        _, t = self._make_member(email="gen@x.com")
        conv = self.client.post(
            "/conversations", json={}, headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        # Pre-seed prior turns so we can verify the prepend path.
        self.db.append_message(
            message_id="msg_pre_u", conversation_id=cid, seq=0,
            role="user", text="capital of france?",
        )
        self.db.append_message(
            message_id="msg_pre_a", conversation_id=cid, seq=1,
            role="assistant", text="Paris.",
        )
        resp = self.client.post(
            "/generate",
            json={"prompt": "what about spain?", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        job_id = resp.json()["job_id"]
        # Original prompt is what's stored (used as the next user message
        # on completion). The worker sees the prepended version via the
        # queue, but we don't introspect that here.
        row = self.db.get_job(job_id)
        self.assertEqual(row["prompt"], "what about spain?")
        self.assertEqual(row["conversation_id"], cid)

    def test_generate_into_others_conversation_rejected(self):
        _, alice = self._make_member(email="aliceX@x.com")
        _, bob = self._make_member(email="bobX@x.com")
        conv = self.client.post(
            "/conversations", json={}, headers={"Authorization": f"Bearer {alice}"},
        ).json()
        resp = self.client.post(
            "/generate",
            json={"prompt": "intrude", "conversation_id": conv["conversation_id"]},
            headers={"Authorization": f"Bearer {bob}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_generate_into_archived_conversation_rejected(self):
        _, t = self._make_member(email="arch@x.com")
        conv = self.client.post(
            "/conversations", json={}, headers={"Authorization": f"Bearer {t}"},
        ).json()
        self.db.archive_conversation(conv["conversation_id"], time.time())
        resp = self.client.post(
            "/generate",
            json={"prompt": "after archive", "conversation_id": conv["conversation_id"]},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(resp.status_code, 410)


class ConversationCompleteAppendsTests(_BaseE2E):
    def _submit_and_complete(self, token: str, conv_id: str, prompt: str, answer: str):
        sub = self.client.post(
            "/generate",
            json={"prompt": prompt, "conversation_id": conv_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(sub.status_code, 200, sub.text)
        job_id = sub.json()["job_id"]
        # Worker reports back.
        comp = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "w1",
                "job_id": job_id,
                "text": answer,
                "model": "llama3.2:1b",
                "prompt_tokens": 5,
                "completion_tokens": 7,
                "duration_seconds": 1.0,
                "status": "complete",
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(comp.status_code, 200, comp.text)

    def test_two_turn_conversation_persists_in_order(self):
        _, t = self._make_member(email="twoturn@x.com")
        conv = self.client.post(
            "/conversations", json={}, headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        self._submit_and_complete(t, cid, "what's 2+2?", "4")
        self._submit_and_complete(t, cid, "what about 3+3?", "6")
        full = self.client.get(
            f"/conversations/{cid}", headers={"Authorization": f"Bearer {t}"}
        ).json()
        seqs = [(m["seq"], m["role"], m["text"]) for m in full["messages"]]
        self.assertEqual(seqs, [
            (0, "user", "what's 2+2?"),
            (1, "assistant", "4"),
            (2, "user", "what about 3+3?"),
            (3, "assistant", "6"),
        ])

    def test_first_prompt_becomes_title(self):
        _, t = self._make_member(email="title@x.com")
        conv = self.client.post(
            "/conversations", json={}, headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        self.assertIsNone(conv["title"])
        self._submit_and_complete(t, cid, "what's the capital of france?", "Paris.")
        info = self.client.get(
            f"/conversations/{cid}", headers={"Authorization": f"Bearer {t}"}
        ).json()
        self.assertEqual(info["title"], "what's the capital of france?")
        # Title persists across the second turn.
        self._submit_and_complete(t, cid, "of spain?", "Madrid.")
        info = self.client.get(
            f"/conversations/{cid}", headers={"Authorization": f"Bearer {t}"}
        ).json()
        self.assertEqual(info["title"], "what's the capital of france?")


class ChatPromptFormatTests(_BaseE2E):
    """Pure-function test on _format_chat_prompt. Inherits the
    boot-the-coordinator scaffolding so env (DB_PATH etc.) is set
    before any coordinator.* import."""

    def test_concatenates_turns(self):
        _format_chat_prompt = self.coord_main._format_chat_prompt
        prior = [
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "hello!"},
        ]
        out = _format_chat_prompt(prior, "how are you?")
        self.assertIn("User: hi", out)
        self.assertIn("Assistant: hello!", out)
        self.assertIn("User: how are you?", out)
        self.assertTrue(out.rstrip().endswith("Assistant:"))


if __name__ == "__main__":
    unittest.main()
