"""Auth guards used by route handlers.

Two flavors:

* :func:`require_session_bearer` — raise 401 if no session cookie. Used
  by every ``/api/*`` proxy; failing JSON requests want a JSON 401, not
  an HTML redirect.
* :func:`require_admin_session` — for the admin HTML pages. Returns
  ``(bearer, None)`` on success; ``(None, response)`` when the caller
  should be redirected to login or shown a 403 page. The HTML response
  is the right shape for browsers (redirects, not JSON).
"""
from typing import Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from client.services.session import identify, login_redirect, session_bearer


def require_session_bearer(request: Request) -> str:
    bearer = session_bearer(request)
    if not bearer:
        raise HTTPException(status_code=401, detail="not signed in")
    return bearer


async def require_admin_session(
    request: Request,
) -> Tuple[Optional[str], Optional[Response]]:
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return None, login_redirect(str(request.url.path))
    if me.get("role") != "admin":
        return None, HTMLResponse(
            "<h1>403 — admin only</h1>"
            '<p><a href="/">Back to chat</a></p>',
            status_code=403,
        )
    return bearer, None
