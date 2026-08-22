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

from client.services import api_client, coordinator_client
from client.services.session import (
    identify,
    login_redirect,
    session_bearer,
)
from client.templating import templates

router = APIRouter()


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, flash: Optional[str] = None):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")

    status, friends = await api_client.fetch_safe(
        bearer=bearer, path="/me/friends",
    )
    if status != 200:
        friends = {"open_invites": [], "accepted": []}
    # The machine list + schedule controls live on /machines now; the
    # account page only needs the partial-contributor count for the
    # activity-card nudge ("N of your machines are chat-only").
    machines_status, machines_body = await api_client.fetch_safe(
        bearer=bearer, path="/me/machines",
    )
    partial_count = (
        machines_body.get("partial_contributor_count", 0)
        if machines_status == 200 else 0
    )
    # Tier-engine status: 7d uptime, current/next-tier requirements,
    # below-threshold grace timer. The account page renders a
    # "Contribution status" card from this. Admins get a synthetic
    # response that the template treats as "engine does not apply".
    contrib_status_code, contrib_status = await api_client.fetch_safe(
        bearer=bearer, path="/me/contributor-status",
    )
    if contrib_status_code != 200:
        contrib_status = {}

    # Friends section is admin-only in v1 (see docs/auth-design.md —
    # tier-gated per-contributor invites come with the 3b.i engine).
    can_invite = me.get("role") == "admin"

    # Tier-based display allowance for the invite-form "% of your daily
    # allowance" tip. /me returns this already shaped; we just unpack
    # the two axes for the template so the JS data-attributes stay
    # primitive. None on either axis = unlimited (admin / PLATINUM),
    # which the JS treats as "no percentage tip".
    tier_quota = me.get("tier_quota") or {}

    return templates.TemplateResponse(
        request,
        "account.html.j2",
        {
            "me": me,
            "friends": friends,
            "partial_contributor_count": partial_count,
            "contrib_status": contrib_status,
            "tier_quota_tokens": tier_quota.get("tokens"),
            "tier_quota_images": tier_quota.get("images"),
            "earnings": me.get("earnings") or {},
            "usage_today": me.get("usage_today") or {},
            "can_invite": can_invite,
            "flash": flash,
        },
    )


@router.post("/account/workers/{worker_id}/forget")
async def account_forget_worker(worker_id: str, request: Request):
    """Delete a stale worker registration from the account page.
    Common case: the host rebuilt their PC, generating a new
    worker_id; the old one sits as an offline 'partial contributor'
    polluting the page. This wipes the row."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(
            f"/me/workers/{worker_id}/forget", timeout=5,
        )
    if r.status_code == 200:
        return RedirectResponse(
            "/account?flash=Worker forgotten.", status_code=303,
        )
    detail = "Couldn't forget worker."
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    return RedirectResponse(f"/account?flash={detail}", status_code=303)


@router.post("/account/machines/{prefix}/unpair")
async def account_unpair_machine(prefix: str, request: Request):
    """Web wrapper for /me/machines/<prefix>/unpair. Coordinator scopes
    the lookup to the caller's member, so a prefix that doesn't match
    anything they own 404s without leaking other members' state."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(f"/me/machines/{prefix}/unpair", timeout=5)
    if r.status_code == 200:
        return RedirectResponse(
            "/account?flash=PC unpaired.", status_code=303,
        )
    detail = "couldn't unpair"
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    return RedirectResponse(
        f"/account?flash={detail}", status_code=303,
    )


@router.post("/account/resend-verification")
async def account_resend_verification(request: Request):
    """Web wrapper for POST /me/resend-verification — the account
    page's "Resend verification email" button for a signup member who
    never got (or lost) the original link."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post("/me/resend-verification", timeout=10)
    if r.status_code == 200:
        flash = (
            "Your email is already verified."
            if r.json().get("email_verified")
            else "Verification email sent — check your inbox."
        )
        return RedirectResponse(f"/account?flash={flash}", status_code=303)
    detail = "Couldn't send verification email."
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    return RedirectResponse(f"/account?flash={detail}", status_code=303)


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
    daily_quota_images: str = Form(""),
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
    if daily_quota_images.strip():
        try:
            body["daily_quota_images"] = int(daily_quota_images)
        except ValueError:
            return RedirectResponse(
                "/account?flash=Daily image cap must be a number.",
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


@router.post("/account/friends/{friend_member_id}/quota")
async def account_update_friend_quota(
    friend_member_id: str,
    request: Request,
    daily_quota_tokens: str = Form(""),
    daily_quota_images: str = Form(""),
):
    """Host updates an accepted invitee's two-dimensional cap from the
    accepted-invitees row. Empty input = unlimited for that axis;
    the form always submits both so the coordinator sees the new
    full-state pair."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")

    def parse_optional_int(raw: str, label: str):
        if not raw.strip():
            return None, None
        try:
            return int(raw), None
        except ValueError:
            return None, f"{label} must be a number."

    tokens_val, err = parse_optional_int(daily_quota_tokens, "Daily token cap")
    if err:
        return RedirectResponse(f"/account?flash={err}", status_code=303)
    images_val, err = parse_optional_int(daily_quota_images, "Daily image cap")
    if err:
        return RedirectResponse(f"/account?flash={err}", status_code=303)

    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(
            f"/me/friends/{friend_member_id}/quota",
            json={
                "daily_quota_tokens": tokens_val,
                "daily_quota_images": images_val,
            },
            timeout=5,
        )
    if r.status_code == 200:
        return RedirectResponse(
            "/account?flash=Friend cap updated.", status_code=303,
        )
    detail = "Couldn't update friend cap."
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    return RedirectResponse(f"/account?flash={detail}", status_code=303)


@router.post("/account/friends/{friend_member_id}/revoke")
async def account_revoke_friend(friend_member_id: str, request: Request):
    """Host revokes an accepted invitee's access. Single-button POST —
    the row in the accepted-invitees table renders a 'revoke' button
    that hits this route."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/account")
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(
            f"/me/friends/{friend_member_id}/revoke", timeout=5,
        )
    if r.status_code == 200:
        body = {}
        try:
            body = r.json()
        except ValueError:
            pass
        if body.get("was_already_revoked"):
            return RedirectResponse(
                "/account?flash=Friend was already revoked.",
                status_code=303,
            )
        return RedirectResponse(
            "/account?flash=Friend revoked.", status_code=303,
        )
    detail = "Couldn't revoke friend."
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    return RedirectResponse(f"/account?flash={detail}", status_code=303)


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
async def agent_pair_landing(request: Request):
    """Browser-side of the agent-pairing handoff. The Windows agent opens
    the user's default browser to this URL (no secret in it) and prints a
    short code on its own screen. The user types that code here to
    approve. We require an authenticated session — if they aren't signed
    in we bounce to /login and bring them right back."""
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/agent/pair")
    return templates.TemplateResponse(
        request, "agent_pair.html.j2", {"state": "enter", "me": me},
    )


@router.post("/agent/pair")
async def agent_pair_confirm(request: Request, user_code: str = Form("")):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/agent/pair")
    async with coordinator_client._client(bearer=bearer) as c:
        r = await c.post(
            "/agents/pair/confirm", json={"user_code": user_code}, timeout=5,
        )
    if r.status_code == 200:
        return templates.TemplateResponse(
            request, "agent_pair.html.j2", {"state": "confirmed", "me": me},
        )
    detail = "couldn't confirm"
    try:
        detail = r.json().get("detail", detail)
    except ValueError:
        pass
    # 404/410 → the code is gone (expired or already used): dead end.
    # Anything else (e.g. 400 mistyped) → re-show the form to retry.
    if r.status_code in (404, 410):
        return templates.TemplateResponse(
            request,
            "agent_pair.html.j2",
            {"state": "expired", "me": me, "error_detail": detail},
            status_code=r.status_code,
        )
    return templates.TemplateResponse(
        request,
        "agent_pair.html.j2",
        {"state": "enter", "me": me, "error_detail": detail,
         "user_code": user_code},
    )
