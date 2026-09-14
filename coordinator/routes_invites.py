"""Invite creation, listing, public redemption details, acceptance, and
revocation. ``db`` and ``tos_version`` are closed over — same shape
coordinator/notifications.py already uses for ``db``; ``tos_version`` is
injected because ``TOS_VERSION`` still lives in coordinator/main.py's
not-yet-extracted community-ToS section. No ``r`` dependency here.
main.py wires it once:
``app.include_router(routes_invites.build_router(db, TOS_VERSION))``.
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

from coordinator import events, member_auth
from shared.auth import AUTH_ENABLED
from shared.models import InviteAcceptRequest, InviteCreateRequest

log = logging.getLogger("coordinator.routes_invites")


def _invite_state(row, now: float) -> str:
    if row["revoked_at"] is not None:
        return "revoked"
    if row["accepted_at"] is not None:
        return "accepted"
    if row["expires_at"] is not None and row["expires_at"] < now:
        return "expired"
    return "open"


def build_router(db, tos_version: str) -> APIRouter:
    router = APIRouter()

    def _invite_summary(row, *, with_contributor_email: bool = False) -> dict:
        """Shared shape for invite responses. ``with_contributor_email`` is
        on for the public redemption endpoint (so Bob sees who invited him);
        off for admin/contributor listings (which already know).

        ``daily_quota_images`` is read defensively — legacy invite rows
        created before the image-limits slice won't have the column."""
        keys = row.keys()
        out = {
            "code": row["code"],
            "invitee_email": row["invitee_email"],
            "daily_quota_tokens": row["daily_quota_tokens"],
            "daily_quota_images": (
                row["daily_quota_images"]
                if "daily_quota_images" in keys
                else None
            ),
            "expires_at": row["expires_at"],
            "accepted_at": row["accepted_at"],
            "accepted_by_member_id": row["accepted_by_member_id"],
            "revoked_at": row["revoked_at"],
            "notes": row["notes"],
            "created_at": row["created_at"],
            "contributor_member_id": row["contributor_member_id"],
        }
        if with_contributor_email:
            contributor = db.get_member(row["contributor_member_id"])
            out["contributor_email"] = contributor["email"] if contributor else None
        return out

    @router.post("/invites")
    def create_invite(req: InviteCreateRequest, request: Request):
        """Authenticated contributors (or admins) create an invite for an
        outside person. Returns the redemption code; the caller's UI is
        responsible for turning that into a URL and handing it off."""
        member = getattr(request.state, "member", None)
        if member is None:
            # Only reachable when AUTH is off — degrade to admin-equivalent
            # so dev/test loops can exercise the flow.
            if AUTH_ENABLED:
                raise HTTPException(status_code=401, detail="unauthorized")
            raise HTTPException(
                status_code=400,
                detail="invites require auth; set API_TOKEN to enable",
            )
        if member.role not in ("admin", "contributor"):
            raise HTTPException(
                status_code=403, detail="only contributors can create invites"
            )
        invitee_email = (req.invitee_email or "").strip()
        if not invitee_email or "@" not in invitee_email:
            raise HTTPException(
                status_code=400,
                detail="invitee_email is required (every member needs a "
                       "recovery address on file)",
            )

        now = time.time()
        expires_at = (
            now + req.expires_hours * 3600.0 if req.expires_hours else None
        )
        invite_id = "inv_id_" + uuid.uuid4().hex[:12]
        code = member_auth.generate_invite_code()
        db.create_invite(
            invite_id=invite_id,
            code=code,
            contributor_member_id=member.member_id,
            daily_quota_tokens=req.daily_quota_tokens,
            daily_quota_images=req.daily_quota_images,
            invitee_email=invitee_email,
            expires_at=expires_at,
            notes=req.notes,
            created_at=now,
        )
        log.info(
            "invite created",
            extra={"event": "invite_created", "worker_id": None},
        )
        return {
            "invite_id": invite_id,
            "code": code,
            "daily_quota_tokens": req.daily_quota_tokens,
            "daily_quota_images": req.daily_quota_images,
            "expires_at": expires_at,
        }

    @router.get("/invites")
    def list_invites(request: Request, all: bool = False):
        """Contributors get back their own invites. Admins listing with
        ``?all=true`` get every invite in the system."""
        member = getattr(request.state, "member", None)
        if member is None:
            if AUTH_ENABLED:
                raise HTTPException(status_code=401, detail="unauthorized")
            rows = db.list_all_invites()
        elif all and member.role == "admin":
            rows = db.list_all_invites()
        else:
            rows = db.list_invites_by_contributor(member.member_id)
        now = time.time()
        return {
            "invites": [
                {**_invite_summary(r), "state": _invite_state(r, now)}
                for r in rows
            ]
        }

    @router.get("/invites/{code}")
    def invite_details(code: str):
        """Public: the redemption page calls this so Bob sees who invited
        him and what cap his prompts will have. Returns the contributor's
        email when present — that's the only PII reveal here, and it's
        the same thing Alice would have put in the text/Slack message that
        delivered the URL."""
        row = db.get_invite_by_code(code)
        if row is None:
            raise HTTPException(status_code=404, detail="invite not found")
        state = _invite_state(row, time.time())
        return {**_invite_summary(row, with_contributor_email=True), "state": state}

    @router.post("/invites/{code}/accept")
    def accept_invite(code: str, req: InviteAcceptRequest):
        """Public: Bob redeems his invite. One-shot — the same code cannot
        be accepted twice. Creates the invitee member with their chosen
        username + password and returns a session token so the redemption
        page can sign them in immediately (no token paste, no email
        service in the loop).

        The redemption page collects an explicit ToS-accepted checkbox;
        the field is required here so a programmatic redeemer cannot
        bypass the click-through. The accepted ToS version is stamped
        onto the new member row."""
        if not req.tos_accepted:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Community ToS must be accepted to redeem an invite "
                    "(see /tos)."
                ),
            )
        email = (req.invitee_email or "").strip()
        if not email:
            raise HTTPException(
                status_code=400, detail="email is required",
            )
        try:
            username = member_auth.validate_username(req.username)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            password = member_auth.validate_password(req.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        now = time.time()
        new_member_id = "mem_" + uuid.uuid4().hex[:12]
        raw_token = member_auth.generate_token()
        token_hash = member_auth.hash_token(raw_token)
        password_hash = member_auth.hash_password(password)

        invite_row, failure = db.accept_invite_atomic(
            code=code,
            new_member_id=new_member_id,
            new_token_hash=token_hash,
            invitee_email=email,
            accepted_at=now,
            tos_version=tos_version,
            username=username,
            password_hash=password_hash,
        )
        if invite_row is None:
            if failure == "username_taken":
                raise HTTPException(
                    status_code=409,
                    detail=f"username {username!r} is already taken",
                )
            if failure == "email_taken":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"that email is already claimed by another GamerAI "
                        "account — sign in with your existing one, or pick a "
                        "different email"
                    ),
                )
            # Distinguish missing vs unredeemable for the redemption page.
            existing = db.get_invite_by_code(code)
            if existing is None:
                raise HTTPException(status_code=404, detail="invite not found")
            state = _invite_state(existing, now)
            raise HTTPException(status_code=410, detail=f"invite {state}")

        log.info(
            "invite accepted",
            extra={"event": "invite_accepted"},
        )
        events.emit(
            "member.created",
            member_id=new_member_id, username=username, email=email,
            invited=True,
        )
        return {
            "member_id": new_member_id,
            "token": raw_token,
            "username": username,
            "role": "invitee",
            "parent_member_id": invite_row["contributor_member_id"],
            "daily_quota_tokens": invite_row["daily_quota_tokens"],
            "tos_version": tos_version,
        }

    @router.post("/invites/{code}/revoke")
    def revoke_invite(code: str, request: Request):
        """Admin-only revocation of an unredeemed invite. Once accepted,
        revoke the *member* via the admin CLI instead — revoking the invite
        after the fact does not invalidate the member's token."""
        member = getattr(request.state, "member", None)
        if AUTH_ENABLED and (member is None or member.role != "admin"):
            raise HTTPException(status_code=403, detail="admin only")
        if not db.revoke_invite_by_code(code, time.time()):
            raise HTTPException(
                status_code=404,
                detail="invite not found, already accepted, or already revoked",
            )
        return {"ok": True}

    return router
