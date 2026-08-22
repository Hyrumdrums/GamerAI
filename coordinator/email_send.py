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


def _send(to_email: str, subject: str, html: str) -> tuple[bool, str]:
    """Shared low-level POST. Returns (ok, detail) — detail is empty on
    success, otherwise a short human-readable reason (missing key,
    HTTP status + Resend's error body, or the exception string) so
    callers that surface this to a human (the admin test-email tool)
    can show something actionable instead of a bare False."""
    if not RESEND_API_KEY:
        return False, "RESEND_API_KEY is not configured"
    payload = json.dumps({
        "from": EMAIL_FROM,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }).encode("utf-8")
    req = urllib.request.Request(
        _RESEND_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            # Cloudflare fronts api.resend.com and blocks the default
            # "Python-urllib/3.x" User-Agent outright (403, CF error
            # 1010 — "banned based on your browser's signature") before
            # the request ever reaches Resend. Confirmed by comparing
            # curl (gets a normal 4xx from Resend itself) against bare
            # urllib (gets Cloudflare's 403) from the same host. Any
            # non-default UA clears it; this one just says what we are.
            "User-Agent": "GamerAI-Coordinator/1.0 (+https://ai.dallinlayton.com)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return True, ""
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        log.warning("resend send failed: HTTP %s %r", e.code, body)
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        log.warning("resend send failed: %s", e)
        return False, str(e)


def send_verification_email(to_email: str, verify_url: str) -> bool:
    """Best-effort send. Returns True on a 2xx from Resend, False on
    any failure (missing key, network error, non-2xx) — callers treat
    False as "couldn't confirm delivery," not a reason to fail the
    request that triggered the send."""
    html = (
        "<p>Welcome to GamerAI! Confirm your email to unlock chat, "
        "image generation, and voice:</p>"
        f'<p><a href="{verify_url}">{verify_url}</a></p>'
        "<p>This link expires in 24 hours. If you didn't create this "
        "account, you can ignore this email.</p>"
    )
    ok, _detail = _send(to_email, "Verify your GamerAI account", html)
    return ok


def send_test_email(to_email: str) -> tuple[bool, str]:
    """Admin-only deliverability check (dashboard's "Email delivery
    test" card) — a plain, unmistakably-a-test message, distinct from
    send_verification_email so a real Resend send during testing never
    looks like an account-verification prompt. Returns (ok, detail)."""
    html = (
        "<p>This is a test email from your GamerAI coordinator, sent "
        "from the admin dashboard to confirm Resend delivery is "
        "working. No action needed.</p>"
    )
    return _send(to_email, "GamerAI test email", html)
