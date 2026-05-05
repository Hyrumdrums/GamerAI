"""Optional bearer-token auth for the GamerAI coordinator API.

This module is the single source of truth for auth. Behaviour is gated by
one environment variable, ``API_TOKEN``:

* unset / empty  → **auth disabled**.  All clients send no auth header,
                   the coordinator accepts every request.  This is the
                   default for local dev, CI, and integration tests so
                   nothing in the test loop has to know auth exists.
* set            → **auth enabled**.  Clients send
                   ``Authorization: Bearer <API_TOKEN>``;  the coordinator
                   rejects anything else with 401.  ``/health`` stays open
                   for uptime probes.

Disable for testing by simply leaving ``API_TOKEN`` unset (or exporting
an empty string).  No code changes required anywhere.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

API_TOKEN: str = os.getenv("API_TOKEN", "").strip()
AUTH_ENABLED: bool = bool(API_TOKEN)

# Paths that bypass auth even when it is enabled. Keep this tiny —
# everything customer-/worker-facing should go through the gate.
PUBLIC_PATHS: frozenset[str] = frozenset({"/health"})


def auth_headers(token: Optional[str] = None) -> dict[str, str]:
    """Headers to attach to outbound requests to the coordinator.

    Returns an empty dict when auth is disabled, so callers can pass the
    result straight to ``httpx.Client(headers=...)`` and forget about it.
    """
    t = (token if token is not None else API_TOKEN).strip()
    return {"Authorization": f"Bearer {t}"} if t else {}


def check_authorization(header_value: Optional[str]) -> bool:
    """True iff the request is authorized.

    Always True when auth is disabled. Otherwise the header must be of
    the form ``Bearer <token>`` and the token must match ``API_TOKEN``.
    Comparison is constant-time.
    """
    if not AUTH_ENABLED:
        return True
    if not header_value:
        return False
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), API_TOKEN)


def is_public_path(path: str) -> bool:
    """Whether a request path bypasses auth even when enabled."""
    return path in PUBLIC_PATHS
