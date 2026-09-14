"""``DB._migrate`` — coordinator/db.py god-file split. Mixin
composition (not delegation): the method keeps referencing
``self._conn`` exactly as before, since ``self`` still resolves to the
composed ``DB`` instance via MRO — a pure cut/paste, no call-site
changes needed anywhere else in the coordinator. Called from
``DB.__init__`` inside the ``with self._lock:`` block, so it never
needs to acquire the lock itself. See coordinator/db.py for the full
``class DB(...)`` composition.

Saved for last in this god-file split (of the 12 db_mixins) since a
subtle drift here is the hardest to detect via tests — every ALTER/
CREATE is additive/idempotent by design (each wrapped in its own
try/except OperationalError), so a broken migration might not fail
loudly until a fresh-DB or upgrade-path test specifically exercises it.
"""
import sqlite3


class MigrationsMixin:
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
