"""Public, invite-free web signup.

Mirrors invites.py's accept flow (collect the form, forward to the
coordinator, auto-login via session cookie on success) but posts to
the coordinator's own public ``POST /signup`` instead of an invite
code. Verification is link-based, not a typed code: the coordinator
emails a confirming link (see coordinator's GET /verify-email) once
Resend is configured, and account.html.j2 carries the ongoing
verified/not-verified status + a resend button — this route just
handles first creation.

The browser-facing page lives at ``/join``, NOT ``/signup`` — Caddy
routes the public domain's bare ``/signup`` straight through to the
coordinator container (see infra/Caddyfile), because the Windows
agent's console signup flow POSTs JSON directly to
``{coordinator_url}/signup`` and expects the coordinator's own
handler, not this form-encoded one. Reusing the same path here would
have silently swallowed the agent's API calls behind this page's
Form(...) parsing. This route still calls the coordinator's
``/signup`` internally (container-to-container, never through Caddy)
— only the browser-facing URL differs."""
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from client.services import coordinator_client
from client.services.session import identify, session_bearer, set_session_cookie
from client.templating import templates

router = APIRouter()


def _render_form(
    request: Request,
    *,
    error: str | None,
    username: str = "",
    email: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "signup.html.j2",
        {"error": error, "username": username, "email": email},
        status_code=status_code,
    )


@router.get("/join", response_class=HTMLResponse)
async def signup_page(request: Request):
    # Already signed in? Nothing to do here.
    bearer = session_bearer(request)
    if bearer and await identify(bearer):
        return RedirectResponse("/", status_code=303)
    return _render_form(request, error=None)


@router.post("/join")
async def signup_submit(
    request: Request,
    username: str = Form(default=""),
    email: str = Form(default=""),
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
    tos_accepted: str = Form(default=""),
):
    # Same belt-and-suspenders as invite_accept: HTML5 `required`
    # covers most browsers, this covers devtools tampering.
    if tos_accepted != "on":
        return _render_form(
            request,
            error=(
                "The community terms must be accepted before creating "
                "an account. Check the box and try again."
            ),
            username=username, email=email, status_code=400,
        )
    if password != password_confirm:
        return _render_form(
            request, error="Passwords didn't match — try again.",
            username=username, email=email, status_code=400,
        )

    body = {
        "username": username.strip(),
        "password": password,
        "email": email.strip(),
        "tos_accepted": True,
    }
    async with coordinator_client._public_client() as c:
        r = await c.post("/signup", json=body, timeout=5)
    if r.status_code == 409:
        detail = r.json().get("detail", "that username or email is taken")
        return _render_form(
            request, error=detail, username=username, email=email,
            status_code=409,
        )
    if r.status_code in (400, 429):
        detail = r.json().get("detail", "signup was rejected")
        return _render_form(
            request, error=detail, username=username, email=email,
            status_code=r.status_code,
        )
    if r.status_code >= 400:
        return _render_form(
            request,
            error=f"Signup failed (status {r.status_code}). {r.text[:300]}",
            username=username, email=email, status_code=r.status_code,
        )

    body_json = r.json()
    # Auto-login: drop the bearer in the session cookie, same as
    # invite-accept — no token reveal, no copy-paste.
    if body_json.get("email_verified"):
        response = RedirectResponse("/", status_code=303)
    else:
        # Contributing (pairing the agent) and browsing the account
        # page are never gated by verification — only chat/image/voice
        # are — so landing on /account (not straight into chat, which
        # would just 403 on the first message) is the honest next step.
        msg = (
            f"Account created! We sent a confirmation link to "
            f"{email.strip()} — click it to unlock chat, image "
            f"generation, and voice."
        )
        response = RedirectResponse(
            f"/account?flash={quote(msg)}", status_code=303,
        )
    set_session_cookie(response, body_json["token"])
    return response
