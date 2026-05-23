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

    body: dict = {}
    if daily_quota_tokens.strip():
        try:
            body["daily_quota_tokens"] = int(daily_quota_tokens)
        except ValueError:
            return RedirectResponse(
                "/account?flash=Daily quota must be a number.",
                status_code=303,
            )
    if invitee_email.strip():
        body["invitee_email"] = invitee_email.strip()
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
