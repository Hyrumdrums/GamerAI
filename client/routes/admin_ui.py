"""Admin-only HTML pages: member roster and invite roster. Both gate
on the session cookie; previously these endpoints used the admin API
token and were kept off the public domain via Caddy."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from client.services import coordinator_client
from client.services.guards import require_admin_session
from client.templating import templates

router = APIRouter()


@router.get("/admin/members", response_class=HTMLResponse)
async def admin_members(request: Request):
    bearer, fail = await require_admin_session(request)
    if fail is not None:
        return fail
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get("/admin/members", timeout=5)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return templates.TemplateResponse(
        request,
        "admin_members.html.j2",
        {"members": r.json()["members"]},
    )


@router.get("/admin/invites", response_class=HTMLResponse)
async def admin_invites(request: Request):
    bearer, fail = await require_admin_session(request)
    if fail is not None:
        return fail
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get("/invites?all=true", timeout=5)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return templates.TemplateResponse(
        request,
        "admin_invites.html.j2",
        {"invites": r.json()["invites"]},
    )
