"""``DB`` member-token / machine methods — coordinator/db.py god-file
split. Mixin composition (not delegation): every method keeps
referencing ``self._conn``/``self._lock`` exactly as before, since
``self`` still resolves to the composed ``DB`` instance via MRO — a
pure cut/paste, no call-site changes needed anywhere else in the
coordinator. See coordinator/db.py for the full ``class DB(...)``
composition.
"""
import sqlite3
from typing import Optional


class MachinesMixin:
    # ---------- member tokens (multi) ----------
    def add_member_token(
        self,
        token_hash: str,
        member_id: str,
        label: Optional[str],
        when: float,
        kind: str = "agent",
        scope: Optional[str] = None,
    ) -> None:
        """Register an additional bearer for ``member_id``. The hash is
        the PRIMARY KEY so the same token can't be re-added twice —
        callers regenerate on collision (statistically impossible at
        256 bits). ``kind``/``scope`` default to the pre-existing
        agent-pairing shape (unrestricted 'agent' token) so the
        /agents/pair/confirm call site is unaffected; self-serve API
        keys pass kind="api_key", scope="generation"."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO member_tokens "
                "(token_hash, member_id, label, created_at, last_used_at, kind, scope) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (token_hash, member_id, label, when, kind, scope),
            )

    def get_secondary_token_row(self, token_hash: str) -> Optional[sqlite3.Row]:
        """Return the full ``member_tokens`` row (member_id, kind, scope)
        for a hash, or None. Distinct from the legacy single-token lookup
        that reads ``members.token_hash`` — that's the wire credential
        rotated by /login, while this is the secondary-token table
        populated by pairing and self-serve API keys. Supersedes
        ``lookup_member_id_by_token_hash_in_tokens_table`` (member_id
        only) now that callers also need to know kind/scope in one trip."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT member_id, kind, scope FROM member_tokens WHERE token_hash=?",
                (token_hash,),
            )
            return cur.fetchone()

    def touch_member_token(self, token_hash: str, when: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE member_tokens SET last_used_at=? WHERE token_hash=?",
                (when, token_hash),
            )

    def list_member_tokens(
        self, member_id: str, kind: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """Returns rows for the Account page's "This PC" / "API keys"
        sections. Excludes the legacy single token stored on ``members``.
        ``kind`` filters to just 'agent' (Machines page) or 'api_key'
        (API keys page) — omit for both."""
        with self._lock:
            if kind is not None:
                cur = self._conn.execute(
                    "SELECT token_hash, label, created_at, last_used_at "
                    "FROM member_tokens WHERE member_id=? AND kind=? "
                    "ORDER BY created_at DESC",
                    (member_id, kind),
                )
            else:
                cur = self._conn.execute(
                    "SELECT token_hash, label, created_at, last_used_at "
                    "FROM member_tokens WHERE member_id=? ORDER BY created_at DESC",
                    (member_id,),
                )
            return cur.fetchall()

    def delete_member_token(
        self, member_id: str, token_hash: str, kind: Optional[str] = None,
    ) -> bool:
        """``kind``, when given, scopes the delete so machine-unpair and
        API-key-revoke can never cross-delete each other's rows even if a
        prefix somehow collided."""
        with self._lock:
            if kind is not None:
                cur = self._conn.execute(
                    "DELETE FROM member_tokens WHERE member_id=? AND "
                    "token_hash=? AND kind=?",
                    (member_id, token_hash, kind),
                )
            else:
                cur = self._conn.execute(
                    "DELETE FROM member_tokens WHERE member_id=? AND token_hash=?",
                    (member_id, token_hash),
                )
            return cur.rowcount > 0

    # ---------- machines (member_tokens as the durable machine record) ----------
    def link_token_to_worker(self, token_hash: str, worker_id: str) -> None:
        """Stamp the runtime ``worker_id`` onto the machine's pairing
        record. Called at /register and /heartbeat; the WHERE guard makes
        the per-heartbeat call a no-op write once the link is set (or
        re-points it if the agent regenerated its worker_id)."""
        with self._lock:
            self._conn.execute(
                "UPDATE member_tokens SET worker_id=? "
                "WHERE token_hash=? AND (worker_id IS NULL OR worker_id <> ?)",
                (worker_id, token_hash, worker_id),
            )

    def get_machine_schedule_by_token(self, token_hash: str) -> Optional[sqlite3.Row]:
        """Schedule fields for the machine identified by its pairing
        token. None when the token isn't a paired machine (e.g. the
        in-VPS server worker, or a web-session token) — callers treat
        None as "no schedule → always allowed"."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT paused, sched_enabled, sched_start_min, sched_end_min, "
                "sched_tz FROM member_tokens WHERE token_hash=?",
                (token_hash,),
            )
            return cur.fetchone()

    def list_machines_for_member(self, member_id: str) -> list[sqlite3.Row]:
        """One row per paired machine, joined to its runtime registration
        in ``workers`` (LEFT JOIN — a freshly-paired machine has no worker
        row until the agent calls /register). This is the single source
        for the Machines page. ``kind='agent'`` excludes self-serve API
        keys, which live in the same table but aren't machines."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT t.token_hash, t.label, t.created_at, t.last_used_at, "
                "t.worker_id, t.paused, t.sched_enabled, t.sched_start_min, "
                "t.sched_end_min, t.sched_tz, "
                "w.status AS worker_status, w.last_seen AS worker_last_seen, "
                "w.tools_json AS worker_tools_json, w.registered_at AS worker_registered_at, "
                "w.display_name AS worker_display_name "
                "FROM member_tokens t "
                "LEFT JOIN workers w ON w.worker_id = t.worker_id "
                "WHERE t.member_id=? AND t.kind='agent' ORDER BY t.created_at DESC",
                (member_id,),
            )
            return cur.fetchall()

    def resolve_member_token_hash(
        self, member_id: str, id_prefix: str, kind: str,
    ) -> Optional[str]:
        """Map the 12-char ``id`` a UI/API carries back to a full
        token_hash, scoped to the caller AND to ``kind`` ('agent' for the
        Machines page, 'api_key' for the API keys page) — so a machine
        unpair and a key revoke can never resolve into each other's rows
        even on a prefix collision. Returns None if no match or an
        ambiguous prefix (collision is statistically impossible at 12 hex
        chars, but we refuse rather than guess)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT token_hash FROM member_tokens "
                "WHERE member_id=? AND kind=? AND token_hash LIKE ?",
                (member_id, kind, id_prefix.replace("%", "") + "%"),
            )
            rows = cur.fetchall()
        if len(rows) != 1:
            return None
        return rows[0]["token_hash"]

    def set_machine_schedule(
        self,
        member_id: str,
        token_hash: str,
        *,
        paused: bool,
        sched_enabled: bool,
        start_min: Optional[int],
        end_min: Optional[int],
        tz: Optional[str],
    ) -> bool:
        """Persist a machine's schedule, scoped to its owner. Returns
        False when the (member, token) pair doesn't exist."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE member_tokens SET paused=?, sched_enabled=?, "
                "sched_start_min=?, sched_end_min=?, sched_tz=? "
                "WHERE member_id=? AND token_hash=?",
                (
                    1 if paused else 0,
                    1 if sched_enabled else 0,
                    start_min,
                    end_min,
                    tz,
                    member_id,
                    token_hash,
                ),
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
