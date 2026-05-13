"""Public invite-redemption flow. No session required — the invite code
itself is the credential. ToS acceptance is enforced both client-side
(HTML5 `required`) and server-side (we refuse to forward to the
coordinator without the checkbox)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from client.services import coordinator_client
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
    return templates.TemplateResponse(
        request,
        "redeem.html.j2",
        {
            "contributor": str(contributor),
            "cap": cap_text,
            "expires_at_text": _format_expiry(details.get("expires_at")),
        },
    )


@router.post("/invite/{code}", response_class=HTMLResponse)
async def invite_accept(
    code: str,
    request: Request,
    invitee_email: str = Form(default=""),
    tos_accepted: str = Form(default=""),
):
    # The ToS checkbox is required client-side AND server-side. Belt-and-
    # suspenders: a savvy user could remove the `required` attribute via
    # devtools, so we refuse to forward to the coordinator without it.
    if tos_accepted != "on":
        return _render_error(
            request,
            "The community terms must be accepted to redeem this "
            "invite. Go back, check the box, and try again.",
            400,
        )
    body = {
        "invitee_email": invitee_email.strip() or None,
        "tos_accepted": True,
    }
    async with coordinator_client._public_client() as c:
        r = await c.post(f"/invites/{code}/accept", json=body, timeout=5)
    if r.status_code == 404:
        return _render_error(request, "The invite code was not found.", 404)
    if r.status_code == 410:
        detail = r.json().get("detail", "no longer redeemable")
        return _render_error(request, f"This invite is {detail}.", 410)
    if r.status_code >= 400:
        return _render_error(
            request,
            f"Accept failed (status {r.status_code}). {r.text[:300]}",
            r.status_code,
        )
    body_json = r.json()
    cap = body_json.get("daily_quota_tokens")
    cap_text = str(cap) if cap else "unlimited"
    return templates.TemplateResponse(
        request,
        "redeem_done.html.j2",
        {
            "token": body_json["token"],
            "member_id": body_json["member_id"],
            "cap": cap_text,
        },
    )
