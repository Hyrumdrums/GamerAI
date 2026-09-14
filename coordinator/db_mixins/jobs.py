"""``DB`` job methods — coordinator/db.py god-file split. Mixin
composition (not delegation): every method keeps referencing
``self._conn``/``self._lock`` exactly as before, since ``self`` still
resolves to the composed ``DB`` instance via MRO — a pure cut/paste, no
call-site changes needed anywhere else in the coordinator. See
coordinator/db.py for the full ``class DB(...)`` composition.
"""
import sqlite3
from typing import Optional


class JobsMixin:
    # ---------- jobs ----------
    def insert_job(
        self,
        job_id: str,
        prompt: str,
        model: Optional[str],
        submitted_at: float,
        submitted_by_member_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        tool: str = "chat",
        status: str = "pending",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(job_id, prompt, model, status, submitted_at, attempts, "
                "submitted_by_member_id, conversation_id, tool) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    job_id, prompt, model, status, submitted_at,
                    submitted_by_member_id, conversation_id, tool,
                ),
            )

    def set_job_pending_with_prompt(self, job_id: str, prompt: str) -> None:
        """Used by the image-prompt rewrite pipeline: when the rewrite
        chat job completes, overwrite the awaiting-rewrite image job's
        prompt with the rewritten text and flip its status to 'pending'
        so it shows up as a normal queued job to /result and the reaper.
        Idempotent: a missing job_id just no-ops."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET prompt=?, status='pending' WHERE job_id=?",
                (prompt, job_id),
            )

    def mark_job_running(self, job_id: str, worker_id: str, started_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='running', worker_id=?, started_at=?, "
                "attempts = attempts + 1 WHERE job_id=?",
                (worker_id, started_at, job_id),
            )

    def mark_job_first_partial(self, job_id: str, when: float) -> None:
        """Stamp the first-streaming-partial timestamp. WHERE clause
        guards on NULL so subsequent partials (one every few hundred ms
        during streaming) don't overwrite the first-token moment."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET first_partial_at=? "
                "WHERE job_id=? AND first_partial_at IS NULL",
                (when, job_id),
            )

    def mark_job_displayed(self, job_id: str, when: float) -> None:
        """Stamp the client-rendered-the-final-text timestamp.
        Idempotent on NULL so a duplicate POST (offline-queue replay,
        accidental double-fire) doesn't move the first-display moment."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET client_displayed_at=? "
                "WHERE job_id=? AND client_displayed_at IS NULL",
                (when, job_id),
            )

    def mark_job_complete(
        self,
        job_id: str,
        worker_id: str,
        model: str,
        text: str,
        prompt_tokens: int,
        completion_tokens: int,
        earnings: float,
        duration_seconds: float,
        completed_at: float,
        status: str = "complete",
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, worker_id=?, model=?, result=?, "
                "prompt_tokens=?, completion_tokens=?, earnings=?, "
                "duration_seconds=?, completed_at=?, error=? WHERE job_id=?",
                (
                    status,
                    worker_id,
                    model,
                    text,
                    prompt_tokens,
                    completion_tokens,
                    earnings,
                    duration_seconds,
                    completed_at,
                    error,
                    job_id,
                ),
            )

    def requeue_job(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='pending', worker_id=NULL, started_at=NULL "
                "WHERE job_id=?",
                (job_id,),
            )

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
            return cur.fetchone()

    def list_jobs(
        self,
        worker_id: Optional[str] = None,
        submitted_by_member_id: Optional[str] = None,
        status: Optional[str] = None,
        tool: Optional[str] = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        """Most-recent-first job history for the admin job-listing page.
        Every filter is optional and AND-ed together — the page uses
        this to drive both the unfiltered feed and the per-worker/
        per-member views linked from the dashboard's All Workers
        table."""
        clauses = []
        params: list = []
        if worker_id:
            clauses.append("worker_id=?")
            params.append(worker_id)
        if submitted_by_member_id:
            clauses.append("submitted_by_member_id=?")
            params.append(submitted_by_member_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if tool:
            clauses.append("tool=?")
            params.append(tool)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY submitted_at DESC LIMIT ?",
                params,
            )
            return cur.fetchall()
