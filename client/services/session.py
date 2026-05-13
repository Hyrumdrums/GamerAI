"""Session cookie helpers and the /me identity lookup.

The session cookie value is literally the user's bearer token. HttpOnly
prevents JavaScript from reading it; Secure keeps it off plaintext HTTP;
SameSite=Lax stops third-party sites from triggering authenticated
requests on the user's behalf.

In dev (no HTTPS) the Secure flag would prevent the cookie from being
set at all. PUBLIC_BASE_URL is set to the public https:// URL on the
VPS, so we use that as the signal for whether to mark the cookie Secure.
"""
import os
from typing import Optional

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse

from client.services import coordinator_client

SESSION_COOKIE = "gai_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
COOKIE_SECURE = os.getenv("PUBLIC_BASE_URL", "http://").startswith("https://")


def session_bearer(request: Request) -> Optional[str]:
    """Bearer token from the session cookie, or None when not logged in."""
    return request.cookies.get(SESSION_COOKIE) or None


async def identify(bearer: str) -> Optional[dict]:
    """Resolve a bearer to the /me payload, or None if invalid.

    Caches nothing — every request re-checks so revoked tokens stop
    working immediately. When the coordinator reports ``auth_disabled``,
    we synthesize a dev admin so local loops don't have to plumb env.
    """
    if not bearer:
        return None
    try:
        async with coordinator_client._client(bearer=bearer) as c:
            r = await c.get("/me", timeout=5)
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("auth_disabled"):
            return {"member_id": "dev", "role": "admin", "email": None}
        return body
    except httpx.HTTPError:
        return None


def set_session_cookie(response, bearer: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, bearer,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def login_redirect(next_path: str = "/") -> RedirectResponse:
    target = f"/login?next={next_path}" if next_path != "/" else "/login"
    return RedirectResponse(target, status_code=303)
