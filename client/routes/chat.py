"""Chat shell and admin dashboard. Anonymous visitors to / get a public
landing page (not a /login redirect — a stranger with no context needs
somewhere to land); the dashboard requires the admin role."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import CLIENT_CACHE_VERSION

from client.services import api_client
from client.services.guards import require_admin_session
from client.services.session import identify, login_redirect, session_bearer
from client.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    bearer = session_bearer(request)
    if not bearer or not await identify(bearer):
        return templates.TemplateResponse(request, "landing.html.j2", {})
    return templates.TemplateResponse(
        request, "chat.html.j2",
        {"cache_version": CLIENT_CACHE_VERSION},
    )


@router.get("/offline", response_class=HTMLResponse)
async def offline(request: Request):
    # Static fallback page returned by the service worker when a
    # navigation can't reach the network. Has no session check — the
    # whole point is that we can render it without contacting the
    # coordinator. Lives here (not in app.py) so it can extend
    # base.html.j2 with the rest of the templates.
    return templates.TemplateResponse(request, "offline.html.j2", {})


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
    # Three sequential reads — fetch_safe gives us a status check we
    # don't currently act on but lets the facade own all the httpx
    # plumbing. If any starts feeling slow, parallelize with
    # asyncio.gather() in a follow-up; for now sequential matches
    # the historical behavior exactly.
    _, metrics = await api_client.fetch_safe(bearer=bearer, path="/metrics")
    _, workers_body = await api_client.fetch_safe(bearer=bearer, path="/workers")
    _, earnings_body = await api_client.fetch_safe(bearer=bearer, path="/earnings")

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
