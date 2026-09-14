"""``DB.insert_upload``/``list_uploads`` — coordinator/db.py god-file
split. Mixin composition (not delegation): every method keeps
referencing ``self._conn``/``self._lock`` exactly as before, since
``self`` still resolves to the composed ``DB`` instance via MRO — a
pure cut/paste, no call-site changes needed anywhere else in the
coordinator. See coordinator/db.py for the full ``class DB(...)``
composition.
"""
import sqlite3
import time
from typing import Optional


class UploadsMixin:
    # ---------- uploads ----------
    def insert_upload(
        self,
        upload_id: str,
        conversation_id: str,
        member_id: Optional[str],
        filename: str,
        content_type: Optional[str],
        extracted_text: str,
        truncated: bool,
        created_at: Optional[float] = None,
    ) -> None:
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO uploads "
                "(upload_id, conversation_id, member_id, filename, "
                "content_type, extracted_text, char_count, truncated, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    upload_id, conversation_id, member_id, filename,
                    content_type, extracted_text, len(extracted_text),
                    1 if truncated else 0, now,
                ),
            )

    def list_uploads(self, conversation_id: str) -> list[sqlite3.Row]:
        """Oldest-first — callers that want a recency-first read (e.g.
        the fence-budget builder) reverse this themselves."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM uploads WHERE conversation_id=? "
                "ORDER BY created_at ASC",
                (conversation_id,),
            )
            return cur.fetchall()
