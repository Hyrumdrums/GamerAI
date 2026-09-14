"""``DB`` member methods — coordinator/db.py god-file split. Mixin
composition (not delegation): every method keeps referencing
``self._conn``/``self._lock`` exactly as before, since ``self`` still
resolves to the composed ``DB`` instance via MRO — a pure cut/paste, no
call-site changes needed anywhere else in the coordinator. See
coordinator/db.py for the full ``class DB(...)`` composition.
"""
import sqlite3
import time
from typing import Optional


class MembersMixin:
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

    def create_signup_member(
        self,
        member_id: str,
        username: str,
        password_hash: str,
        email: str,
        token_hash: str,
        daily_quota_tokens: Optional[int],
        daily_quota_images: Optional[int],
        daily_quota_voice_minutes: Optional[int],
        created_at: float,
        tos_version: Optional[str],
    ) -> tuple[Optional[sqlite3.Row], Optional[str]]:
        """Atomic, invite-free member creation for public POST /signup.
        Same collision-checked shape as ``accept_invite_atomic`` (which
        this is deliberately kept parallel to) but with no invites-table
        interaction: ``role='contributor'`` and ``parent_member_id=NULL``
        — this member is the root of their own branch of the network,
        not someone else's invitee. Returns ``(member_row, None)`` on
        success or ``(None, reason)`` where ``reason`` is
        ``"username_taken"`` or ``"email_taken"``."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                clash = self._conn.execute(
                    "SELECT member_id FROM members "
                    "WHERE LOWER(username)=LOWER(?) AND revoked_at IS NULL",
                    (username,),
                ).fetchone()
                if clash is not None:
                    self._conn.execute("ROLLBACK")
                    return None, "username_taken"
                clash = self._conn.execute(
                    "SELECT member_id FROM members "
                    "WHERE LOWER(email)=LOWER(?) AND revoked_at IS NULL",
                    (email,),
                ).fetchone()
                if clash is not None:
                    self._conn.execute("ROLLBACK")
                    return None, "email_taken"
                self._conn.execute(
                    "INSERT INTO members (member_id, email, role, parent_member_id, "
                    "token_hash, tier, daily_quota_tokens, daily_quota_images, "
                    "daily_quota_voice_minutes, created_at, tos_accepted_at, "
                    "tos_version, username, password_hash, password_set_at, "
                    "email_verified) "
                    "VALUES (?, ?, 'contributor', NULL, ?, 'BRONZE', ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, 0)",
                    (
                        member_id,
                        email,
                        token_hash,
                        daily_quota_tokens,
                        daily_quota_images,
                        daily_quota_voice_minutes,
                        created_at,
                        created_at,  # tos_accepted_at — checkbox required at submit
                        tos_version,
                        username,
                        password_hash,
                        created_at,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM members WHERE member_id=?", (member_id,)
                ).fetchone()
                self._conn.execute("COMMIT")
                return row, None
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def verify_member_email(self, member_id: str) -> None:
        """Mark a member's email confirmed — POST /signup's auto-verify
        fallback (Resend unconfigured) and GET /verify-email's success
        path both call this. Idempotent (a re-click of an already-used
        link is a harmless no-op, not an error)."""
        with self._lock:
            self._conn.execute(
                "UPDATE members SET email_verified=1 WHERE member_id=?",
                (member_id,),
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
