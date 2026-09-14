"""``DB`` push-subscription / in-app-notification / notification-
preference methods — coordinator/db.py god-file split. Mixin
composition (not delegation): every method keeps referencing
``self._conn``/``self._lock`` exactly as before, since ``self`` still
resolves to the composed ``DB`` instance via MRO — a pure cut/paste, no
call-site changes needed anywhere else in the coordinator. See
coordinator/db.py for the full ``class DB(...)`` composition.
"""
import sqlite3
from typing import Optional


class NotificationsMixin:
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
