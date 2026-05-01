"""SQLite write-through store. Redis remains the queue; SQLite is the system of record."""
import os
import sqlite3
import threading
import time
from typing import Optional

from shared.config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    worker_id TEXT,
    model TEXT,
    result TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    earnings REAL,
    submitted_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    duration_seconds REAL,
    error TEXT,
    attempts INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_worker ON jobs(worker_id);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    status TEXT,
    last_seen REAL,
    registered_at REAL
);

CREATE TABLE IF NOT EXISTS earnings (
    worker_id TEXT PRIMARY KEY,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_jobs INTEGER NOT NULL DEFAULT 0,
    total_usd REAL NOT NULL DEFAULT 0,
    updated_at REAL
);
"""


class DB:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    # ---------- jobs ----------
    def insert_job(self, job_id: str, prompt: str, model: Optional[str], submitted_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs (job_id, prompt, model, status, submitted_at, attempts) "
                "VALUES (?, ?, ?, 'pending', ?, 0)",
                (job_id, prompt, model, submitted_at),
            )

    def mark_job_running(self, job_id: str, worker_id: str, started_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='running', worker_id=?, started_at=?, "
                "attempts = attempts + 1 WHERE job_id=?",
                (worker_id, started_at, job_id),
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

    # ---------- workers ----------
    def upsert_worker(self, worker_id: str, status: str, last_seen: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO workers (worker_id, status, last_seen, registered_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(worker_id) DO UPDATE SET status=excluded.status, "
                "last_seen=excluded.last_seen",
                (worker_id, status, last_seen, last_seen),
            )

    def list_workers(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM workers ORDER BY worker_id")
            return cur.fetchall()

    # ---------- earnings ----------
    def add_earnings(self, worker_id: str, tokens: int, usd: float) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO earnings (worker_id, total_tokens, total_jobs, total_usd, updated_at) "
                "VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(worker_id) DO UPDATE SET "
                "total_tokens = total_tokens + excluded.total_tokens, "
                "total_jobs   = total_jobs   + 1, "
                "total_usd    = total_usd    + excluded.total_usd, "
                "updated_at   = excluded.updated_at",
                (worker_id, tokens, usd, now),
            )

    def list_earnings(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM earnings ORDER BY total_usd DESC")
            return cur.fetchall()

    def earnings_for(self, worker_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM earnings WHERE worker_id=?", (worker_id,))
            return cur.fetchone()

    # ---------- metrics ----------
    def metrics(self) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS completed, "
                "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed, "
                "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending, "
                "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running, "
                "AVG(CASE WHEN status='complete' THEN duration_seconds END) AS avg_latency, "
                "COALESCE(SUM(completion_tokens), 0) AS tokens, "
                "COALESCE(SUM(earnings), 0) AS paid "
                "FROM jobs"
            )
            row = cur.fetchone()
        return {
            "total_jobs": row["total"] or 0,
            "completed_jobs": row["completed"] or 0,
            "failed_jobs": row["failed"] or 0,
            "pending_jobs": row["pending"] or 0,
            "running_jobs": row["running"] or 0,
            "avg_latency_seconds": round(row["avg_latency"], 4) if row["avg_latency"] else 0.0,
            "tokens_processed": int(row["tokens"] or 0),
            "total_paid_usd": round(float(row["paid"] or 0), 8),
        }
