"""Session login/logout.

Two POST entry points:

- ``POST /login`` — username + password. The web layer forwards to the
  coordinator's ``/login``, gets back a fresh bearer token, and stamps
  it into the session cookie. This is the primary path for invitees.
- ``POST /login/token`` — paste a bearer token. Hidden under a
  collapsible on the login page; used by admins migrating from
  token-only auth and by anyone whose tooling already speaks the API.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from client.services import coordinator_client
from client.services.session import (
    clear_session_cookie,
    identify,
    session_bearer,
    set_session_cookie,
)
from client.templating import templates

router = APIRouter()


def _safe_next(next_value: str | None) -> str:
    return next_value if next_value and next_value.startswith("/") else "/"


def _render_login(
    request: Request,
    *,
    next_path: str,
    error: str | None,
    username: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html.j2",
        {
            "next_path": next_path,
            "error": error,
            "username": username,
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    # If already logged in, bounce to the destination.
    bearer = session_bearer(request)
    if bearer and await identify(bearer):
        return RedirectResponse(next or "/", status_code=303)
    return _render_login(
        request, next_path=next or "/", error=None,
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    """Forward u/p to the coordinator, set the session cookie on success."""
    next_path = _safe_next(next)
    async with coordinator_client._public_client() as c:
        r = await c.post(
            "/login",
            json={"username": username.strip(), "password": password},
            timeout=5,
        )
    if r.status_code != 200:
        return _render_login(
            request,
            next_path=next_path,
            username=username,
            error=(
                "Username or password didn't match. Double-check both "
                "and try again."
            ),
            status_code=401,
        )
    body = r.json()
    response = RedirectResponse(next_path, status_code=303)
    set_session_cookie(response, body["token"])
    return response


@router.post("/login/token", response_class=HTMLResponse)
async def login_token_submit(
    request: Request,
    token: str = Form(...),
    next: str = Form("/"),
):
    """Bearer-token fallback. Validates against the coordinator by
    calling /me with the supplied bearer; on success, stamps the cookie.
    Used by admins migrating from token-only auth and by CLI/API users."""
    next_path = _safe_next(next)
    bearer = token.strip()
    me = await identify(bearer)
    if me is None:
        return _render_login(
            request,
            next_path=next_path,
            error=(
                "That token was rejected by the coordinator. "
                "Double-check it and try again."
            ),
            status_code=401,
        )
    response = RedirectResponse(next_path, status_code=303)
    set_session_cookie(response, bearer)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response
