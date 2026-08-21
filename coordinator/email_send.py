"""Transactional email via Resend (resend.com).

Lazy-loaded like coordinator.notifications' VAPID handling: with no
RESEND_API_KEY configured, send_verification_email() is a no-op that
returns False, so callers (POST /signup) fall back to auto-verifying
the member instead of gating them behind an email that will never
arrive. Uses stdlib urllib rather than adding a requests/httpx
dependency for coordinator server-side code — this is a single small
JSON POST.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from shared.config import EMAIL_FROM, RESEND_API_KEY

log = logging.getLogger("gamerai.email")

_RESEND_URL = "https://api.resend.com/emails"


def is_configured() -> bool:
    return bool(RESEND_API_KEY)


def send_verification_email(to_email: str, verify_url: str) -> bool:
    """Best-effort send. Returns True on a 2xx from Resend, False on
    any failure (missing key, network error, non-2xx) — callers treat
    False as "couldn't confirm delivery," not a reason to fail the
    request that triggered the send."""
    if not RESEND_API_KEY:
        return False
    html = (
        "<p>Welcome to GamerAI! Confirm your email to unlock chat, "
        "image generation, and voice:</p>"
        f'<p><a href="{verify_url}">{verify_url}</a></p>'
        "<p>This link expires in 24 hours. If you didn't create this "
        "account, you can ignore this email.</p>"
    )
    payload = json.dumps({
        "from": EMAIL_FROM,
        "to": [to_email],
        "subject": "Verify your GamerAI account",
        "html": html,
    }).encode("utf-8")
    req = urllib.request.Request(
        _RESEND_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        log.warning("resend send failed: HTTP %s %r", e.code, body)
        return False
    except Exception as e:
        log.warning("resend send failed: %s", e)
        return False
