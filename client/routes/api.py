"""BFF (backend-for-frontend) proxy endpoints. Browser JS hits these
instead of the coordinator directly so we avoid CORS and so the
session cookie is the only credential the page ever has to handle.

Every /api/* proxy forwards the caller's session bearer to the
coordinator. Requests without a session are rejected 401; the JS will
notice and redirect to /login.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from client.services import coordinator_client
from client.services.guards import require_session_bearer
from client.services.session import identify

router = APIRouter(prefix="/api")


@router.post("/generate")
async def proxy_generate(payload: dict, request: Request):
    bearer = require_session_bearer(request)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post("/generate", json=payload, timeout=10)
    # Forward the coordinator's body verbatim instead of wrapping it
    # in another HTTPException — otherwise a 503 like
    # `{"detail":"No community members are available..."}` would come
    # out as `{"detail":"{\"detail\":\"No community...\"}"}` (double-
    # encoded) and the chat UI couldn't render the message cleanly.
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text}
    return JSONResponse(body, status_code=r.status_code)


@router.get("/result/{job_id}")
async def proxy_result(job_id: str, request: Request):
    bearer = require_session_bearer(request)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get(f"/result/{job_id}", timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@router.get("/images/{name}")
async def proxy_image(name: str, request: Request):
    """Forward a generated PNG with the session's bearer. The browser
    can't send Authorization headers from <img> tags, so the chat UI
    references images at /api/images/<name>; this proxy attaches the
    coordinator bearer the same way other /api/* routes do. Coordinator
    enforces conversation ownership before returning the bytes.

    No need to JSON-decode the body — pass the PNG through as raw bytes
    with the upstream content-type."""
    bearer = require_session_bearer(request)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get(f"/images/{name}", timeout=30)
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "image/png"),
    )


@router.get("/me")
async def proxy_me(request: Request):
    bearer = require_session_bearer(request)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get("/me", timeout=5)
    return JSONResponse(r.json(), status_code=r.status_code)


# Workers / earnings / metrics are operational data — admin only.
async def _admin_only_proxy(request: Request, path: str) -> JSONResponse:
    bearer = require_session_bearer(request)
    me = await identify(bearer)
    if me is None or me.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get(path, timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


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
    bearer = require_session_bearer(request)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get("/conversations", timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@router.post("/conversations")
async def proxy_create_conversation(payload: dict, request: Request):
    bearer = require_session_bearer(request)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post("/conversations", json=payload, timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@router.get("/conversations/{conversation_id}")
async def proxy_get_conversation(conversation_id: str, request: Request):
    bearer = require_session_bearer(request)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get(f"/conversations/{conversation_id}", timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@router.delete("/conversations/{conversation_id}")
async def proxy_archive_conversation(conversation_id: str, request: Request):
    bearer = require_session_bearer(request)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.delete(f"/conversations/{conversation_id}", timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@router.post("/messages/{message_id}/retry")
async def proxy_retry_message(message_id: str, request: Request):
    bearer = require_session_bearer(request)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(f"/messages/{message_id}/retry", timeout=10)
    # Surface the Retry-After header straight through so the JS can read it.
    headers = {}
    if "retry-after" in r.headers:
        headers["Retry-After"] = r.headers["retry-after"]
    return JSONResponse(r.json(), status_code=r.status_code, headers=headers)
