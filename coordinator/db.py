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

-- tools_json is the JSON-encoded WorkerCapabilities.tools list a worker
-- last advertised on /register (e.g. '["chat","image"]'). Persisted so
-- account-page lookups don't need a Redis round-trip and so a worker
-- still tagged as "partial" (image bootstrap failed) is visible even
-- if it's currently offline. NULL on legacy rows pre-dating the
-- 2026-05-23 image-capability slice — treated as "chat only" at read
-- time.
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    status TEXT,
    last_seen REAL,
    registered_at REAL,
    tools_json TEXT
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
--
-- daily_quota_images is a parallel cap to daily_quota_tokens, in units
-- (see shared/config.IMAGE_UNIT_COST_BASE). Two-dimensional quota
-- prevents an invitee from emptying their token budget on chat AND
-- their image budget on generation; each tool gates against its own
-- column. NULL on either column = unlimited for that dimension.
CREATE TABLE IF NOT EXISTS members (
    member_id TEXT PRIMARY KEY,
    email TEXT,
    role TEXT NOT NULL,
    parent_member_id TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL DEFAULT 'BRONZE',
    daily_quota_tokens INTEGER,
    daily_quota_images INTEGER,
    revoked_at REAL,
    created_at REAL NOT NULL,
    last_active_at REAL
);

CREATE INDEX IF NOT EXISTS idx_members_token ON members(token_hash);
CREATE INDEX IF NOT EXISTS idx_members_parent ON members(parent_member_id);

-- Per-day consumption rollup, updated on /jobs/complete by submitter.
-- Used for invitee quota enforcement (see /generate in main.py).
--
-- image_units is REAL (not INTEGER) so future per-image cost
-- multipliers (higher resolution, more steps, larger batch sizes) can
-- credit a fractional or >1 cost without a schema change. The /generate
-- image-tool quota gate compares this column against
-- members.daily_quota_images.
CREATE TABLE IF NOT EXISTS member_usage (
    member_id TEXT NOT NULL,
    day TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    image_units REAL NOT NULL DEFAULT 0,
    jobs INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (member_id, day)
);

-- One row per outstanding or historical invite. The ``code`` is the
-- redemption secret (carried in the invite URL). ``accepted_at`` and
-- ``accepted_by_member_id`` are set atomically with the member-row
-- insert when the invite is redeemed. Both daily caps are inherited
-- onto the new member's row on redemption.
CREATE TABLE IF NOT EXISTS invites (
    invite_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    contributor_member_id TEXT NOT NULL,
    invitee_email TEXT,
    daily_quota_tokens INTEGER,
    daily_quota_images INTEGER,
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

-- Per-worker per-day uptime rollup, written by the UptimeSampler every
-- 5 minutes (one row gains 5 minutes for each worker that heartbeated
-- within the sample window). Day is UTC YYYY-MM-DD so a member in
-- Pacific time and one in Sydney share the same day boundary. The
-- tier engine reads the last 7 days to decide promote/demote/grace.
CREATE TABLE IF NOT EXISTS worker_uptime (
    worker_id TEXT NOT NULL,
    day TEXT NOT NULL,
    minutes_online INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (worker_id, day)
);

CREATE INDEX IF NOT EXISTS idx_worker_uptime_day ON worker_uptime(day);

-- Web Push subscription endpoints (Phase 6 of pwa-refactor.txt). One
-- row per (member_id, device-browser). A member on phone + laptop has
-- two rows and a single send fans out across all of them. (member_id,
-- endpoint) is UNIQUE so re-subscribing from the same browser replaces
-- the prior row instead of accumulating duplicates. The endpoint is
-- the URL the browser's Push service exposes (Mozilla autopush, FCM,
-- WebPush.io, …); p256dh + auth are the per-subscription crypto bits
-- pywebpush needs to encrypt the payload.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT,
    created_at REAL NOT NULL,
    UNIQUE(member_id, endpoint)
);

CREATE INDEX IF NOT EXISTS idx_push_subscriptions_member
    ON push_subscriptions(member_id);

-- Persistent notification record. Backs the in-app notification list
-- and is the source of truth even when push delivery fails (offline
-- device, expired endpoint, user opted out of OS-level notifications,
-- etc.). ``data`` is JSON-encoded {url?, conversation_id?, job_id?, …}
-- and the notificationclick handler in sw.js reads data.url to focus
-- the right view. ``pushed`` records whether we successfully handed
-- off to at least one subscription's Push service.
CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    data TEXT,
    read INTEGER NOT NULL DEFAULT 0,
    pushed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_member_read
    ON notifications(member_id, read);
CREATE INDEX IF NOT EXISTS idx_notifications_member_created
    ON notifications(member_id, created_at DESC);

-- Per-member, per-category opt-in. Default behavior for a category
-- not in the table is enabled=TRUE; users disable categories they
-- find noisy. Categories are string constants defined in
-- coordinator/notifications.py (image_done, voice_done, system, …).
CREATE TABLE IF NOT EXISTS notification_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id TEXT NOT NULL,
    category TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(member_id, category)
);
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
        # u/p credentials — added with the username+password slice. The
        # raw bearer in ``members.token_hash`` stays as the wire-format
        # session credential; ``password_hash`` is the argon2-encoded
        # secret a member uses to mint a fresh token via /login.
        # ``username`` is UNIQUE among non-NULL values so legacy invitee
        # rows (created before this slice) can keep NULL until their
        # owner sets credentials. Partial unique index implemented as a
        # filtered CREATE UNIQUE INDEX so SQLite enforces it.
        for ddl in (
            "ALTER TABLE members ADD COLUMN username TEXT",
            "ALTER TABLE members ADD COLUMN password_hash TEXT",
            "ALTER TABLE members ADD COLUMN password_set_at REAL",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # Username + email uniqueness. Both indexes intentionally
        # *exclude* revoked rows: a revoked member shouldn't squat on
        # the username or email forever, otherwise every revoke leaks
        # an identity slot (e.g. "I revoked the test contributor I
        # made by mistake; now I can't reclaim that email" — exactly
        # the footgun the user hit on 2026-05-23). Lookups in
        # get_member_by_username / get_member_by_email and the
        # collision checks in accept_invite_atomic mirror this filter
        # so the runtime behavior matches the index semantics.
        #
        # DROP + CREATE on every startup rather than IF NOT EXISTS so
        # an upgrade from the earlier (revoked-aware-less) version of
        # this index picks up the new WHERE clause without manual
        # intervention. Both DROPs are no-ops if the index isn't there
        # yet (legacy installs without the auth slice at all).
        for ddl in (
            "DROP INDEX IF EXISTS idx_members_username",
            "DROP INDEX IF EXISTS idx_members_email",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_members_username "
            "ON members(LOWER(username)) "
            "WHERE username IS NOT NULL AND revoked_at IS NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_members_email "
            "ON members(LOWER(email)) "
            "WHERE email IS NOT NULL AND revoked_at IS NULL",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # Two-dimensional quota + image accounting — added with the
        # image-limits slice. members.daily_quota_images is the parallel
        # cap to daily_quota_tokens (NULL = unlimited). member_usage
        # gains image_units REAL so partial-cost weighting works without
        # a future ALTER. invites.daily_quota_images is inherited onto
        # the new member row on redemption.
        for ddl in (
            "ALTER TABLE members ADD COLUMN daily_quota_images INTEGER",
            "ALTER TABLE invites ADD COLUMN daily_quota_images INTEGER",
            "ALTER TABLE member_usage ADD COLUMN image_units REAL "
            "NOT NULL DEFAULT 0",
            "ALTER TABLE workers ADD COLUMN tools_json TEXT",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # Voice (TTS + future STT) accounting — added with the
        # voice-phase1 slice. Same shape as image: a daily-min cap on
        # members/invites (NULL = fall through to the tier default cap
        # at gate time — see /generate for the precedence), and a
        # voice_seconds REAL rollup on member_usage. Seconds rather than
        # minutes so sub-second sentences from client-side segmentation
        # don't round to zero; the /generate gate converts to minutes
        # to compare against the cap.
        for ddl in (
            "ALTER TABLE members ADD COLUMN daily_quota_voice_minutes INTEGER",
            "ALTER TABLE invites ADD COLUMN daily_quota_voice_minutes INTEGER",
            "ALTER TABLE member_usage ADD COLUMN voice_seconds REAL "
            "NOT NULL DEFAULT 0",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # Tier-engine state. tier_last_changed_at gates the
        # 1-move-per-day rule (engine refuses to act on a member moved
        # within the last 24h). tier_below_threshold_since is set when
        # the engine first observes a member failing their current
        # tier's bar; demotion only fires after 14 days of continuous
        # failure, and a single passing day clears it.
        for ddl in (
            "ALTER TABLE members ADD COLUMN tier_last_changed_at REAL",
            "ALTER TABLE members ADD COLUMN tier_below_threshold_since REAL",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # Per-member additional bearer tokens — added with the agent-
        # pairing slice. ``members.token_hash`` stays as the (single)
        # web-session credential rotated by /login. ``member_tokens``
        # holds extras: paired agents, future per-CLI tokens, anything
        # else that needs to authenticate as a member without kicking
        # the user's browser session. Lookups by hash check this table
        # first, then fall back to ``members.token_hash`` for legacy
        # / web-session compatibility.
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS member_tokens ("
            "  token_hash TEXT PRIMARY KEY,"
            "  member_id TEXT NOT NULL,"
            "  label TEXT,"
            "  created_at REAL NOT NULL,"
            "  last_used_at REAL"
            ");"
            "CREATE INDEX IF NOT EXISTS idx_member_tokens_member "
            "ON member_tokens(member_id);"
        )

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

    def list_workers_for_member(self, member_id: str) -> list[sqlite3.Row]:
        """Workers whose ``owner_member_id`` is ``member_id``. Used by
        the account page to surface a "partial contributor — install
        image gen" badge on chat-only workers without needing a Redis
        round-trip. Filters out legacy unowned rows."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM workers WHERE owner_member_id=? "
                "ORDER BY registered_at ASC",
                (member_id,),
            )
            return cur.fetchall()

    def delete_worker(self, worker_id: str) -> bool:
        """Hard-delete a worker row. Used by the account-page "forget"
        button to clear out a stale registration (the previous install,
        a decommissioned PC, etc.). Earnings rows are left alone —
        they keyed on worker_id and form an immutable lifetime
        ledger, so deleting the worker row breaks the bridge for that
        machine only. The owner's lifetime earnings stay visible via
        any remaining owned workers; we accept the bridge gap on a
        forgotten worker rather than orphan-deleting earnings."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM workers WHERE worker_id=?", (worker_id,),
            )
            return cur.rowcount > 0

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
        daily_quota_images: Optional[int] = None,
        daily_quota_voice_minutes: Optional[int] = None,
        created_at: Optional[float] = None,
        tos_accepted_at: Optional[float] = None,
        tos_version: Optional[str] = None,
    ) -> None:
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO members (member_id, email, role, parent_member_id, "
                "token_hash, tier, daily_quota_tokens, daily_quota_images, "
                "daily_quota_voice_minutes, "
                "created_at, tos_accepted_at, tos_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    member_id,
                    email,
                    role,
                    parent_member_id,
                    token_hash,
                    tier,
                    daily_quota_tokens,
                    daily_quota_images,
                    daily_quota_voice_minutes,
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

    def get_member_by_username(self, username: str) -> Optional[sqlite3.Row]:
        """Case-insensitive username lookup. Returns None for the active-
        but-no-username legacy rows since they store NULL. Login callers
        get a row only if the caller has both registered and not been
        revoked."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM members "
                "WHERE LOWER(username)=LOWER(?) AND revoked_at IS NULL",
                (username,),
            )
            return cur.fetchone()

    def get_member_by_email(self, email: str) -> Optional[sqlite3.Row]:
        """Case-insensitive email lookup. Skips revoked rows so a
        revoked member doesn't squat on the address forever — matches
        the partial unique index semantics."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM members "
                "WHERE LOWER(email)=LOWER(?) AND revoked_at IS NULL",
                (email,),
            )
            return cur.fetchone()

    def set_member_email(self, member_id: str, email: str) -> tuple[bool, Optional[str]]:
        """Set or update a member's email. Returns ``(True, None)`` on
        success; ``(False, "email_taken")`` if the address is in use by
        another *active* member (case-insensitive). Revoked rows are
        not considered collisions — same rule as the partial unique
        index. Wrapped in BEGIN/COMMIT so the collision check and the
        UPDATE are atomic."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT member_id FROM members "
                    "WHERE LOWER(email)=LOWER(?) AND member_id<>? "
                    "AND revoked_at IS NULL",
                    (email, member_id),
                )
                clash = cur.fetchone()
                if clash is not None:
                    self._conn.execute("ROLLBACK")
                    return False, "email_taken"
                self._conn.execute(
                    "UPDATE members SET email=? WHERE member_id=?",
                    (email, member_id),
                )
                self._conn.execute("COMMIT")
                return True, None
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def set_member_credentials(
        self,
        member_id: str,
        username: Optional[str],
        password_hash: Optional[str],
        when: float,
    ) -> None:
        """Set username and/or password_hash on a member row. COALESCE
        leaves the prior value when the caller passes None — so
        ``set_credentials(member_id, None, new_hash, now)`` rotates only
        the password and ``(member_id, new_name, None, now)`` claims a
        username without touching the secret. Callers that want to clear
        a value must do so explicitly via a separate UPDATE."""
        with self._lock:
            self._conn.execute(
                "UPDATE members SET "
                "username=COALESCE(?, username), "
                "password_hash=COALESCE(?, password_hash), "
                "password_set_at=CASE WHEN ? IS NOT NULL THEN ? "
                "ELSE password_set_at END "
                "WHERE member_id=?",
                (username, password_hash, password_hash, when, member_id),
            )

    # ---------- member tokens (multi) ----------
    def add_member_token(
        self,
        token_hash: str,
        member_id: str,
        label: Optional[str],
        when: float,
    ) -> None:
        """Register an additional bearer for ``member_id``. The hash is
        the PRIMARY KEY so the same token can't be re-added twice —
        callers regenerate on collision (statistically impossible at
        256 bits)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO member_tokens "
                "(token_hash, member_id, label, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (token_hash, member_id, label, when),
            )

    def lookup_member_id_by_token_hash_in_tokens_table(
        self,
        token_hash: str,
    ) -> Optional[str]:
        """Return ``member_id`` if the hash is registered in
        ``member_tokens``, else None. Distinct from the legacy
        single-token lookup that reads ``members.token_hash`` — that's
        the wire credential rotated by /login, while this is the
        secondary-token table populated by pairing and future API
        keys."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT member_id FROM member_tokens WHERE token_hash=?",
                (token_hash,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return row["member_id"]

    def touch_member_token(self, token_hash: str, when: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE member_tokens SET last_used_at=? WHERE token_hash=?",
                (when, token_hash),
            )

    def list_member_tokens(self, member_id: str) -> list[sqlite3.Row]:
        """Returns rows for the Account page's "This PC" section.
        Excludes the legacy single token stored on ``members``."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT token_hash, label, created_at, last_used_at "
                "FROM member_tokens WHERE member_id=? ORDER BY created_at DESC",
                (member_id,),
            )
            return cur.fetchall()

    def delete_member_token(self, member_id: str, token_hash: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM member_tokens WHERE member_id=? AND token_hash=?",
                (member_id, token_hash),
            )
            return cur.rowcount > 0

    def rotate_member_token(
        self,
        member_id: str,
        new_token_hash: str,
    ) -> bool:
        """Replace a member's wire-format bearer token. Used by /login
        each time u/p auth succeeds — the old token is invalidated, the
        new one becomes the session cookie. Returns False if the new
        hash collides with another member (should be statistically
        impossible with 256-bit tokens but guarded just in case)."""
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE members SET token_hash=? WHERE member_id=?",
                    (new_token_hash, member_id),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def list_members(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM members ORDER BY created_at ASC"
            )
            return cur.fetchall()

    def has_active_admin(self) -> bool:
        """True iff at least one un-revoked admin member exists. Used by
        ``ensure_admin_seed`` to avoid duplicating the admin row after
        the founding admin claims u/p credentials — at that point their
        token has rotated, so the seed's token_hash lookup misses and
        would otherwise create a stale duplicate on every restart."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM members "
                "WHERE role='admin' AND revoked_at IS NULL LIMIT 1"
            )
            return cur.fetchone() is not None

    def revoke_member_by_token_hash(self, token_hash: str, revoked_at: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE members SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (revoked_at, token_hash),
            )
            return cur.rowcount > 0

    def revoke_member_by_id(self, member_id: str, revoked_at: float) -> bool:
        """Mark a member revoked by id. Used by the host-managed
        friends UI — the host clicking "revoke" on an accepted invitee
        gets the same effect as the legacy token-hash path. Idempotent:
        revoking an already-revoked row returns False without raising."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE members SET revoked_at=? "
                "WHERE member_id=? AND revoked_at IS NULL",
                (revoked_at, member_id),
            )
            return cur.rowcount > 0

    def update_member_quotas(
        self,
        member_id: str,
        daily_quota_tokens: Optional[int],
        daily_quota_images: Optional[int],
        daily_quota_voice_minutes: Optional[int] = None,
    ) -> bool:
        """Replace all daily caps on a member row. Passing None for
        any dimension means "unlimited" for tokens/images, or "fall
        through to tier default" for voice (see /generate). Passing
        the same value back is a harmless no-op. Returns False if the
        member doesn't exist. Revoked members are still mutable so a
        host can adjust their cap before re-activating (re-activation
        is a separate flow not yet implemented). The voice column is
        kw-only with a None default so legacy callers updating only
        tokens+images don't have to be patched all at once."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE members SET "
                "daily_quota_tokens=?, "
                "daily_quota_images=?, "
                "daily_quota_voice_minutes=? "
                "WHERE member_id=?",
                (
                    daily_quota_tokens,
                    daily_quota_images,
                    daily_quota_voice_minutes,
                    member_id,
                ),
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

    def add_member_image_usage(
        self,
        member_id: str,
        when: float,
        units: float,
    ) -> None:
        """Credit ``units`` to the submitter's image-units rollup. Called
        from /jobs/complete on successful image jobs in place of the
        old synthetic 200-token credit, so the token ledger only tracks
        real chat tokens and the image quota is enforced against its
        own column. Always increments ``jobs`` by 1 — the rollup counts
        every credited job regardless of tool, mirroring the chat
        path."""
        day = _utc_day(when)
        with self._lock:
            self._conn.execute(
                "INSERT INTO member_usage "
                "(member_id, day, tokens_in, tokens_out, image_units, jobs) "
                "VALUES (?, ?, 0, 0, ?, 1) "
                "ON CONFLICT(member_id, day) DO UPDATE SET "
                "image_units = image_units + excluded.image_units, "
                "jobs        = jobs        + 1",
                (member_id, day, units),
            )

    def add_member_voice_usage(
        self,
        member_id: str,
        when: float,
        seconds: float,
    ) -> None:
        """Credit ``seconds`` of synthesised (or future: transcribed)
        audio to the submitter's voice rollup. Same shape as
        add_member_image_usage: own column on member_usage, jobs+1 per
        call. Stored as seconds (REAL) — the /generate gate divides by
        60 to compare against the daily-minute cap. Sub-second
        sentences from client-side segmentation must accumulate
        accurately, so int-minutes would round away most of a chatty
        conversation's usage."""
        day = _utc_day(when)
        with self._lock:
            self._conn.execute(
                "INSERT INTO member_usage "
                "(member_id, day, tokens_in, tokens_out, voice_seconds, jobs) "
                "VALUES (?, ?, 0, 0, ?, 1) "
                "ON CONFLICT(member_id, day) DO UPDATE SET "
                "voice_seconds = voice_seconds + excluded.voice_seconds, "
                "jobs          = jobs          + 1",
                (member_id, day, seconds),
            )

    def member_usage_today(self, member_id: str, now: Optional[float] = None) -> dict:
        when = now if now is not None else time.time()
        day = _utc_day(when)
        with self._lock:
            cur = self._conn.execute(
                "SELECT tokens_in, tokens_out, image_units, voice_seconds, jobs "
                "FROM member_usage WHERE member_id=? AND day=?",
                (member_id, day),
            )
            row = cur.fetchone()
        if row is None:
            return {
                "day": day,
                "tokens_in": 0,
                "tokens_out": 0,
                "image_units": 0.0,
                "voice_seconds": 0.0,
                "jobs": 0,
            }
        # image_units / voice_seconds may be missing on a row predating
        # their respective migrations — SELECT against a freshly migrated
        # DB always returns the columns, but the row keys() check is
        # cheaper than a try/except and keeps test fixtures happy.
        keys = row.keys()
        return {
            "day": day,
            "tokens_in": int(row["tokens_in"]),
            "tokens_out": int(row["tokens_out"]),
            "image_units": (
                float(row["image_units"]) if "image_units" in keys else 0.0
            ),
            "voice_seconds": (
                float(row["voice_seconds"]) if "voice_seconds" in keys else 0.0
            ),
            "jobs": int(row["jobs"]),
        }

    def member_earnings(self, member_id: str) -> dict:
        """Aggregate earnings across every worker owned by this member.
        Returns ``{total_tokens, total_jobs, total_usd, machine_count}``.

        Bridges the structurally split ledgers: ``earnings`` is keyed
        on ``worker_id``, ``workers.owner_member_id`` ties each machine
        back to its owner. Aggregated at read time so a member adding
        or unpairing a machine is reflected immediately without any
        backfill pass. ``machine_count`` is the count of distinct owned
        workers that have any earnings rows (zero on the common
        "paired-but-never-earned" path)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT "
                "COALESCE(SUM(e.total_tokens), 0) AS total_tokens, "
                "COALESCE(SUM(e.total_jobs),   0) AS total_jobs, "
                "COALESCE(SUM(e.total_usd),    0) AS total_usd, "
                "COUNT(*)                       AS machine_count "
                "FROM earnings e "
                "JOIN workers w ON w.worker_id = e.worker_id "
                "WHERE w.owner_member_id = ?",
                (member_id,),
            )
            row = cur.fetchone()
        if row is None:
            return {
                "total_tokens": 0,
                "total_jobs": 0,
                "total_usd": 0.0,
                "machine_count": 0,
            }
        return {
            "total_tokens": int(row["total_tokens"]),
            "total_jobs": int(row["total_jobs"]),
            "total_usd": round(float(row["total_usd"]), 8),
            "machine_count": int(row["machine_count"]),
        }

    # ---------- worker uptime ----------
    def add_worker_uptime_minutes(
        self,
        worker_id: str,
        when: float,
        minutes: int,
    ) -> None:
        """Credit ``minutes`` of online time to ``worker_id`` for the
        UTC day containing ``when``. Called by the UptimeSampler every
        N minutes for each worker that heartbeated within the window.
        Idempotent INSERT-OR-UPDATE so a sampler restart can't double-
        count or skip — the worst case is a sample missed during the
        downtime window, which is acceptable for tier-promotion
        granularity."""
        day = _utc_day(when)
        with self._lock:
            self._conn.execute(
                "INSERT INTO worker_uptime (worker_id, day, minutes_online) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(worker_id, day) DO UPDATE SET "
                "minutes_online = minutes_online + excluded.minutes_online",
                (worker_id, day, int(minutes)),
            )

    def member_uptime_summary(
        self,
        member_id: str,
        window_days: int = 7,
        now: Optional[float] = None,
    ) -> dict:
        """Aggregate the last ``window_days`` of uptime across every
        worker the member owns. Returns:

        - ``days_online``: number of distinct UTC days in the window
          where at least one owned worker logged ≥60 minutes (1 hour).
          The hour threshold filters out flaky 15-min bursts that
          shouldn't count as "online for the day."
        - ``avg_hours_per_active_day``: average hours of total owned-
          worker uptime on the days that counted as online. A member
          with two PCs each online 4 hours scores 8 hours that day —
          the network sees them as 8 worker-hours of capacity.
        - ``total_minutes``: raw sum across the window (for display).

        Returns zeroes for a member with no owned workers — the engine
        treats them as "not meeting any tier" and keeps them at
        BRONZE without demotion (BRONZE is the floor; can't go
        lower)."""
        when = now if now is not None else time.time()
        # Build the inclusive list of UTC days in the window.
        days = []
        for i in range(window_days):
            ts = when - i * 86400.0
            days.append(_utc_day(ts))
        placeholders = ",".join(["?"] * len(days))
        with self._lock:
            cur = self._conn.execute(
                f"SELECT u.day AS day, SUM(u.minutes_online) AS total_min "
                f"FROM worker_uptime u "
                f"JOIN workers w ON w.worker_id = u.worker_id "
                f"WHERE w.owner_member_id = ? AND u.day IN ({placeholders}) "
                f"GROUP BY u.day",
                (member_id, *days),
            )
            rows = cur.fetchall()
        if not rows:
            return {
                "window_days": window_days,
                "days_online": 0,
                "avg_hours_per_active_day": 0.0,
                "total_minutes": 0,
            }
        # An "online day" is any day where the member's combined
        # owned-worker uptime is ≥ 60 minutes. Below that we treat
        # the day as a fluke and don't credit it.
        ONLINE_DAY_THRESHOLD_MIN = 60
        active_days = [r for r in rows if int(r["total_min"] or 0) >= ONLINE_DAY_THRESHOLD_MIN]
        total_minutes = sum(int(r["total_min"] or 0) for r in rows)
        if not active_days:
            return {
                "window_days": window_days,
                "days_online": 0,
                "avg_hours_per_active_day": 0.0,
                "total_minutes": total_minutes,
            }
        avg_min = sum(int(r["total_min"] or 0) for r in active_days) / len(active_days)
        return {
            "window_days": window_days,
            "days_online": len(active_days),
            "avg_hours_per_active_day": round(avg_min / 60.0, 2),
            "total_minutes": total_minutes,
        }

    def prune_worker_uptime(self, older_than_days: int = 60) -> int:
        """Drop uptime rows older than ``older_than_days``. Called by
        the daily engine to keep the table bounded — we only ever
        look back 7 days, so 60-day retention gives plenty of
        headroom for debugging. Returns the row count deleted."""
        cutoff_ts = time.time() - older_than_days * 86400.0
        cutoff_day = _utc_day(cutoff_ts)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM worker_uptime WHERE day < ?", (cutoff_day,),
            )
            return cur.rowcount

    # ---------- tier engine state ----------
    def set_member_tier(
        self,
        member_id: str,
        new_tier: str,
        when: float,
        new_daily_quota_tokens: Optional[int] = None,
        new_daily_quota_images: Optional[int] = None,
        new_daily_quota_voice_minutes: Optional[int] = None,
    ) -> None:
        """Set a member's tier and stamp ``tier_last_changed_at`` so the
        next engine pass enforces the 1-move-per-day cap. The caller
        passes the new tier's quota defaults so the quotas track the
        tier change atomically — no risk of a half-moved member with
        a new tier but stale quotas. Clears
        ``tier_below_threshold_since`` because the situation changed
        (either promoted out of below-threshold, or demoted into a
        looser bar)."""
        with self._lock:
            self._conn.execute(
                "UPDATE members SET "
                "tier=?, "
                "daily_quota_tokens=?, "
                "daily_quota_images=?, "
                "daily_quota_voice_minutes=?, "
                "tier_last_changed_at=?, "
                "tier_below_threshold_since=NULL "
                "WHERE member_id=?",
                (
                    new_tier,
                    new_daily_quota_tokens,
                    new_daily_quota_images,
                    new_daily_quota_voice_minutes,
                    when,
                    member_id,
                ),
            )

    def mark_member_below_threshold(self, member_id: str, when: float) -> None:
        """Start the demotion countdown for ``member_id``. Only writes
        if ``tier_below_threshold_since`` is currently NULL — the
        countdown is a single sustained streak, not a rolling clock,
        so repeated calls during the grace window don't restart it."""
        with self._lock:
            self._conn.execute(
                "UPDATE members SET tier_below_threshold_since=? "
                "WHERE member_id=? AND tier_below_threshold_since IS NULL",
                (when, member_id),
            )

    def clear_member_below_threshold(self, member_id: str) -> None:
        """Cancel an in-progress demotion countdown. Called by the
        engine when a previously below-threshold member is observed
        meeting their tier's bar again — a single passing day forgives
        the streak."""
        with self._lock:
            self._conn.execute(
                "UPDATE members SET tier_below_threshold_since=NULL "
                "WHERE member_id=?",
                (member_id,),
            )

    def list_members_for_tier_engine(self) -> list[sqlite3.Row]:
        """Members the tier engine should evaluate: non-admin, not
        revoked. Admins are unbounded by design and shouldn't be
        re-tiered. Revoked members are already barred from consuming;
        no need to spend cycles on them."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM members "
                "WHERE role <> 'admin' AND revoked_at IS NULL "
                "ORDER BY member_id"
            )
            return cur.fetchall()

    def set_worker_tools(self, worker_id: str, tools_json: str) -> None:
        """Persist the JSON-encoded tools list a worker last advertised
        on /register. Idempotent UPDATE — the row must already exist
        (claim_worker_ownership runs first in the /register handler).
        Stored alongside status/last_seen so the account page can
        compute is_partial without a Redis round-trip."""
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET tools_json=? WHERE worker_id=?",
                (tools_json, worker_id),
            )

    def worker_tools(self, worker_id: str) -> Optional[list[str]]:
        """Return the persisted tools list for ``worker_id``, or None
        if the worker has never advertised one (legacy or
        chat-only-with-no-explicit-list). Callers treat None as
        equivalent to ``["chat"]`` so an unupdated agent is still a
        valid chat-only contributor."""
        import json as _json
        with self._lock:
            cur = self._conn.execute(
                "SELECT tools_json FROM workers WHERE worker_id=?",
                (worker_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        raw = row["tools_json"] if "tools_json" in row.keys() else None
        if not raw:
            return None
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed]
        except (ValueError, TypeError):
            return None
        return None

    # ---------- invites ----------
    def create_invite(
        self,
        invite_id: str,
        code: str,
        contributor_member_id: str,
        daily_quota_tokens: Optional[int],
        daily_quota_images: Optional[int] = None,
        invitee_email: Optional[str] = None,
        expires_at: Optional[float] = None,
        notes: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> None:
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO invites (invite_id, code, contributor_member_id, "
                "invitee_email, daily_quota_tokens, daily_quota_images, "
                "expires_at, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invite_id,
                    code,
                    contributor_member_id,
                    invitee_email,
                    daily_quota_tokens,
                    daily_quota_images,
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
        username: Optional[str] = None,
        password_hash: Optional[str] = None,
    ) -> tuple[Optional[sqlite3.Row], Optional[str]]:
        """Atomic: verify invite is redeemable, mark accepted, insert the
        new invitee member with username + password set. Returns
        ``(invite_row, None)`` on success or ``(None, reason)`` on
        failure where ``reason`` is one of:

        - ``"not_redeemable"`` — invite missing, expired, revoked, or
          already accepted.
        - ``"username_taken"`` — username collides with another member.

        All writes happen under the same BEGIN/COMMIT so a concurrent
        accept (or a concurrent username claim from /me/username) cannot
        slip past the uniqueness check.

        ``username`` and ``password_hash`` are optional only for
        backward compatibility with internal callers; the public
        redemption flow always provides both."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT * FROM invites WHERE code=?", (code,)
                )
                row = cur.fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return None, "not_redeemable"
                if row["accepted_at"] is not None:
                    self._conn.execute("ROLLBACK")
                    return None, "not_redeemable"
                if row["revoked_at"] is not None:
                    self._conn.execute("ROLLBACK")
                    return None, "not_redeemable"
                if row["expires_at"] is not None and row["expires_at"] < accepted_at:
                    self._conn.execute("ROLLBACK")
                    return None, "not_redeemable"

                # Both collision checks filter out revoked rows — same
                # rule as the partial unique index. Lets a recycled
                # email/username come back after the original holder
                # is revoked.
                if username:
                    clash = self._conn.execute(
                        "SELECT member_id FROM members "
                        "WHERE LOWER(username)=LOWER(?) "
                        "AND revoked_at IS NULL",
                        (username,),
                    ).fetchone()
                    if clash is not None:
                        self._conn.execute("ROLLBACK")
                        return None, "username_taken"
                if invitee_email:
                    clash = self._conn.execute(
                        "SELECT member_id FROM members "
                        "WHERE LOWER(email)=LOWER(?) "
                        "AND revoked_at IS NULL",
                        (invitee_email,),
                    ).fetchone()
                    if clash is not None:
                        self._conn.execute("ROLLBACK")
                        return None, "email_taken"

                # daily_quota_images is read defensively — a row stored
                # before the migration won't have the column at all
                # under SQLite's permissive schema, but row.keys()
                # reflects the live schema so the lookup is safe.
                invite_image_cap = (
                    row["daily_quota_images"]
                    if "daily_quota_images" in row.keys()
                    else None
                )
                self._conn.execute(
                    "INSERT INTO members (member_id, email, role, parent_member_id, "
                    "token_hash, tier, daily_quota_tokens, daily_quota_images, "
                    "created_at, tos_accepted_at, tos_version, username, "
                    "password_hash, password_set_at) "
                    "VALUES (?, ?, 'invitee', ?, ?, 'BRONZE', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_member_id,
                        invitee_email,
                        row["contributor_member_id"],
                        new_token_hash,
                        row["daily_quota_tokens"],
                        invite_image_cap,
                        accepted_at,
                        accepted_at,  # tos_accepted_at — checkbox was required at submit
                        tos_version,
                        username,
                        password_hash,
                        accepted_at if password_hash else None,
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
                return fresh, None
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

    # ---------- push subscriptions (Phase 6 — Web Push) ----------
    def upsert_push_subscription(
        self,
        member_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: Optional[str],
        now: float,
    ) -> int:
        """Insert or replace a subscription row. Replace on the unique
        (member_id, endpoint) so re-subscribing from the same browser
        rotates the keys without leaving an orphan row behind. Returns
        the row id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO push_subscriptions "
                "(member_id, endpoint, p256dh, auth, user_agent, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(member_id, endpoint) DO UPDATE SET "
                "  p256dh=excluded.p256dh, "
                "  auth=excluded.auth, "
                "  user_agent=excluded.user_agent, "
                "  created_at=excluded.created_at",
                (member_id, endpoint, p256dh, auth, user_agent, now),
            )
            # SQLite's lastrowid after ON CONFLICT...DO UPDATE is the
            # existing row's id when it's an update; fetch the canonical
            # id either way so the caller doesn't have to special-case.
            row = self._conn.execute(
                "SELECT id FROM push_subscriptions "
                "WHERE member_id=? AND endpoint=?",
                (member_id, endpoint),
            ).fetchone()
            return int(row["id"]) if row else int(cur.lastrowid or 0)

    def delete_push_subscription(self, member_id: str, endpoint: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM push_subscriptions "
                "WHERE member_id=? AND endpoint=?",
                (member_id, endpoint),
            )
            return cur.rowcount > 0

    def delete_push_subscription_by_id(self, sub_id: int) -> bool:
        """Used by the send loop when a Push service returns 404 / 410
        (subscription expired or unregistered). Distinct from the
        member-driven DELETE above — no member_id check because the
        cleanup happens server-side without a session."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM push_subscriptions WHERE id=?", (sub_id,)
            )
            return cur.rowcount > 0

    def list_push_subscriptions(self, member_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT id, member_id, endpoint, p256dh, auth, user_agent, "
                "       created_at "
                "FROM push_subscriptions WHERE member_id=? "
                "ORDER BY created_at",
                (member_id,),
            ))

    # ---------- in-app notifications ----------
    def insert_notification(
        self,
        notification_id: str,
        member_id: str,
        type_: str,
        title: str,
        body: Optional[str],
        data_json: Optional[str],
        pushed: bool,
        now: float,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO notifications "
                "(notification_id, member_id, type, title, body, data, "
                " read, pushed, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    notification_id, member_id, type_, title, body,
                    data_json, 1 if pushed else 0, now,
                ),
            )

    def list_notifications(
        self,
        member_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT notification_id, member_id, type, title, body, "
                "       data, read, pushed, created_at "
                "FROM notifications WHERE member_id=? "
                "ORDER BY created_at DESC "
                "LIMIT ? OFFSET ?",
                (member_id, int(limit), int(offset)),
            ))

    def unread_notification_count(self, member_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM notifications "
                "WHERE member_id=? AND read=0",
                (member_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def mark_notification_read(
        self, member_id: str, notification_id: str,
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE notifications SET read=1 "
                "WHERE notification_id=? AND member_id=?",
                (notification_id, member_id),
            )
            return cur.rowcount > 0

    def mark_all_notifications_read(self, member_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE notifications SET read=1 "
                "WHERE member_id=? AND read=0",
                (member_id,),
            )
            return int(cur.rowcount)

    # ---------- notification preferences ----------
    def get_notification_preference(
        self, member_id: str, category: str,
    ) -> bool:
        """Return whether this category is enabled for this member.
        Default = True (opt-out) for any category not in the table."""
        with self._lock:
            row = self._conn.execute(
                "SELECT enabled FROM notification_preferences "
                "WHERE member_id=? AND category=?",
                (member_id, category),
            ).fetchone()
        if row is None:
            return True
        return bool(row["enabled"])

    def set_notification_preference(
        self, member_id: str, category: str, enabled: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO notification_preferences "
                "(member_id, category, enabled) VALUES (?, ?, ?) "
                "ON CONFLICT(member_id, category) DO UPDATE SET "
                "  enabled=excluded.enabled",
                (member_id, category, 1 if enabled else 0),
            )

    def get_all_notification_preferences(
        self, member_id: str,
    ) -> dict[str, bool]:
        """Returns explicitly-set prefs only. Caller is expected to
        merge with the default-True for categories the member hasn't
        touched yet."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT category, enabled FROM notification_preferences "
                "WHERE member_id=?",
                (member_id,),
            ).fetchall()
        return {r["category"]: bool(r["enabled"]) for r in rows}

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
