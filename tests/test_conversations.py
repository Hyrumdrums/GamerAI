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


class ChatMessagesBuilderTests(_BaseE2E):
    """Pure-function test on _build_chat_messages. The coordinator
    sends this messages[] array to Ollama's /api/chat so the model's
    own chat template is applied — no more hand-rolled User:/Assistant:
    transcript that the model would echo back at us."""

    def test_builds_role_tagged_messages(self):
        _build_chat_messages = self.coord_main._build_chat_messages
        prior = [
            {"role": "user", "text": "hi", "status": "complete"},
            {"role": "assistant", "text": "hello!", "status": "complete"},
        ]
        out = _build_chat_messages(prior, "how are you?")
        self.assertEqual(
            out,
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello!"},
                {"role": "user", "content": "how are you?"},
            ],
        )

    def test_skips_empty_pending_assistant(self):
        # A failed-but-not-retried turn leaves an empty/pending assistant
        # row in the history. It must NOT appear in the messages array
        # we send to the model, or we'd inject a blank assistant turn
        # mid-conversation.
        _build_chat_messages = self.coord_main._build_chat_messages
        prior = [
            {"role": "user", "text": "first", "status": "complete"},
            {"role": "assistant", "text": "", "status": "pending"},
        ]
        out = _build_chat_messages(prior, "retry please")
        self.assertEqual(
            out,
            [
                {"role": "user", "content": "first"},
                {"role": "user", "content": "retry please"},
            ],
        )


class StreamingPersistenceTests(_BaseE2E):
    """Pending-at-enqueue + /jobs/partial + finalize-in-place. These
    are the server-side guarantees that let a client which reloads the
    page mid-stream see its message + the partial answer so far."""

    def _claim(self, job_id: str) -> None:
        self.client.post(
            "/jobs/claim",
            json={"worker_id": "wstream", "job_id": job_id},
            headers=self._admin_headers(),
        )

    def test_generate_persists_pending_message_immediately(self):
        _, t = self._make_member(email="streamy@x.com")
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        gen = self.client.post(
            "/generate",
            json={"prompt": "stream me", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gen.status_code, 200, gen.text)
        body = gen.json()
        self.assertIn("assistant_message_id", body)
        self.assertTrue(body["assistant_message_id"].startswith("msg_"))
        # Before any worker activity: user message complete, assistant pending.
        full = self.client.get(
            f"/conversations/{cid}",
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        msgs = full["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["status"], "complete")
        self.assertEqual(msgs[0]["text"], "stream me")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["status"], "pending")
        self.assertEqual(msgs[1]["text"], "")
        self.assertEqual(msgs[1]["job_id"], body["job_id"])

    def test_partials_update_pending_message_text(self):
        _, t = self._make_member(email="parti@x.com")
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        gen = self.client.post(
            "/generate", json={"prompt": "hi", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        job_id = gen["job_id"]
        self._claim(job_id)
        for text in ("Hel", "Hello", "Hello!"):
            r = self.client.post(
                "/jobs/partial",
                json={"worker_id": "wstream", "job_id": job_id, "text": text},
                headers=self._admin_headers(),
            )
            self.assertEqual(r.status_code, 200, r.text)
        full = self.client.get(
            f"/conversations/{cid}",
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        assistant = full["messages"][1]
        self.assertEqual(assistant["status"], "pending")
        self.assertEqual(assistant["text"], "Hello!")
        # /result also surfaces the partial with done=False so an
        # active poller sees it.
        res = self.client.get(
            f"/result/{job_id}", headers={"Authorization": f"Bearer {t}"},
        ).json()
        self.assertEqual(res["text"], "Hello!")
        self.assertFalse(res["done"])

    def test_partial_from_non_claimant_is_rejected(self):
        _, t = self._make_member(email="claim@x.com")
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        job_id = self.client.post(
            "/generate", json={"prompt": "x", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()["job_id"]
        # wA claims, wB tries to push a partial
        self.client.post(
            "/jobs/claim", json={"worker_id": "wA", "job_id": job_id},
            headers=self._admin_headers(),
        )
        r = self.client.post(
            "/jobs/partial",
            json={"worker_id": "wB", "job_id": job_id, "text": "stolen"},
            headers=self._admin_headers(),
        )
        self.assertEqual(r.status_code, 403)

    def test_complete_finalizes_pending_in_place(self):
        _, t = self._make_member(email="finz@x.com")
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        job_id = self.client.post(
            "/generate", json={"prompt": "q", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()["job_id"]
        self._claim(job_id)
        self.client.post(
            "/jobs/partial",
            json={"worker_id": "wstream", "job_id": job_id, "text": "partial..."},
            headers=self._admin_headers(),
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wstream", "job_id": job_id,
                "text": "final answer", "model": "llama3.2:1b",
                "prompt_tokens": 2, "completion_tokens": 3,
                "duration_seconds": 0.1, "status": "complete",
            },
            headers=self._admin_headers(),
        )
        full = self.client.get(
            f"/conversations/{cid}",
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        # Still exactly two messages — no double-append on completion.
        self.assertEqual(len(full["messages"]), 2)
        a = full["messages"][1]
        self.assertEqual(a["status"], "complete")
        self.assertEqual(a["text"], "final answer")
        self.assertEqual(a["completion_tokens"], 3)

    def test_error_finalize_leaves_user_message_visible(self):
        _, t = self._make_member(email="err@x.com")
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        job_id = self.client.post(
            "/generate", json={"prompt": "doomed", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()["job_id"]
        self._claim(job_id)
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wstream", "job_id": job_id, "text": "",
                "model": "llama3.2:1b", "prompt_tokens": 0,
                "completion_tokens": 0, "duration_seconds": 0.05,
                "status": "error", "error": "model crashed",
            },
            headers=self._admin_headers(),
        )
        full = self.client.get(
            f"/conversations/{cid}",
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        self.assertEqual(len(full["messages"]), 2)
        u, a = full["messages"]
        self.assertEqual(u["role"], "user")
        self.assertEqual(u["text"], "doomed")  # user message persists on failure
        self.assertEqual(a["status"], "error")
        self.assertIn("model crashed", a["text"])

    def test_generate_blocked_while_previous_pending(self):
        _, t = self._make_member(email="gate@x.com")
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        self.client.post(
            "/generate", json={"prompt": "first", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        )
        # Second turn before completing the first.
        r = self.client.post(
            "/generate", json={"prompt": "second", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 409)


class RetryEndpointTests(_BaseE2E):
    def _setup_failed_message(self, email: str) -> tuple[str, str, str]:
        _, t = self._make_member(email=email)
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        gen = self.client.post(
            "/generate", json={"prompt": "ask me", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        job_id = gen["job_id"]
        message_id = gen["assistant_message_id"]
        self.client.post(
            "/jobs/claim",
            json={"worker_id": "wretry", "job_id": job_id},
            headers=self._admin_headers(),
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wretry", "job_id": job_id, "text": "",
                "model": "llama3.2:1b", "prompt_tokens": 0,
                "completion_tokens": 0, "duration_seconds": 0.05,
                "status": "error", "error": "boom",
            },
            headers=self._admin_headers(),
        )
        return t, cid, message_id

    def test_retry_resets_message_and_enqueues_new_job(self):
        t, cid, message_id = self._setup_failed_message("ret1@x.com")
        r = self.client.post(
            f"/messages/{message_id}/retry",
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertNotEqual(body["job_id"], "")
        # Message reset to pending with the new job_id.
        full = self.client.get(
            f"/conversations/{cid}",
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        a = full["messages"][1]
        self.assertEqual(a["status"], "pending")
        self.assertEqual(a["text"], "")
        self.assertEqual(a["job_id"], body["job_id"])

    def test_retry_cooldown_blocks_second_press(self):
        t, _cid, message_id = self._setup_failed_message("ret2@x.com")
        first = self.client.post(
            f"/messages/{message_id}/retry",
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(first.status_code, 200)
        # Second attempt immediately should hit the cooldown. The
        # message is now pending, but the cooldown check fires first
        # so the user sees 429 not 409.
        second = self.client.post(
            f"/messages/{message_id}/retry",
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(second.status_code, 429)
        self.assertIn("retry-after", {k.lower() for k in second.headers.keys()})

    def test_retry_rejects_non_error_message(self):
        # Build a completed (not failed) message and try to retry it.
        _, t = self._make_member(email="ret3@x.com")
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        gen = self.client.post(
            "/generate", json={"prompt": "ok", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        message_id = gen["assistant_message_id"]
        self.client.post(
            "/jobs/claim", json={"worker_id": "w", "job_id": gen["job_id"]},
            headers=self._admin_headers(),
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "w", "job_id": gen["job_id"], "text": "fine",
                "model": "m", "prompt_tokens": 1, "completion_tokens": 1,
                "duration_seconds": 0.1, "status": "complete",
            },
            headers=self._admin_headers(),
        )
        r = self.client.post(
            f"/messages/{message_id}/retry",
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 409)

    def test_retry_rejects_other_users_message(self):
        _t_owner, _cid, message_id = self._setup_failed_message("ret4@x.com")
        _, intruder = self._make_member(email="intruder@x.com")
        r = self.client.post(
            f"/messages/{message_id}/retry",
            headers={"Authorization": f"Bearer {intruder}"},
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
