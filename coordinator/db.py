"""SQLite write-through store. Redis remains the queue; SQLite is the system of record."""
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
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
    attempts INTEGER DEFAULT 0,
    submitted_by_member_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_worker ON jobs(worker_id);
-- idx_jobs_submitter is created in _migrate() after the
-- submitted_by_member_id column is added, so legacy DBs that pre-date
-- that column don't fail to start.

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

-- Per-member identity. token_hash is sha256(raw_token); raw token is
-- never stored. Admin members are seeded from the API_TOKEN env var
-- on coordinator startup (see coordinator/main.py:ensure_admin_seed).
CREATE TABLE IF NOT EXISTS members (
    member_id TEXT PRIMARY KEY,
    email TEXT,
    role TEXT NOT NULL,
    parent_member_id TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL DEFAULT 'BRONZE',
    daily_quota_tokens INTEGER,
    revoked_at REAL,
    created_at REAL NOT NULL,
    last_active_at REAL
);

CREATE INDEX IF NOT EXISTS idx_members_token ON members(token_hash);
CREATE INDEX IF NOT EXISTS idx_members_parent ON members(parent_member_id);

-- Per-day consumption rollup, updated on /jobs/complete by submitter.
-- Used for invitee quota enforcement (see /generate in main.py).
CREATE TABLE IF NOT EXISTS member_usage (
    member_id TEXT NOT NULL,
    day TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    jobs INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (member_id, day)
);

-- One row per outstanding or historical invite. The ``code`` is the
-- redemption secret (carried in the invite URL). ``accepted_at`` and
-- ``accepted_by_member_id`` are set atomically with the member-row
-- insert when the invite is redeemed.
CREATE TABLE IF NOT EXISTS invites (
    invite_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    contributor_member_id TEXT NOT NULL,
    invitee_email TEXT,
    daily_quota_tokens INTEGER,
    expires_at REAL,
    accepted_at REAL,
    accepted_by_member_id TEXT,
    revoked_at REAL,
    notes TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invites_code ON invites(code);
CREATE INDEX IF NOT EXISTS idx_invites_contributor ON invites(contributor_member_id);

-- Canary prompts. The coordinator periodically injects one of these
-- into the queue (looking identical to a real prompt from the
-- worker's perspective) and verifies the worker's response contains
-- the required_tokens. Used to detect contributors who have swapped
-- in a different model or are tampering with outputs.
--
-- required_tokens is a JSON list of substrings; the response must
-- contain ALL of them (case-insensitive) to pass. Pick prompts with
-- stable factual answers and minimal phrasing variance.
CREATE TABLE IF NOT EXISTS canaries (
    canary_id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    required_tokens TEXT NOT NULL,  -- JSON-encoded list of strings
    model TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

-- One row per completed canary check. ``matched`` is the verdict;
-- response_text_snippet stores the first ~500 chars of the worker's
-- response for forensics. ``worker_id`` may be NULL if no worker
-- claimed the canary before it timed out.
CREATE TABLE IF NOT EXISTS canary_results (
    result_id TEXT PRIMARY KEY,
    canary_id TEXT NOT NULL,
    worker_id TEXT,
    job_id TEXT NOT NULL,
    response_text_snippet TEXT,
    matched INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_canary_results_worker ON canary_results(worker_id);
CREATE INDEX IF NOT EXISTS idx_canary_results_canary ON canary_results(canary_id);

-- Multi-turn conversations. Each conversation is owned by a single
-- member; messages stack in order via `seq`. The `model` column on
-- the conversation pins the default model so a multi-turn thread
-- doesn't drift when the user doesn't specify one per turn.
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    owner_member_id TEXT,
    title TEXT,
    model TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL
);

CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner_member_id);

-- One row per turn. `role` is 'user' or 'assistant'. Assistant rows
-- link back to the jobs row that produced them via `job_id` so an
-- admin debugging a bad answer can see worker_id / duration / etc.
-- `status` is the lifecycle state — 'pending' while a worker is
-- streaming partial tokens, 'complete' once /jobs/complete lands,
-- 'error' if the job failed or timed out. The text column is the
-- accumulated partial during streaming and the final answer once
-- complete; for status='error' it holds a short user-facing reason.
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'complete',
    job_id TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, seq);
"""


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


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
            self._migrate()

    def _migrate(self) -> None:
        """Best-effort additive column adds for DBs created before the
        member-identity columns existed. Safe to call on every startup —
        each ADD COLUMN is a no-op if the column is already present."""
        try:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN submitted_by_member_id TEXT"
            )
        except sqlite3.OperationalError:
            pass
        # Index on the migration-added column. Created here (not in
        # _SCHEMA) so a legacy DB whose jobs table predates the column
        # finishes the ALTER before SQLite parses the CREATE INDEX.
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_submitter "
                "ON jobs(submitted_by_member_id)"
            )
        except sqlite3.OperationalError:
            pass
        # ToS acceptance — added with the first community-trust slice.
        for col, ddl in (
            ("tos_accepted_at", "ALTER TABLE members ADD COLUMN tos_accepted_at REAL"),
            ("tos_version", "ALTER TABLE members ADD COLUMN tos_version TEXT"),
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # Multi-turn conversations — added with the conversations slice.
        # When set on a job row, /jobs/complete will append the result
        # to the named conversation.
        try:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN conversation_id TEXT"
            )
        except sqlite3.OperationalError:
            pass
        # Worker→member binding — added with the 2026-05-13 security
        # slice. Without it any authenticated member can /jobs/complete
        # with any worker_id (fraudulent earnings + ability to inject
        # bogus responses into other members' prompts).
        try:
            self._conn.execute(
                "ALTER TABLE workers ADD COLUMN owner_member_id TEXT"
            )
        except sqlite3.OperationalError:
            pass
        # Streaming lifecycle — added when assistant messages started
        # getting persisted at enqueue time and updated incrementally
        # as the worker streams tokens. Backfills existing rows to
        # 'complete' since pre-streaming all persisted messages were
        # final.
        try:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN status TEXT "
                "NOT NULL DEFAULT 'complete'"
            )
        except sqlite3.OperationalError:
            pass
        # Multi-tool columns — added with the image-generation slice.
        # jobs.tool discriminates chat vs. image so /jobs/next can route
        # by queue and /jobs/complete knows whether to expect image_b64.
        # Legacy rows default to 'chat' (the only thing the pre-image
        # coordinator could produce). messages.image_path stores the
        # filesystem path the UI fetches the generated PNG from.
        for ddl in (
            "ALTER TABLE jobs ADD COLUMN tool TEXT NOT NULL DEFAULT 'chat'",
            "ALTER TABLE messages ADD COLUMN image_path TEXT",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

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
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(job_id, prompt, model, status, submitted_at, attempts, "
                "submitted_by_member_id, conversation_id, tool) "
                "VALUES (?, ?, ?, 'pending', ?, 0, ?, ?, ?)",
                (
                    job_id, prompt, model, submitted_at,
                    submitted_by_member_id, conversation_id, tool,
                ),
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
        """Legacy upsert — preserves the pre-ownership signature for
        callers that don't have a member context (heartbeat path,
        in-process tests, etc.). Does NOT touch owner_member_id, so
        ownership state is preserved across status / heartbeat
        updates."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO workers (worker_id, status, last_seen, registered_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(worker_id) DO UPDATE SET status=excluded.status, "
                "last_seen=excluded.last_seen",
                (worker_id, status, last_seen, last_seen),
            )

    def claim_worker_ownership(
        self,
        worker_id: str,
        member_id: Optional[str],
        status: str,
        last_seen: float,
    ) -> tuple[bool, Optional[str]]:
        """Atomic ownership claim for /register. Returns
        (ok, current_owner).

        - If the worker_id is new, inserts with member_id as owner.
        - If the worker_id exists with owner_member_id NULL (legacy),
          the calling member adopts ownership.
        - If the worker_id exists with the same owner, refresh status.
        - If owned by a different member, returns (False, existing_owner)
          so the caller can 403.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT owner_member_id FROM workers WHERE worker_id=?",
                    (worker_id,),
                )
                row = cur.fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO workers "
                        "(worker_id, status, last_seen, registered_at, owner_member_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (worker_id, status, last_seen, last_seen, member_id),
                    )
                    self._conn.execute("COMMIT")
                    return True, member_id
                existing = row["owner_member_id"]
                if (
                    existing is not None
                    and member_id is not None
                    and existing != member_id
                ):
                    self._conn.execute("ROLLBACK")
                    return False, existing
                # Same owner OR adopting a legacy unowned worker.
                self._conn.execute(
                    "UPDATE workers SET status=?, last_seen=?, "
                    "owner_member_id=COALESCE(owner_member_id, ?) "
                    "WHERE worker_id=?",
                    (status, last_seen, member_id, worker_id),
                )
                self._conn.execute("COMMIT")
                return True, member_id if existing is None else existing
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def worker_owner(self, worker_id: str) -> Optional[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT owner_member_id FROM workers WHERE worker_id=?",
                (worker_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if "owner_member_id" not in row.keys():
                return None  # legacy DB before migration
            return row["owner_member_id"]

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

    # ---------- members ----------
    def create_member(
        self,
        member_id: str,
        email: Optional[str],
        role: str,
        parent_member_id: Optional[str],
        token_hash: str,
        tier: str = "BRONZE",
        daily_quota_tokens: Optional[int] = None,
        created_at: Optional[float] = None,
        tos_accepted_at: Optional[float] = None,
        tos_version: Optional[str] = None,
    ) -> None:
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO members (member_id, email, role, parent_member_id, "
                "token_hash, tier, daily_quota_tokens, created_at, "
                "tos_accepted_at, tos_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    member_id,
                    email,
                    role,
                    parent_member_id,
                    token_hash,
                    tier,
                    daily_quota_tokens,
                    now,
                    tos_accepted_at,
                    tos_version,
                ),
            )

    def get_member_by_token_hash(self, token_hash: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM members WHERE token_hash=?", (token_hash,)
            )
            return cur.fetchone()

    def get_member(self, member_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM members WHERE member_id=?", (member_id,)
            )
            return cur.fetchone()

    def list_members(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM members ORDER BY created_at ASC"
            )
            return cur.fetchall()

    def revoke_member_by_token_hash(self, token_hash: str, revoked_at: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE members SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (revoked_at, token_hash),
            )
            return cur.rowcount > 0

    def touch_member(self, member_id: str, when: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE members SET last_active_at=? WHERE member_id=?",
                (when, member_id),
            )

    # ---------- member usage ----------
    def add_member_usage(
        self,
        member_id: str,
        when: float,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        day = _utc_day(when)
        with self._lock:
            self._conn.execute(
                "INSERT INTO member_usage (member_id, day, tokens_in, tokens_out, jobs) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(member_id, day) DO UPDATE SET "
                "tokens_in  = tokens_in  + excluded.tokens_in, "
                "tokens_out = tokens_out + excluded.tokens_out, "
                "jobs       = jobs       + 1",
                (member_id, day, tokens_in, tokens_out),
            )

    def member_usage_today(self, member_id: str, now: Optional[float] = None) -> dict:
        when = now if now is not None else time.time()
        day = _utc_day(when)
        with self._lock:
            cur = self._conn.execute(
                "SELECT tokens_in, tokens_out, jobs FROM member_usage "
                "WHERE member_id=? AND day=?",
                (member_id, day),
            )
            row = cur.fetchone()
        if row is None:
            return {"day": day, "tokens_in": 0, "tokens_out": 0, "jobs": 0}
        return {
            "day": day,
            "tokens_in": int(row["tokens_in"]),
            "tokens_out": int(row["tokens_out"]),
            "jobs": int(row["jobs"]),
        }

    # ---------- invites ----------
    def create_invite(
        self,
        invite_id: str,
        code: str,
        contributor_member_id: str,
        daily_quota_tokens: Optional[int],
        invitee_email: Optional[str] = None,
        expires_at: Optional[float] = None,
        notes: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> None:
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO invites (invite_id, code, contributor_member_id, "
                "invitee_email, daily_quota_tokens, expires_at, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invite_id,
                    code,
                    contributor_member_id,
                    invitee_email,
                    daily_quota_tokens,
                    expires_at,
                    notes,
                    now,
                ),
            )

    def get_invite_by_code(self, code: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM invites WHERE code=?", (code,)
            )
            return cur.fetchone()

    def list_invites_by_contributor(self, contributor_member_id: str) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM invites WHERE contributor_member_id=? "
                "ORDER BY created_at DESC",
                (contributor_member_id,),
            )
            return cur.fetchall()

    def list_all_invites(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM invites ORDER BY created_at DESC"
            )
            return cur.fetchall()

    def revoke_invite_by_code(self, code: str, revoked_at: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE invites SET revoked_at=? "
                "WHERE code=? AND revoked_at IS NULL AND accepted_at IS NULL",
                (revoked_at, code),
            )
            return cur.rowcount > 0

    def accept_invite_atomic(
        self,
        code: str,
        new_member_id: str,
        new_token_hash: str,
        invitee_email: Optional[str],
        accepted_at: float,
        tos_version: Optional[str] = None,
    ) -> Optional[sqlite3.Row]:
        """Atomic: verify invite is redeemable, mark accepted, insert the
        new invitee member. Returns the (updated) invite row on success,
        or None if the invite was missing / expired / already accepted /
        revoked. All writes happen under the same BEGIN/COMMIT so a
        second concurrent accept of the same code cannot succeed.

        ``tos_version`` is recorded on the new member row so we can
        track which terms version each invitee accepted. ``None`` is
        permitted for backwards compatibility but the caller should
        always pass the current version when invoked via the public
        redemption flow."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT * FROM invites WHERE code=?", (code,)
                )
                row = cur.fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return None
                if row["accepted_at"] is not None:
                    self._conn.execute("ROLLBACK")
                    return None
                if row["revoked_at"] is not None:
                    self._conn.execute("ROLLBACK")
                    return None
                if row["expires_at"] is not None and row["expires_at"] < accepted_at:
                    self._conn.execute("ROLLBACK")
                    return None

                self._conn.execute(
                    "INSERT INTO members (member_id, email, role, parent_member_id, "
                    "token_hash, tier, daily_quota_tokens, created_at, "
                    "tos_accepted_at, tos_version) "
                    "VALUES (?, ?, 'invitee', ?, ?, 'BRONZE', ?, ?, ?, ?)",
                    (
                        new_member_id,
                        invitee_email,
                        row["contributor_member_id"],
                        new_token_hash,
                        row["daily_quota_tokens"],
                        accepted_at,
                        accepted_at,  # tos_accepted_at — checkbox was required at submit
                        tos_version,
                    ),
                )
                self._conn.execute(
                    "UPDATE invites SET accepted_at=?, accepted_by_member_id=?, "
                    "invitee_email=COALESCE(?, invitee_email) "
                    "WHERE code=?",
                    (accepted_at, new_member_id, invitee_email, code),
                )
                cur = self._conn.execute(
                    "SELECT * FROM invites WHERE code=?", (code,)
                )
                fresh = cur.fetchone()
                self._conn.execute("COMMIT")
                return fresh
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

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
