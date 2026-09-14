"""Worker lifecycle: registration, heartbeat, and (in later commits of
this same god-file split) the job long-poll / claim / complete / cancel
surface. ``db`` is closed over directly; ``r`` as ``get_r`` (a zero-arg
getter — see coordinator/prompt_rewrite.py for why); ``write_heartbeat_fn``
because ``_write_heartbeat`` still lives in coordinator/main.py's shared
worker-status-helpers section (used by multiple route groups).

``build_router`` returns ``(router, schedule_payload_fn, require_worker_owner_fn)``
rather than a bare router: ``coordinator/routes_observability.py``'s
``update_machine_schedule`` needs the same ``_schedule_payload`` this
module's own ``heartbeat``/``next_job`` handlers call, and main.py's
not-yet-extracted job routes (``/jobs/next``, ``/jobs/claim``, ...)
still call ``_require_worker_owner`` as a bare name — both get handed
back for main.py to bind, same shape as ``coordinator/openai_compat.py``
receiving ``generate``/``result`` from the (still main.py-local)
generate()/result() pair. main.py wires it once:

    _workers_router, _schedule_payload, _require_worker_owner = (
        routes_workers.build_router(db, lambda: r, _write_heartbeat)
    )
    app.include_router(_workers_router)
    app.include_router(routes_observability.build_router(
        db, lambda: r, TOS_VERSION, _worker_status, _machine_display_name,
        _schedule_payload,
    ))
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from coordinator import events, member_auth
from coordinator import schedule as machine_schedule
from shared.auth import AUTH_ENABLED
from shared.config import WORKER_CAPABILITIES, WORKER_REGISTRY, WORKER_STATUS
from shared.models import HeartbeatRequest, WorkerIdent

log = logging.getLogger("coordinator.routes_workers")


def _caller_token_hash(request: Request) -> Optional[str]:
    """SHA256 of the caller's bearer, or None when there's no bearer
    (AUTH off in dev, or the in-VPS worker). Used to find the machine's
    pairing record for the worker_id link + schedule lookup."""
    raw = member_auth.parse_bearer(request.headers.get("authorization"))
    return member_auth.hash_token(raw) if raw else None


# How long an agent should wait between heartbeats while it's outside
# its uptime window (or paused). Returned in the /heartbeat response so
# a sleeping agent slows its polling yet still notices a schedule change
# within this window. Steady-state (working) cadence stays agent-side.
DOWNTIME_HEARTBEAT_SECONDS = 300


def build_router(db, get_r, write_heartbeat_fn):
    router = APIRouter()

    def _schedule_payload(token_hash: Optional[str]) -> dict:
        """Schedule + computed allowed_now for the machine identified by
        ``token_hash``. An unpaired/tokenless caller (server worker) has no
        schedule and is always allowed."""
        row = (
            db.get_machine_schedule_by_token(token_hash)
            if token_hash is not None
            else None
        )
        if row is None:
            return {"allowed_now": True, "schedule": None, "downtime_poll_seconds": DOWNTIME_HEARTBEAT_SECONDS}
        paused = bool(row["paused"])
        sched_enabled = bool(row["sched_enabled"])
        start_min = row["sched_start_min"]
        end_min = row["sched_end_min"]
        tz = row["sched_tz"]
        allowed = machine_schedule.allowed_now(
            paused=paused,
            sched_enabled=sched_enabled,
            start_min=start_min,
            end_min=end_min,
            tz_name=tz,
        )
        sleeping_until = (
            None if allowed or paused
            else machine_schedule.next_open_local(
                start_min=start_min, end_min=end_min, tz_name=tz,
            )
        )
        return {
            "allowed_now": allowed,
            "downtime_poll_seconds": DOWNTIME_HEARTBEAT_SECONDS,
            "sleeping_until": sleeping_until,
            "schedule": {
                "paused": paused,
                "enabled": sched_enabled,
                "start_min": start_min,
                "end_min": end_min,
                "tz": tz,
            },
        }

    def _require_worker_owner(request: Request, worker_id: str) -> None:
        """Reject when the authenticated member doesn't own the worker_id.
        Admin bypasses for operational override (incident response). When
        AUTH is disabled, ownership is unenforced (dev/test mode).

        An unowned legacy worker_id (owner_member_id NULL) also rejects —
        the caller should hit /register first to stamp ownership."""
        if not AUTH_ENABLED:
            return
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        if member.role == "admin":
            return
        owner = db.worker_owner(worker_id)
        if owner is None:
            raise HTTPException(
                status_code=403,
                detail="worker_id has no registered owner — call /register first",
            )
        if owner != member.member_id:
            raise HTTPException(status_code=403, detail="not your worker")

    @router.post("/register")
    def register(req: WorkerIdent, request: Request):
        r = get_r()
        now = time.time()
        member = getattr(request.state, "member", None)
        member_id = member.member_id if member is not None else None

        # Ownership claim — atomic so concurrent registers can't race.
        # When AUTH is off (dev mode), member_id is None and the
        # ownership check inside claim_worker_ownership is permissive.
        ok, existing_owner, is_new = db.claim_worker_ownership(
            req.worker_id, member_id, "idle", now,
        )
        if not ok:
            log.warning(
                "worker registration rejected",
                extra={
                    "event": "worker_owner_mismatch",
                    "worker_id": req.worker_id,
                },
            )
            raise HTTPException(
                status_code=403,
                detail="worker_id is owned by a different member",
            )

        # Link the runtime worker_id to the machine's pairing record so the
        # Machines page + schedule gate can join the two.
        token_hash = _caller_token_hash(request)
        if token_hash is not None:
            db.link_token_to_worker(token_hash, req.worker_id)

        r.sadd(WORKER_REGISTRY, req.worker_id)
        write_heartbeat_fn(req.worker_id, now, None)
        r.hset(WORKER_STATUS, req.worker_id, "idle")
        if req.capabilities is not None:
            r.hset(
                WORKER_CAPABILITIES,
                req.worker_id,
                req.capabilities.model_dump_json(),
            )
            # Mirror the advertised tools list to SQLite so the account
            # page can flag partial contributors (image bootstrap failed
            # ⇒ tools=["chat"] only) without a Redis round-trip — and so
            # the badge stays visible after the worker goes offline.
            db.set_worker_tools(
                req.worker_id,
                json.dumps(list(req.capabilities.tools or [])),
            )
        if req.display_name and req.display_name.strip():
            db.set_worker_display_name(req.worker_id, req.display_name.strip())
        log.info(
            "worker registered",
            extra={"event": "worker_registered", "worker_id": req.worker_id},
        )
        if is_new:
            events.emit(
                "worker.registered",
                worker_id=req.worker_id,
                owner_member_id=member_id,
            )
        return {"ok": True}

    @router.post("/heartbeat")
    def heartbeat(req: HeartbeatRequest, request: Request):
        _require_worker_owner(request, req.worker_id)
        now = time.time()
        # ``job_id`` is what the worker claims to currently be processing.
        # The reaper reads it on its next tick to decide whether an
        # in-flight job is still in the hands of its rightful claimant
        # (extend the deadline) or has actually gone silent (requeue).
        write_heartbeat_fn(req.worker_id, now, req.job_id)
        get_r().hset(WORKER_STATUS, req.worker_id, req.status)
        db.upsert_worker(req.worker_id, req.status, now)
        # Backfill the link for agents that paired before this slice (their
        # /register predates link stamping), then hand back the current
        # schedule so the agent can self-gate + slow its beat in downtime.
        token_hash = _caller_token_hash(request)
        if token_hash is not None:
            db.link_token_to_worker(token_hash, req.worker_id)
        return {"ok": True, **_schedule_payload(token_hash)}

    return router, _schedule_payload, _require_worker_owner
