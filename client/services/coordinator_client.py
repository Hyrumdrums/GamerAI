"""httpx factories for talking to the coordinator API.

Two factories live here:

* ``_client(bearer=None)`` — authenticated client. When ``bearer`` is
  supplied, sends ``Authorization: Bearer <bearer>`` (the per-session
  user's token). When omitted, falls back to the admin token from the
  env (legacy path used by older admin views). Same coordinator API in
  both cases — only the membership identity changes.
* ``_public_client()`` — no auth header at all. For the invite-redemption
  flow, where the invite code itself is the credential.

The underscore prefixes are kept for backwards compatibility with the
smoke tests, which monkeypatch these names to route through an ASGI
transport instead of a real network call. The routes module imports
this module (not the names), so a patch on ``coordinator_client._client``
is picked up.
"""
import os
from typing import Optional

import httpx

from shared.auth import auth_headers

COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://coordinator:8000")


def _client(bearer: Optional[str] = None) -> httpx.AsyncClient:
    if bearer:
        return httpx.AsyncClient(
            base_url=COORDINATOR_URL,
            headers={"Authorization": f"Bearer {bearer}"},
        )
    return httpx.AsyncClient(base_url=COORDINATOR_URL, headers=auth_headers())


def _public_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=COORDINATOR_URL)
