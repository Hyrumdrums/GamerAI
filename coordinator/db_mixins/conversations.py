"""``DB`` conversation / message methods — coordinator/db.py god-file
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


class ConversationsMixin:
    # ---------- conversations ----------
    def create_conversation(
        self,
        conversation_id: str,
        owner_member_id: Optional[str],
        title: Optional[str],
        model: Optional[str],
        created_at: Optional[float] = None,
    ) -> None:
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations "
                "(conversation_id, owner_member_id, title, model, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, owner_member_id, title, model, now, now),
            )

    def get_conversation(self, conversation_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            )
            return cur.fetchone()

    def list_unowned_conversations(
        self, include_archived: bool = False,
    ) -> list[sqlite3.Row]:
        """Conversations created without an owner_member_id — i.e. in
        auth-off dev mode. Excluded from list_conversations_for_member
        because that filters on a specific owner."""
        with self._lock:
            if include_archived:
                cur = self._conn.execute(
                    "SELECT * FROM conversations WHERE owner_member_id IS NULL "
                    "ORDER BY updated_at DESC",
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM conversations WHERE owner_member_id IS NULL "
                    "AND archived_at IS NULL "
                    "ORDER BY updated_at DESC",
                )
            return cur.fetchall()

    def list_conversations_for_member(
        self, owner_member_id: str, include_archived: bool = False,
    ) -> list[sqlite3.Row]:
        with self._lock:
            if include_archived:
                cur = self._conn.execute(
                    "SELECT * FROM conversations WHERE owner_member_id=? "
                    "ORDER BY updated_at DESC",
                    (owner_member_id,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM conversations WHERE owner_member_id=? "
                    "AND archived_at IS NULL "
                    "ORDER BY updated_at DESC",
                    (owner_member_id,),
                )
            return cur.fetchall()

    def archive_conversation(
        self, conversation_id: str, archived_at: float,
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversations SET archived_at=? "
                "WHERE conversation_id=? AND archived_at IS NULL",
                (archived_at, conversation_id),
            )
            return cur.rowcount > 0

    def purge_conversation(
        self, conversation_id: str,
    ) -> tuple[list[str], list[str]]:
        """Hard-delete a conversation and everything anchored to it.
        Returns ``(image_basenames, job_ids)`` so the caller can clean
        up the matching filesystem (IMAGE_DIR PNG files) and Redis
        entries (JOB_RESULTS / JOB_PROCESSING / JOB_PARTIALS) — those
        live outside the DB and the DB can't reach them directly.

        Idempotent: a missing conversation_id just returns empty
        lists. SQL deletions happen in one transaction so a partial
        failure can't leave a half-purged row set behind."""
        with self._lock:
            image_rows = self._conn.execute(
                "SELECT image_path FROM messages "
                "WHERE conversation_id=? AND image_path IS NOT NULL",
                (conversation_id,),
            ).fetchall()
            image_basenames = [r["image_path"] for r in image_rows if r["image_path"]]
            # Both jobs reachable via messages.job_id AND jobs.conversation_id —
            # the latter catches pending jobs not yet wired to a message
            # row (the brief window between /generate enqueue and the
            # message-insert).
            job_rows = self._conn.execute(
                "SELECT job_id FROM messages "
                "WHERE conversation_id=? AND job_id IS NOT NULL",
                (conversation_id,),
            ).fetchall()
            job_ids = {r["job_id"] for r in job_rows if r["job_id"]}
            extra_rows = self._conn.execute(
                "SELECT job_id FROM jobs WHERE conversation_id=?",
                (conversation_id,),
            ).fetchall()
            job_ids.update(r["job_id"] for r in extra_rows if r["job_id"])

            try:
                self._conn.execute("BEGIN")
                self._conn.execute(
                    "DELETE FROM messages WHERE conversation_id=?",
                    (conversation_id,),
                )
                self._conn.execute(
                    "DELETE FROM jobs WHERE conversation_id=?",
                    (conversation_id,),
                )
                self._conn.execute(
                    "DELETE FROM uploads WHERE conversation_id=?",
                    (conversation_id,),
                )
                self._conn.execute(
                    "DELETE FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return image_basenames, sorted(job_ids)

    def touch_conversation(self, conversation_id: str, when: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                (when, conversation_id),
            )

    def set_conversation_title(
        self, conversation_id: str, title: str,
    ) -> None:
        """Used when the first user prompt is the natural title — set
        only if the conversation has no title yet."""
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET title=? "
                "WHERE conversation_id=? AND (title IS NULL OR title='')",
                (title, conversation_id),
            )

    def set_conversation_summary(
        self, conversation_id: str, summary_text: str, through_seq: int,
    ) -> None:
        """Persist a recap of every turn up through through_seq.
        _build_chat_messages_with_info prepends this as a system
        message and excludes any persisted turn at seq <= through_seq,
        keeping prefill cost bounded on a long thread."""
        with self._lock:
            self._conn.execute(
                "UPDATE conversations "
                "SET summary_text=?, summary_through_seq=? "
                "WHERE conversation_id=?",
                (summary_text, int(through_seq), conversation_id),
            )

    # ---------- messages ----------
    def list_messages(self, conversation_id: str) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM messages WHERE conversation_id=? "
                "ORDER BY seq ASC",
                (conversation_id,),
            )
            return cur.fetchall()

    def next_message_seq(self, conversation_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next "
                "FROM messages WHERE conversation_id=?",
                (conversation_id,),
            )
            row = cur.fetchone()
            return int(row["next"])

    def append_message(
        self,
        message_id: str,
        conversation_id: str,
        seq: int,
        role: str,
        text: str,
        job_id: Optional[str] = None,
        model: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        created_at: Optional[float] = None,
        status: str = "complete",
    ) -> None:
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages "
                "(message_id, conversation_id, seq, role, text, status, "
                "job_id, model, prompt_tokens, completion_tokens, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id, conversation_id, seq, role, text, status,
                    job_id, model, prompt_tokens, completion_tokens, now,
                ),
            )

    def update_message_partial(self, message_id: str, text: str) -> None:
        """Overwrite a pending message's text with the latest accumulated
        stream. Only touches rows still in status='pending' so a late
        partial from an abandoned worker can't clobber a row that already
        moved to 'complete' or 'error'."""
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET text=? "
                "WHERE message_id=? AND status='pending'",
                (text, message_id),
            )

    def finalize_message(
        self,
        message_id: str,
        text: str,
        status: str,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        model: Optional[str] = None,
        image_path: Optional[str] = None,
    ) -> None:
        """Move a pending message to its terminal state ('complete' or
        'error'). Status guard prevents double-finalization if both a
        retry and the original worker race.

        ``image_path`` is set for image-tool jobs; the UI reads it to
        decide whether to render an <img> bubble vs. text. Chat jobs
        leave it NULL."""
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET text=?, status=?, "
                "prompt_tokens=COALESCE(?, prompt_tokens), "
                "completion_tokens=COALESCE(?, completion_tokens), "
                "model=COALESCE(?, model), "
                "image_path=COALESCE(?, image_path) "
                "WHERE message_id=? AND status='pending'",
                (
                    text, status, prompt_tokens, completion_tokens,
                    model, image_path, message_id,
                ),
            )

    def get_message(self, message_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM messages WHERE message_id=?", (message_id,),
            )
            return cur.fetchone()

    def reset_message_for_retry(
        self,
        message_id: str,
        new_job_id: str,
    ) -> bool:
        """Move a status='error' assistant message back to 'pending' so
        a fresh worker job can stream into it. Only succeeds when the
        row is currently 'error' — if the user has already kicked off
        a successful retry from another tab, this is a no-op."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET text='', status='pending', "
                "job_id=?, prompt_tokens=NULL, completion_tokens=NULL "
                "WHERE message_id=? AND status='error'",
                (new_job_id, message_id),
            )
            return cur.rowcount > 0

    def get_message_by_job(self, job_id: str) -> Optional[sqlite3.Row]:
        """Look up the assistant message produced by a given job. Returns
        None if no message row references this job_id (e.g. legacy jobs
        that pre-date enqueue-time persistence)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM messages WHERE job_id=? AND role='assistant' "
                "ORDER BY seq DESC LIMIT 1",
                (job_id,),
            )
            return cur.fetchone()
