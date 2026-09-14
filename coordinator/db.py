"""SQLite write-through store. Redis remains the queue; SQLite is the system of record."""
import os
import sqlite3
import threading
import time
from typing import Optional

from shared.config import DB_PATH

from coordinator.db_mixins.canaries import CanariesMixin
from coordinator.db_mixins.conversations import ConversationsMixin
from coordinator.db_mixins.invites import InvitesMixin
from coordinator.db_mixins.jobs import JobsMixin
from coordinator.db_mixins.machines import MachinesMixin
from coordinator.db_mixins.members import MembersMixin
from coordinator.db_mixins.metrics import MetricsMixin
from coordinator.db_mixins.migrations import MigrationsMixin
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
    CanariesMixin, ConversationsMixin, InvitesMixin, JobsMixin, MachinesMixin,
    MembersMixin, MetricsMixin, MigrationsMixin, NotificationsMixin, QuotasMixin,
    UploadsMixin, WorkersMixin,
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

