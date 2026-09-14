"""Account auth surface: username+password login, invite-free signup,
email verification, and password change. ``db`` and ``tos_version``
are closed over directly; ``r`` as ``get_r`` (a zero-arg getter — see
coordinator/prompt_rewrite.py for why); ``client_ip_fn`` because
``_client_ip`` still lives in coordinator/main.py's rate-limit-
middleware section. main.py wires it once:
``app.include_router(routes_account.build_router(db, lambda: r, TOS_VERSION, _client_ip))``.
"""
from __future__ import annotations

import logging
import secrets
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from coordinator import email_send, events, member_auth
from coordinator.routes_agent_pairing import PUBLIC_BASE_URL
from coordinator.tiers import quota_for as _tier_quota_for
from shared.auth import AUTH_ENABLED
from shared.config import (
    EMAIL_VERIFY_TTL_SECONDS,
    LOGIN_FAIL_MAX,
    LOGIN_FAIL_WINDOW_SECONDS,
    SIGNUP_MAX_PER_IP,
    SIGNUP_WINDOW_SECONDS,
)
from shared.models import LoginRequest, PasswordChangeRequest, SignupRequest

log = logging.getLogger("coordinator.routes_account")


def _login_fail_key(username_norm: str) -> str:
    # Lowercased to match the case-insensitive username lookup
    # (db.get_member_by_username uses LOWER(username)) — otherwise an
    # attacker could vary case to mint a fresh counter and bypass the
    # throttle.
    return f"login_fail:{username_norm}"


def _email_verify_key(code: str) -> str:
    return f"email_verify:{code}"


def _signup_throttle_key(client_ip: str) -> str:
    return f"signup_count:{client_ip}"


def build_router(db, get_r, tos_version: str, client_ip_fn) -> APIRouter:
    router = APIRouter()

    def _start_email_verification(member_id: str, email: str, request: Request) -> bool:
        """Generate a single-use verification code, store it, and try to
        send the email. Returns True iff the member should be LEFT
        unverified pending that click (Resend is configured and accepted
        the send); False means the caller should auto-verify instead
        (Resend unconfigured, or the send attempt itself failed — an
        inbox that will never see the link is not a reason to lock
        someone out of an account they just created).

        ``PUBLIC_BASE_URL`` (imported from coordinator/routes_agent_pairing.py
        — same var, reused rather than re-imported from shared.config to
        avoid two names resolving two different ways) falls back to the
        live request's own host, same pattern as
        /agents/pair/start, so an unconfigured deploy still emails a
        working absolute link instead of a bare "/verify-email?..." path."""
        if not email_send.is_configured():
            return False
        code = secrets.token_urlsafe(32)
        key = _email_verify_key(code)
        get_r().set(key, member_id, ex=EMAIL_VERIFY_TTL_SECONDS)
        base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
        verify_url = f"{base}/verify-email?code={code}"
        if not email_send.send_verification_email(email, verify_url):
            get_r().delete(key)
            return False
        return True

    @router.post("/login")
    def login(req: LoginRequest):
        """Public username + password sign-in. On success, rotates the
        member's wire-format bearer (so any prior session/device is
        immediately logged out) and returns the fresh token for the caller
        to store as their session credential.

        Always returns the same 401 detail for unknown-username and
        bad-password so an attacker can't enumerate accounts.

        Brute-force throttle: after LOGIN_FAIL_MAX failed attempts against
        one username within LOGIN_FAIL_WINDOW_SECONDS, returns 429 until the
        window elapses. Keyed on username (not IP) because the primary login
        path is the web BFF — the coordinator sees the client *container's*
        IP there, not the browser's, so an IP key would lump every web user
        into one bucket. The 429 fires identically for real and unknown
        usernames, so it doesn't leak which accounts exist. A successful
        login clears the counter. Tradeoff: an attacker can soft-lock a
        victim's logins for the window by spamming bad passwords — bounded
        and acceptable for an invite-only userbase; the alternative (no
        throttle) is worse."""
        r = get_r()
        INVALID = HTTPException(status_code=401, detail="invalid credentials")
        username = (req.username or "").strip()
        password = req.password or ""
        username_norm = username.lower()
        fail_key = _login_fail_key(username_norm) if username_norm else None

        if LOGIN_FAIL_MAX > 0 and fail_key is not None:
            current = r.get(fail_key)
            if current is not None and int(current) >= LOGIN_FAIL_MAX:
                ttl = r.ttl(fail_key)
                retry_after = (
                    int(ttl) if ttl and ttl > 0 else LOGIN_FAIL_WINDOW_SECONDS
                )
                raise HTTPException(
                    status_code=429,
                    detail="too many failed login attempts; try again later",
                    headers={"Retry-After": str(max(1, retry_after))},
                )

        def _record_failure() -> None:
            if LOGIN_FAIL_MAX <= 0 or fail_key is None:
                return
            n = int(r.incr(fail_key))
            if n == 1:
                # First failure in a fresh window — arm the TTL so the
                # counter self-clears even if the attacker walks away.
                r.expire(fail_key, LOGIN_FAIL_WINDOW_SECONDS)

        # Malformed (missing field) — nothing to protect, don't burn a
        # counter slot under a meaningless key.
        if not username or not password:
            raise INVALID
        row = db.get_member_by_username(username)
        if row is None:
            _record_failure()
            raise INVALID
        keys = row.keys()
        stored_hash = row["password_hash"] if "password_hash" in keys else None
        if not member_auth.verify_password(password, stored_hash):
            _record_failure()
            raise INVALID
        # Success — clear the failure counter for this username.
        if LOGIN_FAIL_MAX > 0 and fail_key is not None:
            r.delete(fail_key)
        raw_token = member_auth.generate_token()
        new_hash = member_auth.hash_token(raw_token)
        if not db.rotate_member_token(row["member_id"], new_hash):
            # Token-hash collision is statistically impossible at 256 bits,
            # but if it ever fires we want a clean 500 rather than a silent
            # auth failure on next request.
            raise HTTPException(status_code=500, detail="token rotation failed")
        db.touch_member(row["member_id"], time.time())
        log.info(
            "login ok",
            extra={"event": "login_ok", "worker_id": None},
        )
        return {
            "member_id": row["member_id"],
            "token": raw_token,
            "role": row["role"],
            "username": row["username"],
        }

    @router.post("/signup")
    def signup(req: SignupRequest, request: Request):
        """Public, invite-free account creation. This is the thing
        business.md calls "contribute-to-use" made literal: showing up and
        creating an account is how you join the network — no existing
        member has to vouch for you first. The new member is a
        ``contributor`` with no ``parent_member_id`` (they're the root of
        their own branch, not anyone's invitee) and can immediately hit
        ``POST /invites`` with their new bearer token to invite friends,
        the same as any other contributor.

        Throttled per client IP (SIGNUP_MAX_PER_IP / SIGNUP_WINDOW_SECONDS)
        rather than by the generic RATE_LIMIT_PER_MIN, which is sized for
        the streaming-poll path and usually off — account creation needs
        its own, much tighter ceiling once there's no invite gate keeping
        strangers out. Only successful creations count against the quota;
        a mistyped password or a username collision doesn't burn it."""
        r = get_r()
        if not req.tos_accepted:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Community ToS must be accepted to create an account "
                    "(see /tos)."
                ),
            )
        email = (req.email or "").strip()
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="a valid email is required")
        try:
            username = member_auth.validate_username(req.username)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            password = member_auth.validate_password(req.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        client_ip = client_ip_fn(request)
        throttle_key = _signup_throttle_key(client_ip)
        if SIGNUP_MAX_PER_IP > 0:
            current = r.get(throttle_key)
            if current is not None and int(current) >= SIGNUP_MAX_PER_IP:
                ttl = r.ttl(throttle_key)
                retry_after = int(ttl) if ttl and ttl > 0 else SIGNUP_WINDOW_SECONDS
                raise HTTPException(
                    status_code=429,
                    detail="too many accounts created from this network recently; try again later",
                    headers={"Retry-After": str(max(1, retry_after))},
                )

        now = time.time()
        new_member_id = "mem_" + uuid.uuid4().hex[:12]
        raw_token = member_auth.generate_token()
        token_hash = member_auth.hash_token(raw_token)
        password_hash = member_auth.hash_password(password)
        quota = _tier_quota_for("BRONZE")

        member_row, failure = db.create_signup_member(
            member_id=new_member_id,
            username=username,
            password_hash=password_hash,
            email=email,
            token_hash=token_hash,
            daily_quota_tokens=quota["tokens"],
            daily_quota_images=quota["images"],
            daily_quota_voice_minutes=quota["voice_minutes"],
            created_at=now,
            tos_version=tos_version,
        )
        if member_row is None:
            if failure == "username_taken":
                raise HTTPException(
                    status_code=409,
                    detail=f"username {username!r} is already taken",
                )
            raise HTTPException(
                status_code=409,
                detail=(
                    "that email is already claimed by another GamerAI "
                    "account — sign in with your existing one, or use a "
                    "different email"
                ),
            )

        if SIGNUP_MAX_PER_IP > 0:
            n = int(r.incr(throttle_key))
            if n == 1:
                r.expire(throttle_key, SIGNUP_WINDOW_SECONDS)

        # Chat/image/voice consumption gates on email_verified (see the
        # quota-check block in submit_job below) — a signup with no way to
        # ever receive that email (Resend unconfigured, or this particular
        # send failing) auto-verifies instead of permanently locking the
        # account out. Contributing (running the agent as a worker) is
        # never gated by this either way.
        pending_verification = _start_email_verification(new_member_id, email, request)
        if not pending_verification:
            db.verify_member_email(new_member_id)

        log.info("signup ok", extra={"event": "signup_ok"})
        events.emit(
            "member.created",
            member_id=new_member_id, username=username, email=email,
        )
        return {
            "member_id": new_member_id,
            "token": raw_token,
            "username": username,
            "role": "contributor",
            "parent_member_id": None,
            "daily_quota_tokens": quota["tokens"],
            "tos_version": tos_version,
            "email_verified": not pending_verification,
        }

    @router.get("/verify-email", response_class=HTMLResponse)
    def verify_email(code: str = ""):
        """Public landing page for the link in the verification email
        (see _start_email_verification / POST /signup). Single-use — the
        redis key is deleted on the first successful hit, so a forwarded
        or reused link fails cleanly instead of quietly re-verifying."""
        r = get_r()
        member_id = r.get(_email_verify_key(code)) if code else None
        if not member_id:
            return HTMLResponse(
                "<h1>Link expired or invalid</h1>"
                "<p>Verification links expire 24 hours after signup. Sign "
                "in and request a new one from your account page.</p>",
                status_code=400,
            )
        r.delete(_email_verify_key(code))
        db.verify_member_email(member_id)
        return HTMLResponse(
            "<h1>Email verified</h1>"
            "<p>Your GamerAI account is confirmed — chat, image "
            "generation, and voice are unlocked. You can close this tab.</p>"
        )

    @router.post("/me/resend-verification")
    def resend_verification(request: Request):
        """Authenticated re-send for a member who never got (or lost) the
        original verification email. No extra throttle beyond the bearer
        requirement — this is a low-volume, single-member action, not an
        open endpoint an attacker can hammer to spam an inbox they don't
        control (the email is fixed at signup, not caller-supplied here)."""
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        if member.email_verified:
            raise HTTPException(status_code=400, detail="already verified")
        if not member.email:
            raise HTTPException(
                status_code=400,
                detail="no email on file for this account",
            )
        pending = _start_email_verification(member.member_id, member.email, request)
        if not pending:
            # Resend unconfigured, or the send failed — same fallback as
            # signup: don't leave the member stuck with no path forward.
            db.verify_member_email(member.member_id)
            return {"email_verified": True}
        return {"email_verified": False}

    @router.post("/me/password")
    def change_password(req: PasswordChangeRequest, request: Request):
        """Authenticated password rotation. Requires the current password
        so a stolen session cookie can't silently lock the real owner out.
        Does NOT rotate the bearer token — the caller stays signed in on
        this device. To kick other devices, use /login again afterward."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                raise HTTPException(
                    status_code=400,
                    detail="password change requires auth; set API_TOKEN first",
                )
            raise HTTPException(status_code=401, detail="unauthorized")
        row = db.get_member(member.member_id)
        if row is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        keys = row.keys()
        stored_hash = row["password_hash"] if "password_hash" in keys else None
        # A member without a password yet (legacy admin, freshly-claimed
        # invite that didn't set one) can use this endpoint to set their
        # first password — current_password is ignored in that case.
        if stored_hash and not member_auth.verify_password(
            req.current_password or "", stored_hash
        ):
            raise HTTPException(
                status_code=401, detail="current password is incorrect"
            )
        try:
            new_clean = member_auth.validate_password(req.new_password or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.set_member_credentials(
            member_id=member.member_id,
            username=None,
            password_hash=member_auth.hash_password(new_clean),
            when=time.time(),
        )
        log.info(
            "password changed",
            extra={"event": "password_changed", "worker_id": None},
        )
        return {"ok": True}

    return router
