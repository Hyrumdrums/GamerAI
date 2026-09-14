"""Operational visibility (admin worker/job listings) and the member's
own identity/machines/contributor-status/friends surface. ``db`` and
``tos_version`` are closed over directly; ``r`` as ``get_r`` (a zero-arg
getter — see coordinator/prompt_rewrite.py for why); ``worker_status_fn``
/ ``machine_display_name_fn`` because ``_worker_status`` /
``_machine_display_name`` still live in coordinator/main.py's shared
helpers section (used by multiple not-yet-extracted route groups too).

``_require_admin`` has no db/r dependency (just ``AUTH_ENABLED`` +
``request.state.member``), so it stays a plain module-level function —
main.py and coordinator/routes_misc.py / coordinator/routes_admin.py
import it directly as ``require_admin_fn``.

main.py wires it once:
``app.include_router(routes_observability.build_router(db, lambda: r, TOS_VERSION, _worker_status, _machine_display_name))``.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from coordinator import schedule as machine_schedule
from coordinator.routes_invites import _invite_state
from coordinator.tier_engine import STALE_WORKER_HIDE_DAYS
from coordinator.tiers import (
    meets_requirements as _tier_meets,
    quota_for as _tier_quota_for,
    requirements_for as _tier_requirements_for,
    tier_above as _tier_above,
    tier_below as _tier_below,
)
from shared.auth import AUTH_ENABLED
from shared.config import CANARY_SCORE_WINDOW, WORKER_CAPABILITIES
from shared.models import FriendQuotaUpdateRequest, MachineScheduleUpdate

log = logging.getLogger("coordinator.routes_observability")


def _require_admin(request: Request) -> None:
    """Reject non-admins. No-op when AUTH is disabled (dev/test).

    These operational endpoints (workers/earnings/metrics) expose
    cross-member data. The web BFF already gates them admin-only, but
    the coordinator is directly reachable through the Caddy catch-all
    (infra/Caddyfile), so the role check has to live here too — any
    valid member token would otherwise read the whole network's
    earnings + worker inventory."""
    if not AUTH_ENABLED:
        return
    member = getattr(request.state, "member", None)
    if member is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if member.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")


def _member_label(member, member_id: Optional[str]) -> Optional[str]:
    if member is None:
        return member_id
    return member["username"] or member["email"] or member_id


def build_router(
    db, get_r, tos_version: str, worker_status_fn, machine_display_name_fn,
    schedule_payload_fn,
) -> APIRouter:
    router = APIRouter()

    def _load_capabilities(worker_id: str) -> dict | None:
        raw = get_r().hget(WORKER_CAPABILITIES, worker_id)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _host_summary(parent_member_id: Optional[str]) -> Optional[dict]:
        """Display-safe summary of an invitee's host. Returns None when
        there's no parent (admin and root contributors). The summary is the
        only PII reveal here and matches what the redemption page already
        shows the invitee at signup."""
        if parent_member_id is None:
            return None
        row = db.get_member(parent_member_id)
        if row is None:
            return None
        keys = row.keys()
        return {
            "member_id": row["member_id"],
            "username": row["username"] if "username" in keys else None,
            "email": row["email"],
            "tier": row["tier"],
        }

    def _friend_or_403(caller, friend_member_id: str):
        """Resolve an accepted invitee for a friend-management mutation.

        Caller must be either the friend's host (parent_member_id match)
        or an admin. Returns the friend's member row on success; raises
        HTTPException otherwise. 404 (not 403) on a mismatch so the
        endpoint doesn't leak whether the member exists under a different
        host."""
        if caller is None:
            if AUTH_ENABLED:
                raise HTTPException(status_code=401, detail="unauthorized")
            # Auth-off dev mode: no host concept; refuse the mutation
            # because there's no "caller" to authorize.
            raise HTTPException(
                status_code=400,
                detail="friend management requires auth",
            )
        row = db.get_member(friend_member_id)
        if row is None:
            raise HTTPException(status_code=404, detail="friend not found")
        is_owner = row["parent_member_id"] == caller.member_id
        is_admin = caller.role == "admin"
        if not (is_owner or is_admin):
            raise HTTPException(status_code=404, detail="friend not found")
        return row

    @router.get("/workers")
    def workers(request: Request):
        _require_admin(request)
        rows = db.list_workers()
        earnings_by_worker = {row["worker_id"]: row for row in db.list_earnings()}
        # One bulk read instead of a per-worker get_member() query — the
        # admin dashboard renders every row at once, so N+1 here would mean
        # N+1 SQLite round-trips per page load.
        members_by_id = {m["member_id"]: m for m in db.list_members()}
        now = time.time()
        out = []
        for w in rows:
            wid = w["worker_id"]
            live_status = worker_status_fn(wid, now)
            last_seen = float(w["last_seen"] or 0)
            e = earnings_by_worker.get(wid)
            owner_id = w["owner_member_id"] if "owner_member_id" in w.keys() else None
            owner = members_by_id.get(owner_id) if owner_id else None
            owner_label = (
                (owner["username"] or owner["email"] or owner_id) if owner else owner_id
            )
            raw_display_name = w["display_name"] if "display_name" in w.keys() else None
            out.append(
                {
                    "worker_id": wid,
                    "display_name": machine_display_name_fn(raw_display_name, None, wid),
                    "status": live_status,
                    "last_seen": last_seen,
                    "seconds_since_heartbeat": round(now - last_seen, 2) if last_seen else None,
                    "alive": live_status != "offline",
                    "owner_member_id": owner_id,
                    "owner_label": owner_label,
                    "total_tokens": int(e["total_tokens"]) if e else 0,
                    "total_jobs": int(e["total_jobs"]) if e else 0,
                    "total_usd": round(float(e["total_usd"]), 8) if e else 0.0,
                    "capabilities": _load_capabilities(wid),
                    "canary_score": db.canary_score_for_worker(wid, limit=CANARY_SCORE_WINDOW),
                }
            )
        return {"workers": out}

    @router.get("/jobs")
    def jobs_listing(
        request: Request,
        worker_id: Optional[str] = None,
        member_id: Optional[str] = None,
        status: Optional[str] = None,
        tool: Optional[str] = None,
        limit: int = 200,
    ):
        """Most-recent-first job history for the admin dashboard's job
        listing page. Every filter is optional and URL-driven (KISS) —
        ``worker_id``/``member_id`` are what the dashboard's All Workers
        table links to, ``status``/``tool`` are free extras since the
        query already builds a WHERE clause.

        Also resolves display labels for the active worker_id/member_id
        filter (if any) so the page can render "jump to the other view"
        links at the top without a second round-trip."""
        _require_admin(request)
        rows = db.list_jobs(
            worker_id=worker_id or None,
            submitted_by_member_id=member_id or None,
            status=status or None,
            tool=tool or None,
            limit=limit,
        )
        members_by_id = {m["member_id"]: m for m in db.list_members()}
        workers_by_id = {w["worker_id"]: w for w in db.list_workers()}

        def _worker_label(wid: Optional[str]) -> Optional[str]:
            if not wid:
                return None
            w = workers_by_id.get(wid)
            raw = w["display_name"] if w is not None and "display_name" in w.keys() else None
            return machine_display_name_fn(raw, None, wid)

        def _worker_gpu(wid: Optional[str]) -> Optional[str]:
            if not wid:
                return None
            caps = _load_capabilities(wid)
            if not caps or not caps.get("gpu_model"):
                return None
            vram = caps.get("vram_gb")
            return f"{caps['gpu_model']} ({vram} GB)" if vram else caps["gpu_model"]

        jobs_out = []
        for j in rows:
            started_at = j["started_at"]
            queue_seconds = (
                round(started_at - j["submitted_at"], 2)
                if started_at is not None else None
            )
            jobs_out.append({
                "job_id": j["job_id"],
                "prompt": j["prompt"],
                "status": j["status"],
                "tool": j["tool"] if "tool" in j.keys() else "chat",
                "worker_id": j["worker_id"],
                "worker_label": _worker_label(j["worker_id"]),
                "worker_gpu": _worker_gpu(j["worker_id"]),
                "model": j["model"],
                "prompt_tokens": j["prompt_tokens"],
                "completion_tokens": j["completion_tokens"],
                "earnings": j["earnings"],
                "attempts": j["attempts"],
                "queue_seconds": queue_seconds,
                "duration_seconds": j["duration_seconds"],
                "submitted_at": j["submitted_at"],
                "completed_at": j["completed_at"],
                "error": j["error"],
                "submitted_by_member_id": j["submitted_by_member_id"],
                "submitted_by_label": _member_label(
                    members_by_id.get(j["submitted_by_member_id"]),
                    j["submitted_by_member_id"],
                ),
            })

        filter_worker = None
        if worker_id:
            owner_id = db.worker_owner(worker_id)
            caps = _load_capabilities(worker_id) or {}
            filter_worker = {
                "worker_id": worker_id,
                "label": _worker_label(worker_id),
                "owner_member_id": owner_id,
                "owner_label": _member_label(members_by_id.get(owner_id), owner_id) if owner_id else None,
                "gpu_model": caps.get("gpu_model"),
                "vram_gb": caps.get("vram_gb"),
            }

        filter_member = None
        if member_id:
            m = members_by_id.get(member_id)
            owned_workers = db.list_workers_for_member(member_id)
            filter_member = {
                "member_id": member_id,
                "label": _member_label(m, member_id),
                "workers": [
                    {"worker_id": w["worker_id"], "label": _worker_label(w["worker_id"])}
                    for w in owned_workers
                ],
            }

        return {
            "jobs": jobs_out,
            "filter_worker": filter_worker,
            "filter_member": filter_member,
        }

    @router.get("/me")
    def me(request: Request):
        """Identity + quota for the caller. When auth is disabled (no API_TOKEN
        env), reports ``auth_disabled`` so dev/test loops don't have to special-case."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                return {"auth_disabled": True}
            raise HTTPException(status_code=401, detail="unauthorized")
        usage = db.member_usage_today(member.member_id)
        paired_count = len(db.list_member_tokens(member.member_id))
        # Earnings aggregated across every worker this member owns. The
        # bridge join lives in db.member_earnings; admin members and
        # never-contributed members both get zeros, which the account page
        # renders as a "contribute to start earning" empty state. See
        # business.md → "Dual-role accounting" for why the two ledgers
        # stay independent and just meet at display time here.
        earnings = db.member_earnings(member.member_id)
        # tier_quota is the *display* allowance for this tier (see
        # coordinator/tiers.py). It's not the enforcer — the per-member
        # daily_quota_* columns still gate /generate — but the account page
        # uses it as the denominator for the "% of your daily allowance"
        # tip on the invite form. Admin members are unlimited regardless
        # of tier; null out both axes so the UI shows "unlimited" instead
        # of an arbitrary BRONZE-ish percentage.
        if member.role == "admin":
            tier_quota = {"tokens": None, "images": None, "voice_minutes": None}
        else:
            tier_quota = dict(_tier_quota_for(member.tier))
        # voice_seconds → voice_minutes for display. We store seconds in
        # member_usage so sub-second segments accumulate without rounding,
        # but the user-facing meter is minutes — see voice-phase1 design.
        voice_minutes_used = round(usage.get("voice_seconds", 0.0) / 60.0, 2)
        return {
            "member_id": member.member_id,
            "email": member.email,
            "role": member.role,
            "parent_member_id": member.parent_member_id,
            "host": _host_summary(member.parent_member_id),
            "tier": member.tier,
            "tier_quota": tier_quota,
            "daily_quota_tokens": member.daily_quota_tokens,
            "daily_quota_images": member.daily_quota_images,
            "daily_quota_voice_minutes": member.daily_quota_voice_minutes,
            "voice_minutes_today": voice_minutes_used,
            "username": member.username,
            "has_password": member.has_password,
            "password_set_at": member.password_set_at,
            "email_verified": member.email_verified,
            # Count of additional bearers in member_tokens — i.e. paired
            # agents. Zero means "no contributing machine yet" and the
            # web UI shows the contribute pitch in the topbar.
            "paired_machines_count": paired_count,
            "usage_today": usage,
            "earnings": earnings,
            "tos": {
                "accepted_at": member.tos_accepted_at,
                "version": member.tos_version,
                "current_version": tos_version,
                "needs_reaccept": member.tos_version != tos_version,
            },
        }

    @router.get("/me/machines")
    def my_machines(request: Request):
        """Account-page "This PC" section: every paired agent attached to
        this member, with the short hash prefix used as the row id for
        the per-machine unpair button. Never reveals the raw bearer (we
        don't have it — we only stored the hash) or even the full hash;
        the prefix is enough to disambiguate rows in the UI and is what
        the unpair POST takes as a slug.

        Also includes an ``owned_workers`` list — workers the member's
        machines have actually registered. Used by the UI to badge
        "partial contributor" on chat-only workers (image bootstrap
        failed). A pairing token without a matching worker just means the
        agent paired but hasn't called /register yet; it's surfaced as a
        pending machine via the ``machines`` list."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                return {"machines": [], "owned_workers": []}
            raise HTTPException(status_code=401, detail="unauthorized")
        now = time.time()
        # Unified machine list: one row per paired machine (member_tokens),
        # joined to its runtime worker row, enriched with the uptime
        # schedule + a computed status the Machines page renders directly.
        machines = []
        for row in db.list_machines_for_member(member.member_id):
            wid = row["worker_id"]
            paused = bool(row["paused"])
            sched_enabled = bool(row["sched_enabled"])
            start_min = row["sched_start_min"]
            end_min = row["sched_end_min"]
            tz = row["sched_tz"]
            allowed = machine_schedule.allowed_now(
                paused=paused, sched_enabled=sched_enabled,
                start_min=start_min, end_min=end_min, tz_name=tz,
            )
            sleeping_until = (
                None if allowed
                else machine_schedule.next_open_local(
                    start_min=start_min, end_min=end_min, tz_name=tz,
                )
            )
            if wid is None:
                # Paired but the agent hasn't called /register yet.
                status, last_seen, tools, is_partial = "pending", None, [], False
                gpu_model, vram_gb = None, None
            else:
                raw_tools = row["worker_tools_json"]
                try:
                    tools = json.loads(raw_tools) if raw_tools else ["chat"]
                except (TypeError, json.JSONDecodeError):
                    tools = ["chat"]
                is_partial = "image" not in tools
                last_seen = float(row["worker_last_seen"] or 0) or None
                caps = _load_capabilities(wid) or {}
                gpu_model = caps.get("gpu_model")
                vram_gb = caps.get("vram_gb")
                # Schedule state overlays the live worker status: a sleeping
                # or paused machine is intentionally not working, which reads
                # very differently from "offline" (crashed / powered off).
                if paused:
                    status = "paused"
                elif not allowed:
                    status = "sleeping"
                else:
                    status = worker_status_fn(wid, now)
            machines.append({
                "id": row["token_hash"][:12],
                "name": machine_display_name_fn(row["worker_display_name"], row["label"], wid),
                "label": row["label"] or "agent",
                "worker_id": wid,
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
                "last_seen": last_seen,
                "status": status,
                "tools": tools,
                "is_partial": is_partial,
                "gpu_model": gpu_model,
                "vram_gb": vram_gb,
                "allowed_now": allowed,
                "sleeping_until": sleeping_until,
                "schedule": {
                    "enabled": sched_enabled,
                    "paused": paused,
                    "start_min": start_min,
                    "end_min": end_min,
                    "tz": tz,
                },
            })
        worker_rows = db.list_workers_for_member(member.member_id)
        stale_cutoff = now - STALE_WORKER_HIDE_DAYS * 86400.0
        owned_workers = []
        hidden_stale_count = 0
        partial_count = 0
        for w in worker_rows:
            wid = w["worker_id"]
            last_seen = float(w["last_seen"] or 0)
            # Hide workers we haven't heard from in 30+ days — they're
            # almost always an old install lingering after the user
            # rebuilt the machine. The row stays in the DB so the
            # "forget" button can still target it via /me/workers/all
            # (future); the registered-workers UI just doesn't show them.
            if last_seen and last_seen < stale_cutoff:
                hidden_stale_count += 1
                continue
            tools = db.worker_tools(wid) or ["chat"]
            is_partial = "image" not in tools
            if is_partial:
                partial_count += 1
            owned_workers.append({
                "worker_id": wid,
                "status": worker_status_fn(wid, now),
                "last_seen": last_seen,
                "tools": tools,
                "is_partial": is_partial,
            })
        return {
            "machines": machines,
            "owned_workers": owned_workers,
            "hidden_stale_count": hidden_stale_count,
            "partial_contributor_count": partial_count,
        }

    @router.patch("/me/machines/{machine_id}/schedule")
    def update_machine_schedule(
        machine_id: str, req: MachineScheduleUpdate, request: Request,
    ):
        """Set a machine's uptime schedule. ``machine_id`` is the 12-char
        handle from /me/machines; resolved to the full token_hash scoped to
        the caller so one member can't reschedule another's machine."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                return {"ok": True}
            raise HTTPException(status_code=401, detail="unauthorized")
        token_hash = db.resolve_member_token_hash(member.member_id, machine_id, kind="agent")
        if token_hash is None:
            raise HTTPException(status_code=404, detail="machine not found")
        if req.enabled:
            if req.start_min is None or req.end_min is None:
                raise HTTPException(
                    status_code=422,
                    detail="start_min and end_min are required when enabled",
                )
            if not (0 <= req.start_min < 1440 and 0 <= req.end_min < 1440):
                raise HTTPException(
                    status_code=422,
                    detail="start_min/end_min must be minutes-from-midnight in [0,1440)",
                )
            if not req.tz or not machine_schedule.valid_tz(req.tz):
                raise HTTPException(
                    status_code=422, detail=f"unknown or missing timezone: {req.tz!r}",
                )
        ok = db.set_machine_schedule(
            member.member_id, token_hash,
            paused=req.paused, sched_enabled=req.enabled,
            start_min=req.start_min, end_min=req.end_min, tz=req.tz,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="machine not found")
        log.info(
            "machine schedule updated",
            extra={
                "event": "machine_schedule_updated",
                "by_member_id": member.member_id,
                "paused": req.paused,
                "enabled": req.enabled,
            },
        )
        return {"ok": True, **schedule_payload_fn(token_hash)}

    @router.post("/me/workers/{worker_id}/forget")
    def forget_my_worker(worker_id: str, request: Request):
        """Owner deletes a stale worker row from the account page.
        Earnings rows survive (see db.delete_worker docstring); only the
        workers row goes. Scoped to the caller's owned workers — a
        stranger asking to forget someone else's worker gets 404."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                return {"deleted": False}
            raise HTTPException(status_code=401, detail="unauthorized")
        if db.worker_owner(worker_id) != member.member_id:
            # 404 (not 403) — don't reveal whether someone else owns it.
            raise HTTPException(status_code=404, detail="worker not found")
        deleted = db.delete_worker(worker_id)
        log.info(
            "worker forgotten",
            extra={
                "event": "worker_forgotten",
                "worker_id": worker_id,
                "by_member_id": member.member_id,
            },
        )
        return {"deleted": deleted}

    @router.get("/me/contributor-status")
    def my_contributor_status(request: Request):
        """7-day uptime summary + tier-engine state for the caller.

        Returns the data the account page needs to render "you're at
        BRONZE, meeting X/Y requirements; SILVER needs Z" — so the host
        can see exactly what's keeping them from the next tier without
        reading the engine logs. Admin members get a synthetic
        "admin"-shaped response that the UI treats as "unbounded — tier
        engine does not apply."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                return {"auth_disabled": True}
            raise HTTPException(status_code=401, detail="unauthorized")
        if member.role == "admin":
            return {
                "tier": member.tier,
                "is_admin": True,
                "engine_applies": False,
            }
        summary = db.member_uptime_summary(member.member_id, window_days=7)
        current = member.tier
        cur_req = dict(_tier_requirements_for(current))
        next_tier = _tier_above(current)
        next_req = dict(_tier_requirements_for(next_tier)) if next_tier else None
        prev_tier = _tier_below(current)
        # tier_below_threshold_since / tier_last_changed_at live on the
        # member row but aren't on the Member dataclass — read directly.
        row = db.get_member(member.member_id)
        keys = row.keys() if row is not None else []
        below_since = (
            float(row["tier_below_threshold_since"])
            if row is not None
            and "tier_below_threshold_since" in keys
            and row["tier_below_threshold_since"] is not None
            else None
        )
        last_changed = (
            float(row["tier_last_changed_at"])
            if row is not None
            and "tier_last_changed_at" in keys
            and row["tier_last_changed_at"] is not None
            else None
        )
        days_online = summary["days_online"]
        avg_hours = summary["avg_hours_per_active_day"]
        return {
            "tier": current,
            "is_admin": False,
            "engine_applies": True,
            "uptime_7d": summary,
            "current_tier_requirements": cur_req,
            "meets_current": _tier_meets(current, days_online, avg_hours),
            "next_tier": next_tier,
            "next_tier_requirements": next_req,
            "meets_next": (
                next_tier is not None
                and _tier_meets(next_tier, days_online, avg_hours)
            ),
            "previous_tier": prev_tier,
            "tier_below_threshold_since": below_since,
            "tier_last_changed_at": last_changed,
        }

    @router.post("/me/machines/{prefix}/unpair")
    def unpair_my_machine(prefix: str, request: Request):
        """Revoke a paired machine from the account page. Caller can only
        unpair machines they own — the lookup is scoped to the caller's
        member_id, so a prefix that matches another member's row no-ops
        rather than leaking that the row exists."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                return {"deleted": False}
            raise HTTPException(status_code=401, detail="unauthorized")
        # Lookup the full hash by scanning the caller's own machine tokens
        # (kind="agent" — never matches a self-serve API key row). A single
        # member's machine count is tiny (one row per paired PC), so this
        # is cheap.
        rows = db.list_member_tokens(member.member_id, kind="agent")
        target = next((r["token_hash"] for r in rows if r["token_hash"].startswith(prefix)), None)
        if target is None:
            # 404 leaks no info — the prefix just doesn't match anything
            # in the caller's scope, whether or not it exists elsewhere.
            raise HTTPException(status_code=404, detail="machine not found")
        deleted = db.delete_member_token(member.member_id, target, kind="agent")
        return {"deleted": deleted}

    @router.get("/me/friends")
    def my_friends(request: Request):
        """Account-page Friends section: everyone the caller has invited.
        Combines the open/expired/revoked invite list (not yet claimed) with
        the accepted-member list (claimed). One round trip serves both
        states so the UI doesn't have to stitch them.

        Restricted to authenticated callers. The data is the caller's own
        sub-tree; nothing leaks across members."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                # Dev mode without API_TOKEN — return empty so the UI can
                # render without special-casing.
                return {"open_invites": [], "accepted": []}
            raise HTTPException(status_code=401, detail="unauthorized")

        now = time.time()
        invites = db.list_invites_by_contributor(member.member_id)
        open_invites = []
        for inv_row in invites:
            state = _invite_state(inv_row, now)
            if state in ("accepted",):
                continue
            keys = inv_row.keys()
            open_invites.append({
                "code": inv_row["code"],
                "state": state,
                "daily_quota_tokens": inv_row["daily_quota_tokens"],
                "daily_quota_images": (
                    inv_row["daily_quota_images"]
                    if "daily_quota_images" in keys
                    else None
                ),
                "invitee_email": inv_row["invitee_email"],
                "expires_at": inv_row["expires_at"],
                "created_at": inv_row["created_at"],
            })

        accepted = []
        for row in db.list_members():
            if row["parent_member_id"] != member.member_id:
                continue
            keys = row.keys()
            accepted.append({
                "member_id": row["member_id"],
                "username": row["username"] if "username" in keys else None,
                "email": row["email"],
                "daily_quota_tokens": row["daily_quota_tokens"],
                "daily_quota_images": (
                    row["daily_quota_images"]
                    if "daily_quota_images" in keys
                    else None
                ),
                "tier": row["tier"],
                "revoked_at": row["revoked_at"],
                "created_at": row["created_at"],
                "last_active_at": row["last_active_at"],
            })

        return {"open_invites": open_invites, "accepted": accepted}

    @router.post("/me/friends/{friend_member_id}/quota")
    def update_friend_quota(
        friend_member_id: str,
        req: FriendQuotaUpdateRequest,
        request: Request,
    ):
        """Host edits an accepted invitee's daily caps. Both axes are
        rewritten on every call — the host form always sends the
        new-full-state pair. ``null`` on an axis means unlimited.

        Scoped to the friend's host (or admin); a stranger gets 404 so
        the endpoint doesn't reveal whose tree the member belongs to."""
        caller = getattr(request.state, "member", None)
        _friend_or_403(caller, friend_member_id)
        updated = db.update_member_quotas(
            friend_member_id,
            daily_quota_tokens=req.daily_quota_tokens,
            daily_quota_images=req.daily_quota_images,
        )
        if not updated:
            # Row vanished between the auth check and the update — race
            # with a revoke from another tab. Surface a 404 so the UI
            # re-renders against fresh state.
            raise HTTPException(status_code=404, detail="friend not found")
        log.info(
            "friend quota updated",
            extra={
                "event": "friend_quota_updated",
                "friend_member_id": friend_member_id,
            },
        )
        return {
            "ok": True,
            "daily_quota_tokens": req.daily_quota_tokens,
            "daily_quota_images": req.daily_quota_images,
        }

    @router.post("/me/friends/{friend_member_id}/revoke")
    def revoke_friend(friend_member_id: str, request: Request):
        """Host revokes an accepted invitee's access. Sets
        ``members.revoked_at`` so all auth lookups for that member
        immediately fail. Idempotent: revoking an already-revoked friend
        returns ``ok: true, was_already_revoked: true`` rather than 404
        so the UI can render a friendly state without distinguishing
        races from genuine retries."""
        caller = getattr(request.state, "member", None)
        _friend_or_403(caller, friend_member_id)
        now = time.time()
        revoked = db.revoke_member_by_id(friend_member_id, now)
        log.info(
            "friend revoked" if revoked else "friend already revoked",
            extra={
                "event": "friend_revoked",
                "friend_member_id": friend_member_id,
            },
        )
        return {"ok": True, "was_already_revoked": not revoked}

    return router
