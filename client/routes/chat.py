"""Chat shell and admin dashboard. Both gate on session — the chat
route redirects anonymous visitors to /login; the dashboard additionally
requires the admin role."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from client.services import coordinator_client
from client.services.guards import require_admin_session
from client.services.session import identify, login_redirect, session_bearer
from client.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    bearer = session_bearer(request)
    if not bearer or not await identify(bearer):
        return login_redirect("/")
    return templates.TemplateResponse(request, "chat.html.j2", {})


@router.get("/admin")
def admin_redirect():
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/dashboard")
    if me.get("role") != "admin":
        return HTMLResponse(
            "<h1>403 — admin only</h1><p>Dashboard requires the admin role.</p>"
            '<p><a href="/">Back to chat</a></p>',
            status_code=403,
        )
    async with coordinator_client._client(bearer=bearer) as c:
        metrics = (await c.get("/metrics", timeout=5)).json()
        workers_body = (await c.get("/workers", timeout=5)).json()
        earnings_body = (await c.get("/earnings", timeout=5)).json()

    workers = workers_body["workers"]
    active_workers_count = sum(1 for w in workers if w["status"] == "online")
    total_earnings = sum(w.get("total_usd", 0) for w in workers)
    top_earners = sorted(
        earnings_body.get("workers", []) or [],
        key=lambda x: x["total_usd"],
        reverse=True,
    )[:5]
    return templates.TemplateResponse(
        request,
        "dashboard.html.j2",
        {
            "metrics": metrics,
            "workers": workers,
            "active_workers_count": active_workers_count,
            "total_earnings": total_earnings,
            "top_earners": top_earners,
        },
    )
