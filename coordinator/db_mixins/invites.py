"""``DB`` invite methods — coordinator/db.py god-file split. Mixin
composition (not delegation): every method keeps referencing
``self._conn``/``self._lock`` exactly as before, since ``self`` still
resolves to the composed ``DB`` instance via MRO — a pure cut/paste, no
call-site changes needed anywhere else in the coordinator. See
coordinator/db.py for the full ``class DB(...)`` composition.
"""
import sqlite3
import time
from typing import Optional


class InvitesMixin:
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
