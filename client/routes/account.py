"""Per-member account page.

One section per concern:

- **Account** — username, email, password change.
- **Your host** *(invitees only)* — who vouched for them, with an
  honest one-liner about the host's powers.
- **Friends** *(hosts only — admin in v1)* — list of invitees plus the
  open-invite codes, with copy/revoke actions. Other roles get a
  placeholder + a "want to host?" pitch.
- **This PC** — placeholder; populated when agent pairing lands.

Server-renders everything. The friends section is small enough to fetch
synchronously; if it ever grows large enough to feel slow, we'll move
it to an async /api/me/friends fetch.
"""
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from client.services import coordinator_client
from client.services.session import (
    identify,
    login_redirect,
    session_bearer,
)
from client.templating import templates

router = APIRouter()


async def _coord_get(bearer: str, path: str) -> tuple[int, dict]:
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get(path, timeout=5)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"detail": r.text}


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, flash: Optional[str] = None):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")

    status, friends = await _coord_get(bearer, "/me/friends")
    if status != 200:
        friends = {"open_invites": [], "accepted": []}

    # Friends section is admin-only in v1 (see docs/auth-design.md —
    # tier-gated per-contributor invites come with the 3b.i engine).
    can_invite = me.get("role") == "admin"

    return templates.TemplateResponse(
        request,
        "account.html.j2",
        {
            "me": me,
            "friends": friends,
            "can_invite": can_invite,
            "flash": flash,
        },
    )


@router.post("/account/password")
async def account_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")

    if new_password != new_password_confirm:
        return RedirectResponse(
            "/account?flash=" + "Passwords didn't match.", status_code=303,
        )

    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(
            "/me/password",
            json={
                "current_password": current_password,
                "new_password": new_password,
            },
            timeout=5,
        )
    if r.status_code == 200:
        return RedirectResponse(
            "/account?flash=" + "Password updated.", status_code=303,
        )
    detail = "Couldn't update password."
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    return RedirectResponse(
        "/account?flash=" + detail, status_code=303,
    )


@router.post("/account/invites")
async def account_create_invite(
    request: Request,
    daily_quota_tokens: str = Form(""),
    invitee_email: str = Form(""),
    expires_hours: str = Form(""),
):
    """Create an invite from the account page. Admin-only in v1 (the
    coordinator enforces this independently). Future tier-gated path:
    contributors at BRONZE+ get invite slots based on their tier."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")

    email = invitee_email.strip()
    if not email or "@" not in email:
        return RedirectResponse(
            "/account?flash=Friend's email is required.",
            status_code=303,
        )
    body: dict = {"invitee_email": email}
    if daily_quota_tokens.strip():
        try:
            body["daily_quota_tokens"] = int(daily_quota_tokens)
        except ValueError:
            return RedirectResponse(
                "/account?flash=Daily quota must be a number.",
                status_code=303,
            )
    if expires_hours.strip():
        try:
            body["expires_hours"] = float(expires_hours)
        except ValueError:
            return RedirectResponse(
                "/account?flash=Expiry must be a number of hours.",
                status_code=303,
            )

    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post("/invites", json=body, timeout=5)
    if r.status_code == 200:
        return RedirectResponse(
            "/account?flash=Invite created.", status_code=303,
        )
    detail = "Couldn't create invite."
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    return RedirectResponse(
        f"/account?flash={detail}", status_code=303,
    )


@router.post("/account/invites/{code}/revoke")
async def account_revoke_invite(code: str, request: Request):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")
    async with coordinator_client._client(bearer=bearer) as c:
        await c.post(f"/invites/{code}/revoke", timeout=5)
    return RedirectResponse(
        "/account?flash=Invite revoked.", status_code=303,
    )


@router.get("/contribute", response_class=HTMLResponse)
async def contribute_page(request: Request):
    """Onboarding pitch for non-contributors: how the network works,
    how contributing earns access for you and your invitees, and the
    Windows-only TL;DR for actually installing the agent. Reachable
    while signed in (topbar CTA) and anonymously (so a curious
    visitor following a link from somewhere can read it without an
    account)."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    return templates.TemplateResponse(
        request,
        "contribute.html.j2",
        {"me": me},
    )


@router.get("/agent/pair", response_class=HTMLResponse)
async def agent_pair_landing(request: Request, code: str = ""):
    """Browser-side of the agent-pairing handoff. The Windows agent
    opens the user's default browser to this URL. We require an
    authenticated session — if they aren't signed in we bounce to
    /login with this URL as the ``next`` param so they come right back
    after sign-in."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect(f"/agent/pair?code={code}")
    if not code.startswith("pair_"):
        return templates.TemplateResponse(
            request,
            "agent_pair.html.j2",
            {"state": "invalid", "me": me, "code": code},
            status_code=400,
        )
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.get(f"/agents/pair/{code}", timeout=5)
    if r.status_code == 404:
        return templates.TemplateResponse(
            request,
            "agent_pair.html.j2",
            {"state": "expired", "me": me, "code": code},
            status_code=404,
        )
    info = r.json() if r.status_code == 200 else {}
    return templates.TemplateResponse(
        request,
        "agent_pair.html.j2",
        {"state": info.get("state", "unknown"), "me": me, "code": code,
         "expires_at": info.get("expires_at")},
    )


@router.post("/agent/pair")
async def agent_pair_confirm(request: Request, code: str = Form(...)):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect(f"/agent/pair?code={code}")
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(f"/agents/pair/{code}/confirm", timeout=5)
    if r.status_code == 200:
        return templates.TemplateResponse(
            request,
            "agent_pair.html.j2",
            {"state": "confirmed", "me": me, "code": code},
        )
    detail = "couldn't confirm"
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    status = "expired" if r.status_code in (404, 410) else "error"
    return templates.TemplateResponse(
        request,
        "agent_pair.html.j2",
        {"state": status, "me": me, "code": code, "error_detail": detail},
        status_code=r.status_code,
    )
