"""Email the admin on a small set of operational events.

First (and so far only) subscriber to coordinator.events — wires
"member.created" and "worker.registered" to send_admin_alert(). Both
are no-ops when ADMIN_ALERT_EMAIL isn't set (email_send.send_admin_alert
handles that), so this module is safe to import unconditionally.

Importing this module is what makes the subscriptions active (see the
subscribe() calls at the bottom) — coordinator/main.py imports it once
for that side effect, same pattern as coordinator/db.py's module-level
schema setup.
"""
from __future__ import annotations

from coordinator import events
from coordinator.email_send import send_admin_alert


def _on_member_created(data: dict) -> None:
    kind = "invited member" if data.get("invited") else "new signup"
    send_admin_alert(
        "GamerAI: new member",
        f"<p>New member ({kind}):</p>"
        f"<p>username: {data.get('username')}<br>"
        f"email: {data.get('email')}<br>"
        f"member_id: {data.get('member_id')}</p>",
    )


def _on_worker_registered(data: dict) -> None:
    send_admin_alert(
        "GamerAI: new agent",
        f"<p>New worker registered:</p>"
        f"<p>worker_id: {data.get('worker_id')}<br>"
        f"owner_member_id: {data.get('owner_member_id')}</p>",
    )


events.subscribe("member.created", _on_member_created)
events.subscribe("worker.registered", _on_worker_registered)
