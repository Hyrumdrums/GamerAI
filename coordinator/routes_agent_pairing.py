"""Agent pairing (browser handoff) — the Windows agent has no token
yet, so it starts a short-lived pair code, the signed-in user confirms
it in the browser, and the agent polls for the resulting bearer.

``db`` is closed over directly (same shape as coordinator/notifications.py).
``r`` is closed over as ``get_r`` — a zero-arg getter, ``lambda: r`` — not
the object itself: several test modules reassign coordinator.main.r to a
fakeredis instance AFTER importing main, which only a live lookup at
call time observes (see coordinator/prompt_rewrite.py for the same
pattern and the bug it fixes). main.py wires it once:
``app.include_router(routes_agent_pairing.build_router(db, lambda: r))``.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from coordinator import member_auth
from shared.auth import AUTH_ENABLED
from shared.models import AgentPairConfirmRequest, AgentPairPollRequest

log = logging.getLogger("coordinator.routes_agent_pairing")

# Redis keys for the pair-code lifecycle. The code is short-lived
# (PAIR_TTL_SECONDS) and one-shot: once the agent picks up its token via
# /poll, the record is deleted. The token itself is stored in member_tokens
# at /confirm time, so a re-played /poll after pickup is a 404 — the
# token has already been delivered exactly once.
PAIR_KEY_PREFIX = "agents:pair:"
# Index: normalized user_code -> secret pair_code. Lets the browser
# resolve the code the user typed (read off the agent screen) back to
# the pending pair record WITHOUT ever handling the agent's secret
# polling code. Same TTL as the pair record.
PAIR_USERCODE_PREFIX = "agents:pair:uc:"
PAIR_TTL_SECONDS = 300
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# User-code alphabet: uppercase + digits with the visually ambiguous
# characters removed (no 0/O, 1/I/L) so a contributor reading the code
# off their agent window and typing it into the browser doesn't fat-
# finger it. 8 chars over 32 symbols = 40 bits — far beyond brute force
# within the 5-minute TTL, and re-confirming an already-approved code
# is a no-op (410) anyway, so guessing buys an attacker nothing.
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_USER_CODE_LEN = 8


def _pair_key(code: str) -> str:
    return f"{PAIR_KEY_PREFIX}{code}"


def _normalize_user_code(raw: Optional[str]) -> str:
    """Strip formatting (dashes/spaces) and uppercase so 'wdjb-mjht'
    and 'WDJB MJHT' both resolve to the stored 'WDJBMJHT'."""
    return "".join(ch for ch in (raw or "").upper() if ch in _USER_CODE_ALPHABET)


def _pair_usercode_key(user_code: str) -> str:
    return f"{PAIR_USERCODE_PREFIX}{_normalize_user_code(user_code)}"


def _generate_user_code() -> str:
    return "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_LEN))


def _format_user_code(code: str) -> str:
    """Group into XXXX-XXXX for legibility on the agent console."""
    half = _USER_CODE_LEN // 2
    return f"{code[:half]}-{code[half:]}"


def build_router(db, get_r) -> APIRouter:
    router = APIRouter()

    @router.post("/agents/pair/start")
    def agent_pair_start(request: Request):
        """Agent starts the pairing flow. Public — the agent has no token
        yet. Returns the secret ``pair_code`` (the agent polls with it), a
        short ``user_code`` the agent displays on screen, and a verification
        URL with NO secret in it. The browser-side flow happens at
        ``GET /agent/pair`` on the web UI: the signed-in user types the
        ``user_code`` and POSTs it to ``/agents/pair/confirm`` here. Putting
        the secret in the URL instead (the prior design) let an attacker who
        called /start mail a victim a one-click link and harvest a token in
        the victim's name — the typed out-of-band code closes that."""
        code = "pair_" + uuid.uuid4().hex[:16]
        user_code = _generate_user_code()
        expires_at = time.time() + PAIR_TTL_SECONDS
        r = get_r()
        r.set(
            _pair_key(code),
            json.dumps(
                {"state": "pending", "expires_at": expires_at, "user_code": user_code}
            ),
            ex=PAIR_TTL_SECONDS,
        )
        r.set(_pair_usercode_key(user_code), code, ex=PAIR_TTL_SECONDS)
        base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
        # The verification URL carries NO secret. Embedding the code (an
        # RFC 8628 "verification_uri_complete") would let an attacker who
        # ran /start mail a victim a pre-filled link and harvest a token in
        # the victim's name on click. Instead the agent shows `user_code`
        # and the user types it on the (clean) page — they'll only ever
        # type the code their own agent displays.
        return {
            "pair_code": code,
            "user_code": _format_user_code(user_code),
            "verification_url": f"{base}/agent/pair",
            # Back-compat alias for older agents that read `pair_url`.
            "pair_url": f"{base}/agent/pair",
            "expires_at": expires_at,
            "ttl_seconds": PAIR_TTL_SECONDS,
        }

    @router.get("/agents/pair/{code}")
    def agent_pair_info(code: str):
        """Public read of a pair code's state. The web UI calls this from
        the /agent/pair page so a user landing on a stale link gets a clean
        404 rather than seeing a Confirm button that won't work.

        Reveals only state + expiry — never the token, even if the code is
        in 'approved' state."""
        raw = get_r().get(_pair_key(code))
        if raw is None:
            raise HTTPException(status_code=404, detail="pair code not found or expired")
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=404, detail="pair code corrupt")
        return {
            "state": record.get("state"),
            "expires_at": record.get("expires_at"),
        }

    @router.post("/agents/pair/confirm")
    def agent_pair_confirm(req: AgentPairConfirmRequest, request: Request):
        """Web UI calls this when the signed-in user types the code shown by
        their agent and clicks "Pair this PC". Resolves the typed
        ``user_code`` to the pending pair record, mints a fresh ``gai_…``
        bearer, stores its hash in ``member_tokens`` tied to the caller's
        member_id (so future agent requests authenticate as that member),
        and stashes the raw token in Redis behind the secret pair code for
        the agent to retrieve via /poll.

        The browser never sees the secret pair code — it only knows the
        user_code, which is worthless without physical sight of the agent
        screen. Auth required: the confirming session is the account the
        agent gets bound to."""
        member = getattr(request.state, "member", None)
        if member is None:
            if AUTH_ENABLED:
                raise HTTPException(status_code=401, detail="unauthorized")
            # Dev-mode without API_TOKEN — no real identity to pair against.
            raise HTTPException(
                status_code=400,
                detail="pairing requires auth; set API_TOKEN to enable",
            )

        r = get_r()
        normalized = _normalize_user_code(req.user_code)
        if len(normalized) != _USER_CODE_LEN:
            raise HTTPException(
                status_code=400,
                detail="enter the code shown in your GamerAI agent window",
            )
        code = r.get(_pair_usercode_key(normalized))
        if code is None:
            raise HTTPException(
                status_code=404, detail="pairing code not found or expired"
            )
        raw = r.get(_pair_key(code))
        if raw is None:
            raise HTTPException(
                status_code=404, detail="pairing code not found or expired"
            )
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=404, detail="pair code corrupt")
        if record.get("state") != "pending":
            raise HTTPException(status_code=410, detail="pairing code already used")

        raw_token = member_auth.generate_token()
        token_hash = member_auth.hash_token(raw_token)
        now = time.time()
        db.add_member_token(
            token_hash=token_hash,
            member_id=member.member_id,
            label=f"agent (paired {time.strftime('%Y-%m-%d', time.gmtime(now))})",
            when=now,
        )

        # Approved state: stash the raw token in Redis for /poll to pick up.
        # The same TTL applies — if the agent disappears before polling,
        # the token rots out of Redis but the member_tokens row stays.
        # Trade-off: a dead token sticks around in the DB until manually
        # cleaned up via /account → unpair. Acceptable for v1 — the alt
        # (delete-then-re-add on poll) wastes a SQL write per pair.
        record["state"] = "approved"
        record["member_id"] = member.member_id
        record["token"] = raw_token
        record["approved_at"] = now
        r.set(_pair_key(code), json.dumps(record), ex=PAIR_TTL_SECONDS)
        log.info(
            "agent pair approved",
            extra={"event": "agent_pair_approved", "worker_id": None},
        )
        return {"ok": True}

    @router.post("/agents/pair/unpair")
    def agent_pair_unpair(request: Request):
        """Agent retires its own bearer. Called by the Windows agent
        during uninstall (and on demand via `agent --unpair`) so the
        token on disk becomes useless to anyone who later recovers
        state.json from a sold machine or a backup.

        Only deletes from ``member_tokens`` — never from
        ``members.token_hash`` — so a member who manually pasted their
        web session token into the agent isn't accidentally signed out
        of their browser when the agent unpairs. The normal pairing
        flow lands in ``member_tokens``, so this is the right
        behavior for 99% of callers; for the edge case the local
        state wipe still removes the on-disk copy.

        Idempotent: 200 with ``deleted=False`` if the bearer isn't in
        ``member_tokens`` (already unpaired, or it's a primary-token
        paste). The agent doesn't gate uninstall on the response."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                return {"deleted": False}
            raise HTTPException(status_code=401, detail="unauthorized")
        raw_token = member_auth.parse_bearer(
            request.headers.get("authorization")
        )
        if not raw_token:
            raise HTTPException(status_code=401, detail="unauthorized")
        token_hash = member_auth.hash_token(raw_token)
        deleted = db.delete_member_token(member.member_id, token_hash)
        if deleted:
            log.info(
                "agent unpaired",
                extra={"event": "agent_unpaired", "worker_id": None},
            )
        return {"deleted": deleted}

    @router.post("/agents/pair/poll")
    def agent_pair_poll(req: AgentPairPollRequest):
        """Agent polls for the user's approval. Public — the agent has no
        token yet. Returns ``state="pending"`` until the user confirms;
        once approved, returns the fresh token exactly once and deletes the
        pair record so a second poll gets 404."""
        r = get_r()
        raw = r.get(_pair_key(req.pair_code))
        if raw is None:
            raise HTTPException(status_code=404, detail="pair code not found or expired")
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=404, detail="pair code corrupt")

        state = record.get("state", "pending")
        if state != "approved":
            return {"state": state, "expires_at": record.get("expires_at")}

        # One-shot pickup — delete the record so a second poll can't
        # exfiltrate the token.
        r.delete(_pair_key(req.pair_code))
        return {
            "state": "approved",
            "token": record.get("token"),
            "member_id": record.get("member_id"),
        }

    return router
