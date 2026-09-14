"""SQLite write-through store. Redis remains the queue; SQLite is the system of record."""
import os
import sqlite3
import threading
import time
from typing import Optional

from shared.config import DB_PATH

from coordinator.db_mixins.canaries import CanariesMixin
from coordinator.db_mixins.invites import InvitesMixin
from coordinator.db_mixins.machines import MachinesMixin
from coordinator.db_mixins.members import MembersMixin
from coordinator.db_mixins.metrics import MetricsMixin
from coordinator.db_mixins.notifications import NotificationsMixin
from coordinator.db_mixins.quotas import QuotasMixin
from coordinator.db_mixins.uploads import UploadsMixin
from coordinator.db_mixins.workers import WorkersMixin

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
    last_active_at REAL,
    -- 1 = can consume (chat/image/voice); 0 = pending email confirm.
    -- Defaults to 1 (verified) so admin-seed and invite-accept rows
    -- (both already gated by a stronger trust signal than an
    -- unverified inbox) aren't affected. Only create_signup_member()
    -- inserts 0 explicitly — see POST /signup and GET /verify-email.
    email_verified INTEGER NOT NULL DEFAULT 1
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

-- Extracted text from a member-uploaded document (PDF/DOCX/TXT/MD/
-- CSV), attached to a conversation. The raw uploaded file is never
-- written anywhere durable — coordinator/uploads.py parses it
-- straight off the request's spooled upload stream and discards the
-- bytes once extraction finishes. extracted_text is what persists,
-- with the same plaintext-at-rest retention as everything else in
-- this table's neighborhood (jobs.prompt, messages.text) — see
-- project-gaps.md's "Stored prompt history is plaintext at rest" for
-- the encryption plan that will eventually cover this column too.
-- coordinator.main folds a member's uploads for a conversation into
-- a <<document>> fence prepended to that conversation's chat turns.
CREATE TABLE IF NOT EXISTS uploads (
    upload_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    member_id TEXT,
    filename TEXT NOT NULL,
    content_type TEXT,
    extracted_text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_uploads_conversation
    ON uploads(conversation_id, created_at);
"""


class DB(
    CanariesMixin, InvitesMixin, MachinesMixin, MembersMixin, MetricsMixin,
    NotificationsMixin, QuotasMixin, UploadsMixin, WorkersMixin,
):
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
        # Signup email verification. DEFAULT 1 backfills every existing
        # row (pre-dating this column) as verified — they were created
        # via invite/admin, a stronger trust signal than an unconfirmed
        # inbox, so this migration doesn't retroactively lock anyone
        # out. Only new POST /signup rows are inserted with 0.
        try:
            self._conn.execute(
                "ALTER TABLE members ADD COLUMN email_verified "
                "INTEGER NOT NULL DEFAULT 1"
            )
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
        # Uptime-schedule slice. ``member_tokens`` is the durable
        # per-machine record (created at pairing); the schedule lives
        # here, not on ``workers``, because worker_id is regenerated on
        # agent reinstall (win-<host>-<random>) while the paired token
        # persists. ``worker_id`` links this record to its runtime
        # registration in ``workers`` — stamped at /register and
        # backfilled at /heartbeat, so already-paired agents pick it up
        # without re-pairing. Schedule window is minutes-from-midnight
        # in ``sched_tz`` (IANA); start>end means an overnight wrap
        # (e.g. 1320..360 = 22:00-06:00). paused is a hard manual stop
        # independent of the time window.
        for ddl in (
            "ALTER TABLE member_tokens ADD COLUMN worker_id TEXT",
            "ALTER TABLE member_tokens ADD COLUMN paused INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE member_tokens ADD COLUMN sched_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE member_tokens ADD COLUMN sched_start_min INTEGER",
            "ALTER TABLE member_tokens ADD COLUMN sched_end_min INTEGER",
            "ALTER TABLE member_tokens ADD COLUMN sched_tz TEXT",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_member_tokens_worker "
            "ON member_tokens(worker_id)"
        )
        # Self-serve API keys slice. ``kind`` distinguishes an
        # agent-pairing token (the only kind that existed before this)
        # from a member-minted ``api_key`` — 'agent' is the DEFAULT so
        # SQLite backfills every pre-existing row, not just future
        # inserts. ``scope`` is NULL for unrestricted tokens (every
        # agent token, and the primary members.token_hash which never
        # has a row here) or 'generation' for a key restricted to
        # generation-only endpoints — see _is_generation_scoped_allowed
        # in coordinator/main.py.
        for ddl in (
            "ALTER TABLE member_tokens ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent'",
            "ALTER TABLE member_tokens ADD COLUMN scope TEXT",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # History summarization (Phase 3 of the long-context fix).
        # ``summary_text`` is a short natural-language recap of every
        # turn up through ``summary_through_seq``. _build_chat_messages
        # prepends it as a system message and skips the turns it
        # covers, so a 25K-token thread gets shipped to Ollama as a
        # ~200-token summary + the last ~4K tokens of context.
        for ddl in (
            "ALTER TABLE conversations ADD COLUMN summary_text TEXT",
            "ALTER TABLE conversations ADD COLUMN summary_through_seq INTEGER",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # End-to-end latency trail. submitted_at / started_at /
        # completed_at already covered queue + worker time;
        # first_partial_at marks when the first streaming partial landed
        # (separates "queue + prompt eval" from "actual generation"),
        # and client_displayed_at is when the browser reported it had
        # rendered the final text. The browser value is client wall-clock
        # so it's only comparable against other browser-clock points.
        for ddl in (
            "ALTER TABLE jobs ADD COLUMN first_partial_at REAL",
            "ALTER TABLE jobs ADD COLUMN client_displayed_at REAL",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # Contributor-chosen machine name. Replaces the old
        # hostname-embedded-in-worker_id trick (win-<hostname>-<rand>)
        # for identifying a machine on the dashboard/Machines page —
        # the community ToS promises we don't collect hostnames, and
        # worker_id was quietly violating that. The agent now prompts
        # for an optional name at setup (defaulting to a random
        # adjective_noun pair when left blank) and sends it as
        # display_name on /register; worker_id itself is now opaque
        # random. Existing machines that registered before this slice
        # keep their hostname-bearing worker_id (regenerating it would
        # orphan their job/earnings history) — display_name is NULL
        # for those until their agent updates and re-registers.
        try:
            self._conn.execute("ALTER TABLE workers ADD COLUMN display_name TEXT")
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

