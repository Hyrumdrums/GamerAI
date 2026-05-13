"""Coordinator: REST API + Redis queue + SQLite write-through + reaper."""
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from coordinator import canaries as canary_lib
from coordinator import member_auth, model_registry
from coordinator.canaries import CanaryInjector
from coordinator.db import DB
from coordinator.idempotency import IdempotencyStore
from coordinator.rate_limit import RateLimiter
from coordinator.redis_client import get_client
from coordinator.scheduler import Reaper
from shared.auth import API_TOKEN, AUTH_ENABLED, is_public_path
from shared.config import (
    CANARY_INTERVAL_SECONDS,
    CANARY_PENDING,
    CANARY_SCORE_WINDOW,
    IDEMPOTENCY_TTL_SECONDS,
    JOB_PARTIALS,
    JOB_PROCESSING,
    JOB_QUEUE,
    JOB_RESULTS,
    JOB_TIMEOUT_SECONDS,
    RATE_LIMIT_PER_MIN,
    RATE_PER_TOKEN,
    STRICT_MODELS,
    WORKER_CAPABILITIES,
    WORKER_EARNINGS,
    WORKER_HEARTBEATS,
    WORKER_REGISTRY,
    WORKER_SHARE,
    WORKER_STATUS,
    WORKER_TIMEOUT_SECONDS,
)
from shared.models import (
    ConversationCreateRequest,
    GenerateRequest,
    GenerateResponse,
    HeartbeatRequest,
    InviteAcceptRequest,
    InviteCreateRequest,
    JobClaimRequest,
    JobCompleteRequest,
    JobPartialRequest,
    WorkerIdent,
)


# ---------- structured logging ----------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": "coordinator",
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k in ("job_id", "worker_id", "event"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        return json.dumps(payload)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("coordinator")

# ---------- shared resources ----------
r = get_client()
db = DB()
idem = IdempotencyStore(r, IDEMPOTENCY_TTL_SECONDS)
rate_limiter = RateLimiter(r, RATE_LIMIT_PER_MIN)
_reaper: Reaper | None = None
_canary_injector: CanaryInjector | None = None


def ensure_admin_seed() -> None:
    """If ``API_TOKEN`` is set in the env, make sure a corresponding admin
    member exists. Pre-existing clients that send ``Authorization: Bearer
    $API_TOKEN`` are now logged in as that admin member — no client-side
    changes required.

    Idempotent: the seed runs on every startup but does nothing if a
    member with the same token_hash already exists.
    """
    if not API_TOKEN:
        return
    token_hash = member_auth.hash_token(API_TOKEN)
    if db.get_member_by_token_hash(token_hash) is not None:
        return
    db.create_member(
        member_id="mem_admin_seed",
        email=None,
        role="admin",
        parent_member_id=None,
        token_hash=token_hash,
        tier="PLATINUM",
        daily_quota_tokens=None,
        # The admin operates the coordinator; bringing the system up
        # is implicit acceptance of these terms. Stamping the version
        # so the admin row matches the same shape as invitee rows.
        tos_accepted_at=time.time(),
        tos_version=TOS_VERSION,
    )
    log.info(
        "seeded admin member from API_TOKEN",
        extra={"event": "admin_seed"},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reaper, _canary_injector
    ensure_admin_seed()
    _reaper = Reaper(r, db)
    _reaper.start()
    if CANARY_INTERVAL_SECONDS > 0:
        _canary_injector = CanaryInjector(r, db, interval=CANARY_INTERVAL_SECONDS)
        _canary_injector.start()
    log.info("coordinator ready", extra={"event": "startup"})
    try:
        yield
    finally:
        if _reaper:
            _reaper.stop()
        if _canary_injector:
            _canary_injector.stop()


app = FastAPI(title="GamerAI Coordinator", version="0.3.0", lifespan=lifespan)

# Vendored JS for the public ToS page (marked + DOMPurify). Same
# supply-chain logic as the client/web.py mount: avoids depending on
# any third-party CDN. Mount is conditional so test environments
# without the directory don't fail to import.
_COORD_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _COORD_STATIC_DIR.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=str(_COORD_STATIC_DIR)),
        name="static",
    )


def _is_public(method: str, path: str) -> bool:
    """Path+method auth exemption. ``/health`` is fully open. The
    invite-redemption flow needs exactly two endpoints reachable
    without auth: ``GET /invites/<code>`` and ``POST /invites/<code>/accept``.
    The community ToS is public (``/tos`` and ``/tos/raw``). Everything
    else under ``/invites`` (create, list, revoke) requires a valid bearer."""
    if is_public_path(path):
        return True
    if method == "GET" and path in ("/tos", "/tos/raw"):
        return True
    parts = path.strip("/").split("/")
    if method == "GET" and len(parts) == 2 and parts[0] == "invites":
        return True
    if (
        method == "POST"
        and len(parts) == 3
        and parts[0] == "invites"
        and parts[2] == "accept"
    ):
        return True
    return False


# ---------- community ToS ----------
# Version string is checked against the file every startup and stamped
# into each new member row when they accept. Bumping this manually
# (after a substantive change to docs/community-tos.md) will cause
# existing members to be flagged as "needs re-accept" by the per-
# member ToS check.
TOS_VERSION = "2026-05-13"
_TOS_PATH = Path(__file__).resolve().parent.parent / "docs" / "community-tos.md"


def _load_tos_text() -> str:
    try:
        return _TOS_PATH.read_text(encoding="utf-8")
    except OSError:
        return (
            "# GamerAI Community Terms of Service\n\n"
            "Terms document not bundled with this deploy.\n"
        )


# ---------- auth (no-op when API_TOKEN env is unset) ----------
@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    request.state.member = None
    if _is_public(request.method, request.url.path):
        return await call_next(request)
    if not AUTH_ENABLED:
        return await call_next(request)
    raw_token = member_auth.parse_bearer(request.headers.get("authorization"))
    if not raw_token:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    member = member_auth.lookup_member_by_token(db, raw_token)
    if member is None:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    request.state.member = member
    db.touch_member(member.member_id, time.time())
    return await call_next(request)


# ---------- rate limit (no-op when RATE_LIMIT_PER_MIN <= 0) ----------
def _client_ip(request: Request) -> str:
    """Trust the first X-Forwarded-For when present (Caddy adds it),
    otherwise fall back to the direct peer address."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",", 1)[0].strip() or "unknown"
    return getattr(request.client, "host", "unknown")


@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    if not rate_limiter.enabled or is_public_path(request.url.path):
        return await call_next(request)
    if not rate_limiter.allow(_client_ip(request)):
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
    return await call_next(request)


if AUTH_ENABLED:
    log.info("auth enabled (bearer token required)", extra={"event": "auth_on"})
else:
    log.info("auth disabled (API_TOKEN unset)", extra={"event": "auth_off"})

if rate_limiter.enabled:
    log.info("rate limit %d/min/ip", RATE_LIMIT_PER_MIN, extra={"event": "rl_on"})
else:
    log.info("rate limit disabled", extra={"event": "rl_off"})


# ---------- helpers ----------
def _worker_status(worker_id: str, now: float) -> str:
    last = float(r.hget(WORKER_HEARTBEATS, worker_id) or 0)
    if not last or (now - last) > WORKER_TIMEOUT_SECONDS:
        return "offline"
    return r.hget(WORKER_STATUS, worker_id) or "idle"


# ---------- public API ----------
@app.get("/health")
def health():
    try:
        r.ping()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"redis unavailable: {e}")


_TOS_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>GamerAI — Community ToS</title>
<style>
  body{{font-family:-apple-system,system-ui,sans-serif;max-width:760px;margin:2.5rem auto;padding:0 1.25rem;color:#1a1a1a;line-height:1.6}}
  .meta{{color:#666;font-size:.9rem;margin-bottom:1.5rem;padding-bottom:.75rem;border-bottom:1px solid #eee}}
  .meta a{{color:#2d6cdf;text-decoration:none}}
  #content h1{{margin-top:0}}
  #content h2{{margin-top:2rem;font-size:1.25rem;color:#1a1a1a}}
  #content h3{{margin-top:1.5rem;font-size:1.05rem;color:#333}}
  #content p{{margin:.75rem 0}}
  #content ul,#content ol{{padding-left:1.25rem;margin:.5rem 0 .75rem 0}}
  #content li{{margin-bottom:.25rem}}
  #content em{{color:#666}}
  #content strong{{color:#1a1a1a}}
  #content code{{background:#f3f3f3;padding:.05rem .3rem;border-radius:3px;font-size:.9em;font-family:ui-monospace,Menlo,Consolas,monospace}}
  #content hr{{border:0;border-top:1px solid #e5e5e5;margin:1.75rem 0}}
  #content a{{color:#2d6cdf}}
  #loading{{color:#888}}
</style></head>
<body>
<div class="meta">
  Version <strong>{version}</strong> · <a href="/tos/raw">view raw</a>
</div>
<div id="content"><span id="loading">Loading terms…</span></div>
<script src="/static/marked.min.js"></script>
<script src="/static/purify.min.js"></script>
<script>
fetch('/tos/raw').then(r => r.text()).then(md => {{
  const html = window.marked.parse(md);
  document.getElementById('content').innerHTML =
    window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
}}).catch(() => {{
  document.getElementById('content').innerHTML =
    '<p>Could not load terms. <a href="/tos/raw">View raw markdown</a>.</p>';
}});
</script>
</body></html>
"""


@app.get("/tos", response_class=HTMLResponse)
def tos_html():
    """Public ToS page. Used both as the destination of the redemption-
    page link and as a stable URL contributors can revisit any time.
    The markdown body is fetched client-side from /tos/raw and rendered
    via marked.js so headings, lists, and emphasis come through as a
    real document, not preformatted ASCII in a <pre> block."""
    import html as html_lib
    return HTMLResponse(_TOS_HTML_TEMPLATE.format(
        version=html_lib.escape(TOS_VERSION),
    ))


@app.get("/tos/raw", response_class=PlainTextResponse)
def tos_raw():
    """Raw markdown for clients that prefer it (or for grep-friendly
    diffs between versions)."""
    return PlainTextResponse(
        _load_tos_text(),
        headers={"X-Tos-Version": TOS_VERSION},
    )


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest, request: Request):
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt required")

    # optional model-registry validation (off unless STRICT_MODELS=true)
    try:
        model_registry.validate_or_raise(req.model, strict=STRICT_MODELS)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # optional retry-safety: same Idempotency-Key returns the same job_id
    idem_key = request.headers.get("idempotency-key")
    existing = idem.lookup(idem_key)
    if existing:
        log.info(
            "idempotent retry",
            extra={"event": "idempotent_hit", "job_id": existing},
        )
        existing_msg = db.get_message_by_job(existing)
        return GenerateResponse(
            job_id=existing,
            assistant_message_id=(
                existing_msg["message_id"] if existing_msg is not None else None
            ),
        )

    member = getattr(request.state, "member", None)
    submitted_by = member.member_id if member is not None else None

    # Daily-quota enforcement (slice 2). NULL quota = unlimited (admin,
    # tier-unlimited contributor). The check runs against today's
    # usage at submission time; a single prompt can overshoot the cap
    # by its completion size, which we don't predict here.
    if (
        member is not None
        and member.daily_quota_tokens is not None
        and member.daily_quota_tokens > 0
    ):
        used = db.member_usage_today(member.member_id)["tokens_out"]
        if used >= member.daily_quota_tokens:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"daily quota exceeded: {used} / "
                    f"{member.daily_quota_tokens} output tokens used today"
                ),
            )

    # Conversation context: if the caller passed conversation_id, load
    # the prior turns and prepend them to the worker-facing prompt.
    # Ownership is enforced — a caller cannot inject into someone else's
    # conversation. The original (un-prepended) prompt is stored
    # separately so it can be appended back as a user message on
    # completion.
    conversation_id: Optional[str] = req.conversation_id
    worker_prompt = req.prompt
    if conversation_id:
        conv_row = db.get_conversation(conversation_id)
        if conv_row is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        _require_conversation_owner(request, conv_row)
        if conv_row["archived_at"] is not None:
            raise HTTPException(
                status_code=410, detail="conversation is archived"
            )
        prior = db.list_messages(conversation_id)
        # Don't let a caller queue a new turn while a previous one is
        # still streaming — the conversation history would then contain
        # an empty/partial assistant turn wedged between two user turns,
        # which makes a mess of the worker-facing prompt and the UI.
        # The client UI also disables submit while pending, but the
        # server check is what makes the rule load-bearing.
        if prior and prior[-1]["status"] == "pending":
            raise HTTPException(
                status_code=409,
                detail="previous turn is still streaming",
            )
        if prior:
            worker_prompt = _format_chat_prompt(prior, req.prompt)
        # Conversation may pin a default model; honor it when the call
        # didn't override.
        if not req.model and conv_row["model"]:
            req_model = conv_row["model"]
        else:
            req_model = req.model
    else:
        req_model = req.model

    job_id = str(uuid.uuid4())
    submitted_at = time.time()
    # IMPORTANT: do NOT include submitted_by_member_id in the worker-
    # facing envelope. The worker has no need for it, and including
    # it lets a malicious worker recognize canaries (null submitter)
    # and selectively cheat on real prompts. Attribution lives on
    # the jobs DB row instead, which the coordinator reads directly
    # when crediting earnings / member_usage on /jobs/complete.
    job = {
        "job_id": job_id,
        "prompt": worker_prompt,
        "model": req_model,
        "submitted_at": submitted_at,
    }
    # Store the ORIGINAL user message (not the prepended worker-prompt)
    # so /jobs/complete can replay only the new turn into the
    # conversation history.
    db.insert_job(
        job_id,
        req.prompt,
        req_model,
        submitted_at,
        submitted_by,
        conversation_id=conversation_id,
    )
    # Persist the user turn and an empty pending assistant turn now,
    # not at /jobs/complete time. This means: (a) a client that
    # disconnects mid-stream can reload /conversations and see its
    # message + the partial answer so far; (b) if the job fails, the
    # user's message stays visible with an error bubble in its place
    # (vs. the old behavior of erasing the user's prompt on failure).
    assistant_message_id: Optional[str] = None
    if conversation_id:
        base_seq = db.next_message_seq(conversation_id)
        user_msg_id = "msg_" + uuid.uuid4().hex[:12]
        assistant_message_id = "msg_" + uuid.uuid4().hex[:12]
        db.append_message(
            message_id=user_msg_id,
            conversation_id=conversation_id,
            seq=base_seq,
            role="user",
            text=req.prompt,
            model=req_model,
            created_at=submitted_at,
            status="complete",
        )
        db.append_message(
            message_id=assistant_message_id,
            conversation_id=conversation_id,
            seq=base_seq + 1,
            role="assistant",
            text="",
            job_id=job_id,
            model=req_model,
            created_at=submitted_at,
            status="pending",
        )
        db.touch_conversation(conversation_id, submitted_at)
        # First-prompt-becomes-the-title behavior is idempotent (set only
        # when title is NULL/empty) so it's safe to call here even
        # though the message is now persisted earlier than before.
        db.set_conversation_title(conversation_id, req.prompt[:80].strip())
    r.rpush(JOB_QUEUE, json.dumps(job))
    idem.remember(idem_key, job_id)
    log.info(
        "queued job",
        extra={
            "event": "job_queued",
            "job_id": job_id,
        },
    )
    return GenerateResponse(
        job_id=job_id, assistant_message_id=assistant_message_id,
    )


def _format_chat_prompt(prior_messages, new_user_text: str) -> str:
    """Concatenate prior turns into a single chat-style prompt the
    underlying model can consume. We keep this server-side so the
    worker's /api/generate contract doesn't change (no switch to
    Ollama's /api/chat). Model-instruction-tuned LLMs handle this
    format well; we'll switch to /api/chat with role-aware messages
    when we have streaming on the worker side anyway."""
    lines = []
    for m in prior_messages:
        role = m["role"]
        text = (m["text"] or "").strip()
        if role == "user":
            lines.append(f"User: {text}")
        elif role == "assistant":
            lines.append(f"Assistant: {text}")
        # silently drop unknown roles
    lines.append(f"User: {new_user_text.strip()}")
    lines.append("Assistant:")
    return "\n\n".join(lines)


@app.get("/result/{job_id}")
def result(job_id: str):
    raw = r.hget(JOB_RESULTS, job_id)
    if raw:
        data = json.loads(raw)
        data.setdefault("status", "complete")
        data["done"] = data.get("status") in ("complete", "error")
        return data
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    submitted_by = (
        row["submitted_by_member_id"]
        if "submitted_by_member_id" in row.keys()
        else None
    )
    # Mid-stream: if the worker has been pushing partials, surface the
    # latest accumulated text so the polling client can render it.
    # status stays 'pending'/'running' so the client keeps polling.
    partial_text = r.hget(JOB_PARTIALS, job_id)
    status = row["status"]
    return {
        "job_id": row["job_id"],
        "status": status,
        "worker_id": row["worker_id"],
        "model": row["model"],
        "text": partial_text if partial_text is not None else row["result"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "earnings": row["earnings"],
        "duration_seconds": row["duration_seconds"],
        "attempts": row["attempts"],
        "error": row["error"],
        "submitted_by_member_id": submitted_by,
        "done": status in ("complete", "error"),
    }


# ---------- worker lifecycle ----------
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


@app.post("/register")
def register(req: WorkerIdent, request: Request):
    now = time.time()
    member = getattr(request.state, "member", None)
    member_id = member.member_id if member is not None else None

    # Ownership claim — atomic so concurrent registers can't race.
    # When AUTH is off (dev mode), member_id is None and the
    # ownership check inside claim_worker_ownership is permissive.
    ok, existing_owner = db.claim_worker_ownership(
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

    r.sadd(WORKER_REGISTRY, req.worker_id)
    r.hset(WORKER_HEARTBEATS, req.worker_id, now)
    r.hset(WORKER_STATUS, req.worker_id, "idle")
    if req.capabilities is not None:
        r.hset(
            WORKER_CAPABILITIES,
            req.worker_id,
            req.capabilities.model_dump_json(),
        )
    log.info(
        "worker registered",
        extra={"event": "worker_registered", "worker_id": req.worker_id},
    )
    return {"ok": True}


@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest, request: Request):
    _require_worker_owner(request, req.worker_id)
    now = time.time()
    r.hset(WORKER_HEARTBEATS, req.worker_id, now)
    r.hset(WORKER_STATUS, req.worker_id, req.status)
    db.upsert_worker(req.worker_id, req.status, now)
    return {"ok": True}


@app.post("/jobs/next")
def next_job(req: WorkerIdent, request: Request):
    """HTTP-only job pickup for remote agents (e.g. the Windows gamer install).
    Pops one job from the queue and returns it; the agent should immediately
    POST /jobs/claim with the returned job_id."""
    _require_worker_owner(request, req.worker_id)
    raw = r.lpop(JOB_QUEUE)
    if not raw:
        return {"job": None}
    try:
        job = json.loads(raw)
    except json.JSONDecodeError:
        return {"job": None}
    log.info(
        "job dispensed",
        extra={
            "event": "job_dispensed",
            "job_id": job.get("job_id"),
            "worker_id": req.worker_id,
        },
    )
    return {"job": job}


@app.post("/jobs/claim")
def claim(req: JobClaimRequest, request: Request):
    """Worker reports it has claimed a job. Coordinator records processing entry + DB row."""
    _require_worker_owner(request, req.worker_id)
    now = time.time()
    deadline = now + JOB_TIMEOUT_SECONDS

    # find original job payload — best-effort, used only for requeue on timeout
    raw = r.hget(JOB_RESULTS, req.job_id)
    original = None
    if raw is None:
        row = db.get_job(req.job_id)
        if row:
            original = {
                "job_id": row["job_id"],
                "prompt": row["prompt"],
                "model": row["model"],
                "submitted_at": row["submitted_at"],
            }

    r.hset(
        JOB_PROCESSING,
        req.job_id,
        json.dumps({"worker_id": req.worker_id, "deadline": deadline, "job": original}),
    )
    r.hset(WORKER_STATUS, req.worker_id, "busy")
    db.mark_job_running(req.job_id, req.worker_id, now)
    log.info(
        "job claimed",
        extra={"event": "job_claimed", "job_id": req.job_id, "worker_id": req.worker_id},
    )
    return {"ok": True, "deadline": deadline}


@app.post("/jobs/abandon")
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
    _require_worker_owner(request, req.worker_id)
    raw = r.hget(JOB_PROCESSING, req.job_id)
    if raw is None:
        return {"ok": True, "requeued": False, "reason": "not in flight"}
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        meta = {}
    original = meta.get("job")
    if original is None:
        row = db.get_job(req.job_id)
        if row is not None:
            # Reconstructed envelope must match the worker-facing shape
            # we push from /generate — no submitted_by_member_id (see
            # the comment there explaining the canary-detection issue).
            original = {
                "job_id": row["job_id"],
                "prompt": row["prompt"],
                "model": row["model"],
                "submitted_at": row["submitted_at"],
            }
    if original is not None:
        r.rpush(JOB_QUEUE, json.dumps(original))
    r.hdel(JOB_PROCESSING, req.job_id)
    r.hdel(JOB_PARTIALS, req.job_id)
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


@app.post("/jobs/partial")
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
    _require_worker_owner(request, req.worker_id)
    # Verify this worker actually holds the claim — same gating as
    # /jobs/complete, prevents one worker from clobbering another's
    # in-flight job's text.
    raw = r.hget(JOB_PROCESSING, req.job_id)
    if raw is None:
        # Job already completed or never claimed by anyone — accept
        # silently rather than 404, since this is a fire-and-forget
        # path and the worker may be a few ms behind a finalize.
        return {"ok": True, "stale": True}
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        meta = {}
    if meta.get("worker_id") != req.worker_id:
        raise HTTPException(
            status_code=403, detail="not the current claimant",
        )
    text = req.text or ""
    r.hset(JOB_PARTIALS, req.job_id, text)
    msg = db.get_message_by_job(req.job_id)
    if msg is not None:
        db.update_message_partial(msg["message_id"], text)
    return {"ok": True}


@app.post("/jobs/complete")
def complete(req: JobCompleteRequest, request: Request):
    """Worker submits result. Coordinator writes Redis result, earnings, SQLite row."""
    _require_worker_owner(request, req.worker_id)
    now = time.time()
    tokens = int(req.completion_tokens or 0)

    # Canary check: if this job_id was injected as a canary, divert to
    # the verification path and skip earnings + usage rollup. The worker
    # is told "ok" the same way as a real job — we don't surface canary
    # status, because doing so would let a malicious worker special-case
    # canary handling and pass every check.
    canary_id = r.hget(CANARY_PENDING, req.job_id)
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

    earnings = round(tokens * RATE_PER_TOKEN * WORKER_SHARE, 10) if req.status == "complete" else 0.0

    payload = {
        "job_id": req.job_id,
        "status": req.status,
        "worker_id": req.worker_id,
        "model": req.model,
        "text": req.text,
        "prompt_tokens": req.prompt_tokens,
        "completion_tokens": tokens,
        "earnings": earnings,
        "duration_seconds": req.duration_seconds,
        "error": req.error,
    }
    r.hset(JOB_RESULTS, req.job_id, json.dumps(payload))
    r.hdel(JOB_PROCESSING, req.job_id)
    r.hdel(JOB_PARTIALS, req.job_id)
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
            if req.status == "complete":
                db.finalize_message(
                    message_id=existing_msg["message_id"],
                    text=req.text or "",
                    status="complete",
                    prompt_tokens=int(req.prompt_tokens or 0),
                    completion_tokens=tokens,
                    model=req.model,
                )
            else:
                db.finalize_message(
                    message_id=existing_msg["message_id"],
                    text=(req.error or "Generation failed.")[:500],
                    status="error",
                    model=req.model,
                )
        db.touch_conversation(conv_id, now)

    if req.status == "complete" and tokens > 0:
        db.add_earnings(req.worker_id, tokens, earnings)
        submitter = (
            job_row["submitted_by_member_id"]
            if job_row is not None and "submitted_by_member_id" in job_row.keys()
            else None
        )
        if submitter:
            db.add_member_usage(
                submitter,
                now,
                tokens_in=int(req.prompt_tokens or 0),
                tokens_out=tokens,
            )
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
        cur["tokens"] = int(cur.get("tokens", 0)) + tokens
        cur["worker_id"] = req.worker_id
        r.hset(WORKER_EARNINGS, req.worker_id, json.dumps(cur))

    log.info(
        "job complete" if req.status == "complete" else "job error",
        extra={
            "event": "job_complete" if req.status == "complete" else "job_error",
            "job_id": req.job_id,
            "worker_id": req.worker_id,
        },
    )
    return {"ok": True, "earnings": earnings}


# ---------- observability ----------
def _load_capabilities(worker_id: str) -> dict | None:
    raw = r.hget(WORKER_CAPABILITIES, worker_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


@app.get("/workers")
def workers():
    rows = db.list_workers()
    earnings_by_worker = {row["worker_id"]: row for row in db.list_earnings()}
    now = time.time()
    out = []
    for w in rows:
        wid = w["worker_id"]
        live_status = _worker_status(wid, now)
        last_seen = float(w["last_seen"] or 0)
        e = earnings_by_worker.get(wid)
        out.append(
            {
                "worker_id": wid,
                "status": live_status,
                "last_seen": last_seen,
                "seconds_since_heartbeat": round(now - last_seen, 2) if last_seen else None,
                "alive": live_status != "offline",
                "total_tokens": int(e["total_tokens"]) if e else 0,
                "total_jobs": int(e["total_jobs"]) if e else 0,
                "total_usd": round(float(e["total_usd"]), 8) if e else 0.0,
                "capabilities": _load_capabilities(wid),
                "canary_score": db.canary_score_for_worker(wid, limit=CANARY_SCORE_WINDOW),
            }
        )
    return {"workers": out}


@app.get("/me")
def me(request: Request):
    """Identity + quota for the caller. When auth is disabled (no API_TOKEN
    env), reports ``auth_disabled`` so dev/test loops don't have to special-case."""
    member = getattr(request.state, "member", None)
    if member is None:
        if not AUTH_ENABLED:
            return {"auth_disabled": True}
        raise HTTPException(status_code=401, detail="unauthorized")
    usage = db.member_usage_today(member.member_id)
    return {
        "member_id": member.member_id,
        "email": member.email,
        "role": member.role,
        "parent_member_id": member.parent_member_id,
        "tier": member.tier,
        "daily_quota_tokens": member.daily_quota_tokens,
        "usage_today": usage,
        "tos": {
            "accepted_at": member.tos_accepted_at,
            "version": member.tos_version,
            "current_version": TOS_VERSION,
            "needs_reaccept": member.tos_version != TOS_VERSION,
        },
    }


# ---------- conversations ----------
def _conv_row_to_summary(row) -> dict:
    return {
        "conversation_id": row["conversation_id"],
        "title": row["title"],
        "model": row["model"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
        "owner_member_id": row["owner_member_id"],
    }


def _message_row_to_dict(row) -> dict:
    keys = row.keys()
    return {
        "message_id": row["message_id"],
        "seq": row["seq"],
        "role": row["role"],
        "text": row["text"],
        "status": row["status"] if "status" in keys else "complete",
        "job_id": row["job_id"],
        "model": row["model"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "created_at": row["created_at"],
    }


def _require_conversation_owner(request: Request, conv_row) -> None:
    """Reject if the caller is authenticated and the conversation has
    an owner that isn't them. Auth-off mode permits everything (dev/test).
    """
    if not AUTH_ENABLED:
        return
    member = getattr(request.state, "member", None)
    if member is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    owner = conv_row["owner_member_id"]
    # Admin can read any conversation for moderation; otherwise strict
    # member_id match. (When prompt-encryption-at-rest ships, even the
    # admin won't be able to decrypt; for now this is honest.)
    if owner is not None and owner != member.member_id and member.role != "admin":
        raise HTTPException(status_code=404, detail="conversation not found")


@app.post("/conversations")
def create_conversation(req: ConversationCreateRequest, request: Request):
    """Create a new conversation owned by the caller. When auth is off
    (dev mode), owner_member_id is left NULL."""
    member = getattr(request.state, "member", None)
    owner_id = member.member_id if member is not None else None
    conv_id = "conv_" + uuid.uuid4().hex[:12]
    db.create_conversation(
        conversation_id=conv_id,
        owner_member_id=owner_id,
        title=req.title,
        model=req.model,
    )
    log.info(
        "conversation created",
        extra={"event": "conversation_created"},
    )
    return {"conversation_id": conv_id, "title": req.title, "model": req.model}


@app.get("/conversations")
def list_conversations(request: Request, include_archived: bool = False):
    """List the caller's conversations, most-recently-updated first.
    Admin gets back only their OWN conversations here, not the whole
    table — admin moderation of others would go through a separate
    /admin/conversations endpoint (not built yet)."""
    member = getattr(request.state, "member", None)
    if AUTH_ENABLED and member is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    owner_id = member.member_id if member is not None else None
    if owner_id is None:
        # Auth-off dev mode: conversations get owner_member_id=NULL when
        # there's no authenticated caller, so a member-id filter would
        # always miss. Surface those rows so the sidebar isn't empty.
        if not AUTH_ENABLED:
            rows = db.list_unowned_conversations(
                include_archived=include_archived,
            )
            return {"conversations": [_conv_row_to_summary(r) for r in rows]}
        return {"conversations": []}
    rows = db.list_conversations_for_member(
        owner_id, include_archived=include_archived,
    )
    return {"conversations": [_conv_row_to_summary(r) for r in rows]}


@app.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, request: Request):
    row = db.get_conversation(conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conversation_owner(request, row)
    messages = db.list_messages(conversation_id)
    return {
        **_conv_row_to_summary(row),
        "messages": [_message_row_to_dict(m) for m in messages],
    }


# Minimum seconds between retry button presses for the same message.
# Enforced via Redis with a per-message TTL key so a client that
# bypasses the disabled button still gets rejected.
RETRY_COOLDOWN_SECONDS = 10


@app.post("/messages/{message_id}/retry")
def retry_message(message_id: str, request: Request):
    """Re-enqueue a failed assistant message. Caller must own the
    conversation. Cooldown is server-enforced — the client UI disables
    its retry button for the same window but a hand-crafted request
    will still 429."""
    msg = db.get_message(message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    if msg["role"] != "assistant":
        raise HTTPException(status_code=400, detail="not an assistant message")
    conv_row = db.get_conversation(msg["conversation_id"])
    if conv_row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conversation_owner(request, conv_row)

    # Cooldown gate runs BEFORE the state check so a rapid double-click
    # gets 429'd (the spam signal) rather than 409'd (the "already
    # retrying" signal). SET NX EX is the atomic primitive here — only
    # the first caller in the window gets the OK, everyone else sees
    # the remaining TTL and gets 429'd.
    cd_key = f"retry_cd:{message_id}"
    if not r.set(cd_key, "1", nx=True, ex=RETRY_COOLDOWN_SECONDS):
        remaining = r.ttl(cd_key)
        retry_after = max(1, int(remaining)) if remaining is not None else RETRY_COOLDOWN_SECONDS
        raise HTTPException(
            status_code=429,
            detail=f"retry cooldown active; try again in {retry_after}s",
            headers={"Retry-After": str(retry_after)},
        )

    if msg["status"] != "error":
        # Release the cooldown we just claimed since we're not actually
        # going to do work — otherwise an accidental click on a
        # complete/pending message would lock out a legitimate retry
        # on the same id later.
        r.delete(cd_key)
        raise HTTPException(
            status_code=409,
            detail=f"message is not in error state (status={msg['status']})",
        )

    # Rebuild the worker-facing prompt from prior turns. The user
    # message that produced this failure is at seq - 1; everything
    # before it is conversation context. We also pin the conversation
    # owner as the submitter for usage/quota accounting on retry.
    all_messages = db.list_messages(msg["conversation_id"])
    user_msg = None
    prior: list = []
    for m in all_messages:
        if m["message_id"] == message_id:
            break
        if m["seq"] == msg["seq"] - 1 and m["role"] == "user":
            user_msg = m
        else:
            prior.append(m)
    if user_msg is None:
        raise HTTPException(
            status_code=500,
            detail="cannot find the user message that produced this failure",
        )
    worker_prompt = _format_chat_prompt(prior, user_msg["text"] or "")

    # Pick a model: explicit conversation default → original message
    # model → coordinator default at job-fetch time. Keeping the same
    # model on retry avoids a surprise model swap mid-conversation.
    use_model = conv_row["model"] or msg["model"]
    new_job_id = str(uuid.uuid4())
    submitted_at = time.time()
    member = getattr(request.state, "member", None)
    submitted_by = member.member_id if member is not None else None
    db.insert_job(
        new_job_id,
        user_msg["text"] or "",
        use_model,
        submitted_at,
        submitted_by,
        conversation_id=msg["conversation_id"],
    )
    if not db.reset_message_for_retry(message_id, new_job_id):
        # Someone else flipped status between get_message and now —
        # rare but possible. Release the cooldown and surface a 409.
        r.delete(cd_key)
        raise HTTPException(status_code=409, detail="message no longer in error state")
    db.touch_conversation(msg["conversation_id"], submitted_at)
    job = {
        "job_id": new_job_id,
        "prompt": worker_prompt,
        "model": use_model,
        "submitted_at": submitted_at,
    }
    r.rpush(JOB_QUEUE, json.dumps(job))
    log.info(
        "retry queued",
        extra={
            "event": "retry_queued",
            "job_id": new_job_id,
            "message_id": message_id,
        },
    )
    return {
        "ok": True,
        "job_id": new_job_id,
        "message_id": message_id,
        "cooldown_seconds": RETRY_COOLDOWN_SECONDS,
    }


@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request):
    """Soft delete (sets archived_at). The conversation and its
    messages stay in the DB so an admin can audit, but they no longer
    appear in the caller's default list."""
    row = db.get_conversation(conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conversation_owner(request, row)
    db.archive_conversation(conversation_id, time.time())
    return {"ok": True}


# ---------- invites ----------
def _invite_summary(row, *, with_contributor_email: bool = False) -> dict:
    """Shared shape for invite responses. ``with_contributor_email`` is
    on for the public redemption endpoint (so Bob sees who invited him);
    off for admin/contributor listings (which already know)."""
    out = {
        "code": row["code"],
        "invitee_email": row["invitee_email"],
        "daily_quota_tokens": row["daily_quota_tokens"],
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


def _invite_state(row, now: float) -> str:
    if row["revoked_at"] is not None:
        return "revoked"
    if row["accepted_at"] is not None:
        return "accepted"
    if row["expires_at"] is not None and row["expires_at"] < now:
        return "expired"
    return "open"


@app.post("/invites")
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
        invitee_email=req.invitee_email,
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
        "expires_at": expires_at,
    }


@app.get("/invites")
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


@app.get("/invites/{code}")
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


@app.post("/invites/{code}/accept")
def accept_invite(code: str, req: InviteAcceptRequest):
    """Public: Bob redeems his invite. One-shot — the same code cannot
    be accepted twice. Returns the new bearer token exactly once.

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
    now = time.time()
    new_member_id = "mem_" + uuid.uuid4().hex[:12]
    raw_token = member_auth.generate_token()
    token_hash = member_auth.hash_token(raw_token)

    invite_row = db.accept_invite_atomic(
        code=code,
        new_member_id=new_member_id,
        new_token_hash=token_hash,
        invitee_email=req.invitee_email,
        accepted_at=now,
        tos_version=TOS_VERSION,
    )
    if invite_row is None:
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
    return {
        "member_id": new_member_id,
        "token": raw_token,
        "role": "invitee",
        "parent_member_id": invite_row["contributor_member_id"],
        "daily_quota_tokens": invite_row["daily_quota_tokens"],
        "tos_version": TOS_VERSION,
    }


@app.post("/invites/{code}/revoke")
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


# ---------- admin ----------
@app.get("/admin/members")
def admin_list_members(request: Request):
    """Admin-only roster. Returns enough to manage the network: id,
    role, tier, email, parent, quota, revoked-flag, last-active. Never
    returns the raw token (it isn't stored)."""
    member = getattr(request.state, "member", None)
    if AUTH_ENABLED and (member is None or member.role != "admin"):
        raise HTTPException(status_code=403, detail="admin only")
    rows = db.list_members()
    return {
        "members": [
            {
                "member_id": r["member_id"],
                "email": r["email"],
                "role": r["role"],
                "tier": r["tier"],
                "parent_member_id": r["parent_member_id"],
                "daily_quota_tokens": r["daily_quota_tokens"],
                "revoked_at": r["revoked_at"],
                "created_at": r["created_at"],
                "last_active_at": r["last_active_at"],
                "tos_accepted_at": r["tos_accepted_at"] if "tos_accepted_at" in r.keys() else None,
                "tos_version": r["tos_version"] if "tos_version" in r.keys() else None,
            }
            for r in rows
        ]
    }


@app.get("/models")
def models():
    """Catalog of models the coordinator knows about. See coordinator/model_registry.py."""
    return {
        "strict": STRICT_MODELS,
        "models": [m.to_dict() for m in model_registry.list_all()],
    }


@app.get("/earnings")
def earnings():
    rows = db.list_earnings()
    workers_list = [
        {
            "worker_id": row["worker_id"],
            "total_tokens": int(row["total_tokens"]),
            "total_jobs": int(row["total_jobs"]),
            "total_usd": round(float(row["total_usd"]), 8),
        }
        for row in rows
    ]
    return {
        "workers": workers_list,
        "total_usd": round(sum(w["total_usd"] for w in workers_list), 8),
    }


@app.get("/earnings/{worker_id}")
def earnings_for(worker_id: str):
    row = db.earnings_for(worker_id)
    if row is None:
        return {"worker_id": worker_id, "total_tokens": 0, "total_usd": 0.0}
    return {
        "worker_id": row["worker_id"],
        "total_tokens": int(row["total_tokens"]),
        "total_usd": round(float(row["total_usd"]), 8),
    }


@app.get("/metrics")
def metrics():
    m = db.metrics()
    m["queue_depth"] = r.llen(JOB_QUEUE)
    m["processing"] = r.hlen(JOB_PROCESSING)
    now = time.time()
    workers_rows = db.list_workers()
    m["active_workers"] = sum(
        1 for w in workers_rows if (now - float(w["last_seen"] or 0)) < WORKER_TIMEOUT_SECONDS
    )
    m["registered_workers"] = len(workers_rows)
    return m
