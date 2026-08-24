"""Tests for document upload (coordinator/uploads.py): text extraction,
the <<document>> context fence builder, the /uploads router, and its
wiring into /generate + /messages/{id}/retry.

Run with ``python -m unittest tests.test_uploads``.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import unittest
import uuid


# ---------- pure unit tests: extraction + fence building ----------
# No coordinator app/DB needed here — import the module directly.
from coordinator import uploads as uploads_mod  # noqa: E402


def _pdf_bytes(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()


def _docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _csv_bytes(rows: list[list[str]], header: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


class ExtractTextTests(unittest.TestCase):
    def test_extract_txt(self):
        text, truncated = uploads_mod.extract_text(
            "notes.txt", "hello world".encode("utf-8"),
        )
        self.assertEqual(text, "hello world")
        self.assertFalse(truncated)

    def test_extract_md(self):
        text, truncated = uploads_mod.extract_text(
            "notes.md", "# Title\n\nbody text".encode("utf-8"),
        )
        self.assertIn("Title", text)
        self.assertFalse(truncated)

    def test_extract_pdf_recovers_real_text(self):
        data = _pdf_bytes("The quick brown fox jumps over the lazy dog")
        text, truncated = uploads_mod.extract_text("report.pdf", data)
        self.assertIn("quick brown fox", text)
        self.assertFalse(truncated)

    def test_extract_docx_recovers_paragraphs(self):
        data = _docx_bytes(["First paragraph.", "Second paragraph."])
        text, truncated = uploads_mod.extract_text("memo.docx", data)
        self.assertIn("First paragraph.", text)
        self.assertIn("Second paragraph.", text)
        self.assertFalse(truncated)

    def test_extract_csv_describes_shape_and_preview(self):
        data = _csv_bytes(
            [["1", "alice", "42"], ["2", "bob", "17"]],
            header=["id", "name", "age"],
        )
        text, truncated = uploads_mod.extract_text("people.csv", data)
        self.assertIn("2 rows", text)
        self.assertIn("id", text)
        self.assertIn("name", text)
        self.assertIn("alice", text)
        self.assertFalse(truncated)

    def test_extract_unsupported_extension_raises(self):
        with self.assertRaises(ValueError):
            uploads_mod.extract_text("payload.exe", b"MZ\x90\x00")

    def test_extract_no_extension_raises(self):
        with self.assertRaises(ValueError):
            uploads_mod.extract_text("noext", b"hello")

    def test_extract_empty_result_raises(self):
        with self.assertRaises(ValueError):
            uploads_mod.extract_text("empty.txt", b"   \n  ")

    def test_extract_truncates_over_the_per_file_cap(self):
        old = uploads_mod.MAX_UPLOAD_EXTRACTED_CHARS
        uploads_mod.MAX_UPLOAD_EXTRACTED_CHARS = 50
        try:
            text, truncated = uploads_mod.extract_text(
                "big.txt", ("x" * 500).encode("utf-8"),
            )
            self.assertEqual(len(text), 50)
            self.assertTrue(truncated)
        finally:
            uploads_mod.MAX_UPLOAD_EXTRACTED_CHARS = old


class BuildDocumentContextTests(unittest.TestCase):
    def _row(self, filename: str, text: str, truncated: bool = False) -> dict:
        return {
            "filename": filename,
            "extracted_text": text,
            "truncated": truncated,
        }

    def test_empty_list_returns_none(self):
        self.assertIsNone(uploads_mod.build_document_context([]))

    def test_single_upload_fenced_with_filename(self):
        ctx = uploads_mod.build_document_context(
            [self._row("report.pdf", "the contents")],
        )
        self.assertIn('filename="report.pdf"', ctx)
        self.assertIn("the contents", ctx)
        self.assertIn("<<document", ctx)
        self.assertIn("<</document>>", ctx)

    def test_chronological_order_oldest_first_in_output(self):
        rows = [self._row("first.txt", "AAA"), self._row("second.txt", "BBB")]
        ctx = uploads_mod.build_document_context(rows)
        self.assertLess(ctx.index("AAA"), ctx.index("BBB"))

    def test_combined_budget_omits_oldest_not_newest(self):
        old = uploads_mod.MAX_UPLOAD_CONTEXT_CHARS
        uploads_mod.MAX_UPLOAD_CONTEXT_CHARS = 10
        try:
            rows = [
                self._row("old.txt", "x" * 20),
                self._row("new.txt", "y" * 20),
            ]
            ctx = uploads_mod.build_document_context(rows)
            # Newest upload wins the limited budget.
            self.assertIn("new.txt", ctx)
            self.assertIn("y" * 10, ctx)
            # Oldest was pushed out entirely and shows up as an
            # omission marker, not as raw content.
            self.assertNotIn("x" * 5, ctx)
            self.assertIn("omitted", ctx)
        finally:
            uploads_mod.MAX_UPLOAD_CONTEXT_CHARS = old

    def test_filename_quote_is_neutralized(self):
        ctx = uploads_mod.build_document_context(
            [self._row('evil".txt', "content")],
        )
        # A double-quote in the filename can't break out of the
        # filename="..." attribute in the fence header.
        self.assertNotIn('filename="evil".txt"', ctx)


# ---------- router + /generate integration tests ----------

class _BaseE2E(unittest.TestCase):
    """Boot a coordinator instance with auth ON, clean SQLite, fake
    Redis. Mirrors tests/test_conversations.py's _BaseE2E."""

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

    def _make_conversation(self, bearer: str) -> str:
        r = self.client.post(
            "/conversations", json={},
            headers={"Authorization": f"Bearer {bearer}"},
        )
        return r.json()["conversation_id"]


class UploadRouterTests(_BaseE2E):
    def test_upload_txt_success(self):
        _, t = self._make_member(email="u1@x.com")
        cid = self._make_conversation(t)
        r = self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("notes.txt", b"hello from a test file", "text/plain")},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["upload_id"].startswith("up_"))
        self.assertEqual(body["filename"], "notes.txt")
        self.assertEqual(body["char_count"], len("hello from a test file"))
        self.assertFalse(body["truncated"])

    def test_upload_requires_auth(self):
        _, t = self._make_member(email="u2@x.com")
        cid = self._make_conversation(t)
        r = self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("notes.txt", b"hi", "text/plain")},
        )
        self.assertEqual(r.status_code, 401)

    def test_upload_rejects_wrong_owner(self):
        _, alice = self._make_member(email="u3a@x.com")
        _, bob = self._make_member(email="u3b@x.com")
        cid = self._make_conversation(alice)
        r = self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("notes.txt", b"hi", "text/plain")},
            headers={"Authorization": f"Bearer {bob}"},
        )
        self.assertEqual(r.status_code, 404)

    def test_upload_rejects_missing_conversation(self):
        _, t = self._make_member(email="u4@x.com")
        r = self.client.post(
            "/uploads",
            data={"conversation_id": "conv_does_not_exist"},
            files={"file": ("notes.txt", b"hi", "text/plain")},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 404)

    def test_upload_rejects_oversized_file(self):
        old = self.coord_main.uploads_lib.MAX_UPLOAD_BYTES
        self.coord_main.uploads_lib.MAX_UPLOAD_BYTES = 10
        try:
            _, t = self._make_member(email="u5@x.com")
            cid = self._make_conversation(t)
            r = self.client.post(
                "/uploads",
                data={"conversation_id": cid},
                files={"file": ("notes.txt", b"this is way more than 10 bytes", "text/plain")},
                headers={"Authorization": f"Bearer {t}"},
            )
            self.assertEqual(r.status_code, 413)
        finally:
            self.coord_main.uploads_lib.MAX_UPLOAD_BYTES = old

    def test_upload_rejects_unsupported_extension(self):
        _, t = self._make_member(email="u6@x.com")
        cid = self._make_conversation(t)
        r = self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 400)

    def test_upload_rejects_empty_file(self):
        _, t = self._make_member(email="u7@x.com")
        cid = self._make_conversation(t)
        r = self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("notes.txt", b"", "text/plain")},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 400)

    def test_list_uploads_for_conversation(self):
        _, t = self._make_member(email="u8@x.com")
        cid = self._make_conversation(t)
        self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("a.txt", b"aaa", "text/plain")},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("b.txt", b"bbb", "text/plain")},
            headers={"Authorization": f"Bearer {t}"},
        )
        r = self.client.get(
            f"/uploads/{cid}", headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        names = [u["filename"] for u in r.json()["uploads"]]
        self.assertEqual(names, ["a.txt", "b.txt"])

    def test_list_uploads_requires_auth(self):
        _, t = self._make_member(email="u9@x.com")
        cid = self._make_conversation(t)
        r = self.client.get(f"/uploads/{cid}")
        self.assertEqual(r.status_code, 401)


class GenerateWithUploadsTests(_BaseE2E):
    def setUp(self):
        self.r.delete("job_queue")

    def test_document_context_folded_into_chat_job(self):
        _, t = self._make_member(email="g1@x.com")
        cid = self._make_conversation(t)
        self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("report.txt", b"the secret ingredient is basil", "text/plain")},
            headers={"Authorization": f"Bearer {t}"},
        )
        r = self.client.post(
            "/generate",
            json={"prompt": "summarize the attached file", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        envelope = json.loads(self.r.lpop("job_queue"))
        messages = envelope["messages"]
        doc_messages = [
            m for m in messages
            if m["role"] == "system" and "basil" in m["content"]
        ]
        self.assertEqual(len(doc_messages), 1)
        self.assertIn('filename="report.txt"', doc_messages[0]["content"])
        # The last message is still the plain user turn — the document
        # doesn't leak into what's rendered as the user's own text.
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "summarize the attached file")

    def test_no_uploads_means_no_document_system_message(self):
        _, t = self._make_member(email="g2@x.com")
        cid = self._make_conversation(t)
        r = self.client.post(
            "/generate",
            json={"prompt": "hello", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        envelope = json.loads(self.r.lpop("job_queue"))
        for m in envelope["messages"]:
            self.assertNotIn("<<document", m["content"])

    def test_document_context_persists_across_turns(self):
        _, t = self._make_member(email="g3@x.com")
        cid = self._make_conversation(t)
        self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("policy.txt", b"vacation days accrue monthly", "text/plain")},
            headers={"Authorization": f"Bearer {t}"},
        )
        first = self.client.post(
            "/generate",
            json={"prompt": "first question", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        self.r.lpop("job_queue")
        self._complete_job(first["job_id"])

        r2 = self.client.post(
            "/generate",
            json={"prompt": "second question, no new upload", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        envelope = json.loads(self.r.lpop("job_queue"))
        doc_messages = [
            m for m in envelope["messages"]
            if m["role"] == "system" and "vacation days" in m["content"]
        ]
        self.assertEqual(len(doc_messages), 1)

    def _complete_job(self, job_id: str):
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "wupl", "job_id": job_id},
            headers=self._admin_headers(),
        )
        claim_token = claim.json()["claim_token"]
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wupl", "job_id": job_id, "text": "an answer",
                "model": "llama3.2:1b", "prompt_tokens": 5,
                "completion_tokens": 5, "duration_seconds": 0.05,
                "status": "complete", "claim_token": claim_token,
            },
            headers=self._admin_headers(),
        )


class RetryWithUploadsTests(_BaseE2E):
    def setUp(self):
        self.r.delete("job_queue")

    def _setup_failed_message(self, email: str) -> tuple[str, str, str]:
        _, t = self._make_member(email=email)
        cid = self._make_conversation(t)
        self.client.post(
            "/uploads",
            data={"conversation_id": cid},
            files={"file": ("spec.txt", b"widgets ship on tuesdays", "text/plain")},
            headers={"Authorization": f"Bearer {t}"},
        )
        gen = self.client.post(
            "/generate",
            json={"prompt": "ask me", "conversation_id": cid},
            headers={"Authorization": f"Bearer {t}"},
        ).json()
        self.r.lpop("job_queue")
        job_id = gen["job_id"]
        message_id = gen["assistant_message_id"]
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "wretryu", "job_id": job_id},
            headers=self._admin_headers(),
        )
        claim_token = claim.json()["claim_token"]
        self.client.post(
            "/jobs/complete",
            json={
                "worker_id": "wretryu", "job_id": job_id, "text": "",
                "model": "llama3.2:1b", "prompt_tokens": 0,
                "completion_tokens": 0, "duration_seconds": 0.05,
                "status": "error", "error": "boom",
                "claim_token": claim_token,
            },
            headers=self._admin_headers(),
        )
        return t, cid, message_id

    def test_retry_still_carries_document_context(self):
        t, _cid, message_id = self._setup_failed_message("retu1@x.com")
        r = self.client.post(
            f"/messages/{message_id}/retry",
            headers={"Authorization": f"Bearer {t}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        envelope = json.loads(self.r.lpop("job_queue"))
        doc_messages = [
            m for m in envelope["messages"]
            if m["role"] == "system" and "widgets ship on tuesdays" in m["content"]
        ]
        self.assertEqual(len(doc_messages), 1)


if __name__ == "__main__":
    unittest.main()
