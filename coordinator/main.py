"""Coordinator: REST API + Redis queue + SQLite write-through + reaper."""
import base64
import binascii
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from coordinator import admin_alerts  # noqa: F401 — import wires its event subscriptions
from coordinator import api_keys
from coordinator import canaries as canary_lib
from coordinator import events
from coordinator.image_moderation import (
    IMAGE_DIR,
    _image_prompt_is_blocked,
    _save_image_or_raise,
    _validate_and_classify_init_image,
)
from coordinator.image_params import (
    _clamp_image_dim,
    _combine_negative_prompt,
    _default_image_params,
    _png_dimensions,
    image_cost_multiplier,
)
from coordinator import member_auth, model_registry, notifications
from coordinator import openai_compat
from coordinator import prompt_rewrite
from coordinator import routes_account
from coordinator import routes_admin
from coordinator import routes_agent_pairing
from coordinator.routes_agent_pairing import PUBLIC_BASE_URL
from coordinator import routes_images
from coordinator import routes_invites
from coordinator import routes_observability
from coordinator.routes_observability import _require_admin
from coordinator.routes_invites import _invite_state
from coordinator import routes_misc
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
    CANARY_PENDING,
    CANARY_REAL_JOBS_SINCE,
    IMAGE_REWRITE_PENDING,
    IMAGE_UNIT_COST_BASE,
    SEARCH_REWRITE_PENDING,
    SEARCH_AUTO_DISABLED,
    SUMMARY_PENDING,
    IDEMPOTENCY_TTL_SECONDS,
    JOB_AUDIO_CHUNKS,
    JOB_PARTIALS,
    MAX_HISTORY_TOKENS,
    JOB_PROCESSING,
    JOB_QUEUE,
    JOB_RESULTS,
    JOB_TIMEOUT_SECONDS,
    CAPACITY_JOBS_PER_WORKER,
    MAX_PROMPT_BYTES,
    RATE_LIMIT_PER_MIN,
    RATE_PER_TOKEN,
    REQUIRE_LIVE_WORKER,
    STRICT_MODELS,
    WORKER_CAPABILITIES,
    WORKER_EARNINGS,
    WORKER_HEARTBEATS,
    WORKER_REGISTRY,
    WORKER_SHARE,
    WORKER_STATUS,
    WORKER_TIMEOUT_SECONDS,
    job_queue_for,
)
from coordinator.tiers import (
    quota_for as _tier_quota_for,
)
from shared.models import (
    ConversationCreateRequest,
    GenerateRequest,
    GenerateResponse,
    HeartbeatRequest,
    JobCancelRequest,
    JobClaimRequest,
    JobCompleteRequest,
    JobDisplayedRequest,
    JobNextRequest,
    JobPartialRequest,
    WorkerIdent,
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


# ---------- public API ----------
@app.get("/health")
def health():
    try:
        r.ping()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"redis unavailable: {e}")


from string import Template as _Template

from shared.ui import BASE_CSS as _BASE_CSS, VIEWPORT_META as _VIEWPORT_META

_TOS_CSS = """
.page { max-width: 760px; line-height: 1.6; }
.meta { color: var(--muted); font-size: .9rem; margin-bottom: 1.5rem; padding-bottom: .75rem; border-bottom: 1px solid var(--border-soft); }
#content h1 { margin-top: 0; }
#content h2 { margin-top: 2rem; font-size: 1.25rem; }
#content h3 { margin-top: 1.5rem; font-size: 1.05rem; color: #333; }
#content p { margin: .75rem 0; }
#content ul, #content ol { padding-left: 1.25rem; margin: .5rem 0 .75rem; }
#content li { margin-bottom: .25rem; }
#content em { color: var(--muted); }
#content hr { border: 0; border-top: 1px solid var(--border); margin: 1.75rem 0; }
#loading { color: var(--muted); }
"""

_TOS_HTML_TEMPLATE = _Template(
    '<!doctype html><html><head><meta charset="utf-8">'
    + _VIEWPORT_META
    + "<title>GamerAI — Community ToS</title>"
    + "<style>" + _BASE_CSS + _TOS_CSS + "</style></head>"
    + '<body><div class="page">'
    + '<h1><a href="/">GamerAI</a></h1>'
    + '<div class="meta">Version <strong>$version</strong> · '
      '<a href="/tos/raw">view raw</a></div>'
    + '<div id="content"><span id="loading">Loading terms…</span></div>'
    + '<script src="/static/marked.min.js"></script>'
    + '<script src="/static/purify.min.js"></script>'
    + "<script>"
      "fetch('/tos/raw').then(r => r.text()).then(md => {"
      "  const html = window.marked.parse(md);"
      "  document.getElementById('content').innerHTML ="
      "    window.DOMPurify ? window.DOMPurify.sanitize(html) : html;"
      "}).catch(() => {"
      "  document.getElementById('content').innerHTML ="
      "    '<p>Could not load terms. <a href=\"/tos/raw\">View raw markdown</a>.</p>';"
      "});"
      "</script>"
    + "</div></body></html>"
)


@app.get("/tos", response_class=HTMLResponse)
def tos_html():
    """Public ToS page. Used both as the destination of the redemption-
    page link and as a stable URL contributors can revisit any time.
    The markdown body is fetched client-side from /tos/raw and rendered
    via marked.js so headings, lists, and emphasis come through as a
    real document, not preformatted ASCII in a <pre> block."""
    import html as html_lib
    return HTMLResponse(_TOS_HTML_TEMPLATE.substitute(
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
    if MAX_PROMPT_BYTES > 0 and len(req.prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"prompt exceeds MAX_PROMPT_BYTES ({MAX_PROMPT_BYTES})",
        )
    tool = (req.tool or "chat").lower()
    if tool not in ("chat", "image", "search", "tts"):
        raise HTTPException(
            status_code=400, detail=f"unknown tool: {tool!r}",
        )
    # messages[] is the stateless OpenAI-compatible path (see
    # coordinator/openai_compat.py) — chat-only, since image/search/tts
    # don't take a chat-style history.
    if req.messages and tool != "chat":
        raise HTTPException(
            status_code=400,
            detail="messages[] is only supported for tool=\"chat\"",
        )
    # search_mode is validated here so a typo from the UI fails fast
    # instead of leaking through to the agent (which would silently
    # default to "fast"). Only checked when the caller actually
    # selected search.
    search_mode: Optional[str] = None
    if tool == "search":
        search_mode = (req.search_mode or "fast").lower()
        if search_mode not in ("fast", "comprehensive"):
            raise HTTPException(
                status_code=400,
                detail=f"unknown search_mode: {search_mode!r}",
            )

    # Default the model for image jobs when the caller didn't pick one.
    # Done before STRICT_MODELS validation so the registry check sees a
    # concrete name. Single source of truth: model_registry.DEFAULT_IMAGE_MODEL.
    if tool == "image" and not req.model:
        req.model = model_registry.DEFAULT_IMAGE_MODEL
    # Same shape for TTS — the v1 Piper voice is the default the
    # agent's bootstrap pulls, so naming it here keeps coordinator and
    # agent in sync. Voice mode on the client never picks a model
    # explicitly today.
    if tool == "tts" and not req.model:
        req.model = model_registry.DEFAULT_TTS_MODEL
    # Smart mode: the UI sends a boolean, not a model name. Resolve it
    # to the smart-tier default here so everything downstream (strict
    # validation, queue routing via model_registry.route_for, the
    # message rows' model stamp) sees a concrete model. An explicit
    # req.model wins — a caller pinning a smart-tier model directly
    # gets smart routing with or without the flag.
    if tool == "chat" and req.smart and not req.model:
        req.model = model_registry.DEFAULT_SMART_MODEL

    # optional model-registry validation (off unless STRICT_MODELS=true)
    try:
        model_registry.validate_or_raise(req.model, strict=STRICT_MODELS)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Cross-check: tool="chat" must not target an image model, and
    # vice versa. Catches paste-bomb mistakes (someone passing `sd1.5`
    # with tool="chat") regardless of STRICT_MODELS. Only enforced
    # when the model is in the registry — unknown names slip through
    # the same way validate_or_raise lets them through in lax mode.
    # Search jobs run on chat models (they post the search results to
    # the LLM as a system message), so the expected kind is "chat" for
    # both tool="chat" and tool="search".
    expected_kind = "chat" if tool in ("chat", "search") else tool
    if req.model and model_registry.is_known(req.model):
        m = model_registry.get(req.model)
        if m is not None and m.kind != expected_kind:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"model {req.model!r} is a {m.kind} model; "
                    f"submit with tool={m.kind!r} (got {tool!r})"
                ),
            )

    # Refuse banned prompts for image jobs at submit time so a
    # contributor's machine never has to run them. See
    # _IMAGE_PROMPT_DENYLIST for what's covered.
    if tool == "image":
        blocked = _image_prompt_is_blocked(req.prompt)
        if blocked:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"image prompt refused: matches denylist term "
                    f"{blocked!r}. See /tos for the content policy."
                ),
            )

    # optional retry-safety: same Idempotency-Key returns the same job_id.
    # The idempotent path returns BEFORE the live-worker check — the
    # original submission was already accepted, so we just hand the
    # caller back the same job_id to resume polling.
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

    # Refuse to accept the job if no worker advertising this tool has
    # heartbeated recently (REQUIRE_LIVE_WORKER=true on prod). Runs
    # after prompt/idempotency validation so 400s still win, and BEFORE
    # any DB writes so a 503 leaves no orphan job/message rows. Smart-
    # routed chat needs a pipeline head specifically — re-checked below
    # once the conversation's pinned model is resolved, since a pinned
    # smart model can flip the route after this first gate.
    _ensure_live_worker_or_503(tool=model_registry.route_for(tool, req.model))
    # Same placement rationale as the live-worker gate above: after
    # validation, before any DB writes, so a 503 here leaves no orphan
    # rows. Independent of REQUIRE_LIVE_WORKER — this protects fleet
    # capacity even when the live-worker gate is off.
    _ensure_capacity_or_503()

    member = getattr(request.state, "member", None)
    submitted_by = member.member_id if member is not None else None

    # Signup accounts with an unconfirmed email can't consume until
    # they click the link — see POST /signup / GET /verify-email.
    # Contributing (running the agent as a worker) is never gated by
    # this; only submitting a job (this endpoint) is.
    if member is not None and not member.email_verified:
        raise HTTPException(
            status_code=403,
            detail=(
                "verify your email to unlock chat, image generation, "
                "and voice — check your inbox, or POST "
                "/me/resend-verification if it didn't arrive"
            ),
        )

    # Two-dimensional daily-quota enforcement (slice 2 + image-limits
    # slice). NULL on either column = unlimited for that dimension
    # (admin, tier-unlimited contributor). The check runs against
    # today's usage at submission time; a single prompt can overshoot
    # the chat cap by its completion size, which we don't predict here.
    # Image jobs gate on the image_units column instead — token output
    # for image jobs is unrelated to image-cost weighting.
    if member is not None:
        usage_today = db.member_usage_today(member.member_id)
        if tool == "image":
            cap = member.daily_quota_images
            if cap is not None and cap > 0:
                used_units = usage_today["image_units"]
                if used_units >= cap:
                    raise HTTPException(
                        status_code=429,
                        detail=(
                            f"daily image quota exceeded: "
                            f"{used_units:g} / {cap} image-units used today"
                        ),
                    )
        elif tool == "tts":
            # Voice cap precedence: explicit per-member override wins;
            # otherwise the tier's default voice_minutes from
            # tiers.TIER_QUOTAS. Different from tokens/images (where
            # NULL = unlimited) because voice ships with tier-driven
            # defaults — see voice-phase1 design memory. Admin is
            # always unlimited regardless of column value.
            if member.role != "admin":
                cap = member.daily_quota_voice_minutes
                if cap is None:
                    cap = _tier_quota_for(member.tier).get("voice_minutes")
                if cap is not None and cap > 0:
                    used_min = usage_today["voice_seconds"] / 60.0
                    if used_min >= cap:
                        raise HTTPException(
                            status_code=429,
                            detail=(
                                f"daily voice quota exceeded: "
                                f"{used_min:.1f} / {cap} voice-minutes used today"
                            ),
                        )
        else:
            cap = member.daily_quota_tokens
            if cap is not None and cap > 0:
                used = usage_today["tokens_out"]
                if used >= cap:
                    raise HTTPException(
                        status_code=429,
                        detail=(
                            f"daily quota exceeded: {used} / "
                            f"{cap} output tokens used today"
                        ),
                    )

    # Conversation context: if the caller passed conversation_id, load
    # the prior turns and build a chat messages[] array for the worker
    # (Ollama /api/chat) so the model gets its own chat template applied
    # instead of plain-text autocompletion. Ownership is enforced — a
    # caller cannot inject into someone else's conversation.
    #
    # Image jobs skip the messages[] envelope entirely — sd.cpp takes
    # a single prompt string, not a chat history — but they DO live
    # inside conversations so a user's image generations show up
    # interleaved with chat in the sidebar.
    conversation_id: Optional[str] = req.conversation_id
    worker_messages: Optional[list[dict]] = None
    history_info: Optional[dict] = None
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
        if tool in ("chat", "search"):
            # Search jobs reuse the chat-style messages envelope so the
            # worker has the same conversation context to ground the
            # summary in (handy for follow-ups like "what about in
            # Europe?"). The worker prepends its own search-results
            # system message before calling Ollama.
            summary_text = (
                conv_row["summary_text"]
                if "summary_text" in conv_row.keys()
                else None
            )
            summary_through_seq = (
                conv_row["summary_through_seq"]
                if "summary_through_seq" in conv_row.keys()
                else None
            )
            # Attached-document context (chat only — see the doc-upload
            # scope note in coordinator/uploads.py; search jobs build
            # their own worker-side context and don't need this).
            document_context = (
                uploads_lib.build_document_context(db.list_uploads(conversation_id))
                if tool == "chat"
                else None
            )
            worker_messages, history_info = _build_chat_messages_with_info(
                prior, req.prompt,
                summary_text=summary_text,
                summary_through_seq=summary_through_seq,
                document_context=document_context,
            )
            # Fire-and-forget summarization for any newly-dropped turns.
            # The summary job runs through the normal worker pool; its
            # completion replaces conversations.summary_text so the
            # NEXT /generate gets the benefit. The current turn pays
            # only the cap-truncated prefill cost (the summary it
            # eventually produces won't help this request).
            try:
                _maybe_enqueue_summary_job(conv_row, prior, history_info)
            except Exception as e:
                log.warning(
                    "summary enqueue failed (non-fatal): %s", e,
                    extra={"event": "summary_enqueue_failed"},
                )
        # Conversation may pin a default model; honor it when the call
        # didn't override. For image jobs we DO NOT inherit a
        # chat-conversation's pinned LLM (that would re-trigger the
        # tool/model mismatch above) — only inherit when the pinned
        # model is in the same kind. Search and chat share the same
        # underlying model kind, so they can inherit from each other.
        if not req.model and conv_row["model"]:
            pinned = conv_row["model"]
            pinned_kind = (
                model_registry.get(pinned).kind
                if model_registry.is_known(pinned)
                and model_registry.get(pinned) is not None
                else "chat"
            )
            if pinned_kind == expected_kind:
                req_model = pinned
            else:
                req_model = model_registry.DEFAULT_IMAGE_MODEL if tool == "image" else None
        else:
            req_model = req.model
    elif req.messages:
        # Stateless OpenAI-compatible path (coordinator/openai_compat.py):
        # no conversation row to rebuild history from — the external
        # caller manages its own history and resends the full array each
        # call, so pass it straight through to the worker envelope.
        worker_messages = req.messages
        req_model = req.model
    else:
        req_model = req.model

    # Final routing key — req_model may differ from req.model after
    # conversation-pin inheritance (e.g. a smart-mode conversation's
    # follow-up turn arrives with no explicit model or flag). When the
    # route flipped to chat:smart only now, the earlier liveness gate
    # checked the wrong pool, so re-check before any DB writes.
    route = model_registry.route_for(tool, req_model)
    if route != model_registry.route_for(tool, req.model):
        _ensure_live_worker_or_503(tool=route)

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
        "prompt": req.prompt,
        "model": req_model,
        "submitted_at": submitted_at,
        "tool": tool,
    }
    if route != tool:
        # Routing key for requeue paths that only have the envelope in
        # hand (reaper, abandon). tool stays "chat" so the agent's
        # chat handler — streaming, partials, token accounting — runs
        # unchanged; only the queue placement differs.
        job["route"] = route
    if worker_messages is not None:
        # The worker prefers messages[] (routed to Ollama /api/chat) when
        # present, falling back to the bare prompt for single-shot
        # generations and canaries. Keeping both fields keeps the
        # envelope backward-compatible with any worker that's still on
        # the old build.
        job["messages"] = worker_messages
    if tool == "chat" and req.voice_mode:
        # Tell the agent to pipeline first-sentence TTS in parallel with
        # LLM streaming. Only meaningful on chat; image/search/tts agents
        # ignore the field. Omitted (rather than set false) so a legacy
        # agent on an older build never sees an unknown key.
        job["voice_mode"] = True
    if tool == "image":
        # Image-only knobs. Only include fields the user explicitly
        # pinned so the worker can fall through to the model's sidecar
        # defaults (steps / sampler / cfg) for everything else. Hard
        # defaults here silently override LCM-tuned sidecars and cost
        # 3-4× per job — see the v1.1.24 fix.
        params = req.image or _default_image_params()
        image_env: dict = {
            "seed": params.seed,
            "negative_prompt": _combine_negative_prompt(params.negative_prompt),
        }
        # Clamp width/height to sane bounds (multiple of 64 in [256,
        # 1536]) so a malicious or buggy client can't ask the worker
        # to spend 10 minutes on an 8K image. sd.cpp itself also
        # requires multiples of 64.
        if params.width is not None:
            image_env["width"] = _clamp_image_dim(params.width)
        if params.height is not None:
            image_env["height"] = _clamp_image_dim(params.height)
        if params.steps is not None:
            image_env["steps"] = max(1, min(50, int(params.steps)))
        if params.init_image_b64:
            # Image alteration (img2img). Validates + NSFW-classifies
            # BEFORE this job ever reaches a queue — raises 4xx here,
            # same as every other pre-dispatch input check in this
            # handler, rather than letting a contributor's agent
            # discover the problem after doing the work.
            _validate_and_classify_init_image(params.init_image_b64, job_id)
            image_env["init_image_b64"] = params.init_image_b64
            if params.strength is not None:
                # (0, 1] — 0 would mean "no change at all" (a wasted
                # job); sd.exe's own default (0.75) applies when the
                # client omits this entirely.
                image_env["strength"] = min(1.0, max(0.01, float(params.strength)))
        job["image"] = image_env
    if tool == "search":
        # search_mode is validated above; carry it through so the agent
        # can branch fast (snippets) vs comprehensive (fetch + extract).
        job["search"] = {"mode": search_mode or "fast"}
    # Decide whether this job should go through the context-aware
    # rewrite pipeline. Two flavors share the same skip-paths:
    # - tool=image: rewrite the visual prompt using prior turns (see
    #   _enqueue_chat_rewrite_for_image)
    # - tool=search: rewrite the DDG query using prior turns (see
    #   _enqueue_chat_rewrite_for_search) — "try again" → "different
    #   recent news topic"
    #
    # Skipped when: chat tool (no rewrite needed), no conversation_id,
    # empty conversation (first turn — nothing to refine against), no
    # chat worker online (avoid stranding the job behind a rewrite
    # nobody can pick up).
    rewriteable = tool in ("image", "search")
    needs_rewrite = (
        rewriteable
        and conversation_id is not None
        and prior
        and _conversation_has_prior_context(prior)
        and _chat_worker_available_for_rewrite()
    )

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
        tool=tool,
        status=("awaiting_rewrite" if needs_rewrite else "pending"),
    )
    # Feeds the canary injector's traffic gate (see coordinator/canaries.py)
    # — counts real, customer-facing submissions only, not the hidden
    # rewrite/summary jobs this handler may also enqueue below.
    r.incr(CANARY_REAL_JOBS_SINCE)
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
    if needs_rewrite:
        # Hand the envelope to the matching rewrite pipeline (image or
        # search). The pipeline enqueues a hidden chat job; the real
        # job goes on its target queue later, in /jobs/complete's
        # rewrite-dispatch handler.
        if tool == "image":
            _enqueue_chat_rewrite_for_image(
                image_job_id=job_id,
                image_envelope=job,
                original_prompt=req.prompt,
                history=_format_rewrite_history(prior),
                submitted_by=submitted_by,
                submitted_at=submitted_at,
            )
        else:  # tool == "search"
            _enqueue_chat_rewrite_for_search(
                search_job_id=job_id,
                search_envelope=job,
                original_prompt=req.prompt,
                history=_format_rewrite_history(prior),
                submitted_by=submitted_by,
                submitted_at=submitted_at,
            )
    else:
        r.rpush(job_queue_for(route), json.dumps(job))
    idem.remember(idem_key, job_id)
    log.info(
        "queued job",
        extra={
            "event": "job_queued",
            "job_id": job_id,
        },
    )
    return GenerateResponse(
        job_id=job_id,
        assistant_message_id=assistant_message_id,
        history_info=history_info,
    )


_SUMMARY_SYSTEM_PROMPT = (
    "You produce CONVERSATION RECAPS for a chat assistant. Your "
    "output is prepended to the next reply's context so the "
    "assistant remembers who they're talking to and what they "
    "discussed — it is NOT a content summary of any document, "
    "article, code, or text that happens to appear in the "
    "transcript.\n\n"
    "Capture: what the user is working on or interested in, "
    "personal details they shared (name, location, preferences, "
    "projects), questions they asked, decisions or opinions they "
    "expressed, and any unresolved threads. If a document or piece "
    "of content came up, just note that it came up — do not "
    "summarize the document itself. Drop pleasantries, restated "
    "questions, and any quoted/generated text. Write 2-3 short "
    "paragraphs of plain prose as if briefing a colleague taking "
    "over the conversation."
)


def _maybe_enqueue_summary_job(conv_row, prior_messages, history_info) -> None:
    """Fire an async chat job that summarizes the oldest turns the
    truncation pass just dropped. The job runs through the normal
    worker pool; on /jobs/complete the result text replaces
    conversations.summary_text and bumps summary_through_seq, so the
    next /generate ships a short summary + recent turns instead of
    the full transcript. No-op when nothing fresh needs summarizing
    (no drops, or the existing summary already covers them)."""
    if not history_info:
        return
    if history_info.get("messages_dropped", 0) < 2:
        return
    conv_id = conv_row["conversation_id"]
    existing_through = (
        conv_row["summary_through_seq"]
        if "summary_through_seq" in conv_row.keys()
        else None
    ) or 0
    # Reconstruct the eligible-and-sorted view that the build path used,
    # so messages_dropped tracks the same chronologically-ordered set.
    eligible: list = []
    for m in prior_messages:
        role = m["role"]
        status = m["status"] if "status" in m.keys() else "complete"
        text = (m["text"] or "").strip()
        seq = m["seq"] if "seq" in m.keys() else None
        if role not in ("user", "assistant"):
            continue
        if role == "assistant" and (status != "complete" or not text):
            continue
        if seq is None:
            continue
        eligible.append(m)
    if not eligible:
        return
    eligible.sort(key=lambda m: m["seq"])
    dropped_count = history_info["messages_dropped"]
    dropped_msgs = eligible[:dropped_count]
    if not dropped_msgs:
        return
    new_through_seq = int(dropped_msgs[-1]["seq"])
    if new_through_seq <= existing_through:
        return  # already summarized this far
    # Build the summarizer's input. Originally this was the system
    # prompt + the raw user/assistant turns as separate messages, but
    # small models (llama3.2:3b) returned empty text when the message
    # array ended on an assistant turn — Ollama had no "what should I
    # say next?" cue. The conversation-as-single-user-message shape
    # gives the model an unambiguous "user asks for summary" turn to
    # respond to, which yields a non-empty assistant reply every time.
    existing_summary = (
        conv_row["summary_text"]
        if "summary_text" in conv_row.keys()
        else None
    )
    conv_lines: list[str] = []
    if existing_summary:
        conv_lines.append("Summary so far:\n" + existing_summary + "\n")
    for m in eligible:
        if m["seq"] > new_through_seq:
            break
        role = m["role"]
        text = (m["text"] or "").strip()
        if not text:
            continue
        if role == "assistant":
            text = _scrub_citations(text)
        label = "User" if role == "user" else "Assistant"
        conv_lines.append(f"{label}: {text}")
    conversation_text = "\n\n".join(conv_lines)
    summary_input: list[dict] = [
        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": (
            "Below is a conversation between a User and Assistant. "
            "Recap it in 2-3 short paragraphs so the next reply "
            "remembers the USER — what they want help with, who "
            "they are, what they've shared about themselves. Do "
            "NOT summarize any documents, articles, code, or text "
            "that appears in the transcript; just note that they "
            "came up (e.g. 'the user asked for the Declaration of "
            "Independence', not 'the Declaration of Independence "
            "is a foundational document…').\n\n"
            "--- BEGIN CONVERSATION ---\n\n"
            + conversation_text +
            "\n\n--- END CONVERSATION ---\n\n"
            "Provide only the recap text. No preamble, no headers, "
            "no lists, no meta commentary."
        )},
    ]
    # Orphan chat job — no conversation_id link, no submitter, no
    # placeholder message row. Runs through the same worker pool as
    # any other chat job; the worker can't tell it apart, which is
    # fine because the summary system prompt does all the steering.
    job_id = str(uuid.uuid4())
    submitted_at = time.time()
    envelope = {
        "job_id": job_id,
        "prompt": _SUMMARY_SYSTEM_PROMPT,
        "messages": summary_input,
        "model": None,
        "submitted_at": submitted_at,
        "tool": "chat",
    }
    db.insert_job(
        job_id,
        _SUMMARY_SYSTEM_PROMPT,
        None,
        submitted_at,
        None,
        conversation_id=None,
        tool="chat",
        status="pending",
    )
    r.hset(
        SUMMARY_PENDING,
        job_id,
        json.dumps({
            "conversation_id": conv_id,
            "through_seq": new_through_seq,
        }),
    )
    r.rpush(job_queue_for("chat"), json.dumps(envelope))
    log.info(
        "summary job enqueued",
        extra={
            "event": "summary_enqueued",
            "job_id": job_id,
            "conversation_id": conv_id,
            "through_seq": new_through_seq,
            "input_turns": len(summary_input) - 1,
        },
    )


def _estimate_history_tokens(text: str) -> int:
    """Coarse token estimator for the history-cap math. chars/4 lines up
    with how the worker bills tokens elsewhere; perfect parity with the
    model's tokenizer isn't necessary because the cap is a soft target
    aimed at "submit-to-first-token doesn't grow O(history)", not a
    hard quota."""
    return max(1, len(text) // 4) if text else 0


def _build_chat_messages_with_info(
    prior_messages,
    new_user_text: str,
    summary_text: Optional[str] = None,
    summary_through_seq: Optional[int] = None,
    cap_tokens: int = MAX_HISTORY_TOKENS,
    document_context: Optional[str] = None,
) -> tuple[list[dict], dict]:
    """Build the Ollama /api/chat messages[] array from persisted
    conversation rows plus the new user turn, applying a tail-window
    cap so a many-turn thread doesn't pin model prefill to O(history).

    Newest turns are kept verbatim. Once the accumulated estimate hits
    ``cap_tokens`` we stop folding in older turns. If a ``summary_text``
    is supplied it's prepended as a system message and any persisted
    turn with seq <= summary_through_seq is excluded (those turns are
    represented by the summary). Returns the messages array AND an
    info dict the response can surface to the client so the UI can
    display "older turns aren't in context".

    ``document_context`` (from coordinator.uploads.build_document_context)
    is inserted as its own system message immediately before the new
    user turn — deliberately NOT subject to cap_tokens/MAX_HISTORY_TOKENS,
    since an attached document is current-turn context to answer against,
    not aging history to be pruned; it has its own independent budget
    (MAX_UPLOAD_CONTEXT_CHARS, applied by the caller).

    Pending/empty assistant rows from a previous-failed-but-not-yet-
    retried turn are skipped so the model doesn't see a stray empty-
    assistant message in the middle of the history.

    Citation markers (``[1]``, ``[2, 3]``) are scrubbed from prior
    assistant content — see _scrub_citations for the bug they caused
    when handed back to a model alongside a new search step's sources."""
    # Filter + normalize, keeping seq so we can apply summary_through_seq.
    eligible: list[dict] = []
    for m in prior_messages:
        role = m["role"]
        text = (m["text"] or "").strip()
        status = m["status"] if "status" in m.keys() else "complete"
        seq = m["seq"] if "seq" in m.keys() else None
        if role not in ("user", "assistant", "system"):
            continue
        if role == "assistant" and (status != "complete" or not text):
            continue
        if (
            summary_through_seq is not None
            and seq is not None
            and seq <= summary_through_seq
        ):
            # Replaced by the summary; do not include the raw turn too.
            continue
        if role == "assistant":
            text = _scrub_citations(text)
        eligible.append({"role": role, "content": text, "seq": seq})

    # Walk newest → oldest, keeping turns until the cap is hit. Stop at
    # the first overshoot so we don't half-include a long turn (e.g.,
    # a 3000-token paste). The newest turn always lands even if it
    # alone exceeds cap_tokens — dropping it would defeat the point.
    kept_rev: list[dict] = []
    tokens_used = 0
    for m in reversed(eligible):
        cost = _estimate_history_tokens(m["content"])
        if kept_rev and tokens_used + cost > cap_tokens:
            break
        tokens_used += cost
        kept_rev.append(m)
    kept = list(reversed(kept_rev))
    dropped_count = len(eligible) - len(kept)
    tokens_dropped = sum(
        _estimate_history_tokens(m["content"])
        for m in eligible[: len(eligible) - len(kept)]
    )

    out: list[dict] = []
    if summary_text:
        # Anchor the model with the earlier-history summary first so it
        # has continuity without paying the full token cost. Phrased as
        # a system message because it's editorial context, not user or
        # assistant words.
        out.append({
            "role": "system",
            "content": (
                "Earlier in this conversation (summarized):\n" + summary_text
            ),
        })
    for m in kept:
        out.append({"role": m["role"], "content": m["content"]})
    if document_context:
        # Placed right before the current turn (not up top with the
        # summary) so the model's attention lands on it next to the
        # question it's actually needed for.
        out.append({
            "role": "system",
            "content": (
                "The user has attached one or more documents to this "
                "conversation. Use them to answer when relevant:\n\n"
                + document_context
            ),
        })
    out.append({"role": "user", "content": (new_user_text or "").strip()})

    info = {
        "messages_total": len(eligible) + (1 if summary_text else 0),
        "messages_kept": len(kept),
        "messages_dropped": dropped_count,
        "tokens_kept": tokens_used,
        "tokens_dropped": tokens_dropped,
        "summary_in_use": bool(summary_text),
        "cap_tokens": cap_tokens,
    }
    return out, info


def _build_chat_messages(prior_messages, new_user_text: str) -> list[dict]:
    """Back-compat wrapper that discards the truncation info dict.
    Used by the requeue path, where there's no /generate response to
    surface stats on."""
    msgs, _info = _build_chat_messages_with_info(prior_messages, new_user_text)
    return msgs


def _job_row_to_envelope(row) -> dict:
    """Reconstruct the worker-facing job envelope from a jobs row.
    Used when the in-flight processing-hash entry is missing (claim
    raced with a reaper, or abandon arrived before claim).

    Reconstructed envelope must match the shape /generate pushes —
    no submitted_by_member_id (see the canary-detection comment in
    /generate), tool carried through so requeue lands on the right
    queue, and image_params restored to defaults for image jobs
    (per-job params aren't persisted; a requeue after timeout may
    therefore use defaults instead of the user's chosen width/steps —
    a deliberate KISS tradeoff)."""
    keys = row.keys() if hasattr(row, "keys") else []
    tool = row["tool"] if "tool" in keys else "chat"
    env: dict = {
        "job_id": row["job_id"],
        "prompt": row["prompt"],
        "model": row["model"],
        "submitted_at": row["submitted_at"],
        "tool": tool,
    }
    # Smart-routed chat is derived from the persisted model, the same
    # rule /generate applied — so a requeued smart job goes back to the
    # pipeline head's queue instead of a 3B chat worker.
    route = model_registry.route_for(tool, row["model"])
    if route != tool:
        env["route"] = route
    if tool in ("chat", "search"):
        msgs = _rebuild_messages_for_requeue(row)
        if msgs is not None:
            env["messages"] = msgs
    elif tool == "image":
        env["image"] = _default_image_params().model_dump()
    if tool == "search":
        # search_mode isn't persisted to the jobs row, so a requeue
        # after reaper timeout defaults to "fast". Same KISS tradeoff
        # image-param requeue makes.
        env["search"] = {"mode": "fast"}
    return env


def _rebuild_messages_for_requeue(row) -> Optional[list[dict]]:
    """When a worker abandons or times out a job, the next worker needs
    the same chat envelope the original /generate produced. We rebuild
    it from the persisted conversation messages so requeued jobs still
    hit /api/chat instead of silently degrading to /api/generate.

    Returns ``None`` for jobs that were never part of a conversation
    (canaries, /generate calls with no ``conversation_id``) — those
    keep the legacy single-prompt path."""
    if row is None:
        return None
    keys = row.keys() if hasattr(row, "keys") else []
    if "conversation_id" not in keys:
        return None
    conversation_id = row["conversation_id"]
    if not conversation_id:
        return None
    all_msgs = db.list_messages(conversation_id)
    # The user message that triggered this job is the latest non-empty
    # user row; everything earlier than its assistant pair is the
    # history. Since the coordinator stores the user turn at enqueue
    # time, walking from the back picks it up reliably even when the
    # in-flight assistant row is still pending/error.
    new_user_text = row["prompt"] or ""
    prior: list = []
    found_match = False
    for m in all_msgs:
        if (
            not found_match
            and m["role"] == "user"
            and (m["text"] or "") == new_user_text
        ):
            found_match = True
            continue
        if found_match:
            continue
        prior.append(m)
    return _build_chat_messages(prior, new_user_text)


@app.get("/result/{job_id}")
def result(job_id: str, request: Request):
    # Ownership gate: a member may only poll their own jobs; admin can
    # read any (moderation). Mirrors /jobs/cancel + /jobs/displayed.
    # No-op when AUTH is disabled (dev/test). job_id is a uuid4 so this
    # is defense-in-depth, but the control must not rely on entropy.
    if AUTH_ENABLED:
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        if member.role != "admin":
            owner_row = db.get_job(job_id)
            submitted_by = (
                owner_row["submitted_by_member_id"]
                if owner_row is not None
                and "submitted_by_member_id" in owner_row.keys()
                else None
            )
            if submitted_by is None or submitted_by != member.member_id:
                raise HTTPException(status_code=404, detail="job not found")
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
    # Voice-mode chat: the agent ships audio chunks on partials before
    # the LLM completes. Read the per-seq hash, sort by seq, and surface
    # the ordered list so the polling client can queue new chunks as
    # they arrive.
    audio_chunks_list: list[dict] = []
    chunks_raw = r.hgetall(f"{JOB_AUDIO_CHUNKS}:{job_id}")
    if chunks_raw:
        try:
            audio_chunks_list = [json.loads(v) for v in chunks_raw.values()]
            audio_chunks_list.sort(key=lambda c: int(c.get("seq", 0)))
        except (json.JSONDecodeError, ValueError):
            audio_chunks_list = []
    status = row["status"]
    # Image jobs persist their result as messages.image_path (not on
    # the jobs row). Look it up so the polling path can surface the
    # PNG URL after a JOB_RESULTS eviction.
    image_path: Optional[str] = None
    msg = db.get_message_by_job(job_id)
    if msg is not None and "image_path" in msg.keys():
        image_path = msg["image_path"]
    # Search jobs also carry a sources[] list when complete (lives only
    # on JOB_RESULTS — it's render-only data, not stored back to the
    # jobs row). On the DB-fallback path here there's no JOB_RESULTS
    # entry to read from, so sources end up null — that's fine because
    # the client uses the JOB_RESULTS path for fresh completions and
    # the DB-fallback path only fires after eviction.
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
        "image_path": image_path,
        "sources": None,
        # search_was_skipped is a one-shot client signal and only
        # lives on JOB_RESULTS while the result is fresh. By the time
        # we're on this DB-fallback path (post-eviction), the client
        # has long since acted on it (or missed it). Defaulting to
        # null is harmless.
        "search_was_skipped": False,
        "audio_chunks": audio_chunks_list,
        "done": status in ("complete", "error"),
    }


# Self-serve API keys + the OpenAI-compatible chat surface. Registered
# here (rather than up with notifications/uploads) because openai_compat
# needs generate()/result() already defined — same closure-over-db
# reasoning as those two, extended to inject generate/result themselves
# so openai_compat.py never has to import anything from this module.
app.include_router(api_keys.build_router(db))
app.include_router(openai_compat.build_router(db, generate, result, model_registry))


app.include_router(routes_images.build_router(db))


# ---------- worker lifecycle ----------
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


@app.post("/register")
def register(req: WorkerIdent, request: Request):
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
    _write_heartbeat(req.worker_id, now, None)
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


@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest, request: Request):
    _require_worker_owner(request, req.worker_id)
    now = time.time()
    # ``job_id`` is what the worker claims to currently be processing.
    # The reaper reads it on its next tick to decide whether an
    # in-flight job is still in the hands of its rightful claimant
    # (extend the deadline) or has actually gone silent (requeue).
    _write_heartbeat(req.worker_id, now, req.job_id)
    r.hset(WORKER_STATUS, req.worker_id, req.status)
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
    raw = r.hget(JOB_PROCESSING, job_id)
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


# Upper bound for /jobs/next long-poll wait time, enforced server-side
# so a misconfigured worker can't pin a coordinator request thread for
# minutes. Caddy's reverse-proxy has generous default timeouts but we
# don't want to depend on that — 30s also keeps the response window
# inside any aggressive intermediate proxy / load balancer defaults.
MAX_LONGPOLL_SECONDS = 30.0


@app.post("/jobs/next")
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


@app.post("/jobs/claim")
def claim(req: JobClaimRequest, request: Request):
    """Legacy claim path for the in-VPS worker (which pops jobs from
    Redis directly with BLPOP and then calls /jobs/claim). Remote agents
    use the atomic /jobs/next path instead. Returns the ``claim_token``
    the worker must include on subsequent /jobs/complete and
    /jobs/partial calls."""
    _require_worker_owner(request, req.worker_id)
    # find original job payload — best-effort, used only for requeue on timeout
    raw = r.hget(JOB_RESULTS, req.job_id)
    original = None
    if raw is None:
        row = db.get_job(req.job_id)
        if row:
            original = _job_row_to_envelope(row)
    claim_token, deadline = _issue_claim(req.worker_id, req.job_id, original)
    log.info(
        "job claimed",
        extra={"event": "job_claimed", "job_id": req.job_id, "worker_id": req.worker_id},
    )
    return {"ok": True, "deadline": deadline, "claim_token": claim_token}


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
            original = _job_row_to_envelope(row)
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


@app.post("/jobs/complete")
def complete(req: JobCompleteRequest, request: Request):
    """Worker submits result. Coordinator writes Redis result, earnings, SQLite row."""
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
                _dispatch_image_after_rewrite(req.job_id, link, rewritten)
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
                _dispatch_search_after_rewrite(req.job_id, srlink, rewritten)
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


@app.post("/jobs/cancel")
def cancel_job(req: JobCancelRequest, request: Request):
    """Member-initiated cancellation. The worker (if still processing)
    discovers the cancellation when its /jobs/complete returns 410 —
    we don't have a worker-side push channel, so the contract is "the
    worker's eventual result is dropped on the floor."

    Idempotent: cancelling an already-terminal job is a 200 no-op so
    a double-click from a flaky network can't 404 the second click."""
    row = db.get_job(req.job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")

    # Ownership: the submitter (or admin) can cancel. Auth-off mode
    # permits everything (dev/test) — same shape as the conversation
    # owner check.
    if AUTH_ENABLED:
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        submitted_by = (
            row["submitted_by_member_id"]
            if "submitted_by_member_id" in row.keys() else None
        )
        if (
            submitted_by is not None
            and submitted_by != member.member_id
            and member.role != "admin"
        ):
            raise HTTPException(status_code=404, detail="job not found")

    # Idempotency: already terminal → no-op. We do this AFTER the
    # ownership check so a guess at someone else's job_id still gets
    # 404, not "already complete."
    current_status = row["status"]
    if current_status in ("complete", "error", "cancelled"):
        return {"ok": True, "already_terminal": True, "status": current_status}

    now = time.time()
    user_message = "Cancelled by you."
    payload = {
        "job_id": req.job_id,
        "status": "cancelled",
        "worker_id": row["worker_id"],
        "model": row["model"],
        "text": user_message,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "earnings": 0.0,
        "duration_seconds": 0.0,
        "error": "cancelled",
    }
    r.hset(JOB_RESULTS, req.job_id, json.dumps(payload))
    # Remove JOB_PROCESSING so the worker's eventual /complete sees no
    # active claim and returns 410. Also blow away any stale partials.
    r.hdel(JOB_PROCESSING, req.job_id)
    r.hdel(JOB_PARTIALS, req.job_id)
    r.delete(f"{JOB_AUDIO_CHUNKS}:{req.job_id}")
    db.mark_job_complete(
        job_id=req.job_id,
        worker_id=row["worker_id"] or "",
        model=row["model"] or "",
        text=user_message,
        prompt_tokens=0,
        completion_tokens=0,
        earnings=0.0,
        duration_seconds=0.0,
        completed_at=now,
        status="cancelled",
        error="cancelled by user",
    )
    msg = db.get_message_by_job(req.job_id)
    if msg is not None:
        db.finalize_message(
            message_id=msg["message_id"],
            text=user_message,
            status="error",
        )
    log.info(
        "job cancelled",
        extra={"event": "job_cancelled", "job_id": req.job_id},
    )
    return {"ok": True, "status": "cancelled"}


@app.post("/jobs/displayed")
def displayed(req: JobDisplayedRequest, request: Request):
    """Client signals it rendered the final text. Closes the end-to-end
    timing trail (submitted_at → started_at → first_partial_at →
    completed_at → client_displayed_at) on the jobs row. Ownership-
    gated like /jobs/cancel; idempotent so a duplicate POST from a
    queue replay can't move the first-display moment."""
    row = db.get_job(req.job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if AUTH_ENABLED:
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        submitted_by = (
            row["submitted_by_member_id"]
            if "submitted_by_member_id" in row.keys() else None
        )
        if (
            submitted_by is not None
            and submitted_by != member.member_id
            and member.role != "admin"
        ):
            raise HTTPException(status_code=404, detail="job not found")
    db.mark_job_displayed(req.job_id, float(req.displayed_at_ms) / 1000.0)
    return {"ok": True}


app.include_router(
    routes_observability.build_router(
        db, lambda: r, TOS_VERSION, _worker_status, _machine_display_name,
        _schedule_payload,
    )
)
app.include_router(
    routes_account.build_router(db, lambda: r, TOS_VERSION, _client_ip)
)


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
        "image_path": row["image_path"] if "image_path" in keys else None,
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
    # Stash the latest summary on the detail response so the client's
    # collapsible stats panel can render it without an extra round-trip.
    # Deliberately not on the list endpoint — the per-row payload would
    # balloon with thousand-char summaries no sidebar entry uses.
    keys = row.keys()
    summary_text = row["summary_text"] if "summary_text" in keys else None
    summary_through_seq = (
        row["summary_through_seq"] if "summary_through_seq" in keys else None
    )
    return {
        **_conv_row_to_summary(row),
        "summary_text": summary_text,
        "summary_through_seq": summary_through_seq,
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

    # Refuse to re-enqueue when no worker has heartbeated recently —
    # same policy as /generate. Runs BEFORE the cooldown gate so a
    # 503 doesn't burn the user's retry window.
    _ensure_live_worker_or_503()

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
    # Retries stay consistent with a normal /generate call: an
    # attached document doesn't silently drop out of context just
    # because this particular turn failed and got retried.
    document_context = uploads_lib.build_document_context(
        db.list_uploads(msg["conversation_id"])
    )
    worker_messages, _retry_history_info = _build_chat_messages_with_info(
        prior, user_msg["text"] or "", document_context=document_context,
    )

    # Pick a model: explicit conversation default → original message
    # model → coordinator default at job-fetch time. Keeping the same
    # model on retry avoids a surprise model swap mid-conversation.
    use_model = conv_row["model"] or msg["model"]
    # Carry the original job's tool forward so the retry lands on the
    # matching per-tool queue. Today only chat reaches this endpoint
    # (image/search messages don't surface retry buttons), but
    # defaulting to "chat" with a DB-driven lookup means an image
    # retry added later doesn't silently land on the chat queue.
    orig_tool = "chat"
    # sqlite3.Row exposes columns via __getitem__, not .get() — wrap
    # the lookup in try/except so a row without a job_id (legacy
    # message rows) falls through to the chat default cleanly.
    try:
        orig_job_id = msg["job_id"]
    except (IndexError, KeyError):
        orig_job_id = None
    if orig_job_id:
        orig_job = db.get_job(orig_job_id)
        if orig_job is not None:
            try:
                orig_tool = (orig_job["tool"] or "chat").lower()
            except (IndexError, KeyError):
                pass
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
    # Feeds the canary injector's traffic gate — see coordinator/canaries.py.
    r.incr(CANARY_REAL_JOBS_SINCE)
    db.touch_conversation(msg["conversation_id"], submitted_at)
    job = {
        "job_id": new_job_id,
        "prompt": user_msg["text"] or "",
        "model": use_model,
        "submitted_at": submitted_at,
        "messages": worker_messages,
        "tool": orig_tool,
    }
    # A retried smart-mode turn keeps its model, so it must also keep
    # its queue — same model-derived rule as /generate and the reaper.
    retry_route = model_registry.route_for(orig_tool, use_model)
    if retry_route != orig_tool:
        job["route"] = retry_route
    r.rpush(job_queue_for(retry_route), json.dumps(job))
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
    """Hard-delete the conversation and everything attached:

    - all message rows
    - all job rows linked via ``messages.job_id`` or ``jobs.conversation_id``
    - every generated PNG referenced by those messages (off disk)
    - residual Redis state for those jobs (JOB_RESULTS / JOB_PROCESSING / JOB_PARTIALS)
    - the conversation row itself

    A request to delete an unknown conversation returns 404; the
    member must own the conversation (admin can override) or they
    get the same 404 (we don't distinguish "yours doesn't exist" from
    "someone else's exists", per _require_conversation_owner).

    This used to be a soft archive (sets ``archived_at``). The
    contract changed in v1.1.26 once the UI grew a real delete button
    — users expect "delete" to mean "the bytes are gone," not "hidden
    from my sidebar." There is no current consumer of the archive
    semantics; if one shows up later, the path forward is a separate
    /conversations/{id}/archive endpoint rather than reviving this
    one."""
    row = db.get_conversation(conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conversation_owner(request, row)
    image_basenames, job_ids = db.purge_conversation(conversation_id)
    # Filesystem cleanup — best-effort per file; one missing or
    # locked file shouldn't block the deletion of the others.
    for name in image_basenames:
        # Defense in depth: the basename came from our own DB write
        # (a uuid + ".png"), but path-traversal validation here costs
        # nothing and protects against a future code path that stores
        # an attacker-controlled string in image_path.
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", name or ""):
            continue
        path = IMAGE_DIR / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning(
                "image file unlink failed",
                extra={
                    "event": "image_unlink_failed",
                    "conversation_id": conversation_id,
                    "image_path": name,
                    "error": str(exc),
                },
            )
    # Redis cleanup. Late /jobs/complete from a worker after deletion
    # will already 410 via the claim-token gate (the JOB_PROCESSING
    # entry is gone), so this is just sweeping up the result + partial
    # text that would otherwise linger until natural eviction.
    for jid in job_ids:
        r.hdel(JOB_RESULTS, jid)
        r.hdel(JOB_PROCESSING, jid)
        r.hdel(JOB_PARTIALS, jid)
        r.delete(f"{JOB_AUDIO_CHUNKS}:{jid}")
    log.info(
        "conversation deleted",
        extra={
            "event": "conversation_deleted",
            "conversation_id": conversation_id,
            "images_removed": len(image_basenames),
            "jobs_removed": len(job_ids),
        },
    )
    return {
        "ok": True,
        "deleted": True,
        "images_removed": len(image_basenames),
        "jobs_removed": len(job_ids),
    }


app.include_router(routes_invites.build_router(db, TOS_VERSION))


app.include_router(
    routes_admin.build_router(db, lambda: r, require_admin_fn=_require_admin)
)
app.include_router(routes_misc.build_router(db, r, require_admin_fn=_require_admin))


app.include_router(routes_agent_pairing.build_router(db, lambda: r))
