"""Public invite-redemption flow. No session required — the invite code
itself is the credential.

The redemption page collects username + email + password + ToS-accepted
and posts them to the coordinator. On success the new member is
auto-signed-in via the session cookie (no token reveal, no copy-paste).
ToS acceptance is enforced both client-side (HTML5 `required`) and
server-side."""
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from client.services import coordinator_client
from client.services.session import set_session_cookie
from client.templating import templates

router = APIRouter()


def _render_error(request: Request, detail: str, status_code: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "redeem_error.html.j2",
        {"detail": detail},
        status_code=status_code,
    )


def _format_expiry(expires_at) -> str:
    if not expires_at:
        return ""
    return datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _render_form(
    request: Request,
    *,
    contributor: str,
    cap_text: str,
    expires_at_text: str,
    error: str | None,
    username: str = "",
    email: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "redeem.html.j2",
        {
            "contributor": contributor,
            "cap": cap_text,
            "expires_at_text": expires_at_text,
            "error": error,
            "username": username,
            "email": email,
        },
        status_code=status_code,
    )


@router.get("/invite/{code}", response_class=HTMLResponse)
async def invite_landing(code: str, request: Request):
    async with coordinator_client._public_client() as c:
        r = await c.get(f"/invites/{code}", timeout=5)
    if r.status_code == 404:
        return _render_error(request, "The invite code was not found.", 404)
    if r.status_code >= 400:
        return _render_error(
            request,
            f"The coordinator returned status {r.status_code}.",
            r.status_code,
        )
    details = r.json()
    state = details.get("state")
    if state and state != "open":
        return _render_error(request, f"This invite is {state}.", 410)

    contributor = (
        details.get("contributor_email")
        or details.get("contributor_member_id")
        or "a GamerAI contributor"
    )
    cap = details.get("daily_quota_tokens")
    cap_text = f"{cap} tokens/day" if cap else "unlimited"
    return _render_form(
        request,
        contributor=str(contributor),
        cap_text=cap_text,
        expires_at_text=_format_expiry(details.get("expires_at")),
        error=None,
        email=details.get("invitee_email") or "",
    )


async def _fetch_invite_form_context(code: str) -> dict:
    """Refetch the invite details so an error re-render keeps the host
    name and quota in the page header. Falls back to a generic context
    if the second fetch fails — better to show the form with stale
    decoration than to surface a confusing 500."""
    try:
        async with coordinator_client._public_client() as c:
            r = await c.get(f"/invites/{code}", timeout=5)
        if r.status_code == 200:
            d = r.json()
            cap = d.get("daily_quota_tokens")
            return {
                "contributor": str(
                    d.get("contributor_email")
                    or d.get("contributor_member_id")
                    or "a GamerAI contributor"
                ),
                "cap_text": f"{cap} tokens/day" if cap else "unlimited",
                "expires_at_text": _format_expiry(d.get("expires_at")),
            }
    except Exception:
        pass
    return {
        "contributor": "a GamerAI contributor",
        "cap_text": "unlimited",
        "expires_at_text": "",
    }


@router.post("/invite/{code}")
async def invite_accept(
    code: str,
    request: Request,
    username: str = Form(default=""),
    invitee_email: str = Form(default=""),
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
    tos_accepted: str = Form(default=""),
):
    form_ctx = await _fetch_invite_form_context(code)

    def render_form_error(message: str, status_code: int = 400) -> HTMLResponse:
        return _render_form(
            request,
            contributor=form_ctx["contributor"],
            cap_text=form_ctx["cap_text"],
            expires_at_text=form_ctx["expires_at_text"],
            error=message,
            username=username,
            email=invitee_email,
            status_code=status_code,
        )

    # The ToS checkbox is required client-side AND server-side. Belt-and-
    # suspenders: a savvy user could remove the `required` attribute via
    # devtools, so we refuse to forward to the coordinator without it.
    if tos_accepted != "on":
        return render_form_error(
            "The community terms must be accepted before claiming an "
            "account. Check the box and try again."
        )
    if password != password_confirm:
        return render_form_error("Passwords didn't match — try again.")

    body = {
        "username": username.strip(),
        "password": password,
        "invitee_email": invitee_email.strip(),
        "tos_accepted": True,
    }
    async with coordinator_client._public_client() as c:
        r = await c.post(f"/invites/{code}/accept", json=body, timeout=5)
    if r.status_code == 404:
        return _render_error(request, "The invite code was not found.", 404)
    if r.status_code == 410:
        detail = r.json().get("detail", "no longer redeemable")
        return _render_error(request, f"This invite is {detail}.", 410)
    if r.status_code == 409:
        # Username collision — re-render the form so the user can pick
        # another without re-entering everything else.
        detail = r.json().get("detail", "that username is taken")
        return render_form_error(detail, status_code=409)
    if r.status_code == 400:
        detail = r.json().get("detail", "validation failed")
        return render_form_error(detail)
    if r.status_code >= 400:
        return _render_error(
            request,
            f"Accept failed (status {r.status_code}). {r.text[:300]}",
            r.status_code,
        )
    body_json = r.json()
    # Auto-login: drop the bearer in the session cookie and send them
    # straight to the chat page. No token reveal, no copy-paste.
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, body_json["token"])
    return response
