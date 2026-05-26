"""Coordinator API facade.

Wraps the raw ``coordinator_client._client`` / ``_public_client``
``httpx.AsyncClient`` factories with three method shapes that cover
every call site in ``client/routes/``:

* :func:`proxy_json` — BFF passthrough. Forward a coordinator call
  to the browser as a ``JSONResponse``, preserving status code.
  Used by every ``/api/*`` route in :mod:`client.routes.api`.

* :func:`proxy_raw` — same shape but for binary bodies (generated
  PNGs from ``/api/images/{name}``). Preserves ``Content-Type``.

* :func:`fetch_safe` — internal fetch + ``(status, body)`` tuple.
  Used by server-rendered pages (``/account``, ``/dashboard``) that
  need to inspect status and render a fallback when the call fails.

Why a facade: before this module, every route had four lines of
``async with coordinator_client._client(...)`` boilerplate plus a
try/except for the JSON-decode case. The single change point also
lets us swap connection pooling / retries / timeouts in one place
later without touching ~20 call sites.

Test compatibility: the facade calls the underscored factories
(``coordinator_client._client`` and ``_public_client``) by name, so
the existing smoke-test monkeypatch — which substitutes
``coordinator_client._client`` with an ASGI transport against the
in-process coordinator — still works transparently through this
layer.
"""
from __future__ import annotations

from typing import Iterable, Optional

from fastapi.responses import JSONResponse, Response

from client.services import coordinator_client


def _client_ctx(bearer: Optional[str]):
    """Pick the right httpx client. ``None`` bearer → no
    ``Authorization`` header (the public/invite redemption path)."""
    if bearer:
        return coordinator_client._client(bearer=bearer)
    return coordinator_client._public_client()


async def proxy_json(
    *,
    bearer: Optional[str],
    method: str,
    path: str,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 10.0,
    forward_response_headers: Iterable[str] = (),
    text_fallback: bool = False,
) -> JSONResponse:
    """Forward a coordinator call to the browser as a JSONResponse.

    Args:
        bearer: caller's session bearer (None → use the public client).
        method/path/json/params/headers/timeout: passed verbatim to
            ``httpx.AsyncClient.request``.
        forward_response_headers: lowercase header names to copy from
            the coordinator response into ours (e.g. ``"retry-after"``
            for the 429 retry path).
        text_fallback: when True, a non-JSON response body is wrapped
            as ``{"detail": r.text}`` instead of being discarded. Used
            by ``/generate`` and ``/cancel`` so the chat UI sees a
            user-friendly 503 like ``{"detail":"No community members
            are available..."}`` even if the coordinator's body parser
            hiccups.
    """
    async with _client_ctx(bearer) as c:
        r = await c.request(
            method, path,
            json=json, params=params, headers=headers, timeout=timeout,
        )
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text} if text_fallback else {}
    out_headers = None
    if forward_response_headers:
        wanted = {h.lower() for h in forward_response_headers}
        copied = {k: v for k, v in r.headers.items() if k.lower() in wanted}
        out_headers = copied or None
    return JSONResponse(body, status_code=r.status_code, headers=out_headers)


async def proxy_raw(
    *,
    bearer: Optional[str],
    method: str,
    path: str,
    timeout: float = 30.0,
    default_content_type: str = "application/octet-stream",
) -> Response:
    """Forward a binary coordinator response (e.g. generated PNG)
    preserving the upstream ``Content-Type``. ``timeout`` defaults
    higher than :func:`proxy_json` because images can be larger
    than the chat JSON payloads."""
    async with _client_ctx(bearer) as c:
        r = await c.request(method, path, timeout=timeout)
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", default_content_type),
    )


async def fetch_safe(
    *,
    bearer: Optional[str],
    path: str,
    method: str = "GET",
    json: Optional[dict] = None,
    timeout: float = 5.0,
) -> tuple[int, dict]:
    """Fetch a coordinator endpoint and return ``(status_code, body)``.

    Non-JSON response body is wrapped as ``{"detail": r.text}`` so the
    caller never sees a ``ValueError`` mid-render. Used by server-
    rendered pages (account, dashboard) that decide how to render
    based on the status code rather than passing the response through
    to the browser verbatim.
    """
    async with _client_ctx(bearer) as c:
        r = await c.request(method, path, json=json, timeout=timeout)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"detail": r.text}
