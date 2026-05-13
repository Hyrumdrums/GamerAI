"""Session login/logout. POST /login takes a bearer token as form input
and stamps it into the session cookie if the coordinator accepts it as
a valid member token."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from client.services.session import (
    clear_session_cookie,
    identify,
    session_bearer,
    set_session_cookie,
)
from client.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    # If already logged in, bounce to the destination.
    bearer = session_bearer(request)
    if bearer and await identify(bearer):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html.j2",
        {"next_path": next or "/", "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    token: str = Form(...),
    next: str = Form("/"),
):
    bearer = token.strip()
    me = await identify(bearer)
    if me is None:
        return templates.TemplateResponse(
            request,
            "login.html.j2",
            {
                "next_path": next or "/",
                "error": (
                    "That token was rejected by the coordinator. "
                    "Double-check it and try again."
                ),
            },
            status_code=401,
        )
    safe_next = next if next.startswith("/") else "/"
    response = RedirectResponse(safe_next, status_code=303)
    set_session_cookie(response, bearer)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response
