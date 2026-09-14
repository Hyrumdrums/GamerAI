"""Worker lifecycle: registration, heartbeat, job long-poll/claim/abandon/
partial/complete (and, in a later commit of this same god-file split,
/jobs/cancel + /jobs/displayed). ``db`` is closed over directly; ``r``
as ``get_r`` (a zero-arg getter — see coordinator/prompt_rewrite.py for
why); ``write_heartbeat_fn`` / ``job_row_to_envelope_fn`` /
``dispatch_image_after_rewrite_fn`` / ``dispatch_search_after_rewrite_fn``
because ``_write_heartbeat`` / ``_job_row_to_envelope`` still live in
coordinator/main.py's shared worker-status-helpers / not-yet-extracted
generate() sections, and the two rewrite-dispatch callables are
coordinator/main.py-local names bound from
``coordinator.prompt_rewrite.build_rewrite_helpers(...)``'s own return
tuple — not directly importable from prompt_rewrite.py itself.

``build_router`` returns ``(router, schedule_payload_fn,
require_worker_owner_fn, verify_claim_or_410_fn)`` rather than a bare
router: ``coordinator/routes_observability.py``'s
``update_machine_schedule`` needs the same ``_schedule_payload`` this
module's own ``heartbeat``/``next_job`` handlers call, and main.py's
not-yet-extracted ``/jobs/cancel``/``/jobs/displayed`` still call
``_require_worker_owner`` as a bare name — all get handed back for
main.py to bind, same shape as ``coordinator/openai_compat.py``
receiving ``generate``/``result`` from the (still main.py-local)
generate()/result() pair. main.py wires it once:

    _workers_router, _schedule_payload, _require_worker_owner, _verify_claim_or_410 = (
        routes_workers.build_router(
            db, lambda: r, _write_heartbeat, _job_row_to_envelope,
            _dispatch_image_after_rewrite, _dispatch_search_after_rewrite,
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

from coordinator import canaries as canary_lib
from coordinator import events, member_auth, model_registry, notifications
from coordinator import schedule as machine_schedule
from coordinator.image_moderation import _save_image_or_raise
from coordinator.image_params import image_cost_multiplier
from shared.auth import AUTH_ENABLED
from shared.config import (
    CANARY_PENDING,
    IMAGE_REWRITE_PENDING,
    IMAGE_UNIT_COST_BASE,
    JOB_AUDIO_CHUNKS,
    JOB_PARTIALS,
    JOB_PROCESSING,
    JOB_RESULTS,
    JOB_TIMEOUT_SECONDS,
    RATE_PER_TOKEN,
    SEARCH_AUTO_DISABLED,
    SEARCH_REWRITE_PENDING,
    SUMMARY_PENDING,
    WORKER_CAPABILITIES,
    WORKER_EARNINGS,
    WORKER_REGISTRY,
    WORKER_SHARE,
    WORKER_STATUS,
    job_queue_for,
)
from shared.models import (
    HeartbeatRequest,
    JobClaimRequest,
    JobCompleteRequest,
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


def build_router(
    db, get_r, write_heartbeat_fn, job_row_to_envelope_fn,
    dispatch_image_after_rewrite_fn, dispatch_search_after_rewrite_fn,
):
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

    @router.post("/jobs/complete")
    def complete(req: JobCompleteRequest, request: Request):
        """Worker submits result. Coordinator writes Redis result, earnings, SQLite row."""
        r = get_r()
        _require_worker_owner(request, req.worker_id)
        # Canaries are issued straight into JOB_PROCESSING but the canary
        # path predates claim tokens — skip the gate for those so the
        # canary scheduler doesn't have to thread a token through. Real
        # jobs always carry one.
        canary_id = r.hget(CANARY_PENDING, req.job_id)
        if canary_id is None:
            # 410 here propagates back to the worker so it knows the
            # result isn't being accepted — agent code interprets this
            # as "lost the race / cancelled" and skips local earnings
            # credit.
            _verify_claim_or_410(req.job_id, req.worker_id, req.claim_token)
        now = time.time()
        tokens = int(req.completion_tokens or 0)

        # Canary check: if this job_id was injected as a canary, divert to
        # the verification path and skip earnings + usage rollup. The worker
        # is told "ok" the same way as a real job — we don't surface canary
        # status, because doing so would let a malicious worker special-case
        # canary handling and pass every check.
        if canary_id:
            canary_row = db.get_canary(canary_id)
            matched = (
                req.status == "complete"
                and canary_row is not None
                and canary_lib.verify_response(canary_row, req.text or "")
            )
            snippet = (req.text or "")[:500]
            db.record_canary_result(
                result_id="cr_" + uuid.uuid4().hex[:12],
                canary_id=canary_id,
                worker_id=req.worker_id,
                job_id=req.job_id,
                response_text_snippet=snippet,
                matched=matched,
            )
            db.mark_job_complete(
                job_id=req.job_id,
                worker_id=req.worker_id,
                model=req.model,
                text=req.text,
                prompt_tokens=req.prompt_tokens,
                completion_tokens=tokens,
                earnings=0.0,
                duration_seconds=req.duration_seconds,
                completed_at=now,
                status="canary_complete" if matched else "canary_failed",
                error=req.error,
            )
            r.hdel(CANARY_PENDING, req.job_id)
            r.hdel(JOB_PROCESSING, req.job_id)
            r.hset(WORKER_STATUS, req.worker_id, "idle")
            log.info(
                "canary verified" if matched else "canary failed",
                extra={
                    "event": "canary_matched" if matched else "canary_mismatch",
                    "job_id": req.job_id,
                    "worker_id": req.worker_id,
                    "canary_id": canary_id,
                },
            )
            return {"ok": True, "earnings": 0.0}

        # Image-prompt rewrite: this completion is a hidden chat job whose
        # only purpose is to produce a context-aware rewrite of a queued
        # image job's prompt. Dispatching happens BEFORE the rest of the
        # complete flow runs, so the image lands on the image queue at the
        # earliest moment after the rewrite text is available. The rewrite
        # job itself then falls through to the normal chat-completion path
        # so the worker gets paid for its compute (it really did inference
        # work) and the rewrite job's ledger row is recorded normally.
        rewrite_link_raw = r.hget(IMAGE_REWRITE_PENDING, req.job_id)
        if rewrite_link_raw:
            try:
                link = json.loads(rewrite_link_raw)
            except json.JSONDecodeError:
                link = None
            if link:
                rewritten = req.text if req.status == "complete" else None
                try:
                    dispatch_image_after_rewrite_fn(req.job_id, link, rewritten)
                except Exception as exc:
                    log.warning(
                        "image-rewrite dispatch failed; falling back to raw prompt",
                        extra={
                            "event": "rewrite_dispatch_failed",
                            "rewrite_job_id": req.job_id,
                            "error": str(exc),
                        },
                    )
                    # Best-effort recovery: push the image envelope with
                    # the original prompt so the user still gets SOMETHING
                    # rather than a stuck pending bubble.
                    try:
                        fallback_env = link.get("image_envelope") or {}
                        fallback_env["prompt"] = link.get("original_prompt", "")
                        image_job_id = link.get("image_job_id")
                        if image_job_id:
                            db.set_job_pending_with_prompt(
                                image_job_id, fallback_env["prompt"],
                            )
                        r.rpush(job_queue_for("image"), json.dumps(fallback_env))
                    except Exception:
                        pass
                    r.hdel(IMAGE_REWRITE_PENDING, req.job_id)

        # Same pattern as the image-rewrite handler above but for the
        # search-query rewrite pipeline. Linkage lives in a separate hash
        # so we don't have to inspect the original job's tool field to
        # pick the right dispatcher.
        search_rewrite_link_raw = r.hget(SEARCH_REWRITE_PENDING, req.job_id)
        if search_rewrite_link_raw:
            try:
                srlink = json.loads(search_rewrite_link_raw)
            except json.JSONDecodeError:
                srlink = None
            if srlink:
                rewritten = req.text if req.status == "complete" else None
                try:
                    dispatch_search_after_rewrite_fn(req.job_id, srlink, rewritten)
                except Exception as exc:
                    log.warning(
                        "search-rewrite dispatch failed; falling back to raw query",
                        extra={
                            "event": "search_rewrite_dispatch_failed",
                            "rewrite_job_id": req.job_id,
                            "error": str(exc),
                        },
                    )
                    # Best-effort recovery: push the search envelope with
                    # the original query so the user still gets SOMETHING
                    # rather than a stuck pending bubble.
                    try:
                        fallback_env = srlink.get("search_envelope") or {}
                        fallback_env["prompt"] = srlink.get("original_prompt", "")
                        search_job_id = srlink.get("search_job_id")
                        if search_job_id:
                            db.set_job_pending_with_prompt(
                                search_job_id, fallback_env["prompt"],
                            )
                        r.rpush(job_queue_for("search"), json.dumps(fallback_env))
                    except Exception:
                        pass
                    r.hdel(SEARCH_REWRITE_PENDING, req.job_id)

        # Conversation-summary linkage. Same shape as the rewrite paths
        # above: if this job_id is mapped in SUMMARY_PENDING, persist the
        # produced text as the conversation's summary, then fall through
        # to the normal complete flow so the worker is paid and the job
        # row is marked. The job is orphan (no conversation_id on it) so
        # no message-row writes happen downstream.
        summary_link_raw = r.hget(SUMMARY_PENDING, req.job_id)
        if summary_link_raw:
            try:
                slink = json.loads(summary_link_raw)
            except json.JSONDecodeError:
                slink = None
            clean_text = (req.text or "").strip()
            if slink and req.status == "complete" and clean_text:
                try:
                    db.set_conversation_summary(
                        slink["conversation_id"],
                        clean_text,
                        int(slink["through_seq"]),
                    )
                    log.info(
                        "summary stored",
                        extra={
                            "event": "summary_stored",
                            "conversation_id": slink["conversation_id"],
                            "through_seq": slink["through_seq"],
                            "job_id": req.job_id,
                            "chars": len(clean_text),
                        },
                    )
                except Exception as e:
                    log.warning(
                        "summary store failed: %s", e,
                        extra={
                            "event": "summary_store_failed",
                            "job_id": req.job_id,
                        },
                    )
            elif slink:
                # Distinguishes "agent returned empty text" (prompt-shape bug,
                # model refusal, etc.) from "agent failed loudly". Without
                # this log, an empty-text summary just silently drops on the
                # floor and the client's spinner spins forever.
                log.warning(
                    "summary job returned empty/non-complete result — skipping store",
                    extra={
                        "event": "summary_empty_result",
                        "job_id": req.job_id,
                        "conversation_id": slink["conversation_id"],
                        "status": req.status,
                        "chars": len(clean_text),
                    },
                )
            r.hdel(SUMMARY_PENDING, req.job_id)

        # Look up the original job row so we can branch image vs. chat
        # before touching earnings + storage.
        pre_complete_row = db.get_job(req.job_id)
        pre_complete_tool = (
            pre_complete_row["tool"]
            if pre_complete_row is not None and "tool" in pre_complete_row.keys()
            else "chat"
        )

        # Image jobs: decode + store the PNG, set image_path. Earnings are
        # currently chat-token-priced; per-image pricing ships with the
        # paid-customer slice (Phase 3b.ii). For MVP image earnings are
        # flat-rated as if the job produced ~200 tokens of work — gives the
        # contributor a non-zero credit without standing up a whole new
        # pricing table.
        image_path: Optional[str] = None
        image_save_error: Optional[str] = None
        image_width: int = 0
        image_height: int = 0
        if pre_complete_tool == "image" and req.status == "complete":
            try:
                image_path, image_width, image_height = _save_image_or_raise(
                    req.job_id, req.image_b64,
                )
            except HTTPException:
                # Re-raise — the worker sent malformed bytes; surface a 400.
                raise
            except Exception as e:
                image_save_error = str(e)
                log.warning(
                    "image save failed",
                    extra={
                        "event": "image_save_failed",
                        "job_id": req.job_id,
                        "worker_id": req.worker_id,
                    },
                )

        earnings = round(tokens * RATE_PER_TOKEN * WORKER_SHARE, 10) if req.status == "complete" else 0.0
        if (
            pre_complete_tool == "image"
            and req.status == "complete"
            and image_save_error is None
        ):
            # Flat-rate image earnings: treat each image as the rough work
            # equivalent of a 200-token chat completion. Replaced by per-
            # image pricing in Phase 3b.ii.
            earnings = round(200 * RATE_PER_TOKEN * WORKER_SHARE, 10)
        if pre_complete_tool == "tts" and req.status == "complete":
            # TTS earnings model: pay per-second of audio produced rather
            # than per-token, since the unit of work the contributor is
            # selling is "audio you can listen to" not "tokens you can
            # read." 50 token-equivalents per audio-second lands a 5-second
            # sentence at the same payout as a 250-token chat completion,
            # which roughly matches the GPU-vs-CPU work delta (Piper is
            # cheap, so under-priced vs chat is correct). Tuned in
            # Phase 2 once per-second TTS demand data exists; tracked in
            # project_open_strategy_questions § 6.
            audio_secs = float(req.audio_seconds or 0.0)
            earnings = round(
                audio_secs * 50.0 * RATE_PER_TOKEN * WORKER_SHARE, 10,
            )

        payload = {
            "job_id": req.job_id,
            "status": req.status if image_save_error is None else "error",
            "worker_id": req.worker_id,
            "model": req.model,
            "text": req.text,
            "prompt_tokens": req.prompt_tokens,
            "completion_tokens": tokens,
            "earnings": earnings,
            "duration_seconds": req.duration_seconds,
            "error": image_save_error or req.error,
        }
        if image_path:
            payload["image_path"] = image_path
        if pre_complete_tool == "tts" and req.audio_b64:
            # Ephemeral — never written to disk. Client reads it off
            # /result/{job_id}, plays it, drops it. Saves a /audio/<name>
            # round-trip per sentence, which matters for voice-mode latency.
            payload["audio_b64"] = req.audio_b64
            payload["audio_seconds"] = float(req.audio_seconds or 0.0)
        if pre_complete_tool == "chat" and req.audio_chunks:
            # Voice-mode chat: agent emitted N chunks during the LLM stream
            # (exponential batching). The complete request carries the full
            # ordered list so a client that reloaded the page after the
            # job finished still gets every chunk. Sort by seq defensively;
            # the agent emits in order but a future retry path or merge of
            # late partials might not.
            chunks = list(req.audio_chunks)
            chunks.sort(key=lambda c: int(c.get("seq", 0)))
            payload["audio_chunks"] = chunks
        if req.sources:
            # Render-only data: the polling client reads it from
            # /result/{job_id} and shows it under the bubble. We don't
            # persist sources to the jobs row — the DB-fallback path on
            # /result loses them after JOB_RESULTS eviction, which is fine
            # for a feature that's about "now I see the answer with its
            # links" rather than long-term archive.
            payload["sources"] = req.sources
        # Reverse-detection signal. If the rewrite classifier rerouted
        # this job from search → chat ("That's cool!"-style closure), the
        # dispatcher set a marker in SEARCH_AUTO_DISABLED. Surface it on
        # the result so the client auto-unchecks the sticky search box.
        if r.hdel(SEARCH_AUTO_DISABLED, req.job_id):
            payload["search_was_skipped"] = True
        r.hset(JOB_RESULTS, req.job_id, json.dumps(payload))
        r.hdel(JOB_PROCESSING, req.job_id)
        r.hdel(JOB_PARTIALS, req.job_id)
        r.delete(f"{JOB_AUDIO_CHUNKS}:{req.job_id}")
        r.hset(WORKER_STATUS, req.worker_id, "idle")
        db.mark_job_complete(
            job_id=req.job_id,
            worker_id=req.worker_id,
            model=req.model,
            text=req.text,
            prompt_tokens=req.prompt_tokens,
            completion_tokens=tokens,
            earnings=earnings,
            duration_seconds=req.duration_seconds,
            completed_at=now,
            status=req.status,
            error=req.error,
        )
        job_row = db.get_job(req.job_id)
        conv_id = (
            job_row["conversation_id"]
            if job_row is not None and "conversation_id" in job_row.keys()
            else None
        )
        # Finalize the pending assistant message that was created at
        # enqueue time. On success we write the final text + tokens; on
        # error we write a short user-facing reason as the bubble text
        # and flip status to 'error' so the client can render a retry
        # button. The user turn is already in the table from enqueue, so
        # we never insert it here.
        if conv_id:
            existing_msg = db.get_message_by_job(req.job_id)
            if existing_msg is not None:
                terminal_status = (
                    "complete"
                    if req.status == "complete" and image_save_error is None
                    else "error"
                )
                if terminal_status == "complete":
                    # For image jobs the body text is the original prompt
                    # (we already stored that on the user message at enqueue
                    # time); the bubble itself is rendered as <img> off
                    # image_path. We persist the prompt as the assistant-
                    # bubble text too so a no-CSS fallback still shows
                    # something useful instead of an empty row.
                    bubble_text = (
                        f"[image: {existing_msg['text'] or req.text or ''}]"
                        if pre_complete_tool == "image"
                        else (req.text or "")
                    )
                    db.finalize_message(
                        message_id=existing_msg["message_id"],
                        text=bubble_text,
                        status="complete",
                        prompt_tokens=int(req.prompt_tokens or 0),
                        completion_tokens=tokens,
                        model=req.model,
                        image_path=image_path,
                    )
                else:
                    db.finalize_message(
                        message_id=existing_msg["message_id"],
                        text=(
                            image_save_error or req.error or "Generation failed."
                        )[:500],
                        status="error",
                        model=req.model,
                    )
            db.touch_conversation(conv_id, now)

        # Credit on completion.
        #
        # Chat jobs credit by completion_tokens against both the worker
        # earnings ledger (for payout) and the member usage ledger (for
        # quota). Image jobs are independent: they credit IMAGE_UNIT_COST_BASE
        # to a dedicated image_units column on member_usage (gated by
        # daily_quota_images) and credit a small token-equivalent to the
        # earnings ledger only — the per-image USD amount comes from
        # ``earnings`` computed upstream. Pre-image-limits behavior was to
        # fake a 200-token credit on the chat ledger; that conflated two
        # resources and broke the token ledger as a chat-throughput
        # signal. See business.md → "Dual-role accounting" for the model.
        is_image_complete = (
            pre_complete_tool == "image"
            and req.status == "complete"
            and image_save_error is None
        )
        is_tts_complete = (
            pre_complete_tool == "tts"
            and req.status == "complete"
        )
        is_chat_complete = (
            req.status == "complete"
            and not is_image_complete
            and not is_tts_complete
            and tokens > 0
            and image_save_error is None
        )
        submitter = (
            job_row["submitted_by_member_id"]
            if job_row is not None and "submitted_by_member_id" in job_row.keys()
            else None
        )
        earnings_token_credit = 0
        if is_chat_complete:
            db.add_earnings(req.worker_id, tokens, earnings)
            earnings_token_credit = tokens
            if submitter:
                db.add_member_usage(
                    submitter,
                    now,
                    tokens_in=int(req.prompt_tokens or 0),
                    tokens_out=tokens,
                )
                # Voice-mode chat: the worker also produced TTS audio inline
                # with the LLM stream. Bill the user's voice_minutes ledger
                # for the sum of all chunks. Earnings for the TTS work
                # itself stay with the chat job's per-token credit rather
                # than re-priced as standalone TTS — separate ledger lines
                # per chunk is more bookkeeping than we want until per-tool
                # pricing lands (project_open_strategy_questions § 6).
                voice_secs_total = 0.0
                for c in (req.audio_chunks or []):
                    voice_secs_total += float(c.get("audio_seconds") or 0.0)
                if voice_secs_total > 0:
                    db.add_member_voice_usage(
                        submitter,
                        now,
                        seconds=voice_secs_total,
                    )
        elif is_image_complete:
            # Earnings still posts USD for the image; the synthesized 200
            # is preserved here ONLY as a tokens-equivalent so the
            # earnings ledger's tokens column stays additive across both
            # tools. The member-side quota uses image_units, not tokens.
            earnings_token_credit = 200
            db.add_earnings(req.worker_id, earnings_token_credit, earnings)
            if submitter:
                # Charge by rendered resolution, not a flat per-image rate:
                # a 1024² image is ~4× the GPU work of a 512², and the
                # multiplier (see image_cost_multiplier) tracks pixel area
                # so the daily image quota measures actual cost. PNG dims
                # come from the saved file's IHDR — the worker can't bias
                # the bill by lying about width/height in the envelope.
                units = IMAGE_UNIT_COST_BASE * image_cost_multiplier(
                    image_width, image_height,
                )
                db.add_member_image_usage(
                    submitter,
                    now,
                    units=units,
                )
        elif is_tts_complete:
            # Voice charges by the audio's playback duration, not by the
            # worker's wall-clock synthesis time — the user thinks in
            # "minutes of voice consumed," not "compute spent." The audio
            # length comes from the worker's audio_seconds (Piper reports
            # frame count / sample rate), which the agent cannot inflate
            # without producing a longer file: the audio_b64 we just stored
            # is the ground-truth artifact a future audit could re-measure.
            # The tokens-equivalent for the earnings ledger mirrors the
            # earnings rate used above (50 token-equivalents / audio-second).
            audio_secs = float(req.audio_seconds or 0.0)
            earnings_token_credit = int(round(audio_secs * 50.0))
            db.add_earnings(req.worker_id, earnings_token_credit, earnings)
            if submitter:
                db.add_member_voice_usage(
                    submitter,
                    now,
                    seconds=audio_secs,
                )
        if is_chat_complete or is_image_complete or is_tts_complete:
            # mirror to redis hash for backwards compat
            existing = r.hget(WORKER_EARNINGS, req.worker_id)
            if existing:
                try:
                    cur = json.loads(existing)
                except json.JSONDecodeError:
                    cur = {"earnings": 0.0, "jobs": 0, "tokens": 0}
            else:
                cur = {"earnings": 0.0, "jobs": 0, "tokens": 0}
            cur["earnings"] = round(float(cur.get("earnings", 0)) + earnings, 10)
            cur["jobs"] = int(cur.get("jobs", 0)) + 1
            cur["tokens"] = int(cur.get("tokens", 0)) + earnings_token_credit
            cur["worker_id"] = req.worker_id
            r.hset(WORKER_EARNINGS, req.worker_id, json.dumps(cur))

        # Phase 6: push notification on long-running tool completion. Chat
        # is streamed live so the user is presumably watching; image and
        # voice often run for many seconds and the user switches away.
        # The notification deep-links back to "/" — chat.js auto-opens the
        # most-recent conversation on load, which is the one that just
        # completed. The delivery is best-effort: send_to_member persists
        # the in-app row regardless of push success, respects per-member
        # opt-out, and short-circuits to a no-op when VAPID isn't configured.
        if submitter and req.status == "complete":
            push_title = None
            push_body = None
            push_category = None
            if is_image_complete:
                push_category = notifications.CATEGORY_IMAGE_DONE
                push_title = "Your image is ready"
                push_body = "Tap to view it."
            elif is_tts_complete:
                push_category = notifications.CATEGORY_VOICE_DONE
                push_title = "Your audio is ready"
                push_body = "Tap to listen."
            if push_category:
                try:
                    notifications.send_to_member(
                        db, submitter, push_category,
                        title=push_title, body=push_body,
                        data={
                            "url": "/",
                            "conversation_id": conv_id,
                            "job_id": req.job_id,
                        },
                    )
                except Exception as e:
                    # Never let a push-delivery hiccup fail /jobs/complete —
                    # the worker already did its work and the caller
                    # depends on a 200 to release its claim token.
                    log.warning(
                        "push send failed: %s",
                        e, extra={"event": "push_send_failed"},
                    )

        log.info(
            "job complete" if req.status == "complete" else "job error",
            extra={
                "event": "job_complete" if req.status == "complete" else "job_error",
                "job_id": req.job_id,
                "worker_id": req.worker_id,
            },
        )
        return {"ok": True, "earnings": earnings}

    return router, _schedule_payload, _require_worker_owner, _verify_claim_or_410
