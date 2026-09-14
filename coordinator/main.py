"""Coordinator: REST API + Redis queue + SQLite write-through + reaper."""
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from coordinator import admin_alerts  # noqa: F401 — import wires its event subscriptions
from coordinator import api_keys
from coordinator import member_auth, model_registry, notifications
from coordinator import openai_compat
from coordinator import prompt_rewrite
from coordinator import routes_account
from coordinator import routes_admin
from coordinator import routes_agent_pairing
from coordinator import routes_conversations
from coordinator.routes_agent_pairing import PUBLIC_BASE_URL
from coordinator import routes_generate
from coordinator.routes_generate import _build_chat_messages_with_info
from coordinator import routes_images
from coordinator import routes_invites
from coordinator import routes_observability
from coordinator.routes_observability import _require_admin
from coordinator.routes_invites import _invite_state
from coordinator import routes_misc
from coordinator import routes_workers
from coordinator.routes_workers import _caller_token_hash
from coordinator import schedule as machine_schedule
from coordinator import uploads as uploads_lib
from coordinator.canaries import CanaryInjector
from coordinator.db import DB
from coordinator.idempotency import IdempotencyStore
from coordinator.rate_limit import RateLimiter
from coordinator.redis_client import get_client
from coordinator.scheduler import Reaper
from coordinator.tier_engine import (
    TierEngine,
    UptimeSampler,
)
from shared.auth import API_TOKEN, AUTH_ENABLED, is_public_path
from shared.config import (
    CANARY_INTERVAL_SECONDS,
    IDEMPOTENCY_TTL_SECONDS,
    JOB_PROCESSING,
    JOB_QUEUE,
    CAPACITY_JOBS_PER_WORKER,
    RATE_LIMIT_PER_MIN,
    REQUIRE_LIVE_WORKER,
    WORKER_CAPABILITIES,
    WORKER_HEARTBEATS,
    WORKER_STATUS,
    WORKER_TIMEOUT_SECONDS,
)


# ---------- structured logging ----------
class JsonFormatter(logging.Formatter):
    # Allowlist of extras that get pulled out of log records and into
    # the JSON line. Add a key here when you want a new structured
    # field to survive into the prod logs — without this, `extra=`
    # values are silently dropped by Python's logging module. Keep
    # the list short: large payloads (e.g. full prompts) should be
    # truncated at the callsite, not here.
    _EXTRAS = (
        "job_id",
        "worker_id",
        "event",
        # Search-rewrite debug fields. Added 2026-05-23 after a
        # diagnosis cycle required SSHing into prod and dumping the
        # SQLite jobs table to see what the classifier was outputting.
        "rewrite_job_id",
        "search_job_id",
        "original_prompt",
        "rewrite_output",
        "decision",
        "parsed_value",
        "final_query",
        "rewrite_was_used",
        "history_chars",
        "search_status",
        "image_job_id",
        "image_status",
        "error",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": "coordinator",
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k in self._EXTRAS:
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
_uptime_sampler: UptimeSampler | None = None
_tier_engine: TierEngine | None = None


def ensure_admin_seed() -> None:
    """If ``API_TOKEN`` is set in the env, make sure an admin member
    exists. Pre-existing clients that send ``Authorization: Bearer
    $API_TOKEN`` are logged in as that admin — no client-side changes
    required.

    The check is "is there any active admin?" rather than "does
    API_TOKEN match an existing member?" Once the founding admin
    claims u/p credentials, their token rotates and the env value no
    longer matches any row; without this broader check we'd seed a
    second admin row on every restart.

    Trade-off: in the rare case of "I lost the password AND
    rotated-out-API_TOKEN no longer logs in," recovery is
    ``revoke + manual DELETE`` rather than ``set a fresh API_TOKEN
    and restart``. Acceptable — the multi-token table + the
    set-credentials CLI cover the realistic recovery paths.
    """
    if not API_TOKEN:
        return
    if db.has_active_admin():
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
    global _reaper, _canary_injector, _uptime_sampler, _tier_engine
    ensure_admin_seed()
    _reaper = Reaper(r, db)
    _reaper.start()
    if CANARY_INTERVAL_SECONDS > 0:
        _canary_injector = CanaryInjector(r, db, interval=CANARY_INTERVAL_SECONDS)
        _canary_injector.start()
    # Uptime sampler + tier engine — separate threads so a slow tier
    # evaluation can't starve the sampler (which feeds the very data
    # the engine reads). Sampler runs every 5 min; engine runs once
    # daily a few minutes after UTC midnight (see tier_engine
    # constructor for offset logic).
    _uptime_sampler = UptimeSampler(r, db)
    _uptime_sampler.start()
    _tier_engine = TierEngine(db)
    _tier_engine.start()
    log.info("coordinator ready", extra={"event": "startup"})
    try:
        yield
    finally:
        if _reaper:
            _reaper.stop()
        if _canary_injector:
            _canary_injector.stop()
        if _uptime_sampler:
            _uptime_sampler.stop()
        if _tier_engine:
            _tier_engine.stop()


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

# Push-notification endpoints (Phase 6 of pwa-refactor.txt). The router
# is built via a closure that captures the module-level ``db`` so we
# don't have to import db from this module into notifications.py
# (circular) or instantiate a fresh DB() per request.
app.include_router(notifications.build_router(db))
if not notifications.is_vapid_configured():
    log.info(
        "VAPID keys not configured — push delivery disabled "
        "(in-app notifications row still persists). Set "
        "VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY to enable.",
        extra={"event": "push_disabled"},
    )

# Document-upload endpoints (PDF/DOCX/TXT/MD/CSV → extracted text
# folded into chat context). Same closure-over-db reasoning as
# notifications above.
app.include_router(uploads_lib.build_router(db))


def _is_public(method: str, path: str) -> bool:
    """Path+method auth exemption. ``/health`` is fully open. The
    invite-redemption flow needs exactly two endpoints reachable
    without auth: ``GET /invites/<code>`` and ``POST /invites/<code>/accept``.
    The community ToS is public (``/tos`` and ``/tos/raw``). Username +
    password sign-in (``POST /login``) is public so the web UI can call
    it without a bearer — the credentials are the credential. Everything
    else under ``/invites`` (create, list, revoke) requires a valid bearer."""
    if is_public_path(path):
        return True
    if method == "GET" and path in ("/tos", "/tos/raw"):
        return True
    if method == "POST" and path == "/login":
        return True
    # Public, invite-free account creation — see POST /signup below.
    # Throttled separately (SIGNUP_MAX_PER_IP), not by RATE_LIMIT_PER_MIN.
    if method == "POST" and path == "/signup":
        return True
    # Email-verification link — clicked from an email, no bearer to
    # send. Security relies on the code itself (opaque, 24h TTL,
    # single-use — see POST /signup and GET /verify-email below).
    if method == "GET" and path == "/verify-email":
        return True
    # Agent pairing — the agent has no token yet, so all three of these
    # are public. Security relies on the short-lived pair_code (5-min
    # TTL) plus the requirement that an authenticated browser session
    # explicitly approves it before the token is handed out.
    if method == "POST" and path == "/agents/pair/start":
        return True
    if method == "POST" and path == "/agents/pair/poll":
        return True
    parts = path.strip("/").split("/")
    if (
        method == "GET"
        and len(parts) == 3
        and parts[0] == "agents"
        and parts[1] == "pair"
    ):
        return True
    if method == "GET" and len(parts) == 2 and parts[0] == "invites":
        return True
    if (
        method == "POST"
        and len(parts) == 3
        and parts[0] == "invites"
        and parts[2] == "accept"
    ):
        return True
    # The VAPID public key is meant to be distributed (it's PUBLIC by
    # design — the private half is what enables signing pushes). The
    # client fetches it before they can subscribe to push.
    if method == "GET" and path == "/notifications/vapid-key":
        return True
    return False


# ---------- community ToS ----------
# Version string is checked against the file every startup and stamped
# into each new member row when they accept. Bumping this manually
# (after a substantive change to docs/community-tos.md) will cause
# existing members to be flagged as "needs re-accept" by the per-
# member ToS check.
TOS_VERSION = "2026-08-22"
_TOS_PATH = Path(__file__).resolve().parent.parent / "docs" / "community-tos.md"


def _load_tos_text() -> str:
    try:
        return _TOS_PATH.read_text(encoding="utf-8")
    except OSError:
        return (
            "# GamerAI Community Terms of Service\n\n"
            "Terms document not bundled with this deploy.\n"
        )


def _is_generation_scoped_allowed(method: str, path: str) -> bool:
    """Routes a generation-scoped self-serve API key may call. Everything
    else — invites, friends' quotas, password/machine management, admin —
    is refused with 403 regardless of the member's own role, since a key
    like this may end up pasted into a third-party tool's config file.
    Closed by default (mirrors _is_public's shape above) so every current
    and future account-management route stays blocked without anyone
    needing to remember a per-route check."""
    if method == "POST" and path in ("/generate", "/v1/chat/completions"):
        return True
    if method == "GET" and path in ("/me", "/models", "/v1/models"):
        return True
    parts = path.strip("/").split("/")
    if method == "GET" and len(parts) == 2 and parts[0] in ("result", "images"):
        return True
    return False


# ---------- auth (no-op when API_TOKEN env is unset) ----------
@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    request.state.member = None
    request.state.token_scope = None
    if _is_public(request.method, request.url.path):
        return await call_next(request)
    if not AUTH_ENABLED:
        return await call_next(request)
    raw_token = member_auth.parse_bearer(request.headers.get("authorization"))
    if not raw_token:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    resolution = member_auth.resolve_token(db, raw_token)
    if resolution is None:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    request.state.member = resolution.member
    request.state.token_scope = resolution.scope
    if resolution.is_secondary:
        db.touch_member_token(resolution.token_hash, time.time())
    if resolution.scope == "generation" and not _is_generation_scoped_allowed(
        request.method, request.url.path
    ):
        return JSONResponse(
            {"detail": "this API key is scoped to generation endpoints"},
            status_code=403,
        )
    db.touch_member(resolution.member.member_id, time.time())
    return await call_next(request)


# ---------- rate limit (no-op when RATE_LIMIT_PER_MIN <= 0) ----------
def _client_ip(request: Request) -> str:
    """Best-effort real client IP for rate-limit keying.

    Only Caddy talks to the coordinator in prod (the container port is
    bound to localhost — see infra/docker-compose.prod.yml), and Caddy
    *appends* the observed peer to any inbound X-Forwarded-For. So the
    RIGHTMOST entry is the address Caddy actually saw; the leftmost is
    attacker-controlled. Trusting the leftmost let a client forge its
    own rate-limit bucket (send "X-Forwarded-For: 1.2.3.4", rotate it
    per request, bypass the limiter entirely). Take the rightmost hop
    instead, and fall back to the direct peer when there's no forwarded
    header (direct access / local dev)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
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
def _read_heartbeat(worker_id: str) -> tuple[float, Optional[str]]:
    """Return (last_ts, current_job_id) for ``worker_id``.

    WORKER_HEARTBEATS stores a JSON envelope ``{"ts": float, "job_id":
    str|null}`` so the reaper can distinguish "worker silent" from
    "worker still on the right job." Falls back to (ts, None) if the
    stored value is a bare float — covers the upgrade window where
    pre-rollout entries linger until the next heartbeat overwrites
    them."""
    raw = r.hget(WORKER_HEARTBEATS, worker_id)
    if not raw:
        return 0.0, None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            ts = float(data.get("ts", 0) or 0)
            job_id = data.get("job_id")
            return ts, job_id if isinstance(job_id, str) else None
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    try:
        return float(raw), None
    except (ValueError, TypeError):
        return 0.0, None


def _write_heartbeat(worker_id: str, ts: float, job_id: Optional[str]) -> None:
    r.hset(
        WORKER_HEARTBEATS,
        worker_id,
        json.dumps({"ts": ts, "job_id": job_id}),
    )


def _worker_status(worker_id: str, now: float) -> str:
    last, _ = _read_heartbeat(worker_id)
    if not last or (now - last) > WORKER_TIMEOUT_SECONDS:
        return "offline"
    return r.hget(WORKER_STATUS, worker_id) or "idle"


_WORKER_ID_RE = re.compile(r"^win-(.+)-[0-9a-f]{8}$")


def _machine_display_name(
    display_name: Optional[str],
    label: Optional[str],
    worker_id: Optional[str],
) -> str:
    """Friendly machine name for the UI. Preference order:

    1. ``display_name`` — the agent's own chosen (or randomly
       defaulted) machine name, sent on /register and stored on the
       ``workers`` row. The intended path going forward.
    2. A custom pairing ``label``.
    3. The hostname embedded in a pre-migration ``worker_id``
       (``win-<hostname>-<rand>``) — agents built before this slice
       still send that shape until they update; new registrations use
       an opaque random worker_id instead (see coordinator/db.py's
       display_name migration for why: the ToS promises we don't
       collect hostnames).
    4. The generic pairing label, or "agent"."""
    if display_name and display_name.strip():
        return display_name.strip()
    if label and label.strip() and label.strip().lower() != "agent":
        return label.strip()
    if worker_id:
        m = _WORKER_ID_RE.match(worker_id)
        if m:
            return m.group(1)
    return label or "agent"


_NO_WORKERS_MESSAGE = (
    "No community members are available right now. Please try again in a few minutes."
)
_NO_IMAGE_WORKERS_MESSAGE = (
    "No image-capable community members are online right now. "
    "Please try again in a few minutes, or use chat."
)
_NO_SEARCH_WORKERS_MESSAGE = (
    "No search-capable community members are online right now. "
    "Please try again in a few minutes, or uncheck search."
)
_NO_TTS_WORKERS_MESSAGE = (
    "No voice-capable community members are online right now. "
    "Please try again in a few minutes, or turn off voice mode."
)
_NO_SMART_WORKERS_MESSAGE = (
    "The smart-mode pipeline is offline right now (it needs its "
    "machines online and linked). Please try again in a few minutes, "
    "or turn off smart mode."
)
_AT_CAPACITY_MESSAGE = (
    "The network is at capacity right now — every contributor machine "
    "is busy with earlier requests. Please try again in a minute or two."
)

# All Redis list keys jobs can queue on, across every tool. Kept as a
# literal tuple (mirroring job_queue_for's own literals) rather than
# calling job_queue_for for every known tool, since that function's
# contract is "route one job", not "enumerate all queues".
_ALL_JOB_QUEUES = (
    JOB_QUEUE,
    "job_queue:image",
    "job_queue:search",
    "job_queue:tts",
    "job_queue:chat:smart",
)


def _worker_advertises_tool(worker_id: str, tool: str) -> bool:
    """Read the cached WorkerCapabilities for a worker and check whether
    it claims the given tool. Missing/legacy capabilities default to
    chat-only (the pre-multi-tool behavior). An explicit EMPTY tools
    list is honored as "serves nothing on the GPU" — that's how a
    smart-pipeline backend registers (its GPU is lent to the head via
    rpc-server, so it must never count as a live chat worker)."""
    raw = r.hget(WORKER_CAPABILITIES, worker_id)
    if not raw:
        return tool == "chat"
    try:
        caps = json.loads(raw)
    except json.JSONDecodeError:
        return tool == "chat"
    tools = caps.get("tools")
    if tools is None:
        tools = ["chat"]
    return tool in tools


def _ensure_live_worker_or_503(tool: str = "chat") -> None:
    """Refuse to enqueue when no worker advertising *tool* has
    heartbeated recently. Gated by REQUIRE_LIVE_WORKER so dev/tests can
    queue jobs without a worker attached. Without this, a job sits on
    the queue indefinitely (or, worse on a misconfigured prod, gets
    picked up by an in-VPS mock that fakes a reply).

    Tool-aware: image jobs need an image-capable worker; a swarm of
    chat-only workers does not unblock an image submission. The error
    message differs accordingly so the user knows which tool to fall
    back to."""
    if not REQUIRE_LIVE_WORKER:
        return
    now = time.time()
    heartbeats = r.hgetall(WORKER_HEARTBEATS) or {}
    for worker_id, _raw in heartbeats.items():
        ts, _ = _read_heartbeat(worker_id)
        if not ts or (now - ts) > WORKER_TIMEOUT_SECONDS:
            continue
        if _worker_advertises_tool(worker_id, tool):
            return
    if tool == "image":
        detail = _NO_IMAGE_WORKERS_MESSAGE
    elif tool == "search":
        detail = _NO_SEARCH_WORKERS_MESSAGE
    elif tool == "tts":
        detail = _NO_TTS_WORKERS_MESSAGE
    elif tool == "chat:smart":
        detail = _NO_SMART_WORKERS_MESSAGE
    else:
        detail = _NO_WORKERS_MESSAGE
    raise HTTPException(status_code=503, detail=detail)


def _live_worker_count() -> int:
    now = time.time()
    heartbeats = r.hgetall(WORKER_HEARTBEATS) or {}
    live = 0
    for worker_id, _raw in heartbeats.items():
        ts, _ = _read_heartbeat(worker_id)
        if ts and (now - ts) <= WORKER_TIMEOUT_SECONDS:
            live += 1
    return live


def _current_inflight_count() -> int:
    """Jobs queued (any tool) plus jobs currently claimed/processing.
    A coarse global count, not per-tool — the ceiling this backs is
    meant to protect the fleet's total capacity, not any one queue."""
    total = r.hlen(JOB_PROCESSING)
    for q in _ALL_JOB_QUEUES:
        total += r.llen(q)
    return total


def _ensure_capacity_or_503() -> None:
    """Refuse new jobs once the network is saturated relative to its
    live worker count, instead of letting the queue grow unbounded
    against what might be one or two contributor machines. Opt-in —
    0/unset CAPACITY_JOBS_PER_WORKER disables the check entirely
    (today's default: unbounded queueing)."""
    if CAPACITY_JOBS_PER_WORKER <= 0:
        return
    ceiling = max(_live_worker_count(), 1) * CAPACITY_JOBS_PER_WORKER
    if _current_inflight_count() >= ceiling:
        raise HTTPException(status_code=503, detail=_AT_CAPACITY_MESSAGE)


# Image-prompt / search-query rewrite helpers live in
# coordinator/prompt_rewrite.py now — closed over db/the heartbeat
# helpers just above and unpacked back into the same bare names
# generate()/`/jobs/complete`/retry_message already call. r is passed
# as a zero-arg getter (lambda: r), not the object itself, because
# some test modules reassign this module's `r` to a fakeredis
# instance AFTER import (see tests/test_conversations.py) — only a
# live lookup at call time observes that swap.
(
    _format_rewrite_history,
    _conversation_has_prior_context,
    _chat_worker_available_for_rewrite,
    _enqueue_chat_rewrite_for_image,
    _dispatch_image_after_rewrite,
    _parse_search_rewrite_output,
    _enqueue_chat_rewrite_for_search,
    _dispatch_search_after_rewrite,
    _scrub_citations,
) = prompt_rewrite.build_rewrite_helpers(
    db, lambda: r, _read_heartbeat, _worker_advertises_tool,
)


# /health, /tos, /tos/raw, /generate, /result live in
# coordinator/routes_generate.py now. Wired before api_keys/openai_compat
# since openai_compat needs generate()/result() already defined — same
# closure-over-db reasoning as notifications/uploads, extended to inject
# generate/result themselves so openai_compat.py never has to import
# anything from this module. routes_workers.py also needs the returned
# _job_row_to_envelope, so this must run before that wiring too.
_generate_router, generate, result, _job_row_to_envelope = routes_generate.build_router(
    db, lambda: r, idem, _ensure_live_worker_or_503, _ensure_capacity_or_503,
    TOS_VERSION, _load_tos_text, _chat_worker_available_for_rewrite,
    _enqueue_chat_rewrite_for_image, _enqueue_chat_rewrite_for_search,
)
app.include_router(_generate_router)
app.include_router(api_keys.build_router(db))
app.include_router(openai_compat.build_router(db, generate, result, model_registry))


app.include_router(routes_images.build_router(db))


_workers_router, _schedule_payload, _require_worker_owner, _verify_claim_or_410 = (
    routes_workers.build_router(
        db, lambda: r, _write_heartbeat, _job_row_to_envelope,
        _dispatch_image_after_rewrite, _dispatch_search_after_rewrite,
    )
)
app.include_router(_workers_router)
app.include_router(
    routes_observability.build_router(
        db, lambda: r, TOS_VERSION, _worker_status, _machine_display_name,
        _schedule_payload,
    )
)
app.include_router(
    routes_account.build_router(db, lambda: r, TOS_VERSION, _client_ip)
)
app.include_router(
    routes_conversations.build_router(
        db, lambda: r, _ensure_live_worker_or_503, _build_chat_messages_with_info,
    )
)


app.include_router(routes_invites.build_router(db, TOS_VERSION))


app.include_router(
    routes_admin.build_router(db, lambda: r, require_admin_fn=_require_admin)
)
app.include_router(routes_misc.build_router(db, r, require_admin_fn=_require_admin))


app.include_router(routes_agent_pairing.build_router(db, lambda: r))
