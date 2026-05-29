"""Machines page — the single place to manage paired PCs.

Replaces the old "Paired machines" + "Registered workers" tables on the
account page with one list (the coordinator merges member_tokens +
workers into one row per machine). Per machine the host can pause
contributing or set a daily uptime window; the coordinator enforces it
at /jobs/next.

The schedule editor saves via a same-origin PATCH (machines.js) that
this module proxies to the coordinator with the session bearer — the
browser never talks to the coordinator directly. Remove (unpair) is a
plain form POST so it works without JS.
"""
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from client.services import api_client, coordinator_client
from client.services.session import (
    identify,
    login_redirect,
    session_bearer,
)
from client.templating import templates

router = APIRouter()

# Curated IANA zones for the window picker. machines.js prepends the
# browser's own zone if it isn't already here, so this list just needs
# to cover the common cases without being a full tz database dump.
_COMMON_TIMEZONES = [
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Moscow",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney",
    "UTC",
]


@router.get("/machines", response_class=HTMLResponse)
async def machines_page(request: Request, flash: Optional[str] = None):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/machines")
    status, body = await api_client.fetch_safe(bearer=bearer, path="/me/machines")
    machines = body.get("machines", []) if status == 200 else []
    return templates.TemplateResponse(
        request,
        "machines.html.j2",
        {
            "me": me,
            "machines": machines,
            "timezones": _COMMON_TIMEZONES,
            "flash": flash,
        },
    )


@router.patch("/machines/{machine_id}/schedule")
async def update_schedule(machine_id: str, request: Request):
    """Proxy the schedule PATCH to the coordinator. Returns the
    coordinator's JSON + status straight through so machines.js can
    react (update the status pill, surface validation errors)."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.patch(
            f"/me/machines/{machine_id}/schedule", json=payload, timeout=5,
        )
    try:
        data = r.json()
    except ValueError:
        data = {"detail": "coordinator error"}
    return JSONResponse(data, status_code=r.status_code)


@router.post("/machines/{prefix}/unpair")
async def unpair_machine(prefix: str, request: Request):
    """Remove a machine: revokes its pairing credential server-side, so
    the next heartbeat 401s and the row drops off this page. Lifetime
    earnings are preserved coordinator-side."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/machines")
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(f"/me/machines/{prefix}/unpair", timeout=5)
    if r.status_code == 200:
        return RedirectResponse("/machines?flash=Machine removed.", status_code=303)
    detail = "Couldn't remove machine."
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    return RedirectResponse(f"/machines?flash={detail}", status_code=303)
