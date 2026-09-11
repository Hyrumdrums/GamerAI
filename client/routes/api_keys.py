"""API keys page — self-serve personal API keys for the same contributor
GPU network / tier quota the web chat UI already uses, meant for pasting
into a third-party tool's config (Home Assistant, Open WebUI, a script)
as a plain ``Authorization: Bearer gai_api_...`` credential, the same way
you'd use a key from any other API provider.

The raw key is only ever available in the response of the create POST
(rendered directly on the page, not via redirect — a redirect would put
the secret in browser history/referrer). A page refresh loses it, same
as every other "shown once" API-key UX; the coordinator never stores it
in plaintext either, so there's no way to recover a lost key — only
revoke and mint a new one.
"""
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from client.services import api_client
from client.services.session import identify, login_redirect, session_bearer
from client.templating import templates

router = APIRouter()


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request, flash: Optional[str] = None):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/api-keys")
    status, body = await api_client.fetch_safe(bearer=bearer, path="/me/api-keys")
    api_keys = body.get("api_keys", []) if status == 200 else []
    return templates.TemplateResponse(
        request,
        "api_keys.html.j2",
        {
            "me": me,
            "api_keys": api_keys,
            "flash": flash,
            "created_key": None,
            "created_label": None,
        },
    )


@router.post("/api-keys", response_class=HTMLResponse)
async def create_api_key(request: Request, label: str = Form("")):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/api-keys")
    status, body = await api_client.fetch_safe(
        bearer=bearer, method="POST", path="/me/api-keys",
        json={"label": label.strip() or None},
    )
    if status != 200:
        detail = body.get("detail", "Couldn't create API key.")
        return RedirectResponse(f"/api-keys?flash={detail}", status_code=303)
    # Rendered directly (not a redirect) so the raw key appears exactly
    # once and never touches the URL.
    _, list_body = await api_client.fetch_safe(bearer=bearer, path="/me/api-keys")
    api_keys = list_body.get("api_keys", [])
    return templates.TemplateResponse(
        request,
        "api_keys.html.j2",
        {
            "me": me,
            "api_keys": api_keys,
            "flash": None,
            "created_key": body.get("api_key"),
            "created_label": body.get("label"),
        },
    )


@router.post("/api-keys/{key_id}/revoke")
async def revoke_api_key(key_id: str, request: Request):
    bearer = session_bearer(request)
    me = await identify(bearer) if bearer else None
    if me is None:
        return login_redirect("/api-keys")
    status, body = await api_client.fetch_safe(
        bearer=bearer, method="POST", path=f"/me/api-keys/{key_id}/revoke",
    )
    if status == 200 and body.get("deleted"):
        return RedirectResponse("/api-keys?flash=API key revoked.", status_code=303)
    detail = body.get("detail", "Couldn't revoke API key.")
    return RedirectResponse(f"/api-keys?flash={detail}", status_code=303)
