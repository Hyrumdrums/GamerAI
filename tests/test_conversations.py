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

    def test_delete_hard_deletes_empty_conversation(self):
        # v1.1.26: DELETE is no longer a soft archive — the row goes
        # away. Even include_archived=true cannot bring it back.
        _, t = self._make_member(email="d@x.com")
        conv = self.client.post(
            "/conversations", json={"title": "todelete"},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        d = self.client.delete(
            f"/conversations/{cid}", headers={"Authorization": f"Bearer {t}"}
        )
        self.assertEqual(d.status_code, 200, d.text)
        self.assertTrue(d.json().get("deleted"))
        # Gone from the default list.
        listed = self.client.get(
            "/conversations", headers={"Authorization": f"Bearer {t}"}
        ).json()["conversations"]
        self.assertNotIn(cid, [c["conversation_id"] for c in listed])
        # Gone from the with-archived list too — it's not archived, it's
        # actually deleted.
        listed_all = self.client.get(
            "/conversations?include_archived=true",
            headers={"Authorization": f"Bearer {t}"},
        ).json()["conversations"]
        self.assertNotIn(cid, [c["conversation_id"] for c in listed_all])
        # DB row truly gone.
        self.assertIsNone(self.db.get_conversation(cid))

    def test_delete_cleans_up_messages_jobs_images_and_redis(self):
        # End-to-end purge: a conversation with one chat turn + one
        # image turn must leave NOTHING behind after DELETE — no
        # messages, no jobs, no PNG, no Redis residue. The "obese cow"
        # incident is the motivating scenario.
        import os
        from coordinator.main import IMAGE_DIR
        _, t = self._make_member(email="purge@x.com")
        conv = self.client.post(
            "/conversations", json={}, headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]

        # --- chat turn ---
        chat_gen = self.client.post(
            "/generate", json={"prompt": "hi", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        chat_jid = chat_gen["job_id"]
        chat_claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "wpurge", "job_id": chat_jid},
            headers=self._admin_headers(),
        ).json()
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wpurge", "job_id": chat_jid, "text": "hello",
                "model": "m", "prompt_tokens": 1, "completion_tokens": 1,
                "duration_seconds": 0.1, "status": "complete",
                "claim_token": chat_claim["claim_token"],
            },
            headers=self._admin_headers(),
        )

        # --- image turn ---
        img_gen = self.client.post(
            "/generate",
            json={"prompt": "a dog", "tool": "image", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        img_jid = img_gen["job_id"]
        img_claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "wpurge-img", "job_id": img_jid},
            headers=self._admin_headers(),
        ).json()
        # 1x1 transparent PNG — same fixture the image suite uses.
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wpurge-img", "job_id": img_jid, "text": "",
                "model": "dreamshaper8", "prompt_tokens": 0,
                "completion_tokens": 0, "duration_seconds": 1.0,
                "status": "complete", "image_b64": png_b64,
                "claim_token": img_claim["claim_token"],
            },
            headers=self._admin_headers(),
        )
        image_path = IMAGE_DIR / f"{img_jid}.png"
        self.assertTrue(image_path.exists(), "image should have been written")

        # --- DELETE ---
        d = self.client.delete(
            f"/conversations/{cid}", headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(d.status_code, 200, d.text)
        body = d.json()
        self.assertEqual(body.get("images_removed"), 1)
        self.assertGreaterEqual(body.get("jobs_removed"), 2)

        # Conversation gone.
        self.assertIsNone(self.db.get_conversation(cid))
        # Messages gone.
        self.assertEqual(self.db.list_messages(cid), [])
        # Jobs gone.
        self.assertIsNone(self.db.get_job(chat_jid))
        self.assertIsNone(self.db.get_job(img_jid))
        # Image file off disk.
        self.assertFalse(image_path.exists(), "image file should have been unlinked")
        # Redis residue gone — nothing left under any of the per-job keys.
        self.assertIsNone(self.r.hget("job_results", chat_jid))
        self.assertIsNone(self.r.hget("job_results", img_jid))
        self.assertIsNone(self.r.hget("job_processing", chat_jid))
        self.assertIsNone(self.r.hget("job_processing", img_jid))

    def test_delete_rejects_non_owner(self):
        _, owner = self._make_member(email="powner@x.com")
        _, intruder = self._make_member(email="pintruder@x.com")
        conv = self.client.post(
            "/conversations", json={}, headers={"Authorization": f"Bearer {owner}"},
        ).json()
        cid = conv["conversation_id"]
        # Intruder gets 404 (we don't leak "exists but not yours").
        resp = self.client.delete(
            f"/conversations/{cid}",
            headers={"Authorization": f"Bearer {intruder}"},
        )
        self.assertEqual(resp.status_code, 404)
        # Conversation still exists for the rightful owner.
        self.assertIsNotNone(self.db.get_conversation(cid))


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
        # Claim the job to mint a claim_token. /jobs/complete now
        # enforces token possession to keep a stale (reaper-requeued)
        # worker from clobbering the new claimant.
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "w1", "job_id": job_id},
            headers=self._admin_headers(),
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        claim_token = claim.json()["claim_token"]
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
                "claim_token": claim_token,
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

    def _claim(self, job_id: str) -> str:
        resp = self.client.post(
            "/jobs/claim",
            json={"worker_id": "wstream", "job_id": job_id},
            headers=self._admin_headers(),
        )
        return resp.json()["claim_token"]

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
        claim_token = self._claim(job_id)
        for text in ("Hel", "Hello", "Hello!"):
            r = self.client.post(
                "/jobs/partial",
                json={
                    "worker_id": "wstream", "job_id": job_id, "text": text,
                    "claim_token": claim_token,
                },
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
        # Partial is fire-and-forget so coordinator absorbs the rejection
        # as a 200/stale rather than failing the worker — but the
        # contract is that the text MUST NOT land. Verify both halves.
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("stale"))
        res = self.client.get(
            f"/result/{job_id}",
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        self.assertNotIn("stolen", res.get("text") or "")

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
        claim_token = self._claim(job_id)
        self.client.post(
            "/jobs/partial",
            json={
                "worker_id": "wstream", "job_id": job_id, "text": "partial...",
                "claim_token": claim_token,
            },
            headers=self._admin_headers(),
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wstream", "job_id": job_id,
                "text": "final answer", "model": "llama3.2:1b",
                "prompt_tokens": 2, "completion_tokens": 3,
                "duration_seconds": 0.1, "status": "complete",
                "claim_token": claim_token,
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
        claim_token = self._claim(job_id)
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wstream", "job_id": job_id, "text": "",
                "model": "llama3.2:1b", "prompt_tokens": 0,
                "completion_tokens": 0, "duration_seconds": 0.05,
                "status": "error", "error": "model crashed",
                "claim_token": claim_token,
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
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "wretry", "job_id": job_id},
            headers=self._admin_headers(),
        )
        claim_token = claim.json()["claim_token"]
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wretry", "job_id": job_id, "text": "",
                "model": "llama3.2:1b", "prompt_tokens": 0,
                "completion_tokens": 0, "duration_seconds": 0.05,
                "status": "error", "error": "boom",
                "claim_token": claim_token,
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
        claim = self.client.post(
            "/jobs/claim", json={"worker_id": "w", "job_id": gen["job_id"]},
            headers=self._admin_headers(),
        )
        claim_token = claim.json()["claim_token"]
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "w", "job_id": gen["job_id"], "text": "fine",
                "model": "m", "prompt_tokens": 1, "completion_tokens": 1,
                "duration_seconds": 0.1, "status": "complete",
                "claim_token": claim_token,
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


class ImagePromptRewriteTests(_BaseE2E):
    """v1.1.26: when an image submission lives inside a conversation
    that already has prior turns, /generate enqueues a hidden chat
    job that rewrites the image prompt using context, then dispatches
    the real image job with the rewritten text. Skipped (image goes
    directly to the queue with the raw prompt) when there's no
    context to draw on or no chat worker is online."""

    def _make_chat_worker(self, token: str) -> str:
        # Register a chat-capable worker AND give it a fresh heartbeat
        # so _chat_worker_available_for_rewrite() sees it. Without the
        # heartbeat the worker is considered offline.
        worker_id = f"wkr-rw-{uuid.uuid4().hex[:6]}"
        self.client.post(
            "/register", json={"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.client.post(
            "/heartbeat", json={"worker_id": worker_id, "status": "idle"},
            headers={"Authorization": f"Bearer {token}"},
        )
        return worker_id

    def setUp(self):
        # Class-level setUp doesn't reset Redis between tests, so each
        # test in this class clears just the queue + rewrite-link
        # state it asserts on. Avoids a full flushall (which would
        # blow away worker registrations and heartbeats that prior
        # tests in OTHER classes rely on).
        self.r.delete("job_queue", "job_queue:image", "image_rewrite_pending")

    def _seed_conversation_with_chat_turn(self, t: str) -> str:
        # Build a conversation that has at least one prior completed
        # message, so the "first message in this conv" skip doesn't
        # apply.
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        sub = self.client.post(
            "/generate",
            json={"prompt": "draw some corn", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        )
        job_id = sub.json()["job_id"]
        # /generate enqueues onto the chat queue; a real worker would
        # have LPOPed via /jobs/next. We mimic that by popping
        # directly so the seed doesn't leak residue into the rewrite
        # assertions below.
        self.r.lpop("job_queue")
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "wkr-seed", "job_id": job_id},
            headers=self._admin_headers(),
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wkr-seed", "job_id": job_id,
                "text": "[image: corn]", "model": "llama3.2:1b",
                "prompt_tokens": 5, "completion_tokens": 5,
                "duration_seconds": 0.1, "status": "complete",
                "claim_token": claim.json()["claim_token"],
            },
            headers=self._admin_headers(),
        )
        return cid

    def test_image_in_empty_conversation_skips_rewrite(self):
        # First message in a brand-new conversation is the image
        # request → no context to combine, send straight to image
        # queue with the raw prompt. Tests the explicit skip the
        # user asked for ("yes skip if first message is the image
        # request, with no context").
        _, t = self._make_member(email="rwskip1@x.com")
        self._make_chat_worker(t)
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        gr = self.client.post(
            "/generate",
            json={"prompt": "a cat", "tool": "image", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        # Image landed directly on the image queue, no rewrite link
        # was created.
        self.assertEqual(self.r.llen("job_queue:image"), 1)
        self.assertEqual(self.r.hlen("image_rewrite_pending"), 0)
        # Job row is normal 'pending', not 'awaiting_rewrite'.
        job_row = self.db.get_job(gr.json()["job_id"])
        self.assertEqual(job_row["status"], "pending")

    def test_image_without_conversation_id_skips_rewrite(self):
        # No conversation_id → nothing to draw context from → no
        # rewrite. Same behavior as before the feature shipped.
        _, t = self._make_member(email="rwskip2@x.com")
        self._make_chat_worker(t)
        gr = self.client.post(
            "/generate",
            json={"prompt": "a dog", "tool": "image"},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        self.assertEqual(self.r.llen("job_queue:image"), 1)
        self.assertEqual(self.r.hlen("image_rewrite_pending"), 0)

    def test_image_with_no_chat_worker_online_skips_rewrite(self):
        # Conversation has context but NO chat worker is heartbeating
        # — we fall back to the raw prompt rather than stranding the
        # image behind a rewrite no one can complete. Use the seeded
        # conversation, but DON'T register a fresh worker for the
        # image submission.
        _, t = self._make_member(email="rwskip3@x.com")
        self._make_chat_worker(t)
        cid = self._seed_conversation_with_chat_turn(t)
        # Now blow away the heartbeats so the worker is considered
        # offline at submit time.
        self.r.delete("worker_heartbeats")
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "wrapped in bacon",
                "tool": "image",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        # Image queue gets the raw prompt, no rewrite link stored.
        self.assertEqual(self.r.llen("job_queue:image"), 1)
        envelope = json.loads(self.r.lpop("job_queue:image"))
        self.assertEqual(envelope["prompt"], "wrapped in bacon")
        self.assertEqual(self.r.hlen("image_rewrite_pending"), 0)

    def test_image_with_context_routes_through_rewrite_pipeline(self):
        # The happy path: conversation has a prior turn, chat worker
        # online → /generate enqueues a hidden chat rewrite job
        # (NOT the image directly), the image job row is
        # 'awaiting_rewrite', and an image_rewrite_pending link
        # exists.
        _, t = self._make_member(email="rwhappy@x.com")
        self._make_chat_worker(t)
        cid = self._seed_conversation_with_chat_turn(t)
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "wrapped in bacon",
                "tool": "image",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        image_job_id = gr.json()["job_id"]
        # Image is NOT on the image queue yet.
        self.assertEqual(self.r.llen("job_queue:image"), 0)
        # A chat rewrite job IS on the chat queue.
        self.assertEqual(self.r.llen("job_queue"), 1)
        rewrite_env = json.loads(self.r.lindex("job_queue", 0))
        self.assertEqual(rewrite_env["tool"], "chat")
        # The meta-prompt mentions both the original request and the
        # prior turn (the corn).
        self.assertIn("wrapped in bacon", rewrite_env["prompt"])
        self.assertIn("corn", rewrite_env["prompt"].lower())
        # Image job row is 'awaiting_rewrite'.
        img_row = self.db.get_job(image_job_id)
        self.assertEqual(img_row["status"], "awaiting_rewrite")
        # Linkage stored.
        rewrite_job_id = rewrite_env["job_id"]
        link_raw = self.r.hget("image_rewrite_pending", rewrite_job_id)
        self.assertIsNotNone(link_raw)
        link = json.loads(link_raw)
        self.assertEqual(link["image_job_id"], image_job_id)
        self.assertEqual(link["original_prompt"], "wrapped in bacon")

    def test_rewrite_complete_dispatches_image_with_rewritten_prompt(self):
        # Simulate a chat worker completing the rewrite job and
        # verify the image gets pushed onto the image queue with the
        # rewritten text — and the image job row flips to 'pending'.
        _, t = self._make_member(email="rwdispatch@x.com")
        worker_id = self._make_chat_worker(t)
        cid = self._seed_conversation_with_chat_turn(t)
        image_job_id = self.client.post(
            "/generate",
            json={
                "prompt": "wrapped in bacon",
                "tool": "image",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        ).json()["job_id"]
        rewrite_env = json.loads(self.r.lpop("job_queue"))
        rewrite_job_id = rewrite_env["job_id"]
        # Chat worker claims and completes the rewrite job.
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": rewrite_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        rewritten = "corn wrapped in bacon"
        comp = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id,
                "job_id": rewrite_job_id,
                "text": rewritten,
                "model": "llama3.2:1b",
                "prompt_tokens": 50,
                "completion_tokens": 5,
                "duration_seconds": 1.0,
                "status": "complete",
                "claim_token": claim.json()["claim_token"],
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(comp.status_code, 200, comp.text)
        # Image was pushed onto the image queue with the rewritten
        # prompt, image job is now 'pending', link is gone.
        self.assertEqual(self.r.llen("job_queue:image"), 1)
        image_env = json.loads(self.r.lpop("job_queue:image"))
        self.assertEqual(image_env["prompt"], rewritten)
        img_row = self.db.get_job(image_job_id)
        self.assertEqual(img_row["status"], "pending")
        self.assertEqual(img_row["prompt"], rewritten)
        self.assertEqual(self.r.hlen("image_rewrite_pending"), 0)

    def test_rewrite_error_falls_back_to_original_prompt(self):
        # If the chat worker errors out on the rewrite job (model
        # crash, etc.), the image must still go through — with the
        # original prompt — so the user isn't stranded.
        _, t = self._make_member(email="rwerror@x.com")
        worker_id = self._make_chat_worker(t)
        cid = self._seed_conversation_with_chat_turn(t)
        self.client.post(
            "/generate",
            json={
                "prompt": "wrapped in bacon",
                "tool": "image",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        rewrite_env = json.loads(self.r.lpop("job_queue"))
        rewrite_job_id = rewrite_env["job_id"]
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": rewrite_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id, "job_id": rewrite_job_id,
                "text": "", "model": "llama3.2:1b",
                "prompt_tokens": 0, "completion_tokens": 0,
                "duration_seconds": 0.05, "status": "error",
                "error": "model crashed",
                "claim_token": claim.json()["claim_token"],
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        # Image got dispatched with the ORIGINAL prompt (fallback).
        self.assertEqual(self.r.llen("job_queue:image"), 1)
        image_env = json.loads(self.r.lpop("job_queue:image"))
        self.assertEqual(image_env["prompt"], "wrapped in bacon")
        self.assertEqual(self.r.hlen("image_rewrite_pending"), 0)

    def test_cancel_during_awaiting_rewrite_prevents_dispatch(self):
        # User cancels the image while the rewrite is still in
        # flight. When the rewrite later completes, the dispatcher
        # must notice the image is no longer awaiting_rewrite and
        # NOT push it onto the image queue.
        _, t = self._make_member(email="rwcancel@x.com")
        worker_id = self._make_chat_worker(t)
        cid = self._seed_conversation_with_chat_turn(t)
        image_job_id = self.client.post(
            "/generate",
            json={
                "prompt": "wrapped in bacon",
                "tool": "image",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        ).json()["job_id"]
        rewrite_env = json.loads(self.r.lpop("job_queue"))
        rewrite_job_id = rewrite_env["job_id"]
        # User cancels.
        cancel = self.client.post(
            "/jobs/cancel", json={"job_id": image_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        # Rewrite chat worker eventually returns.
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "job_id": rewrite_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": worker_id, "job_id": rewrite_job_id,
                "text": "corn wrapped in bacon",
                "model": "llama3.2:1b",
                "prompt_tokens": 50, "completion_tokens": 5,
                "duration_seconds": 1.0, "status": "complete",
                "claim_token": claim.json()["claim_token"],
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        # Image was NOT pushed onto the image queue — the dispatcher
        # saw the cancelled status and bailed.
        self.assertEqual(self.r.llen("job_queue:image"), 0)
        # Link cleaned up.
        self.assertEqual(self.r.hlen("image_rewrite_pending"), 0)
        # Image stays cancelled.
        self.assertEqual(self.db.get_job(image_job_id)["status"], "cancelled")


class RewriteHelperUnitTests(_BaseE2E):
    """Unit-level tests for the rewrite helpers, in isolation from the
    /generate + /jobs/complete plumbing. Lives alongside the
    integration tests because the helpers live in coordinator.main."""

    def test_clean_preserves_multi_sentence_output(self):
        # sd.cpp does fine with multi-sentence prompts; the cleaner
        # used to truncate to the first line and threw away the rich
        # description the intent-aware meta-prompt now asks for.
        clean = self.coord_main._clean_rewritten_prompt
        raw = (
            "A standard-sized can of corn with a brown and tan label.\n"
            "The label reads Green Giant in bold lettering.\n"
            "Sitting upright on a store shelf."
        )
        out = clean(raw, fallback="orig")
        self.assertIn("brown and tan label", out)
        self.assertIn("Green Giant", out)
        self.assertIn("store shelf", out)

    def test_clean_strips_echoed_headers(self):
        clean = self.coord_main._clean_rewritten_prompt
        for header in ("PROMPT:", "FINAL PROMPT:", "Image:", "Rewritten:"):
            out = clean(f"{header} a red apple", fallback="orig")
            self.assertEqual(out, "a red apple", f"header={header!r}")

    def test_clean_strips_wrapping_quotes(self):
        clean = self.coord_main._clean_rewritten_prompt
        self.assertEqual(
            clean('"a red apple"', fallback="orig"), "a red apple",
        )

    def test_clean_falls_back_on_empty_or_oversized(self):
        clean = self.coord_main._clean_rewritten_prompt
        self.assertEqual(clean("", "orig"), "orig")
        self.assertEqual(clean(None, "orig"), "orig")
        # Over 500 chars → fall back rather than ship a runaway
        # ramble to sd.cpp.
        self.assertEqual(clean("x" * 600, "orig"), "orig")

    def test_meta_prompt_carries_intent_rules(self):
        # The meta-prompt has to teach a 1-3B model how to interpret
        # follow-ups; the worked examples are the load-bearing part.
        # Smoke-check that the four intent rules survive future edits.
        build = self.coord_main._build_rewrite_meta_prompt
        prompt = build("User: hi\nAssistant: hi", "yes")
        lower = prompt.lower()
        self.assertIn("yes", lower)         # rule 1: agreement
        self.assertIn("make it bigger", lower)  # rule 2: modification
        self.assertIn("from scratch", lower)    # rule 3: new subject
        self.assertIn("fragment", lower)        # rule 4: combine fragment
        self.assertIn("user: hi", lower)        # history is inlined
        self.assertIn("new user message: yes", lower)  # new request is inlined


class SearchToolTests(_BaseE2E):
    """v1.1.27: tool=search routes to a dedicated job_queue:search,
    requires a search-capable worker, accepts a search_mode knob, and
    surfaces a sources[] list on /result for the bubble-footer renderer."""

    def setUp(self):
        # Drop any residual queue state between tests so the assertions
        # on queue length aren't polluted by leftovers from other classes
        # in this file.
        self.r.delete("job_queue", "job_queue:image", "job_queue:search")

    def _make_search_worker(self, token: str) -> str:
        worker_id = f"wkr-srch-{uuid.uuid4().hex[:6]}"
        # Search-capable worker advertises tools=["chat","search"] so
        # _ensure_live_worker_or_503(tool="search") finds it. The
        # heartbeat is what counts; without it the worker is considered
        # offline by the live-worker gate.
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"tools": ["chat", "search"]},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.client.post(
            "/heartbeat", json={"worker_id": worker_id, "status": "idle"},
            headers={"Authorization": f"Bearer {token}"},
        )
        return worker_id

    def test_search_routes_to_search_queue(self):
        _, t = self._make_member(email="srch1@x.com")
        self._make_search_worker(t)
        gr = self.client.post(
            "/generate",
            json={"prompt": "what is svb", "tool": "search"},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        # Job landed on the search queue, not chat or image.
        self.assertEqual(self.r.llen("job_queue:search"), 1)
        self.assertEqual(self.r.llen("job_queue"), 0)
        self.assertEqual(self.r.llen("job_queue:image"), 0)
        # Envelope carries the search.mode knob so the worker can
        # branch fast vs comprehensive without making a second
        # request.
        env = json.loads(self.r.lindex("job_queue:search", 0))
        self.assertEqual(env["tool"], "search")
        self.assertEqual(env["search"]["mode"], "fast")

    def test_search_mode_comprehensive_carried_in_envelope(self):
        _, t = self._make_member(email="srch2@x.com")
        self._make_search_worker(t)
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "x", "tool": "search",
                "search_mode": "comprehensive",
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        env = json.loads(self.r.lindex("job_queue:search", 0))
        self.assertEqual(env["search"]["mode"], "comprehensive")

    def test_search_mode_invalid_rejected_400(self):
        _, t = self._make_member(email="srch3@x.com")
        self._make_search_worker(t)
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "x", "tool": "search",
                "search_mode": "exhaustive",
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 400)
        # Nothing enqueued — a malformed request must leave the queue
        # untouched (no orphan job).
        self.assertEqual(self.r.llen("job_queue:search"), 0)

    def test_search_with_conversation_goes_through_rewrite(self):
        # Search inside a conversation with prior context AND a chat
        # worker online → hidden chat rewrite job lands on the chat
        # queue, search job is held in 'awaiting_rewrite' status. The
        # search envelope stashed in SEARCH_REWRITE_PENDING still
        # carries the conversation history so the worker has context
        # for the LLM summary once it actually runs.
        _, t = self._make_member(email="srch4@x.com")
        self._make_search_worker(t)
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        # Seed one completed chat turn so prior history exists.
        self.db.append_message(
            message_id="msg_seed1", conversation_id=cid, seq=0,
            role="user", text="tell me about banking",
        )
        self.db.append_message(
            message_id="msg_seed2", conversation_id=cid, seq=1,
            role="assistant", text="...", status="complete",
        )
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "what about in Europe?",
                "tool": "search", "search_mode": "fast",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        # The search envelope is NOT on the search queue yet — it's
        # held pending until the rewrite chat job completes.
        self.assertEqual(self.r.llen("job_queue:search"), 0)
        # A rewrite chat job IS on the chat queue.
        self.assertEqual(self.r.llen("job_queue"), 1)
        rewrite_env = json.loads(self.r.lindex("job_queue", 0))
        self.assertEqual(rewrite_env["tool"], "chat")
        # The rewrite meta-prompt threads the history + new message
        # so the rewriter can read intent ("what about in Europe?"
        # referencing the prior banking context).
        self.assertIn("banking", rewrite_env["prompt"])
        self.assertIn("what about in Europe?", rewrite_env["prompt"])
        # Search job row is in the holding state.
        self.assertEqual(
            self.db.get_job(gr.json()["job_id"])["status"], "awaiting_rewrite",
        )
        # Linkage hash is populated; the stashed envelope retains
        # the conversation messages[] so when the rewrite returns
        # and the search envelope gets pushed to job_queue:search,
        # the worker still has context for the LLM summary.
        self.assertEqual(self.r.hlen("search_rewrite_pending"), 1)
        link_raw = list(self.r.hgetall("search_rewrite_pending").values())[0]
        link = json.loads(link_raw)
        env = link["search_envelope"]
        self.assertIn("messages", env)
        self.assertEqual(
            env["messages"][-1]["content"], "what about in Europe?",
        )
        self.assertTrue(
            any("banking" in m.get("content", "") for m in env["messages"]),
            f"prior history not threaded into stashed envelope: {env['messages']!r}",
        )

    def test_complete_with_sources_surfaces_on_result(self):
        # End-to-end: enqueue → claim → complete with sources →
        # /result/{job_id} returns the sources list verbatim for the
        # client to render under the bubble.
        _, t = self._make_member(email="srch5@x.com")
        self._make_search_worker(t)
        gr = self.client.post(
            "/generate",
            json={"prompt": "what is svb", "tool": "search"},
            headers={"Authorization": f"Bearer {t}"},
        )
        job_id = gr.json()["job_id"]
        # Pop off the queue to simulate the worker BLPOP and claim.
        self.r.lpop("job_queue:search")
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "wkr-srch-test", "job_id": job_id},
            headers=self._admin_headers(),
        )
        sources_payload = [
            {"n": 1, "title": "SVB explained",
             "url": "https://example.com/svb",
             "domain": "example.com"},
            {"n": 2, "title": "Bank collapse",
             "url": "https://news.test/bank",
             "domain": "news.test"},
        ]
        comp = self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wkr-srch-test", "job_id": job_id,
                "text": "SVB collapsed in March 2023 [1][2].",
                "model": "llama3.2:1b",
                "prompt_tokens": 5, "completion_tokens": 10,
                "duration_seconds": 1.2, "status": "complete",
                "sources": sources_payload,
                "claim_token": claim.json()["claim_token"],
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(comp.status_code, 200, comp.text)
        res = self.client.get(
            f"/result/{job_id}",
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        self.assertEqual(res["status"], "complete")
        self.assertEqual(res["sources"], sources_payload)


class SearchQueryRewriteTests(_BaseE2E):
    """v1.1.28: when a search submission lives inside a conversation
    that already has prior turns, /generate enqueues a hidden chat
    job that rewrites the literal DDG query using context, then
    dispatches the search job with the rewritten query.

    Same skip-paths as the image rewrite. The motivating user
    transcript was 'news' → Wikipedia hit, then 'try again' → Aaliyah
    song lyrics — both unrecoverable as literal queries; the rewrite
    re-frames 'try again' as 'different recent news event' using the
    conversation history."""

    def _make_search_capable_worker(self, token: str) -> str:
        # Worker advertises both chat (to serve the rewrite job) and
        # search (to satisfy the live-worker gate for /generate). The
        # rewrite + dispatch flow needs both online; without the chat
        # capability, /generate's `_chat_worker_available_for_rewrite`
        # would skip the rewrite and push the literal query straight
        # to the search queue.
        worker_id = f"wkr-sr-{uuid.uuid4().hex[:6]}"
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"tools": ["chat", "search"]},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.client.post(
            "/heartbeat", json={"worker_id": worker_id, "status": "idle"},
            headers={"Authorization": f"Bearer {token}"},
        )
        return worker_id

    def setUp(self):
        # Each test in this class clears just the queues + linkage
        # hash it asserts on. Mirror of ImagePromptRewriteTests.setUp.
        self.r.delete("job_queue", "job_queue:search", "search_rewrite_pending")

    def _seed_conversation_with_one_turn(self, t: str) -> str:
        # Build a conversation that has at least one completed turn,
        # so the "first message in this conv" skip doesn't apply.
        # message_ids are randomized per call so multiple tests in
        # this class don't trip the messages.message_id UNIQUE
        # constraint sharing the same _BaseE2E DB.
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        suffix = uuid.uuid4().hex[:8]
        self.db.append_message(
            message_id=f"msg_sseed_{suffix}_u", conversation_id=cid, seq=0,
            role="user", text="tell me the latest news",
        )
        self.db.append_message(
            message_id=f"msg_sseed_{suffix}_a", conversation_id=cid, seq=1,
            role="assistant", text="News A, News B, News C.",
            status="complete",
        )
        return cid

    def test_search_first_message_skips_rewrite(self):
        # First message in a brand-new conversation IS a search → no
        # prior context to disambiguate against → push the literal
        # query straight to the search queue. Same skip-path as the
        # image rewrite's first-message case.
        _, t = self._make_member(email="srrw1@x.com")
        self._make_search_capable_worker(t)
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "news", "tool": "search",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        # Search landed on the search queue with the raw prompt.
        self.assertEqual(self.r.llen("job_queue:search"), 1)
        self.assertEqual(self.r.hlen("search_rewrite_pending"), 0)
        env = json.loads(self.r.lindex("job_queue:search", 0))
        self.assertEqual(env["prompt"], "news")
        # Job row is normal 'pending', not 'awaiting_rewrite'.
        self.assertEqual(self.db.get_job(gr.json()["job_id"])["status"], "pending")

    def test_search_without_conversation_id_skips_rewrite(self):
        # No conversation_id → no history → no rewrite. Same skip-path
        # as image rewrite's "no conversation_id" branch.
        _, t = self._make_member(email="srrw2@x.com")
        self._make_search_capable_worker(t)
        gr = self.client.post(
            "/generate",
            json={"prompt": "news", "tool": "search"},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        self.assertEqual(self.r.llen("job_queue:search"), 1)
        self.assertEqual(self.r.hlen("search_rewrite_pending"), 0)

    def test_search_with_no_chat_worker_online_skips_rewrite(self):
        # Conversation has prior context but NO chat-capable worker is
        # heartbeating — fall back to the raw query rather than
        # stranding the search behind a rewrite no one can complete.
        # Mirror of the image-rewrite skip-path for this case: register
        # a worker (so /generate has *something* in the registry), then
        # blow away the heartbeats hash so
        # _chat_worker_available_for_rewrite() sees nothing.
        _, t = self._make_member(email="srrw3@x.com")
        self._make_search_capable_worker(t)
        cid = self._seed_conversation_with_one_turn(t)
        # Same trick as ImagePromptRewriteTests uses: drop heartbeats
        # so the rewrite-availability check (which DOES inspect
        # heartbeats directly, regardless of REQUIRE_LIVE_WORKER) sees
        # no recent chat worker. Other tests in this class set up their
        # own heartbeats fresh; this isolates the skip-path.
        self.r.delete("worker_heartbeats")
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "try again", "tool": "search",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        # Skipped rewrite → raw query straight to the search queue.
        self.assertEqual(self.r.llen("job_queue:search"), 1)
        self.assertEqual(self.r.hlen("search_rewrite_pending"), 0)
        env = json.loads(self.r.lindex("job_queue:search", 0))
        self.assertEqual(env["prompt"], "try again")

    def test_search_with_context_enqueues_rewrite(self):
        # Happy path: conversation has prior context AND a chat-
        # capable worker is online → rewrite chat job is enqueued
        # on the CHAT queue, search job is held in 'awaiting_rewrite'
        # until the rewrite returns.
        _, t = self._make_member(email="srrw4@x.com")
        self._make_search_capable_worker(t)
        cid = self._seed_conversation_with_one_turn(t)
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "try again", "tool": "search",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(gr.status_code, 200, gr.text)
        # Search queue empty (held back for the rewrite).
        self.assertEqual(self.r.llen("job_queue:search"), 0)
        # Rewrite chat job on the chat queue.
        self.assertEqual(self.r.llen("job_queue"), 1)
        rewrite_env = json.loads(self.r.lindex("job_queue", 0))
        self.assertEqual(rewrite_env["tool"], "chat")
        # The meta-prompt embeds the user's literal "try again" plus
        # the prior assistant message so the rewriter can disambiguate.
        self.assertIn("try again", rewrite_env["prompt"])
        self.assertIn("News A", rewrite_env["prompt"])
        # Search job row is held.
        self.assertEqual(
            self.db.get_job(gr.json()["job_id"])["status"], "awaiting_rewrite",
        )
        # Linkage hash populated.
        self.assertEqual(self.r.hlen("search_rewrite_pending"), 1)

    def test_rewrite_complete_dispatches_search_with_new_query(self):
        # End-to-end: rewrite returns a cleaner query, the dispatcher
        # plugs it into the held search envelope, pushes onto the
        # search queue, and the search job row flips from
        # awaiting_rewrite → pending so the worker picks it up.
        _, t = self._make_member(email="srrw5@x.com")
        wid = self._make_search_capable_worker(t)
        cid = self._seed_conversation_with_one_turn(t)
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "try again", "tool": "search",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        search_job_id = gr.json()["job_id"]
        # Pop the rewrite chat job off (mimic worker BLPOP), claim,
        # complete with the "rewritten" query.
        self.r.lpop("job_queue")
        rewrite_links = self.r.hgetall("search_rewrite_pending")
        rewrite_job_id = list(rewrite_links.keys())[0]
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": wid, "job_id": rewrite_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": wid, "job_id": rewrite_job_id,
                "text": "different recent news event",
                "model": "llama3.2:1b",
                "prompt_tokens": 50, "completion_tokens": 5,
                "duration_seconds": 1.0, "status": "complete",
                "claim_token": claim.json()["claim_token"],
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        # Search envelope now on the search queue with the rewritten
        # query as its prompt.
        self.assertEqual(self.r.llen("job_queue:search"), 1)
        search_env = json.loads(self.r.lindex("job_queue:search", 0))
        self.assertEqual(search_env["prompt"], "different recent news event")
        # Conversation history is still present in the dispatched
        # envelope so the LLM summary step has context.
        self.assertIn("messages", search_env)
        # Search job row flipped out of awaiting_rewrite.
        self.assertEqual(
            self.db.get_job(search_job_id)["status"], "pending",
        )
        # Linkage cleaned up.
        self.assertEqual(self.r.hlen("search_rewrite_pending"), 0)

    def test_rewrite_error_falls_back_to_raw_query(self):
        # If the rewrite chat job errors out, the dispatcher's
        # _clean_rewritten_search_query falls back to the original
        # prompt — better to ship a literal DDG query than no query
        # at all.
        _, t = self._make_member(email="srrw6@x.com")
        wid = self._make_search_capable_worker(t)
        cid = self._seed_conversation_with_one_turn(t)
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "try again", "tool": "search",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        search_job_id = gr.json()["job_id"]
        self.r.lpop("job_queue")
        rewrite_job_id = list(self.r.hgetall("search_rewrite_pending").keys())[0]
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": wid, "job_id": rewrite_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": wid, "job_id": rewrite_job_id,
                "text": "",
                "model": "llama3.2:1b",
                "prompt_tokens": 50, "completion_tokens": 0,
                "duration_seconds": 0.5, "status": "error",
                "error": "model died",
                "claim_token": claim.json()["claim_token"],
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        # Search dispatched with the ORIGINAL query.
        self.assertEqual(self.r.llen("job_queue:search"), 1)
        env = json.loads(self.r.lindex("job_queue:search", 0))
        self.assertEqual(env["prompt"], "try again")
        self.assertEqual(self.db.get_job(search_job_id)["status"], "pending")
        self.assertEqual(self.r.hlen("search_rewrite_pending"), 0)

    def test_cancel_search_during_rewrite_skips_dispatch(self):
        # User cancels the search after the rewrite job is in flight
        # but before it returns. When the rewrite eventually finishes,
        # the dispatcher notices the search row is no longer in
        # awaiting_rewrite and bails — no orphan search lands on the
        # queue.
        _, t = self._make_member(email="srrw7@x.com")
        wid = self._make_search_capable_worker(t)
        cid = self._seed_conversation_with_one_turn(t)
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "try again", "tool": "search",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        search_job_id = gr.json()["job_id"]
        # Get the rewrite envelope (it's still on the chat queue).
        self.r.lpop("job_queue")
        rewrite_job_id = list(self.r.hgetall("search_rewrite_pending").keys())[0]
        # User cancels the search.
        cancel = self.client.post(
            "/jobs/cancel", json={"job_id": search_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        # Rewrite chat job returns.
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": wid, "job_id": rewrite_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": wid, "job_id": rewrite_job_id,
                "text": "different recent news event",
                "model": "llama3.2:1b",
                "prompt_tokens": 50, "completion_tokens": 5,
                "duration_seconds": 1.0, "status": "complete",
                "claim_token": claim.json()["claim_token"],
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        # No search on the queue (dispatcher saw cancelled status, bailed).
        self.assertEqual(self.r.llen("job_queue:search"), 0)
        self.assertEqual(self.r.hlen("search_rewrite_pending"), 0)
        self.assertEqual(self.db.get_job(search_job_id)["status"], "cancelled")


class SearchQueryCleanerUnitTests(_BaseE2E):
    """Unit-level coverage for _clean_rewritten_search_query in
    isolation. The cleaner is stricter than the image-prompt cleaner
    (search queries are short by nature; multi-line is wrong)."""

    def test_strips_quotes_and_headers(self):
        clean = self.coord_main._clean_rewritten_search_query
        self.assertEqual(clean('"recent news today"', "orig"), "recent news today")
        self.assertEqual(clean("QUERY: SVB collapse 2023", "orig"), "SVB collapse 2023")
        self.assertEqual(clean("Search: latest iPhone", "orig"), "latest iPhone")

    def test_takes_first_line_only(self):
        # The image cleaner preserves multi-line because sd.cpp likes
        # rich prompts; DDG does NOT — extra words are hard
        # requirements. Multi-line model output is the rewriter
        # rambling; take the first line and drop the rest.
        clean = self.coord_main._clean_rewritten_search_query
        raw = (
            "recent news today\n"
            "Also you might want to search for...\n"
            "Or another option is..."
        )
        self.assertEqual(clean(raw, "orig"), "recent news today")

    def test_falls_back_on_empty_or_oversized(self):
        clean = self.coord_main._clean_rewritten_search_query
        self.assertEqual(clean("", "orig"), "orig")
        self.assertEqual(clean(None, "orig"), "orig")
        # 200-char cap — anything longer is the model writing prose.
        self.assertEqual(clean("x" * 250, "orig"), "orig")


class SearchRewriteParserTests(_BaseE2E):
    """v1.1.28 reverse-detection: _parse_search_rewrite_output decides
    whether the rewrite chat job output means 'no search needed' or
    'here's a search query'. The classifier is the load-bearing piece
    of the reverse-detection feature — if it misfires, the user
    either keeps getting irrelevant searches (false negative) or
    loses search mode prematurely (false positive)."""

    def test_no_search_sentinel_classifies_as_skip(self):
        parse = self.coord_main._parse_search_rewrite_output
        self.assertEqual(parse("NO_SEARCH")[0], "skip")
        self.assertEqual(parse(" no_search ")[0], "skip")
        self.assertEqual(parse("NoSearch")[0], "skip")

    def test_echoed_header_then_no_search_still_skips(self):
        # A model that helpfully echoes "OUTPUT: NO_SEARCH" must still
        # classify — the parser strips the header before checking.
        parse = self.coord_main._parse_search_rewrite_output
        self.assertEqual(parse("OUTPUT: NO_SEARCH")[0], "skip")
        self.assertEqual(parse("Output:\nNO_SEARCH")[0], "skip")

    def test_no_search_with_trailing_commentary_still_skips(self):
        # First-line containment is enough — the rewriter sometimes
        # adds a trailing reason which we discard.
        parse = self.coord_main._parse_search_rewrite_output
        self.assertEqual(
            parse("NO_SEARCH (user is just thanking)")[0], "skip",
        )

    def test_a_real_query_classifies_as_query(self):
        parse = self.coord_main._parse_search_rewrite_output
        kind, val = parse("recent banking news Europe")
        self.assertEqual(kind, "query")
        self.assertEqual(val, "recent banking news Europe")

    def test_empty_or_none_classifies_as_error(self):
        # The dispatcher uses 'error' to fall back to the original
        # prompt, never to skip — better to ship a literal search
        # than silently drop the user's request.
        parse = self.coord_main._parse_search_rewrite_output
        self.assertEqual(parse("")[0], "error")
        self.assertEqual(parse(None)[0], "error")


class SearchRewriteSkipDispatchTests(_BaseE2E):
    """v1.1.28 reverse-detection: when the rewrite chat job emits
    NO_SEARCH, the dispatcher reroutes the original search job to the
    chat queue (not the search queue) and sets the SEARCH_AUTO_DISABLED
    marker so /jobs/complete can signal the client to flip off the
    sticky search box.

    Motivating user transcript: search mode on, user says 'That's
    cool!' after a Boring-Company search thread. Current behavior
    fires another DDG query for 'That's cool!' and returns Aaliyah-
    song-tier results. Reverse detection drops the search, returns a
    plain chat reply, and turns the box off for the next turn."""

    def _make_search_capable_worker(self, token: str) -> str:
        worker_id = f"wkr-sk-{uuid.uuid4().hex[:6]}"
        self.client.post(
            "/register",
            json={
                "worker_id": worker_id,
                "capabilities": {"tools": ["chat", "search"]},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.client.post(
            "/heartbeat", json={"worker_id": worker_id, "status": "idle"},
            headers={"Authorization": f"Bearer {token}"},
        )
        return worker_id

    def setUp(self):
        self.r.delete(
            "job_queue", "job_queue:search",
            "search_rewrite_pending", "search_auto_disabled",
        )

    def _seed_conversation_with_one_turn(self, t: str) -> str:
        conv = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        cid = conv["conversation_id"]
        suffix = uuid.uuid4().hex[:8]
        self.db.append_message(
            message_id=f"msg_skipseed_{suffix}_u", conversation_id=cid, seq=0,
            role="user", text="news on the boring company",
        )
        self.db.append_message(
            message_id=f"msg_skipseed_{suffix}_a", conversation_id=cid, seq=1,
            role="assistant",
            text="The Boring Company is...",
            status="complete",
        )
        return cid

    def test_no_search_reroutes_to_chat_queue(self):
        _, t = self._make_member(email="srskip1@x.com")
        wid = self._make_search_capable_worker(t)
        cid = self._seed_conversation_with_one_turn(t)
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "That's cool!", "tool": "search",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        search_job_id = gr.json()["job_id"]
        # Pop the rewrite chat job, then complete it with NO_SEARCH.
        self.r.lpop("job_queue")
        rewrite_job_id = list(
            self.r.hgetall("search_rewrite_pending").keys()
        )[0]
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": wid, "job_id": rewrite_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": wid, "job_id": rewrite_job_id,
                "text": "NO_SEARCH",
                "model": "llama3.2:1b",
                "prompt_tokens": 50, "completion_tokens": 2,
                "duration_seconds": 0.5, "status": "complete",
                "claim_token": claim.json()["claim_token"],
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        # Search queue empty — we DROPPED the search entirely.
        self.assertEqual(self.r.llen("job_queue:search"), 0)
        # Chat queue has the rerouted job.
        self.assertEqual(self.r.llen("job_queue"), 1)
        rerouted = json.loads(self.r.lindex("job_queue", 0))
        self.assertEqual(rerouted["tool"], "chat")
        self.assertEqual(rerouted["prompt"], "That's cool!")
        # No search-specific fields leaked into the chat envelope.
        self.assertNotIn("search", rerouted)
        # Marker present so /jobs/complete can signal the client.
        self.assertEqual(
            self.r.hget("search_auto_disabled", search_job_id), "1",
        )
        # Search job row flipped out of awaiting_rewrite so /result
        # doesn't get stuck waiting on a search that's never coming.
        self.assertEqual(
            self.db.get_job(search_job_id)["status"], "pending",
        )
        # Linkage cleaned up.
        self.assertEqual(self.r.hlen("search_rewrite_pending"), 0)

    def test_search_was_skipped_surfaces_on_complete(self):
        # End-to-end: NO_SEARCH → chat worker completes → /result
        # returns search_was_skipped: true so the client knows to flip
        # the sticky toggle off.
        _, t = self._make_member(email="srskip2@x.com")
        wid = self._make_search_capable_worker(t)
        cid = self._seed_conversation_with_one_turn(t)
        gr = self.client.post(
            "/generate",
            json={
                "prompt": "Thanks!", "tool": "search",
                "conversation_id": cid,
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        search_job_id = gr.json()["job_id"]
        # Rewrite returns NO_SEARCH → chat envelope on chat queue.
        self.r.lpop("job_queue")
        rewrite_job_id = list(
            self.r.hgetall("search_rewrite_pending").keys()
        )[0]
        claim_r = self.client.post(
            "/jobs/claim",
            json={"worker_id": wid, "job_id": rewrite_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": wid, "job_id": rewrite_job_id,
                "text": "NO_SEARCH",
                "model": "llama3.2:1b",
                "prompt_tokens": 50, "completion_tokens": 2,
                "duration_seconds": 0.5, "status": "complete",
                "claim_token": claim_r.json()["claim_token"],
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        # Now the chat worker picks up the rerouted job.
        self.r.lpop("job_queue")
        claim_c = self.client.post(
            "/jobs/claim",
            json={"worker_id": wid, "job_id": search_job_id},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": wid, "job_id": search_job_id,
                "text": "You're welcome!",
                "model": "llama3.2:1b",
                "prompt_tokens": 50, "completion_tokens": 3,
                "duration_seconds": 0.5, "status": "complete",
                "claim_token": claim_c.json()["claim_token"],
            },
            headers={"Authorization": f"Bearer {t}"},
        )
        # /result carries the flag for the client.
        res = self.client.get(
            f"/result/{search_job_id}",
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        self.assertTrue(res.get("search_was_skipped"))
        # Marker hash cleaned up after surfacing — second poll of the
        # same job ID shouldn't keep re-flipping the client's checkbox.
        self.assertEqual(self.r.hlen("search_auto_disabled"), 0)


if __name__ == "__main__":
    unittest.main()
