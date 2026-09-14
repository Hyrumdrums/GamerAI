"""``DB`` worker / earnings methods — coordinator/db.py god-file split.
Mixin composition (not delegation): every method keeps referencing
``self._conn``/``self._lock`` exactly as before, since ``self`` still
resolves to the composed ``DB`` instance via MRO — a pure cut/paste, no
call-site changes needed anywhere else in the coordinator. See
coordinator/db.py for the full ``class DB(...)`` composition.
"""
import sqlite3
import time
from typing import Optional


class WorkersMixin:
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
    ) -> tuple[bool, Optional[str], bool]:
        """Atomic ownership claim for /register. Returns
        (ok, current_owner, is_new).

        - If the worker_id is new, inserts with member_id as owner.
        - If the worker_id exists with owner_member_id NULL (legacy),
          the calling member adopts ownership.
        - If the worker_id exists with the same owner, refresh status.
        - If owned by a different member, returns (False, existing_owner, False)
          so the caller can 403.

        ``is_new`` lets the caller fire a "new agent" event exactly
        once per worker_id, rather than on every reconnect/restart.
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
                    return True, member_id, True
                existing = row["owner_member_id"]
                if (
                    existing is not None
                    and member_id is not None
                    and existing != member_id
                ):
                    self._conn.execute("ROLLBACK")
                    return False, existing, False
                # Same owner OR adopting a legacy unowned worker.
                self._conn.execute(
                    "UPDATE workers SET status=?, last_seen=?, "
                    "owner_member_id=COALESCE(owner_member_id, ?) "
                    "WHERE worker_id=?",
                    (status, last_seen, member_id, worker_id),
                )
                self._conn.execute("COMMIT")
                return True, (member_id if existing is None else existing), False
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_worker(self, worker_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM workers WHERE worker_id=?", (worker_id,),
            )
            return cur.fetchone()

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

    def set_worker_display_name(self, worker_id: str, display_name: str) -> None:
        """Persist the contributor-chosen (or randomly-defaulted)
        machine name sent on /register. Idempotent UPDATE, same
        precondition as set_worker_tools — claim_worker_ownership runs
        first so the row already exists."""
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET display_name=? WHERE worker_id=?",
                (display_name, worker_id),
            )

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
