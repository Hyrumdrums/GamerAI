"""BFF (backend-for-frontend) proxy endpoints. Browser JS hits these
instead of the coordinator directly so we avoid CORS and so the
session cookie is the only credential the page ever has to handle.

Every /api/* proxy forwards the caller's session bearer to the
coordinator. Requests without a session are rejected 401; the JS will
notice and redirect to /login.

All the underlying httpx wiring lives in
``client/services/api_client.py`` — these handlers should stay short
data-shaping wrappers that name the path and let the facade do the
forwarding.
"""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from client.services import api_client
from client.services.guards import require_session_bearer
from client.services.session import identify

router = APIRouter(prefix="/api")


@router.post("/generate")
async def proxy_generate(payload: dict, request: Request):
    # text_fallback so a 503 like {"detail":"No community members are
    # available right now"} reaches the chat UI verbatim even if the
    # coordinator's body parser hiccups — without that, the UI
    # double-encodes and shows raw JSON instead of the message.
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="POST", path="/generate",
        json=payload, text_fallback=True,
    )


@router.get("/result/{job_id}")
async def proxy_result(job_id: str, request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="GET", path=f"/result/{job_id}",
    )


@router.post("/cancel/{job_id}")
async def proxy_cancel(job_id: str, request: Request):
    """Member-initiated cancel. The coordinator marks the job
    cancelled and the worker's eventual /complete gets 410'd, so
    even though the worker keeps grinding the result is dropped."""
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="POST", path="/jobs/cancel",
        json={"job_id": job_id}, text_fallback=True,
    )


@router.post("/displayed/{job_id}")
async def proxy_displayed(job_id: str, payload: dict, request: Request):
    """Fire-and-forget client-rendered ping. Closes the end-to-end
    timing trail on the jobs row; debug-only data so a network failure
    here is harmless."""
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="POST", path="/jobs/displayed",
        json={
            "job_id": job_id,
            "displayed_at_ms": float(payload.get("displayed_at_ms") or 0),
        },
        timeout=5,
    )


@router.get("/images/{name}")
async def proxy_image(name: str, request: Request):
    """Forward a generated PNG with the session's bearer. The browser
    can't send Authorization headers from <img> tags, so the chat UI
    references images at /api/images/<name>; this proxy attaches the
    coordinator bearer the same way other /api/* routes do. Coordinator
    enforces conversation ownership before returning the bytes.

    Long-lived + immutable Cache-Control: `name` is a job id, and a
    given job's PNG is written once and never changes, so the browser
    can treat a copy it already fetched as good forever instead of
    re-downloading the full image on every page navigation (that
    re-fetch was the cause of images visibly re-painting row-by-row on
    every chat load -- this route previously forwarded no caching
    headers at all). `private` because it's proxied per-session, not
    something a shared/intermediate cache should store."""
    return await api_client.proxy_raw(
        bearer=require_session_bearer(request),
        method="GET", path=f"/images/{name}",
        timeout=30, default_content_type="image/png",
        cache_control="private, max-age=31536000, immutable",
    )


@router.get("/me")
async def proxy_me(request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="GET", path="/me", timeout=5,
    )


# Workers / earnings / metrics are operational data — admin only.
async def _admin_only_proxy(request: Request, path: str):
    bearer = require_session_bearer(request)
    me = await identify(bearer)
    if me is None or me.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return await api_client.proxy_json(
        bearer=bearer, method="GET", path=path,
    )


@router.get("/workers")
async def proxy_workers(request: Request):
    return await _admin_only_proxy(request, "/workers")


@router.get("/earnings")
async def proxy_earnings(request: Request):
    return await _admin_only_proxy(request, "/earnings")


@router.get("/metrics")
async def proxy_metrics(request: Request):
    return await _admin_only_proxy(request, "/metrics")


@router.get("/conversations")
async def proxy_list_conversations(request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="GET", path="/conversations",
    )


@router.post("/conversations")
async def proxy_create_conversation(payload: dict, request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="POST", path="/conversations", json=payload,
    )


@router.get("/conversations/{conversation_id}")
async def proxy_get_conversation(conversation_id: str, request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="GET", path=f"/conversations/{conversation_id}",
    )


@router.delete("/conversations/{conversation_id}")
async def proxy_archive_conversation(conversation_id: str, request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="DELETE", path=f"/conversations/{conversation_id}",
    )


@router.post("/uploads")
async def proxy_upload(
    request: Request,
    conversation_id: str = Form(...),
    file: UploadFile = File(...),
):
    content = await file.read()
    return await api_client.proxy_upload(
        bearer=require_session_bearer(request),
        path="/uploads",
        filename=file.filename or "upload",
        content=content,
        content_type=file.content_type,
        form={"conversation_id": conversation_id},
        timeout=30,
    )


@router.get("/uploads/{conversation_id}")
async def proxy_list_uploads(conversation_id: str, request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="GET", path=f"/uploads/{conversation_id}",
    )


@router.post("/messages/{message_id}/retry")
async def proxy_retry_message(message_id: str, request: Request):
    # forward_response_headers so the JS retry cooldown can read
    # Retry-After off the 429 response (see streamingEngine.js).
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="POST", path=f"/messages/{message_id}/retry",
        forward_response_headers=("retry-after",),
    )


# ---------- push notifications (Phase 6 of pwa-refactor.txt) ----------
# Eight BFF proxies sitting in front of coordinator/notifications.py.
# The VAPID public-key fetch is public (coordinator marks it so too);
# everything else attaches the session bearer.

@router.get("/notifications/vapid-key")
async def proxy_vapid_key():
    """Public — no session required. The coordinator returns
    ``{key: null}`` when VAPID isn't configured, which the JS uses to
    skip showing the opt-in banner instead of attempting a doomed
    subscribe."""
    return await api_client.proxy_json(
        bearer=None, method="GET", path="/notifications/vapid-key",
        timeout=5,
    )


@router.post("/notifications/subscribe")
async def proxy_subscribe(payload: dict, request: Request):
    # Forward the browser's User-Agent so the coordinator can label
    # which device this subscription belongs to (Chrome on Pixel vs.
    # Safari on iPhone) — useful for the future "manage devices" view
    # and harmless if the header is absent.
    ua = request.headers.get("user-agent")
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="POST", path="/notifications/subscribe",
        json=payload,
        headers={"User-Agent": ua} if ua else None,
    )


@router.delete("/notifications/subscribe")
async def proxy_unsubscribe(payload: dict, request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="DELETE", path="/notifications/subscribe",
        json=payload,
    )


@router.get("/notifications")
async def proxy_list_notifications(
    request: Request, limit: int = 50, offset: int = 0,
):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="GET", path="/notifications",
        params={"limit": limit, "offset": offset},
    )


@router.get("/notifications/unread-count")
async def proxy_unread_count(request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="GET", path="/notifications/unread-count",
        timeout=5,
    )


@router.put("/notifications/read-all")
async def proxy_mark_all_read(request: Request):
    # NOTE: registered before /{id}/read so FastAPI doesn't capture
    # 'read-all' as a notification_id. Matches the ordering in
    # coordinator/notifications.py.
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="PUT", path="/notifications/read-all", timeout=5,
    )


@router.put("/notifications/{notification_id}/read")
async def proxy_mark_read(notification_id: str, request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="PUT", path=f"/notifications/{notification_id}/read",
        timeout=5,
    )


@router.get("/notifications/preferences")
async def proxy_get_preferences(request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="GET", path="/notifications/preferences", timeout=5,
    )


@router.put("/notifications/preferences")
async def proxy_put_preferences(payload: dict, request: Request):
    return await api_client.proxy_json(
        bearer=require_session_bearer(request),
        method="PUT", path="/notifications/preferences",
        json=payload, timeout=5,
    )
