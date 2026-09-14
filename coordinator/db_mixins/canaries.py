"""``DB`` canary-injection methods — coordinator/db.py god-file split.
Mixin composition (not delegation): every method keeps referencing
``self._conn``/``self._lock`` exactly as before, since ``self`` still
resolves to the composed ``DB`` instance via MRO — a pure cut/paste, no
call-site changes needed anywhere else in the coordinator. See
coordinator/db.py for the full ``class DB(...)`` composition.
"""
import sqlite3
import time
from typing import Optional


class CanariesMixin:
    # ---------- canaries ----------
    def create_canary(
        self,
        canary_id: str,
        prompt: str,
        required_tokens_json: str,
        model: str,
        active: bool = True,
        created_at: Optional[float] = None,
    ) -> None:
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO canaries "
                "(canary_id, prompt, required_tokens, model, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (canary_id, prompt, required_tokens_json, model, 1 if active else 0, now),
            )

    def list_active_canaries(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM canaries WHERE active=1 ORDER BY created_at"
            )
            return cur.fetchall()

    def get_canary(self, canary_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM canaries WHERE canary_id=?", (canary_id,)
            )
            return cur.fetchone()

    def record_canary_result(
        self,
        result_id: str,
        canary_id: str,
        worker_id: Optional[str],
        job_id: str,
        response_text_snippet: Optional[str],
        matched: bool,
        created_at: Optional[float] = None,
    ) -> None:
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO canary_results "
                "(result_id, canary_id, worker_id, job_id, "
                "response_text_snippet, matched, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (result_id, canary_id, worker_id, job_id,
                 response_text_snippet, 1 if matched else 0, now),
            )

    def canary_score_for_worker(
        self,
        worker_id: str,
        limit: int = 50,
    ) -> dict:
        """Per-worker canary pass rate over the last ``limit`` checks.
        Returns ``{passed, total, score}`` where score is 0.0-1.0, or
        None when the worker has no canary history yet."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT matched FROM canary_results WHERE worker_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (worker_id, limit),
            )
            rows = cur.fetchall()
        if not rows:
            return {"passed": 0, "total": 0, "score": None}
        passed = sum(1 for r in rows if r["matched"])
        total = len(rows)
        return {"passed": passed, "total": total, "score": passed / total}
