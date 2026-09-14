"""Worker lifecycle: registration, heartbeat, job long-poll/claim/abandon/
partial (and, in a later commit of this same god-file split,
/jobs/complete + /jobs/cancel + /jobs/displayed). ``db`` is closed over
directly; ``r`` as ``get_r`` (a zero-arg getter — see
coordinator/prompt_rewrite.py for why); ``write_heartbeat_fn`` /
``job_row_to_envelope_fn`` because ``_write_heartbeat`` /
``_job_row_to_envelope`` still live in coordinator/main.py's shared
worker-status-helpers / not-yet-extracted generate() sections.

``build_router`` returns ``(router, schedule_payload_fn,
require_worker_owner_fn, verify_claim_or_410_fn)`` rather than a bare
router: ``coordinator/routes_observability.py``'s
``update_machine_schedule`` needs the same ``_schedule_payload`` this
module's own ``heartbeat``/``next_job`` handlers call, and main.py's
not-yet-extracted ``/jobs/complete`` still calls ``_require_worker_owner``
and ``_verify_claim_or_410`` as bare names — all three get handed back
for main.py to bind, same shape as ``coordinator/openai_compat.py``
receiving ``generate``/``result`` from the (still main.py-local)
generate()/result() pair. main.py wires it once:

    _workers_router, _schedule_payload, _require_worker_owner, _verify_claim_or_410 = (
        routes_workers.build_router(
            db, lambda: r, _write_heartbeat, _job_row_to_envelope,
        )
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
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from coordinator import events, member_auth, model_registry
from coordinator import schedule as machine_schedule
from shared.auth import AUTH_ENABLED
from shared.config import (
    JOB_AUDIO_CHUNKS,
    JOB_PARTIALS,
    JOB_PROCESSING,
    JOB_RESULTS,
    JOB_TIMEOUT_SECONDS,
    WORKER_CAPABILITIES,
    WORKER_REGISTRY,
    WORKER_STATUS,
    job_queue_for,
)
from shared.models import (
    HeartbeatRequest,
    JobClaimRequest,
    JobNextRequest,
    JobPartialRequest,
    WorkerIdent,
)

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

# Upper bound for /jobs/next long-poll wait time, enforced server-side
# so a misconfigured worker can't pin a coordinator request thread for
# minutes. Caddy's reverse-proxy has generous default timeouts but we
# don't want to depend on that — 30s also keeps the response window
# inside any aggressive intermediate proxy / load balancer defaults.
MAX_LONGPOLL_SECONDS = 30.0


def build_router(db, get_r, write_heartbeat_fn, job_row_to_envelope_fn):
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

    def _verify_claim_or_410(
        job_id: str,
        worker_id: str,
        claim_token: Optional[str],
    ) -> dict:
        """Verify the caller still holds the current claim for ``job_id``.

        Returns the parsed JOB_PROCESSING meta on success. On mismatch raises
        410 Gone with a structured reason so the worker can log it and
        bail without crediting local state — this is the protocol path for
        "the reaper requeued your job and someone else picked it up."

        A missing JOB_PROCESSING entry means either (a) the job was
        cancelled, or (b) the job already completed. Either way the
        completer's work is irrelevant and we 410. The completer is
        expected to drop the result on the floor; the user-visible state
        came from whoever finished first (or from the cancel marker)."""
        raw = get_r().hget(JOB_PROCESSING, job_id)
        if raw is None:
            raise HTTPException(
                status_code=410,
                detail={"reason": "no_active_claim",
                        "message": "job is no longer in flight (cancelled or already completed)"},
            )
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            meta = {}
        if meta.get("worker_id") != worker_id:
            raise HTTPException(
                status_code=410,
                detail={"reason": "claim_owned_by_other_worker",
                        "message": "another worker holds the current claim"},
            )
        expected = meta.get("claim_token")
        if expected and claim_token != expected:
            raise HTTPException(
                status_code=410,
                detail={"reason": "claim_token_mismatch",
                        "message": "your claim has expired; the job was reassigned"},
            )
        return meta

    def _issue_claim(worker_id: str, job_id: str, original: Optional[dict]) -> tuple[str, float]:
        """Record a claim for ``worker_id`` on ``job_id`` and return
        ``(claim_token, deadline)``. The token is the per-claim secret a
        completer/partialer must echo back; a re-dispense after the reaper
        requeues this job will mint a fresh token, so the original worker's
        eventual /complete trips a 410 instead of clobbering the new
        claimant's work."""
        r = get_r()
        now = time.time()
        deadline = now + JOB_TIMEOUT_SECONDS
        claim_token = uuid.uuid4().hex
        r.hset(
            JOB_PROCESSING,
            job_id,
            json.dumps({
                "worker_id": worker_id,
                "deadline": deadline,
                "job": original,
                "claim_token": claim_token,
            }),
        )
        r.hset(WORKER_STATUS, worker_id, "busy")
        db.mark_job_running(job_id, worker_id, now)
        return claim_token, deadline

    @router.post("/jobs/next")
    def next_job(req: JobNextRequest, request: Request):
        """HTTP job pickup for remote agents (e.g. the Windows gamer install).
        Pops one job, atomically issues a claim, and returns both the job
        and the ``claim_token`` the worker must echo back on subsequent
        /jobs/complete and /jobs/partial. Folding claim into the pickup
        means there is no in-between state where a job is popped but
        unclaimed.

        ``wait > 0`` switches the handler to long-poll mode (BLPOP). The
        worker call blocks for up to ``wait`` seconds (capped at
        MAX_LONGPOLL_SECONDS) until a job lands on any of the requested
        queues, then returns immediately. This drops job-dispatch latency
        from "0-5s polling gap" to "one network round-trip" — the worker
        is already blocking on Redis when /generate enqueues, and BLPOP
        wakes it up the moment the LPUSH completes.

        ``tools=[...]`` lets a multi-tool worker BLPOP across both chat
        and image queues in one call. Legacy single-tool agents (and the
        in-VPS worker.py) pass ``tool=X`` and ``wait=0`` and get the
        classic immediate-LPOP behavior unchanged."""
        r = get_r()
        _require_worker_owner(request, req.worker_id)
        # Uptime-schedule gate. A paused machine, or one outside its allowed
        # window, gets no work — returned as the normal no-job shape so the
        # agent's long-poll loop stays quiet (same as an empty queue).
        # Tokenless callers (in-VPS server worker) have no schedule and pass
        # through. This is the authoritative gate; the agent also self-gates
        # for efficiency, but never claims here regardless.
        if not _schedule_payload(_caller_token_hash(request))["allowed_now"]:
            return {"job": None}
        # Resolve the queue list. tools (list) wins when provided so a
        # v1.1.25+ agent's long-poll request takes precedence over its
        # legacy single-tool field; otherwise fall back to ``tool``.
        if req.tools:
            tools = [t.lower() for t in req.tools if t]
        else:
            tools = [(req.tool or "chat").lower()]
        if not tools:
            return {"job": None}
        # Defensive: never hand a worker a tool it didn't advertise at
        # /register. Without this, an image-only agent whose dual-loop
        # polling layer still BLPOPs the chat queue can pick up a chat
        # job and run it in mock mode (canary system catches some of
        # these as canary_failed; users feel the rest as mock responses
        # mid-conversation). worker_tools() returns None for legacy
        # workers that never advertised — those keep the historical
        # chat-only behavior implicitly via the chat-default fallback
        # below.
        advertised = db.worker_tools(req.worker_id)
        if advertised is None:
            advertised = ["chat"]
        advertised_set = {t.lower() for t in advertised}
        filtered = [t for t in tools if t in advertised_set]
        if not filtered:
            # Worker is asking for queues it can't actually serve. Treat
            # as no-job-available rather than a 4xx — keeps the
            # long-poll loop quiet on legacy agents that overshoot.
            return {"job": None}
        tools = filtered
        queues = [job_queue_for(t) for t in tools]

        wait = max(0.0, min(float(req.wait or 0.0), MAX_LONGPOLL_SECONDS))

        raw: Optional[str] = None
        if wait > 0:
            # BLPOP returns (key, value) tuple or None on timeout. With
            # multiple keys it scans them in argument order and pops from
            # the first non-empty one — so passing the agent-preferred
            # queue order (last_tool first, then the rest) preserves the
            # warm-model affinity the legacy two-LPOP-loop had.
            result = r.blpop(queues, timeout=int(round(wait)))
            if not result:
                return {"job": None}
            _, raw = result
        else:
            for q in queues:
                raw = r.lpop(q)
                if raw is not None:
                    break
            if raw is None:
                return {"job": None}

        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            return {"job": None}
        job_id = job.get("job_id")
        claim_token, deadline = _issue_claim(req.worker_id, job_id, job)
        log.info(
            "job dispensed",
            extra={
                "event": "job_dispensed",
                "job_id": job_id,
                "worker_id": req.worker_id,
            },
        )
        log.info(
            "job claimed",
            extra={"event": "job_claimed", "job_id": job_id, "worker_id": req.worker_id},
        )
        return {"job": job, "claim_token": claim_token, "deadline": deadline}

    @router.post("/jobs/claim")
    def claim(req: JobClaimRequest, request: Request):
        """Legacy claim path for the in-VPS worker (which pops jobs from
        Redis directly with BLPOP and then calls /jobs/claim). Remote agents
        use the atomic /jobs/next path instead. Returns the ``claim_token``
        the worker must include on subsequent /jobs/complete and
        /jobs/partial calls."""
        _require_worker_owner(request, req.worker_id)
        # find original job payload — best-effort, used only for requeue on timeout
        raw = get_r().hget(JOB_RESULTS, req.job_id)
        original = None
        if raw is None:
            row = db.get_job(req.job_id)
            if row:
                original = job_row_to_envelope_fn(row)
        claim_token, deadline = _issue_claim(req.worker_id, req.job_id, original)
        log.info(
            "job claimed",
            extra={"event": "job_claimed", "job_id": req.job_id, "worker_id": req.worker_id},
        )
        return {"ok": True, "deadline": deadline, "claim_token": claim_token}

    @router.post("/jobs/abandon")
    def abandon(req: JobClaimRequest, request: Request):
        """Worker voluntarily gives a claimed job back to the queue.

        Used when the contributor's machine sees user activity and the
        agent is configured with ``idle.override_drain: true`` (the
        "throw out the money" path — paid contributors who'd rather
        forfeit earnings than make the user wait).

        Idempotent: a missing job_id is a no-op. We requeue the job from
        the processing-hash record so the next worker picks up the same
        prompt + model. Earnings are zeroed because no work was reported.
        """
        r = get_r()
        _require_worker_owner(request, req.worker_id)
        raw = r.hget(JOB_PROCESSING, req.job_id)
        if raw is None:
            return {"ok": True, "requeued": False, "reason": "not in flight"}
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            meta = {}
        # Token gate — a stale agent whose claim was already requeued and
        # reassigned must not be able to requeue a SECOND copy via abandon.
        expected_token = meta.get("claim_token")
        if expected_token and req.claim_token and req.claim_token != expected_token:
            raise HTTPException(
                status_code=410,
                detail={"reason": "claim_token_mismatch",
                        "message": "your claim has expired; abandon is a no-op"},
            )
        if meta.get("worker_id") != req.worker_id:
            return {"ok": True, "requeued": False, "reason": "not the current claimant"}
        original = meta.get("job")
        if original is None:
            row = db.get_job(req.job_id)
            if row is not None:
                original = job_row_to_envelope_fn(row)
        if original is not None:
            # Requeue to the original routing queue so an abandoned image
            # job doesn't end up in front of chat workers (which would
            # then 'error' on every claim), and an abandoned smart job
            # goes back to the pipeline head rather than a 3B worker.
            r.rpush(
                job_queue_for(
                    original.get("route") or original.get("tool", "chat")
                ),
                json.dumps(original),
            )
        r.hdel(JOB_PROCESSING, req.job_id)
        r.hdel(JOB_PARTIALS, req.job_id)
        r.delete(f"{JOB_AUDIO_CHUNKS}:{req.job_id}")
        r.hset(WORKER_STATUS, req.worker_id, "idle")
        db.requeue_job(req.job_id)
        log.info(
            "job abandoned",
            extra={
                "event": "job_abandoned",
                "job_id": req.job_id,
                "worker_id": req.worker_id,
            },
        )
        return {"ok": True, "requeued": True}

    @router.post("/jobs/partial")
    def partial(req: JobPartialRequest, request: Request):
        """Streaming push from the worker mid-generation.

        ``text`` is the full accumulated output so far (not a delta). We
        write to Redis JOB_PARTIALS for the polling read path and UPDATE
        the pending assistant message row so a client that reloads the
        conversation mid-stream sees the partial answer immediately,
        without needing to wait for the next poll. Late partials that
        arrive after /jobs/complete are silently dropped — the message
        has moved past 'pending' and update_message_partial's WHERE clause
        filters them out.
        """
        r = get_r()
        _require_worker_owner(request, req.worker_id)
        try:
            _verify_claim_or_410(req.job_id, req.worker_id, req.claim_token)
        except HTTPException as exc:
            # Fire-and-forget partial: a stale partial dropping out as 410
            # is harmless (the worker will see the matching 410 on its
            # /complete and act there). But surface the 410 anyway so the
            # worker can log it.
            if exc.status_code == 410:
                return {"ok": True, "stale": True}
            raise
        text = req.text or ""
        r.hset(JOB_PARTIALS, req.job_id, text)
        # Stamp first-partial wall-clock on the jobs row. The WHERE-NULL
        # guard in mark_job_first_partial means only the first partial of
        # each job wins; subsequent partials are no-ops.
        db.mark_job_first_partial(req.job_id, time.time())
        if req.audio_chunk_b64 is not None and req.audio_chunk_seq is not None:
            # Voice-mode chat exponential batching: each Piper synth for a
            # sentence-batch lands here keyed by seq. Storing per-seq means
            # a retried partial overwrites idempotently and the result
            # endpoint can HGETALL → sort by seq → return ordered chunks.
            # Audio kept in its own hash (not JSON-merged into JOB_PARTIALS)
            # so the text-only partial reader stays an hget.
            chunks_key = f"{JOB_AUDIO_CHUNKS}:{req.job_id}"
            r.hset(
                chunks_key,
                str(req.audio_chunk_seq),
                json.dumps({
                    "seq": int(req.audio_chunk_seq),
                    "audio_b64": req.audio_chunk_b64,
                    "audio_seconds": float(req.audio_chunk_seconds or 0.0),
                }),
            )
            # Expire the chunk set after a generous TTL so a cancelled or
            # orphaned job doesn't leak audio bytes in Redis forever.
            # Re-sets the TTL on every chunk write, which is harmless and
            # keeps the key alive while the job is still streaming.
            r.expire(chunks_key, 24 * 3600)
            log.info(
                "voice chunk received",
                extra={
                    "event": "voice_chunk_partial",
                    "job_id": req.job_id,
                    "seq": req.audio_chunk_seq,
                    "audio_seconds": float(req.audio_chunk_seconds or 0.0),
                },
            )
        msg = db.get_message_by_job(req.job_id)
        if msg is not None:
            db.update_message_partial(msg["message_id"], text)
        return {"ok": True}

    return router, _schedule_payload, _require_worker_owner, _verify_claim_or_410
