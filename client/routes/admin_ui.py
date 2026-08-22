"""Admin-only HTML pages: member roster and invite roster. Both gate
on the session cookie; previously these endpoints used the admin API
token and were kept off the public domain via Caddy."""
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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


@router.post("/admin/test-email")
async def admin_test_email(request: Request, to: str = Form(default="")):
    """Dashboard's "Email delivery test" card — sends a one-off test
    message via the coordinator's POST /admin/test-email so an admin
    can confirm Resend delivery (DNS verification, API key) without
    creating a throwaway signup just to trigger an email."""
    bearer, fail = await require_admin_session(request)
    if fail is not None:
        return fail
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(
            "/admin/test-email", json={"to": to.strip()}, timeout=15,
        )
    if r.status_code == 200:
        msg = f"Sent to {to.strip()} — check the inbox (and spam folder)."
        return RedirectResponse(
            f"/dashboard?test_email_ok=1&test_email_msg={quote(msg)}",
            status_code=303,
        )
    detail = "Send failed."
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    return RedirectResponse(
        f"/dashboard?test_email_ok=0&test_email_msg={quote(detail)}",
        status_code=303,
    )
