"""Web Push notifications (Phase 6 of pwa-refactor.txt).

Two responsibilities:

1. ``send_to_member(db, ...)`` — best-effort delivery of one
   notification: check the member's per-category preference, INSERT a
   row in ``notifications`` (the in-app fallback / source of truth),
   then fan-out via pywebpush to every active push_subscriptions row
   for that member. Dead endpoints (404/410 from the Push service)
   are deleted as we discover them so the subscription table doesn't
   accrue garbage.

2. ``build_router(db)`` — returns the APIRouter that exposes the eight
   endpoints from PWA_REFERENCE.md §7, mapped to FastAPI:

       GET    /notifications/vapid-key           public
       POST   /notifications/subscribe           member
       DELETE /notifications/subscribe           member
       GET    /notifications                     member (paginated)
       GET    /notifications/unread-count        member
       PUT    /notifications/{id}/read           member
       PUT    /notifications/read-all            member
       GET    /notifications/preferences         member
       PUT    /notifications/preferences         member

VAPID config is read from env (``VAPID_PUBLIC_KEY``, ``VAPID_PRIVATE_KEY``,
``VAPID_SUBJECT``). If the keys are missing, ``is_vapid_configured()``
returns False and ``send_to_member`` becomes a no-op for the push side
(the row still goes into the in-app list). Dev/test loops without
VAPID configured therefore keep working — the only thing they lose
is the actual browser delivery.

The router is built via a closure that captures ``db`` rather than
importing it from ``coordinator.main`` (circular) or instantiating a
fresh ``DB()`` per call (separate connection, bad). main.py wires it
once: ``app.include_router(notifications.build_router(db))``.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger("coordinator.notifications")

# Notification categories. Used as both the ``type`` column value on the
# notifications row and the ``category`` key for opt-in preferences. The
# UI keeps a parallel list for the preferences page; keep them in sync.
CATEGORY_IMAGE_DONE = "image_done"
CATEGORY_VOICE_DONE = "voice_done"
CATEGORY_SYSTEM = "system"
KNOWN_CATEGORIES = frozenset({
    CATEGORY_IMAGE_DONE,
    CATEGORY_VOICE_DONE,
    CATEGORY_SYSTEM,
})

# VAPID config (Voluntary Application Server Identification). The public
# key is shipped to the client via /notifications/vapid-key; the private
# key never leaves the coordinator. ``VAPID_SUBJECT`` is a mailto: URI
# the browser's Push service uses to contact the application server if
# something looks abusive. Default falls back to the project email so a
# missing env var doesn't fail the send with a confusing 400.
#
# Private key can be supplied two ways:
#   VAPID_PRIVATE_KEY       — raw PEM string (awkward inline since
#                             it's multi-line, but supported)
#   VAPID_PRIVATE_KEY_PATH  — path to a .pem file the coordinator
#                             reads at module-import time. Preferred,
#                             because the file permissions are a
#                             separate trust boundary from the env.
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY") or None
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY") or None
_VAPID_PRIVATE_KEY_PATH = os.environ.get("VAPID_PRIVATE_KEY_PATH") or None
if not VAPID_PRIVATE_KEY and _VAPID_PRIVATE_KEY_PATH:
    try:
        with open(_VAPID_PRIVATE_KEY_PATH) as _f:
            VAPID_PRIVATE_KEY = _f.read()
    except OSError as _e:
        # Don't crash startup if the path is misconfigured —
        # is_vapid_configured() will return False and push gracefully
        # no-ops. Log loudly so the operator notices in /health logs.
        log.warning(
            "VAPID_PRIVATE_KEY_PATH=%s could not be read: %s",
            _VAPID_PRIVATE_KEY_PATH, _e,
        )
VAPID_SUBJECT = (
    os.environ.get("VAPID_SUBJECT")
    or "mailto:admin@gamerai.app"
)

NOTIFICATION_ID_PREFIX = "notif_"


def is_safe_push_endpoint(endpoint: str) -> bool:
    """Reject push endpoints that could be used for SSRF.

    The browser hands us a push-service URL at subscribe time and the
    coordinator later POSTs to it via pywebpush — server-side, from
    inside the Docker network. Without validation a member could
    register ``http://coordinator:8000/...`` or
    ``https://169.254.169.254/...`` and turn their own image-done
    notification into a request against internal services / cloud
    metadata. Real push endpoints are always https FQDNs at the big
    providers, so require https, reject IP-literal hosts in
    private/loopback/link-local/reserved ranges, and reject single-label
    hosts (``localhost``, ``redis``, ``coordinator``). This is a blunt
    instrument — DNS rebinding at send time isn't covered — but it kills
    the practical internal-reach vector at zero cost to legitimate
    subscriptions."""
    try:
        u = urlparse(endpoint)
    except (ValueError, TypeError):
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    host = u.hostname
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname (not an IP literal). Reject obviously-internal
        # single-label names; require a dotted FQDN.
        return host.lower() != "localhost" and "." in host
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _new_notification_id() -> str:
    # 12 hex chars = 48 bits; 64K unique notifications per member per
    # day still has <1 in 10^9 collision chance — adequate.
    return NOTIFICATION_ID_PREFIX + secrets.token_hex(12)


def is_vapid_configured() -> bool:
    """True iff both VAPID keys are present. Used at startup to log a
    'push disabled' warning and at send time to short-circuit the
    delivery path before pywebpush is even imported."""
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def _import_webpush():
    """Lazy import so a missing pywebpush install doesn't break module
    load (e.g. tests that don't exercise the send path). Returns
    (webpush, WebPushException) tuple, or (None, None) if unavailable."""
    try:
        from pywebpush import WebPushException, webpush
        return webpush, WebPushException
    except ImportError:
        return None, None


def send_to_member(
    db,
    member_id: str,
    type_: str,
    title: str,
    body: Optional[str] = None,
    data: Optional[dict] = None,
) -> int:
    """Insert a notification row + best-effort push delivery.

    Returns the number of push subscriptions we successfully handed off
    to (NOT the number of devices that displayed the notification —
    that's the browser's call after we deliver to its Push service).

    Skips delivery and skips the row entirely if the member has opted
    out of this category. The row IS persisted when push delivery
    fails (offline device, expired endpoint, VAPID unconfigured) so the
    in-app list always reflects the event.

    ``data`` is JSON-serialized into the row's ``data`` column and into
    the push payload. The sw.js notificationclick handler reads
    ``data.url`` to focus / open the right view; validate it starts
    with '/' there to prevent an open-redirect (see PWA_REFERENCE §14).
    """
    # Opt-out check. Default for an unknown category is enabled=True
    # so a new category we haven't shipped a preferences toggle for
    # still gets through. KNOWN_CATEGORIES exists to gate that for
    # the categories the UI does expose — caller passing
    # 'system' / 'image_done' etc. respects the user's preference,
    # caller passing a never-shown 'experimental_thing' overrides.
    if type_ in KNOWN_CATEGORIES:
        if not db.get_notification_preference(member_id, type_):
            return 0

    notif_id = _new_notification_id()
    data_json = json.dumps(data) if data is not None else None
    now = time.time()

    pushed_count = 0
    if is_vapid_configured():
        webpush, WebPushException = _import_webpush()
        if webpush is None:
            log.warning(
                "pywebpush not installed — push delivery disabled "
                "(falling back to in-app row only)",
            )
        else:
            payload = json.dumps({
                "title": title,
                "body": body or "",
                "tag": type_,
                "data": data or {},
            })
            for sub_row in db.list_push_subscriptions(member_id):
                sub_info = {
                    "endpoint": sub_row["endpoint"],
                    "keys": {
                        "p256dh": sub_row["p256dh"],
                        "auth": sub_row["auth"],
                    },
                }
                try:
                    webpush(
                        subscription_info=sub_info,
                        data=payload,
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": VAPID_SUBJECT},
                    )
                    pushed_count += 1
                except WebPushException as e:
                    status = (
                        e.response.status_code if e.response is not None
                        else None
                    )
                    # 404 / 410 from the Push service mean "this
                    # subscription is dead, stop trying". Per
                    # PWA_REFERENCE §14: delete it, don't retry.
                    if status in (404, 410):
                        db.delete_push_subscription_by_id(
                            int(sub_row["id"])
                        )
                        log.info(
                            "removed expired push subscription "
                            "(status=%s) member=%s",
                            status, member_id,
                        )
                    else:
                        log.warning(
                            "push delivery failed (status=%s) member=%s: %s",
                            status, member_id, e,
                        )

    db.insert_notification(
        notification_id=notif_id,
        member_id=member_id,
        type_=type_,
        title=title,
        body=body,
        data_json=data_json,
        pushed=pushed_count > 0,
        now=now,
    )
    return pushed_count


# ---------- Request/response models ----------
class SubscribeKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeRequest(BaseModel):
    endpoint: str
    keys: SubscribeKeys


class UnsubscribeRequest(BaseModel):
    endpoint: str


class PreferencesRequest(BaseModel):
    # {category: enabled}; ignored entries are skipped silently so the
    # UI can ship a category the backend hasn't deployed yet without
    # 400ing the whole preferences save.
    preferences: dict[str, bool]


# ---------- Router ----------
def build_router(db) -> APIRouter:
    """Returns the APIRouter for the /notifications/* endpoints.

    db is captured in the closure so we don't have to import it from
    coordinator.main (circular) or instantiate a fresh DB() per
    request (extra SQLite connection).
    """
    router = APIRouter(prefix="/notifications", tags=["notifications"])

    @router.get("/vapid-key")
    def get_vapid_key():
        """Public endpoint — the VAPID public key is meant to be
        distributed to clients. Returns ``{key: null}`` when the
        coordinator hasn't been configured, which the client uses
        to disable the subscribe button instead of attempting a
        guaranteed-failure subscription."""
        return {"key": VAPID_PUBLIC_KEY}

    @router.post("/subscribe")
    def subscribe(req: SubscribeRequest, request: Request):
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        if not req.endpoint:
            raise HTTPException(
                status_code=400, detail="endpoint is required",
            )
        if not is_safe_push_endpoint(req.endpoint):
            raise HTTPException(
                status_code=400,
                detail="endpoint must be an https push-service URL",
            )
        ua = request.headers.get("user-agent")
        sub_id = db.upsert_push_subscription(
            member_id=member.member_id,
            endpoint=req.endpoint,
            p256dh=req.keys.p256dh,
            auth=req.keys.auth,
            user_agent=ua,
            now=time.time(),
        )
        return {"id": sub_id, "ok": True}

    @router.delete("/subscribe")
    def unsubscribe(req: UnsubscribeRequest, request: Request):
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        removed = db.delete_push_subscription(
            member.member_id, req.endpoint,
        )
        return {"removed": removed}

    @router.get("")
    def list_notifications(
        request: Request, limit: int = 50, offset: int = 0,
    ):
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        # Cap limit so a misbehaving client can't load a member's
        # entire history in one go. 200 is generous; the UI paginates
        # by 50 in the standard list.
        if limit < 1: limit = 1
        if limit > 200: limit = 200
        if offset < 0: offset = 0
        rows = db.list_notifications(member.member_id, limit, offset)
        out = []
        for r in rows:
            data = None
            if r["data"]:
                try:
                    data = json.loads(r["data"])
                except (ValueError, TypeError):
                    data = None
            out.append({
                "notification_id": r["notification_id"],
                "type": r["type"],
                "title": r["title"],
                "body": r["body"],
                "data": data,
                "read": bool(r["read"]),
                "pushed": bool(r["pushed"]),
                "created_at": r["created_at"],
            })
        return {"notifications": out, "limit": limit, "offset": offset}

    @router.get("/unread-count")
    def unread_count(request: Request):
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        return {"count": db.unread_notification_count(member.member_id)}

    @router.put("/read-all")
    def mark_all_read(request: Request):
        # NOTE: This handler is registered BEFORE the /{notification_id}/read
        # handler so FastAPI's path matcher doesn't treat "read-all" as
        # a notification_id. (FastAPI matches in registration order
        # for paths with the same method.)
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        n = db.mark_all_notifications_read(member.member_id)
        return {"marked": n}

    @router.put("/{notification_id}/read")
    def mark_read(notification_id: str, request: Request):
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        ok = db.mark_notification_read(
            member.member_id, notification_id,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    @router.get("/preferences")
    def get_preferences(request: Request):
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        explicit = db.get_all_notification_preferences(member.member_id)
        # Merge with defaults so the UI gets a stable shape regardless
        # of whether the user has touched each toggle. Defaults are
        # True (opt-out model) for every known category.
        merged = {cat: True for cat in sorted(KNOWN_CATEGORIES)}
        merged.update(explicit)
        return {"preferences": merged, "categories": sorted(KNOWN_CATEGORIES)}

    @router.put("/preferences")
    def set_preferences(req: PreferencesRequest, request: Request):
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        applied = {}
        for cat, enabled in req.preferences.items():
            # Silently ignore categories the backend doesn't know
            # about so the UI can ship a new toggle a release ahead
            # of the backend deploy.
            if cat not in KNOWN_CATEGORIES:
                continue
            db.set_notification_preference(
                member.member_id, cat, bool(enabled),
            )
            applied[cat] = bool(enabled)
        return {"applied": applied}

    return router
