"""Coordinator: REST API + Redis queue + SQLite write-through + reaper."""
import base64
import binascii
import json
import logging
import os
import re
import secrets
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
from coordinator import canaries as canary_lib
from coordinator import email_send
from coordinator import events
from coordinator import member_auth, model_registry, notifications
from coordinator import schedule as machine_schedule
from coordinator.canaries import CanaryInjector
from coordinator.db import DB
from coordinator.idempotency import IdempotencyStore
from coordinator.rate_limit import RateLimiter
from coordinator.redis_client import get_client
from coordinator.scheduler import Reaper
from coordinator.tier_engine import (
    STALE_WORKER_HIDE_DAYS,
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
    CANARY_SCORE_WINDOW,
    IDEMPOTENCY_TTL_SECONDS,
    JOB_AUDIO_CHUNKS,
    JOB_PARTIALS,
    MAX_HISTORY_TOKENS,
    JOB_PROCESSING,
    EMAIL_VERIFY_TTL_SECONDS,
    JOB_QUEUE,
    JOB_RESULTS,
    JOB_TIMEOUT_SECONDS,
    CAPACITY_JOBS_PER_WORKER,
    LOGIN_FAIL_MAX,
    LOGIN_FAIL_WINDOW_SECONDS,
    MAX_PROMPT_BYTES,
    RATE_LIMIT_PER_MIN,
    SIGNUP_MAX_PER_IP,
    SIGNUP_WINDOW_SECONDS,
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
    requirements_for as _tier_requirements_for,
    tier_above as _tier_above,
    tier_below as _tier_below,
    meets_requirements as _tier_meets,
)
from shared.models import (
    AgentPairConfirmRequest,
    AgentPairPollRequest,
    ConversationCreateRequest,
    FriendQuotaUpdateRequest,
    GenerateRequest,
    GenerateResponse,
    HeartbeatRequest,
    InviteAcceptRequest,
    InviteCreateRequest,
    JobCancelRequest,
    JobClaimRequest,
    JobCompleteRequest,
    JobDisplayedRequest,
    JobNextRequest,
    JobPartialRequest,
    LoginRequest,
    MachineScheduleUpdate,
    PasswordChangeRequest,
    SignupRequest,
    TestEmailRequest,
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


# Where generated images are written. Lives next to the SQLite DB so
# it shares the same volume — one mount, one backup, one rotation
# policy. Filename is {job_id}.png so the messages.image_path =
# "{job_id}.png" suffix stays portable across deploys (no absolute
# path baked into the DB).
#
# IMAGE_DIR resolves env var > sibling-of-DB_PATH > /data/images,
# in that order. Test suites that override DB_PATH to a tmp dir
# therefore get a writable images dir for free.
def _resolve_image_dir() -> Path:
    explicit = os.getenv("IMAGE_DIR")
    if explicit:
        return Path(explicit)
    from shared.config import DB_PATH as _DB_PATH
    db_parent = Path(_DB_PATH).parent
    if db_parent and str(db_parent) not in ("", "."):
        return db_parent / "images"
    return Path("/data/images")


IMAGE_DIR = _resolve_image_dir()
try:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Fall back to a tmp dir if the configured path isn't writable
    # (e.g. a developer running the coordinator outside Docker with
    # no /data volume). Image features still work; persistence is
    # lost across process restarts.
    import tempfile
    IMAGE_DIR = Path(tempfile.mkdtemp(prefix="gamerai-images-"))

# Cap on accepted PNG size. 8 MB lets a 1024×1024 image through with
# headroom; anything bigger is likely a misbehaving (or malicious)
# worker. ``image_b64`` arrives base64-encoded so the wire payload is
# ~4/3 of this.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Lightweight prompt-side denylist. NOT a content moderation system —
# this just refuses the most obviously banned categories at submit time
# so a contributor's machine never has to run them. Phase 3b+ ships a
# real classifier (image-side); for now we keep the surface small and
# explicit so we can point at it during incident review.
_IMAGE_PROMPT_DENYLIST = re.compile(
    r"\b("
    r"csam|child(?:\s+|-)porn|cp\b|"
    r"loli(?:con)?|shota|underage\b|minor\b|prepubescent|"
    r"bestiality|zoophilia|"
    r"non[- ]?consensual|rape\b"
    r")\b",
    re.IGNORECASE,
)


def _image_prompt_is_blocked(prompt: str) -> Optional[str]:
    """Return the matched denylist phrase if the prompt should be
    refused, else None. Surface the phrase so the user sees a concrete
    reason — vague 'rejected for content' is hostile and hides bugs."""
    m = _IMAGE_PROMPT_DENYLIST.search(prompt or "")
    return m.group(0) if m else None


# sd.cpp requires image dims to be multiples of 64. We clamp into
# [256, 1536] so a client can't ask the worker to render a 16K canvas.
_IMAGE_DIM_MIN = 256
_IMAGE_DIM_MAX = 1536


# Always-on negative prompt forced onto every image job. Layer-1
# mitigation for the case the v1.1.24 incident report flagged:
# "obese cow" → topless woman wearing cow ears. DreamShaper-class
# SD1.5 models are happy to drift toward nudity when the prompt is
# vague; biasing the sampler away with explicit negatives catches
# ~90% of the accidental-nudity case at zero infrastructure cost.
# Layer-2 is NudeNet on the output; see _classify_image_or_filter.
# Env-tunable so production can swap phrasing without a redeploy.
_FORCED_NEGATIVE_PROMPT = os.getenv(
    "FORCED_NEGATIVE_PROMPT",
    "nsfw, nude, naked, topless, partially nude, bare skin, "
    "sexual, sexually suggestive, explicit, lingerie, underwear",
)


# ---------- image-prompt rewrite (context-aware) ----------
# When a user submits an image inside an existing conversation, their
# new request often references prior turns ("draw some corn" → image →
# "I wanted it wrapped in bacon"). Sending "wrapped in bacon" to
# sd.cpp produces a strip of bacon. The rewrite pipeline runs a hidden
# chat job through Ollama that combines the recent conversation with
# the new request and produces a self-contained image prompt
# ("corn wrapped in bacon"). The image worker never sees the original
# request, only the rewritten one. Side-effects: ~5s latency added to
# any image-with-context submission; one extra chat job billed to the
# submitter. Skipped when: no conversation, empty conversation, no
# chat workers online.
_REWRITE_HISTORY_LIMIT = 6


def _format_rewrite_history(prior_messages) -> str:
    """Build a short text snippet representing recent conversation
    state for the rewriter. Limited to the most recent
    ``_REWRITE_HISTORY_LIMIT`` non-empty / non-pending messages so the
    chat model isn't asked to digest 200 turns. Image-bubble
    placeholders (``[image: foo]``) are rewritten as
    ``[generated image: foo]`` so the model understands what the
    assistant did instead of seeing the prompt repeated verbatim.

    Citation markers from prior search-grounded answers are scrubbed
    so the classifier reads clean prose — a 1-3B model squinting at
    a transcript full of ``[1][2]`` markers tends to treat the
    conversation as already-resolved and over-classify as
    NO_SEARCH, which is the exact misfire that motivates the
    enclosing rewrite pipeline."""
    relevant = [
        m for m in prior_messages
        if (m["text"] or "").strip() and m["status"] != "pending"
    ]
    recent = relevant[-_REWRITE_HISTORY_LIMIT:]
    lines = []
    for m in recent:
        role_label = "User" if m["role"] == "user" else "Assistant"
        text = (m["text"] or "").strip()
        if m["role"] == "assistant" and text.startswith("[image:"):
            inner = text[len("[image:"):].rstrip("]").strip()
            text = f"[generated image: {inner}]"
        elif m["role"] == "assistant":
            text = _scrub_citations(text)
        lines.append(f"{role_label}: {text}")
    return "\n".join(lines)


def _build_rewrite_meta_prompt(history: str, user_prompt: str) -> str:
    # The rewriter has to infer intent, not just splice tokens — the
    # "Canned corn → 'Yes' → Label Green Giant" failure showed up
    # because the previous version literally told the model to
    # "combine" the new request with the prior subject. Real-world
    # follow-ups are more varied: agreement with a prior assistant
    # suggestion, redirection, modification, fresh standalone. Each
    # gets its own paragraph in the instructions plus a worked
    # example so a 1-3B chat model can actually pick the right move.
    return (
        "You craft image-generation prompts from conversational context.\n\n"
        "RECENT CONVERSATION:\n"
        f"{history}\n\n"
        f"NEW USER MESSAGE: {user_prompt}\n\n"
        "Decide what image the user wants, then write a clear visual "
        "prompt for the image generator. Follow these intent-reading "
        "rules:\n\n"
        "1. If the user says \"yes\", \"ok\", \"that one\", \"do it\", "
        "or similar agreement and the assistant's MOST RECENT message "
        "contains a visual description or image suggestion: write a "
        "prompt that captures the SUBSTANCE of that description "
        "(subject, setting, visible details). Do not just echo a "
        "short label — pull out the actual visual content the "
        "assistant described.\n"
        "2. If the user says something like \"make it bigger\", \"but "
        "blue\", \"wrapped in bacon\", \"on a beach instead\": that's "
        "a modification of the most recent image. Combine the prior "
        "subject with the new detail.\n"
        "3. If the user describes a NEW subject from scratch (e.g. "
        "\"a sunset over the ocean\"): use it as-is, ignore prior "
        "context.\n"
        "4. If the user gives a fragment that depends on prior "
        "context (e.g. \"with a label\" after \"canned corn\"): "
        "combine.\n\n"
        "Style requirements for your output prompt:\n"
        "- 1-3 sentences. Be concrete and visual: name the subject, "
        "the setting, key features, materials, lighting. Length "
        "should match how much context the user gave you — short "
        "if they gave little, rich if they gave a lot.\n"
        "- Skip storytelling, brand disclaimers, refusals, or "
        "meta-commentary. Just describe the image.\n"
        "- Do not wrap in quotes. Do not include labels like "
        "\"PROMPT:\" or \"Image:\". Output the prompt only.\n\n"
        "PROMPT:"
    )


def _clean_rewritten_prompt(raw: Optional[str], fallback: str) -> str:
    """Defensive cleanup on model output: trim, strip wrapping quotes,
    strip echoed headers, collapse internal blank lines, and refuse
    on suspiciously empty / oversized output by falling back to the
    original prompt.

    Multi-line output is preserved (sd.cpp accepts multi-sentence
    prompts and the richer ones produce better images) — the previous
    first-line-only cut was throwing away the descriptive detail the
    intent-aware meta-prompt is now asking the model to produce."""
    if not raw:
        return fallback
    s = raw.strip()
    # Strip any echoed header the model parrots back from the
    # meta-prompt. Loop so e.g. "PROMPT:\nFINAL PROMPT:" gets fully
    # peeled.
    for _ in range(3):
        stripped = False
        for header in (
            "PROMPT:", "FINAL PROMPT:", "Final prompt:",
            "Rewritten:", "Rewritten prompt:", "Output:",
            "Image:", "IMAGE PROMPT:",
        ):
            if s.lower().startswith(header.lower()):
                s = s[len(header):].strip()
                stripped = True
                break
        if not stripped:
            break
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    # Collapse runs of blank lines but keep paragraph structure —
    # sd.cpp parses the whole string, not just line one.
    s = re.sub(r"\n\s*\n+", "\n", s).strip()
    if not s or len(s) > 500:
        return fallback
    return s


def _chat_worker_available_for_rewrite() -> bool:
    """Is any chat-capable worker heartbeating recently? Without one,
    the rewrite chat job would sit on the queue forever and the user
    would never get their image, so we skip the rewrite step entirely
    and fall back to the raw prompt."""
    now = time.time()
    heartbeats = r.hgetall(WORKER_HEARTBEATS) or {}
    for worker_id, _raw in heartbeats.items():
        ts, _ = _read_heartbeat(worker_id)
        if not ts or (now - ts) > WORKER_TIMEOUT_SECONDS:
            continue
        if _worker_advertises_tool(worker_id, "chat"):
            return True
    return False


def _conversation_has_prior_context(prior_messages) -> bool:
    """A conversation has 'context' for rewrite purposes iff there's at
    least one prior completed message in it (any role). An empty
    conversation or one with only the just-inserted pending pair has
    nothing for the rewriter to work with — skip and use the raw
    prompt. This check runs BEFORE the new user+pending-assistant pair
    is appended in /generate, so ``prior_messages`` is the pre-insert
    snapshot."""
    return any(
        (m["text"] or "").strip() and m["status"] != "pending"
        for m in prior_messages
    )


def _enqueue_chat_rewrite_for_image(
    image_job_id: str,
    image_envelope: dict,
    original_prompt: str,
    history: str,
    submitted_by: Optional[str],
    submitted_at: float,
) -> str:
    """Enqueue the hidden chat rewrite job, store the linkage in
    Redis, and return the rewrite job_id. The actual image dispatch
    happens later, in /jobs/complete, when the rewrite returns.

    Linkage Redis HSET happens BEFORE the queue push so a fast worker
    that completes the rewrite job in microseconds can't race ahead of
    the linkage write and find no dispatch instructions."""
    rewrite_job_id = str(uuid.uuid4())
    meta_prompt = _build_rewrite_meta_prompt(history, original_prompt)
    rewrite_envelope = {
        "job_id": rewrite_job_id,
        "prompt": meta_prompt,
        # No `model` — worker uses its default chat model. No
        # `conversation_id` — this job is invisible to the chat UI.
        "submitted_at": submitted_at,
        "tool": "chat",
    }
    db.insert_job(
        rewrite_job_id,
        meta_prompt,
        None,
        submitted_at,
        submitted_by,
        conversation_id=None,
        tool="chat",
    )
    r.hset(
        IMAGE_REWRITE_PENDING,
        rewrite_job_id,
        json.dumps({
            "image_job_id": image_job_id,
            "image_envelope": image_envelope,
            "original_prompt": original_prompt,
        }),
    )
    r.rpush(job_queue_for("chat"), json.dumps(rewrite_envelope))
    log.info(
        "image prompt rewrite enqueued",
        extra={
            "event": "rewrite_enqueued",
            "rewrite_job_id": rewrite_job_id,
            "image_job_id": image_job_id,
        },
    )
    return rewrite_job_id


def _dispatch_image_after_rewrite(
    rewrite_job_id: str,
    link: dict,
    rewritten_text: Optional[str],
) -> None:
    """Called from /jobs/complete when the rewrite chat job finishes.
    Plugs the cleaned rewritten text into the stored image envelope,
    flips the image job row out of 'awaiting_rewrite', and RPUSHes the
    image onto the real image queue.

    Skipped (with the link still cleaned up) when the image job is
    no longer in 'awaiting_rewrite' status — covers the cancel-while-
    rewriting race (user clicked Cancel after we enqueued the rewrite
    but before it completed) and the conversation-was-purged race
    (image job row deleted while in flight). Without this guard we'd
    push an image onto the queue that no one is watching anymore.

    Always deletes the link entry so a stale duplicate completion (or
    a future replay) can't re-trigger dispatch."""
    image_job_id = link["image_job_id"]
    image_envelope = link["image_envelope"]
    original = link["original_prompt"]
    try:
        image_row = db.get_job(image_job_id)
        if image_row is None or image_row["status"] != "awaiting_rewrite":
            r.hdel(IMAGE_REWRITE_PENDING, rewrite_job_id)
            log.info(
                "image rewrite finished but image job no longer awaiting "
                "rewrite — skipping dispatch",
                extra={
                    "event": "rewrite_dispatch_skipped",
                    "rewrite_job_id": rewrite_job_id,
                    "image_job_id": image_job_id,
                    "image_status": image_row["status"] if image_row else "missing",
                },
            )
            return
        final_prompt = _clean_rewritten_prompt(rewritten_text, original)
        image_envelope["prompt"] = final_prompt
        db.set_job_pending_with_prompt(image_job_id, final_prompt)
        r.rpush(job_queue_for("image"), json.dumps(image_envelope))
        log.info(
            "image dispatched after rewrite",
            extra={
                "event": "rewrite_dispatched",
                "rewrite_job_id": rewrite_job_id,
                "image_job_id": image_job_id,
                "rewrite_was_used": final_prompt != original,
            },
        )
    finally:
        r.hdel(IMAGE_REWRITE_PENDING, rewrite_job_id)


def _build_search_query_meta_prompt(history: str, user_prompt: str) -> tuple[str, str]:
    """Return ``(system_message, user_message)`` for the routing
    classifier. We feed Ollama via /api/chat (messages[]) instead of
    /api/generate (raw prompt) so the small chat model's instruction-
    tuning chat template kicks in and the model treats this as
    "follow instructions" instead of "continue text".

    The drift from /api/generate was killing reliability — manual
    testing of "for sure it's happening?" / "really?" / "For sure?"
    on three consecutive turns produced three different failure
    modes (NO_SEARCH sentinel for the wrong reason, panic recant
    text, and a verbatim echo of the user prompt) when the same
    instructions were sent as a single completion prompt. Splitting
    rules+examples into the system slot and the per-call data into
    the user slot is the standard fix.

    Examples that motivated the design:
    - User: "news"           → "today's top news headlines"
    - User: "try again"      + prior news topic → "different recent news event"
    - User: "That's cool!"   + prior Boring Co topic → NO_SEARCH
    - User: "thanks!"        + any context       → NO_SEARCH
    - User: "for sure?"      + prior project topic → "<project> confirmed"
    """
    system = (
        "You are a STRICT routing classifier for a web-search "
        "assistant. Your ONLY job is to output one of two things:\n"
        "  (a) the literal token NO_SEARCH, or\n"
        "  (b) a short web-search query (3-8 keywords).\n"
        "Nothing else. No explanations. No apologies. No prose. No "
        "quoted answers to the user. You are not the user-facing "
        "assistant — you are a classifier whose output goes into a "
        "search engine.\n\n"
        "HARD RULES (apply FIRST):\n"
        "  1. If the new user message contains \"?\" → output a "
        "query. NEVER NO_SEARCH.\n"
        "  2. If the new user message expresses doubt, verification, "
        "or asks for confirmation → output a query. NEVER NO_SEARCH.\n"
        "  3. Default is QUERY. Only output NO_SEARCH for a pure ack "
        "with NO question and NO doubt.\n\n"
        "QUERY CRAFT:\n"
        "- Fragment that depends on prior context: use the prior "
        "topic to fill in the missing subject.\n"
        "- Standalone topic: tighten into keywords. Drop conversational "
        "filler.\n"
        "- Latest/newest? Add \"today\" / \"latest\" / the current year.\n"
        "- 3-8 keywords. No quotes. No labels.\n\n"
        "WORKED EXAMPLES:\n\n"
        "Example 1 — verification of a prior topic:\n"
        "  RECENT CONVERSATION:\n"
        "  User: Kevin O'Leary Utah data center news\n"
        "  Assistant: A $70B project has been announced...\n"
        "  NEW USER MESSAGE: for sure it's happening?\n"
        "  OUTPUT: kevin oleary utah data center confirmed status\n\n"
        "Example 2 — short doubt:\n"
        "  RECENT CONVERSATION:\n"
        "  User: SVB collapse rundown\n"
        "  Assistant: Silicon Valley Bank failed in March 2023...\n"
        "  NEW USER MESSAGE: really?\n"
        "  OUTPUT: SVB silicon valley bank collapse confirmed\n\n"
        "Example 3 — pure ack:\n"
        "  RECENT CONVERSATION:\n"
        "  User: NBA scores yesterday\n"
        "  Assistant: Lakers beat the Suns 110-102...\n"
        "  NEW USER MESSAGE: thanks!\n"
        "  OUTPUT: NO_SEARCH\n\n"
        "Example 4 — drill-in:\n"
        "  RECENT CONVERSATION:\n"
        "  User: banking news today\n"
        "  Assistant: Major US banks reported earnings...\n"
        "  NEW USER MESSAGE: what about Europe?\n"
        "  OUTPUT: european banking news today\n\n"
        "Example 5 — verification with question mark:\n"
        "  RECENT CONVERSATION:\n"
        "  User: latest iPhone\n"
        "  Assistant: The iPhone 17 was released...\n"
        "  NEW USER MESSAGE: are you sure that's the latest?\n"
        "  OUTPUT: latest iPhone model 2026\n"
    )
    user = (
        "RECENT CONVERSATION:\n"
        f"{history}\n\n"
        f"NEW USER MESSAGE: {user_prompt}\n\n"
        "OUTPUT:"
    )
    return (system, user)


# Sentinel returned by _parse_search_rewrite_output when the classifier
# says no search is needed. Constant rather than magic string so the
# dispatcher and the tests can refer to the same value.
SEARCH_REWRITE_NO_SEARCH = "__no_search__"


def _parse_search_rewrite_output(raw: Optional[str]) -> tuple[str, Optional[str]]:
    """Parse the rewrite chat job's output into one of:
    - ("skip", None) — the model decided no search is needed
    - ("query", "<cleaned query>") — the model produced a query
    - ("error", None) — output was empty / nonsense; caller should
      fall back to the original prompt

    Splitting parse from clean lets the dispatcher branch on intent
    without smuggling sentinels through the existing clean function."""
    if not raw:
        return ("error", None)
    s = raw.strip()
    # Strip any echoed header so "OUTPUT: NO_SEARCH" also classifies.
    for _ in range(3):
        stripped = False
        for header in (
            "OUTPUT:", "Output:", "QUERY:", "Query:", "Search query:",
            "Search:", "FINAL QUERY:", "Rewritten query:",
        ):
            if s.lower().startswith(header.lower()):
                s = s[len(header):].strip()
                stripped = True
                break
        if not stripped:
            break
    # NO_SEARCH detection — first-line, case-insensitive. We accept
    # the sentinel anywhere on the first line so "NO_SEARCH (the user
    # is just thanking me)" still classifies as skip.
    first_line = s.split("\n", 1)[0].strip().upper()
    if first_line.startswith("NO_SEARCH") or first_line == "NOSEARCH":
        return ("skip", None)
    return ("query", s)


def _enqueue_chat_rewrite_for_search(
    search_job_id: str,
    search_envelope: dict,
    original_prompt: str,
    history: str,
    submitted_by: Optional[str],
    submitted_at: float,
) -> str:
    """Same plumbing as `_enqueue_chat_rewrite_for_image` but linkage
    lives in SEARCH_REWRITE_PENDING and the eventual dispatch lands
    on `job_queue:search` instead of `job_queue:image`. The two
    rewrite types are stored under separate hashes so /jobs/complete
    knows which dispatcher to call without inspecting the original
    job's tool field."""
    rewrite_job_id = str(uuid.uuid4())
    system_msg, user_msg = _build_search_query_meta_prompt(
        history, original_prompt,
    )
    # The worker prefers messages[] when present (routes to Ollama
    # /api/chat with the model's chat template applied) and falls
    # back to `prompt` for legacy clients / single-shot generations.
    # Both fields are populated for backward-compatibility, but the
    # chat path is the load-bearing one — without the chat template,
    # a 3B model treats this as "continue text" and produces weird
    # output (panic recants, verbatim echoes, etc.). The persisted
    # DB row stores the concatenated text as the canonical prompt
    # for ledger / admin debug purposes.
    persisted_prompt = system_msg + "\n\n---\n\n" + user_msg
    rewrite_envelope = {
        "job_id": rewrite_job_id,
        "prompt": user_msg,  # legacy fallback only
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "submitted_at": submitted_at,
        "tool": "chat",
    }
    db.insert_job(
        rewrite_job_id,
        persisted_prompt,
        None,
        submitted_at,
        submitted_by,
        conversation_id=None,
        tool="chat",
    )
    r.hset(
        SEARCH_REWRITE_PENDING,
        rewrite_job_id,
        json.dumps({
            "search_job_id": search_job_id,
            "search_envelope": search_envelope,
            "original_prompt": original_prompt,
        }),
    )
    r.rpush(job_queue_for("chat"), json.dumps(rewrite_envelope))
    log.info(
        "search query rewrite enqueued",
        extra={
            "event": "search_rewrite_enqueued",
            "rewrite_job_id": rewrite_job_id,
            "search_job_id": search_job_id,
            # Content logging — without these, diagnosis required
            # SSHing into prod and dumping the SQLite jobs table.
            "original_prompt": (original_prompt or "")[:200],
            "history_chars": len(history or ""),
        },
    )
    return rewrite_job_id


def _clean_rewritten_search_query(raw: Optional[str], fallback: str) -> str:
    """Same defensive cleanup as `_clean_rewritten_prompt` but with a
    tighter length budget (search queries should be SHORT — anything
    over 200 chars is the model rambling) and an additional pass that
    flattens multi-line output to a single line. DDG accepts long
    queries but treats every extra word as a hard requirement, which
    is the opposite of what we want for a paraphrased follow-up."""
    if not raw:
        return fallback
    s = raw.strip()
    for _ in range(3):
        stripped = False
        for header in (
            "QUERY:", "Query:", "Search query:", "Search:",
            "FINAL QUERY:", "Rewritten query:", "Output:",
        ):
            if s.lower().startswith(header.lower()):
                s = s[len(header):].strip()
                stripped = True
                break
        if not stripped:
            break
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    # First line only — a multi-line query is the model writing prose,
    # which DDG won't handle. The first line is almost always the
    # actual query.
    s = s.split("\n", 1)[0].strip()
    if not s or len(s) > 200:
        return fallback
    return s


_CITATION_PATTERN = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")


def _scrub_citations(text: str) -> str:
    """Strip ``[1]``, ``[2]``, ``[1, 2]``-style citation markers from
    an assistant message body. Used when we reroute a search job to
    the chat queue (NO_SEARCH classification): the original chat
    model produced citations backed by sources, but the chat reroute
    won't have those sources, and a small model seeing dangling
    citation markers tends to recant ("I made an error, I don't have
    sources for that"). Scrubbing leaves the prose intact and the
    follow-up has a clean context to respond from."""
    if not text:
        return text
    return _CITATION_PATTERN.sub("", text).strip()


def _dispatch_search_after_rewrite(
    rewrite_job_id: str,
    link: dict,
    rewritten_text: Optional[str],
) -> None:
    """Called from /jobs/complete when a search-rewrite chat job
    finishes. Plumbs three outcomes:

    a) The classifier says NO_SEARCH (user message was a closure like
       "thanks" / "that's cool!"). We DROP the search step entirely,
       reroute the job to the chat queue with the conversation
       context intact, and record a marker so /jobs/complete can
       stamp `search_was_skipped: true` on the result. The client
       uses that to auto-uncheck the search box and reset the
       sticky-mode state for this conversation.

    b) The classifier produced a query. Plug it into the stored
       search envelope, flip the job out of `awaiting_rewrite`,
       RPUSH onto the search queue. Same shape as
       `_dispatch_image_after_rewrite`.

    c) Parse error / empty rewrite. Fall back to the user's original
       prompt, send to the search queue. Conservative — better to
       give the user a literal-prompt search than a stuck bubble.

    Skips entirely (still cleans the link) if the search job is no
    longer awaiting_rewrite — the cancel-while-rewriting and
    conversation-purged races."""
    search_job_id = link["search_job_id"]
    search_envelope = link["search_envelope"]
    original = link["original_prompt"]
    try:
        search_row = db.get_job(search_job_id)
        if search_row is None or search_row["status"] != "awaiting_rewrite":
            log.info(
                "search rewrite finished but search job no longer awaiting "
                "rewrite — skipping dispatch",
                extra={
                    "event": "search_rewrite_dispatch_skipped",
                    "rewrite_job_id": rewrite_job_id,
                    "search_job_id": search_job_id,
                    "search_status": search_row["status"] if search_row else "missing",
                },
            )
            return

        decision, value = _parse_search_rewrite_output(rewritten_text)
        # Always log the raw rewrite output + parsed decision so any
        # future regression is diagnosable from logs alone — the prior
        # Kevin O'Leary bug required SSHing into prod and dumping the
        # SQLite jobs table to figure out the classifier was
        # producing panic recants instead of NO_SEARCH or a query.
        log.info(
            "search rewrite parsed",
            extra={
                "event": "search_rewrite_parsed",
                "rewrite_job_id": rewrite_job_id,
                "search_job_id": search_job_id,
                "original_prompt": (original or "")[:200],
                "rewrite_output": (rewritten_text or "")[:200],
                "decision": decision,
                "parsed_value": (value or "")[:200] if value else None,
            },
        )

        # Hard guardrail: a question is never a closure. If the user's
        # original prompt contains a "?", we ignore a SKIP decision
        # and force a query. This catches a real failure observed in
        # manual testing where "for sure it's happening?" (a follow-
        # up VERIFICATION question) was misclassified as NO_SEARCH —
        # the chat reroute then panic-recanted the prior search-
        # grounded answer. Meta-prompt fix is also in place; the
        # guardrail is belt-and-suspenders.
        if decision == "skip" and "?" in (original or ""):
            log.info(
                "search rewrite skip overridden by ? guardrail",
                extra={
                    "event": "search_rewrite_skip_overridden",
                    "rewrite_job_id": rewrite_job_id,
                    "search_job_id": search_job_id,
                    "original_prompt": (original or "")[:200],
                },
            )
            decision = "error"  # fall through to original-prompt path
            value = None

        if decision == "skip":
            # Reverse-detection branch: reroute as plain chat. Strip
            # the search-specific fields so the worker treats it as a
            # normal chat job with the conversation history.
            #
            # Citation markers in prior assistant turns are scrubbed
            # — without sources to back them up, a small chat model
            # will see "[1][2]" and panic-recant ("I made an error,
            # I don't have sources for that claim"). The actual
            # information in the prior turn is fine to keep; just
            # the citation hooks need to go. Caught in manual testing
            # by the "for sure it's happening?" → recanted-prior-
            # answer failure.
            chat_envelope = dict(search_envelope)
            chat_envelope.pop("search", None)
            chat_envelope["tool"] = "chat"
            chat_envelope["prompt"] = original
            if chat_envelope.get("messages"):
                chat_envelope["messages"] = [
                    {
                        "role": m["role"],
                        "content": _scrub_citations(m.get("content", "")),
                    }
                    for m in chat_envelope["messages"]
                ]
            db.set_job_pending_with_prompt(search_job_id, original)
            r.hset(SEARCH_AUTO_DISABLED, search_job_id, "1")
            r.rpush(job_queue_for("chat"), json.dumps(chat_envelope))
            log.info(
                "search disabled by classifier — rerouted to chat",
                extra={
                    "event": "search_rewrite_classified_skip",
                    "rewrite_job_id": rewrite_job_id,
                    "search_job_id": search_job_id,
                    "original_prompt": (original or "")[:200],
                },
            )
            return

        # Query or fall-back-to-original.
        if decision == "query":
            final_query = _clean_rewritten_search_query(value, original)
        else:
            final_query = original
        search_envelope["prompt"] = final_query
        db.set_job_pending_with_prompt(search_job_id, final_query)
        r.rpush(job_queue_for("search"), json.dumps(search_envelope))
        log.info(
            "search dispatched after rewrite",
            extra={
                "event": "search_rewrite_dispatched",
                "rewrite_job_id": rewrite_job_id,
                "search_job_id": search_job_id,
                "rewrite_was_used": final_query != original,
                "original_prompt": (original or "")[:200],
                "final_query": (final_query or "")[:200],
                "decision": decision,
            },
        )
    finally:
        r.hdel(SEARCH_REWRITE_PENDING, rewrite_job_id)


def _combine_negative_prompt(user_negative: Optional[str]) -> str:
    """Layer the forced SFW phrases in FRONT of whatever the user
    asked to negate so the sampler sees them with full weight. Empty
    user input degenerates to just the forced prefix; missing forced
    prefix (env override to '') degenerates to user input unchanged."""
    user_negative = (user_negative or "").strip()
    forced = (_FORCED_NEGATIVE_PROMPT or "").strip()
    if not forced:
        return user_negative
    if not user_negative:
        return forced
    return f"{forced}, {user_negative}"


def _clamp_image_dim(value: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 512
    v = max(_IMAGE_DIM_MIN, min(_IMAGE_DIM_MAX, v))
    return (v // 64) * 64 or _IMAGE_DIM_MIN


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Parse (width, height) from a PNG IHDR. Layout: 8-byte signature
    + 4-byte chunk length + 4-byte 'IHDR' + 4-byte width BE + 4-byte
    height BE. Returns (0, 0) on a buffer too short to hold the chunk
    — callers treat that as 'unknown' and fall back to the smallest
    cost bucket so an oddball worker output never auto-bills 4×."""
    if len(data) < 24:
        return (0, 0)
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


def image_cost_multiplier(width: int, height: int) -> float:
    """Quota multiplier for an image of (*width*, *height*). Three
    buckets aligned with the composer's small/medium/large radios:

    - small  (≤ 512²)             → 1×
    - medium (≤ 768²)             → 2×
    - large  (anything bigger,
      including SDXL-native 1024²) → 4×

    Compute is roughly proportional to pixel area on diffusion models,
    so the factors approximate GPU work while keeping the displayed
    cost an integer the UI can render as 'costs N image-credits'.
    Unknown / zero dims fall through to 1× — fair-by-default for
    edge cases like a sub-256 canary."""
    area = max(int(width), 0) * max(int(height), 0)
    if area <= 0 or area <= 512 * 512:
        return 1.0
    if area <= 768 * 768:
        return 2.0
    return 4.0


def _default_image_params():
    """Wraps ImageParams() so the import lives at the top of the file
    only — keeps the /generate body short."""
    from shared.models import ImageParams
    return ImageParams()


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ---------- NSFW output classifier (layer 2) ----------
# The forced negative prompt (layer 1) catches most accidental nudity
# from vague prompts, but DreamShaper can still produce explicit
# output. NudeNet runs after generation as a hard gate: any detection
# of the explicit-anatomy classes above NSFW_THRESHOLD turns the job
# into a friendly "image filtered by content policy" error bubble.
# Soft-fails when the package isn't installed (dev/test environments
# without the docker layer) so the rest of the code path is unaffected.
_NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.5"))
_NSFW_BLOCKED_CLASSES = frozenset({
    # NudeNet v3 class names. The bar here is "family-friendly" — a
    # 6-year-old should be able to look over the requester's shoulder
    # without surprises. That means we go past the just-genitalia
    # set into shirtlessness and bare-midriff territory:
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
    "MALE_BREAST_EXPOSED",   # shirtless males — not family-friendly default
    "BELLY_EXPOSED",         # bare midriff / crop-top — same standard
    # COVERED variants (FEMALE_BREAST_COVERED, BUTTOCKS_COVERED, etc.)
    # are deliberately NOT in the block set. NudeNet fires those on
    # any clothed body part visible through fabric — adding them
    # would block ~80% of normal pictures of people. Other EXPOSED
    # categories (FEET_EXPOSED, ARMPITS_EXPOSED) are normal family
    # content (sandals, sleeveless shirts) and stay permitted.
})
_nudenet_detector = None  # lazy-loaded singleton
_nudenet_load_attempted = False


def _get_nudenet():
    """Lazy-load the NudeNet detector once. Returns None if the package
    isn't installed (dev/test). Failure is logged once, then silently
    skipped on every subsequent call so a missing dep doesn't break
    image jobs — the system stays usable, just without the safety
    net, and operators see the warning."""
    global _nudenet_detector, _nudenet_load_attempted
    if _nudenet_load_attempted:
        return _nudenet_detector or None
    _nudenet_load_attempted = True
    try:
        from nudenet import NudeDetector  # type: ignore
        _nudenet_detector = NudeDetector()
        log.info(
            "NudeNet classifier loaded (threshold=%.2f)",
            _NSFW_THRESHOLD,
            extra={"event": "nudenet_loaded"},
        )
    except Exception as e:
        log.warning(
            "NudeNet unavailable — image NSFW classifier disabled: %s",
            e,
            extra={"event": "nudenet_unavailable"},
        )
        _nudenet_detector = None
    return _nudenet_detector


class _NSFWFilteredError(Exception):
    """Image was rejected by the output classifier. Raised from inside
    _save_image_or_raise so the existing /jobs/complete catch-all
    surfaces it as image_save_error → friendly error bubble + retry
    button + worker doesn't get credited (the existing flow for any
    image save failure). Distinct class so we can identify the
    refusal in logs vs. a malformed-bytes / disk-full failure."""


def _classify_image_or_raise(png_bytes: bytes, job_id: str) -> None:
    """Run NudeNet on the bytes; raise _NSFWFilteredError if any
    explicit-anatomy class scores above the threshold. No-op (soft
    success) when NudeNet isn't installed — the operator gets a
    one-time warning at startup."""
    detector = _get_nudenet()
    if detector is None:
        return
    # NudeNet's detect() expects a file path. Materialize to a temp
    # file so we don't have to monkey-patch its internals — the bytes
    # are already in memory after base64 decode, so this is one
    # additional disk round-trip per image, ~5ms.
    import tempfile
    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="nudenet-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png_bytes)
        try:
            detections = detector.detect(tmp_path)
        except Exception as e:
            # Classifier failure should not block image delivery —
            # log loudly, then let the image through. An attacker
            # can't deliberately trigger this since they don't
            # control the model code path.
            log.warning(
                "NudeNet detect() failed — letting image through: %s", e,
                extra={"event": "nudenet_detect_failed", "job_id": job_id},
            )
            return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    matches = [
        d for d in (detections or [])
        if d.get("class") in _NSFW_BLOCKED_CLASSES
        and float(d.get("score") or 0) >= _NSFW_THRESHOLD
    ]
    if matches:
        log.warning(
            "image filtered by NSFW classifier",
            extra={
                "event": "nsfw_filtered",
                "job_id": job_id,
                "matches": [
                    {"class": m["class"], "score": round(float(m["score"]), 3)}
                    for m in matches
                ],
            },
        )
        raise _NSFWFilteredError(
            "Generated image was filtered by the content classifier. "
            "Please try a different prompt."
        )


def _save_image_or_raise(
    job_id: str, image_b64: Optional[str]
) -> tuple[str, int, int]:
    """Decode the worker-supplied PNG and persist it under IMAGE_DIR.
    Returns ``(basename, width, height)`` — basename is the
    messages.image_path the UI fetches from /images/<basename>;
    width/height come from the PNG's IHDR and are used by
    /jobs/complete to bill the right quota multiplier for the
    rendered size. Raises HTTPException on malformed payloads so
    /jobs/complete responds 400 — the worker should not retry the
    same broken bytes.

    Validates the PNG magic header so a worker can't sneak a JPEG (or
    a raw HTML page) past the route handler. Real moderation (NSFW
    classifier) is post-MVP; this is just shape validation."""
    if not image_b64:
        raise HTTPException(
            status_code=400,
            detail="image job completed without image_b64",
        )
    # Estimated decoded size — base64 overhead is 4/3. Reject before
    # decoding to avoid materializing a 1 GB string in memory.
    if len(image_b64) > int(MAX_IMAGE_BYTES * 4 / 3) + 16:
        raise HTTPException(
            status_code=413,
            detail=f"image too large (>{MAX_IMAGE_BYTES} bytes)",
        )
    try:
        data = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"image_b64 not valid base64: {e}",
        )
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"image too large after decode (>{MAX_IMAGE_BYTES} bytes)",
        )
    if not data.startswith(_PNG_SIGNATURE):
        raise HTTPException(
            status_code=400,
            detail="image bytes are not a PNG (missing magic header)",
        )
    # NSFW classifier runs BEFORE persisting so a blocked image never
    # touches disk. Raises _NSFWFilteredError on a hit; the outer
    # /jobs/complete handler catches that as image_save_error and the
    # UI gets a friendly "filtered" bubble.
    _classify_image_or_raise(data, job_id)
    # job_id is a uuid we generated — safe as a path component, but
    # belt-and-braces against ../ traversal.
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", job_id)
    fname = f"{safe}.png"
    dest = IMAGE_DIR / fname
    tmp = dest.with_suffix(".png.tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    width, height = _png_dimensions(data)
    return fname, width, height


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


def _machine_display_name(label: Optional[str], worker_id: Optional[str]) -> str:
    """Friendly machine name for the UI. Prefer a custom label; else the
    hostname embedded in worker_id (win-<hostname>-<rand>); else the
    generic pairing label."""
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
            worker_messages, history_info = _build_chat_messages_with_info(
                prior, req.prompt,
                summary_text=summary_text,
                summary_through_seq=summary_through_seq,
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


# ---------- image serving ----------
@app.get("/images/{name}")
def serve_image(name: str, request: Request):
    """Serve a generated PNG. Ownership-checked: an authenticated caller
    can only fetch images attached to a conversation they own (admin
    bypass for moderation). Auth-off dev mode serves everything so
    local development with no API_TOKEN still works.

    The filename is the basename stored in messages.image_path
    ({job_id}.png). We resolve the job → conversation → owner chain
    on every request rather than baking a token into the URL so
    revocation is automatic when a member is removed."""
    # Path-traversal defense — filename must be plain.
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", name or ""):
        raise HTTPException(status_code=400, detail="bad image name")
    path = IMAGE_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="image not found")

    if AUTH_ENABLED:
        member = getattr(request.state, "member", None)
        if member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        if member.role != "admin":
            # Look up the job and walk to the conversation owner.
            job_id = name[:-4]  # strip .png
            job_row = db.get_job(job_id)
            if job_row is None:
                raise HTTPException(status_code=404, detail="image not found")
            conv_id = (
                job_row["conversation_id"]
                if "conversation_id" in job_row.keys()
                else None
            )
            if conv_id is None:
                # Orphan image — no conversation to gate on. Refuse
                # rather than leak; the only path that creates an
                # orphan today is canary, which shouldn't produce
                # images.
                raise HTTPException(status_code=404, detail="image not found")
            conv_row = db.get_conversation(conv_id)
            if conv_row is None or conv_row["owner_member_id"] != member.member_id:
                raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(str(path), media_type="image/png")


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


# ---------- observability ----------
def _load_capabilities(worker_id: str) -> dict | None:
    raw = r.hget(WORKER_CAPABILITIES, worker_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


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


@app.get("/workers")
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
        live_status = _worker_status(wid, now)
        last_seen = float(w["last_seen"] or 0)
        e = earnings_by_worker.get(wid)
        owner_id = w["owner_member_id"] if "owner_member_id" in w.keys() else None
        owner = members_by_id.get(owner_id) if owner_id else None
        owner_label = (
            (owner["username"] or owner["email"] or owner_id) if owner else owner_id
        )
        out.append(
            {
                "worker_id": wid,
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


def _member_label(member, member_id: Optional[str]) -> Optional[str]:
    if member is None:
        return member_id
    return member["username"] or member["email"] or member_id


@app.get("/jobs")
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
    jobs_out = []
    for j in rows:
        jobs_out.append({
            "job_id": j["job_id"],
            "prompt": j["prompt"],
            "status": j["status"],
            "tool": j["tool"] if "tool" in j.keys() else "chat",
            "worker_id": j["worker_id"],
            "model": j["model"],
            "prompt_tokens": j["prompt_tokens"],
            "completion_tokens": j["completion_tokens"],
            "earnings": j["earnings"],
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
            "label": worker_id[-12:],
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
                {"worker_id": w["worker_id"], "label": w["worker_id"][-12:]}
                for w in owned_workers
            ],
        }

    return {
        "jobs": jobs_out,
        "filter_worker": filter_worker,
        "filter_member": filter_member,
    }


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
            "current_version": TOS_VERSION,
            "needs_reaccept": member.tos_version != TOS_VERSION,
        },
    }


@app.get("/me/machines")
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
                status = _worker_status(wid, now)
        machines.append({
            "id": row["token_hash"][:12],
            "name": _machine_display_name(row["label"], wid),
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
            "status": _worker_status(wid, now),
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


@app.patch("/me/machines/{machine_id}/schedule")
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
    token_hash = db.resolve_machine_token_hash(member.member_id, machine_id)
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
    return {"ok": True, **_schedule_payload(token_hash)}


@app.post("/me/workers/{worker_id}/forget")
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


@app.get("/me/contributor-status")
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


@app.post("/me/machines/{prefix}/unpair")
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
    # Lookup the full hash by scanning the caller's own tokens.
    # member_tokens for a single member is tiny (one row per paired
    # PC), so this is cheap.
    rows = db.list_member_tokens(member.member_id)
    target = next((r["token_hash"] for r in rows if r["token_hash"].startswith(prefix)), None)
    if target is None:
        # 404 leaks no info — the prefix just doesn't match anything
        # in the caller's scope, whether or not it exists elsewhere.
        raise HTTPException(status_code=404, detail="machine not found")
    deleted = db.delete_member_token(member.member_id, target)
    return {"deleted": deleted}


@app.get("/me/friends")
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


@app.post("/me/friends/{friend_member_id}/quota")
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


@app.post("/me/friends/{friend_member_id}/revoke")
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


def _login_fail_key(username_norm: str) -> str:
    # Lowercased to match the case-insensitive username lookup
    # (db.get_member_by_username uses LOWER(username)) — otherwise an
    # attacker could vary case to mint a fresh counter and bypass the
    # throttle.
    return f"login_fail:{username_norm}"


@app.post("/login")
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


def _email_verify_key(code: str) -> str:
    return f"email_verify:{code}"


def _start_email_verification(member_id: str, email: str, request: Request) -> bool:
    """Generate a single-use verification code, store it, and try to
    send the email. Returns True iff the member should be LEFT
    unverified pending that click (Resend is configured and accepted
    the send); False means the caller should auto-verify instead
    (Resend unconfigured, or the send attempt itself failed — an
    inbox that will never see the link is not a reason to lock
    someone out of an account they just created).

    ``PUBLIC_BASE_URL`` (module-level, set below in the agent-pairing
    section — same var, reused rather than re-imported from
    shared.config to avoid two names resolving two different ways)
    falls back to the live request's own host, same pattern as
    /agents/pair/start, so an unconfigured deploy still emails a
    working absolute link instead of a bare "/verify-email?..." path."""
    if not email_send.is_configured():
        return False
    code = secrets.token_urlsafe(32)
    key = _email_verify_key(code)
    r.set(key, member_id, ex=EMAIL_VERIFY_TTL_SECONDS)
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    verify_url = f"{base}/verify-email?code={code}"
    if not email_send.send_verification_email(email, verify_url):
        r.delete(key)
        return False
    return True


def _signup_throttle_key(client_ip: str) -> str:
    return f"signup_count:{client_ip}"


@app.post("/signup")
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

    client_ip = _client_ip(request)
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
        tos_version=TOS_VERSION,
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
        "tos_version": TOS_VERSION,
        "email_verified": not pending_verification,
    }


@app.get("/verify-email", response_class=HTMLResponse)
def verify_email(code: str = ""):
    """Public landing page for the link in the verification email
    (see _start_email_verification / POST /signup). Single-use — the
    redis key is deleted on the first successful hit, so a forwarded
    or reused link fails cleanly instead of quietly re-verifying."""
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


@app.post("/me/resend-verification")
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


@app.post("/me/password")
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
    worker_messages = _build_chat_messages(prior, user_msg["text"] or "")

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


# ---------- invites ----------
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
        tos_version=TOS_VERSION,
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
@app.get("/admin/debug/job/{job_id}")
def admin_debug_job(job_id: str, request: Request):
    """Admin-only: dump everything we know about a job's pipeline
    state, especially for search-rewrite debugging. Returns:

    - The DB job row (prompt, status, tool, result, etc.)
    - For a search job: any associated rewrite chat job — its own DB
      row + the parser decision (re-parsed from the stored output so
      we don't have to ship the dispatcher's runtime decision through
      another channel).
    - JOB_RESULTS payload (sources, search_was_skipped, image_path)
    - Any live pending-rewrite linkage (still in flight)
    - The Redis claim / processing state

    Built to make ANOTHER debug cycle for search regressions take
    minutes instead of the SSH-and-dump-SQLite dance that diagnosed
    the prior Kevin-O'Leary panic-recant bug."""
    member = getattr(request.state, "member", None)
    if AUTH_ENABLED and (member is None or member.role != "admin"):
        raise HTTPException(status_code=403, detail="admin only")

    job_row = db.get_job(job_id)
    if job_row is None:
        raise HTTPException(status_code=404, detail="job not found")

    def _row_to_dict(row):
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    job_dict = _row_to_dict(job_row)

    # JOB_RESULTS — the final Redis payload the polling client reads.
    results_raw = r.hget(JOB_RESULTS, job_id)
    results_payload = None
    if results_raw:
        try:
            results_payload = json.loads(results_raw)
        except json.JSONDecodeError:
            results_payload = {"_raw": results_raw}

    # Find the rewrite chat job linked to this search/image, if any.
    # The linkage hash stores rewrite_job_id → {search_job_id|image_
    # job_id, envelope, original}; we scan for an entry where the
    # target job_id matches. Small hash (a few entries at most), so
    # O(n) is fine.
    pending_rewrite_link = None
    for hash_key in (SEARCH_REWRITE_PENDING, IMAGE_REWRITE_PENDING):
        for rid, link_raw in (r.hgetall(hash_key) or {}).items():
            try:
                link = json.loads(link_raw)
            except json.JSONDecodeError:
                continue
            target_id = link.get("search_job_id") or link.get("image_job_id")
            if target_id == job_id:
                pending_rewrite_link = {
                    "kind": hash_key,
                    "rewrite_job_id": rid,
                    "original_prompt": link.get("original_prompt"),
                }
                break
        if pending_rewrite_link:
            break

    # Find the COMPLETED rewrite chat job that produced this search
    # job's current prompt. We don't store the linkage long-term
    # (it's deleted from SEARCH_REWRITE_PENDING after dispatch), so
    # this is a best-effort search through recent chat jobs that
    # have this job's conversation context. For now we just look up
    # the immediate prior rewrite-shaped chat job from the same
    # submission window.
    rewrite_job_dict = None
    rewrite_parsed = None
    if job_row["tool"] == "search":
        # Heuristic: a rewrite chat job submitted within 2s of this
        # search by the same submitter, with the meta-prompt
        # signature in its prompt field. Cheap and deterministic
        # for the recent-history case the user actually wants to
        # debug. We dip directly into the DB connection since this
        # query exists only for the debug surface; not worth a
        # promoted db.* method.
        rows = db._conn.execute(
            "SELECT * FROM jobs WHERE submitted_by_member_id IS ? "
            "AND tool = 'chat' AND submitted_at BETWEEN ? AND ? "
            "ORDER BY submitted_at DESC LIMIT 8",
            (
                job_row["submitted_by_member_id"],
                job_row["submitted_at"] - 2.0,
                job_row["submitted_at"] + 2.0,
            ),
        ).fetchall()
        for c in rows:
            prompt = c["prompt"] or ""
            if "routing classifier" in prompt:
                rewrite_job_dict = _row_to_dict(c)
                rewrite_parsed = _parse_search_rewrite_output(c["result"])
                break

    return {
        "job": job_dict,
        "job_results": results_payload,
        "pending_rewrite_link": pending_rewrite_link,
        "rewrite_job": rewrite_job_dict,
        "rewrite_parsed": (
            {"decision": rewrite_parsed[0], "value": rewrite_parsed[1]}
            if rewrite_parsed else None
        ),
    }


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


@app.post("/admin/test-email")
def admin_test_email(req: TestEmailRequest, request: Request):
    """Admin dashboard's "Email delivery test" card — sends a plain,
    unmistakably-a-test message (not the verification template) to a
    typed-in address so the admin can confirm Resend is actually
    delivering (DNS/domain verification, RESEND_API_KEY, etc.) without
    creating a throwaway signup just to trigger an email."""
    _require_admin(request)
    to = (req.to or "").strip()
    if not to or "@" not in to:
        raise HTTPException(status_code=400, detail="a valid email is required")
    if not email_send.is_configured():
        raise HTTPException(
            status_code=400,
            detail="RESEND_API_KEY is not configured on this coordinator",
        )
    ok, detail = email_send.send_test_email(to)
    if not ok:
        raise HTTPException(status_code=502, detail=f"send failed: {detail}")
    return {"sent": True, "to": to}


@app.get("/models")
def models():
    """Catalog of models the coordinator knows about. See coordinator/model_registry.py."""
    return {
        "strict": STRICT_MODELS,
        "models": [m.to_dict() for m in model_registry.list_all()],
    }


@app.get("/earnings")
def earnings(request: Request):
    _require_admin(request)
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
def earnings_for(worker_id: str, request: Request):
    _require_admin(request)
    row = db.earnings_for(worker_id)
    if row is None:
        return {"worker_id": worker_id, "total_tokens": 0, "total_usd": 0.0}
    return {
        "worker_id": row["worker_id"],
        "total_tokens": int(row["total_tokens"]),
        "total_usd": round(float(row["total_usd"]), 8),
    }


@app.get("/metrics")
def metrics(request: Request):
    _require_admin(request)
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


# ---------- agent pairing (browser handoff) ----------
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


@app.post("/agents/pair/start")
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


@app.get("/agents/pair/{code}")
def agent_pair_info(code: str):
    """Public read of a pair code's state. The web UI calls this from
    the /agent/pair page so a user landing on a stale link gets a clean
    404 rather than seeing a Confirm button that won't work.

    Reveals only state + expiry — never the token, even if the code is
    in 'approved' state."""
    raw = r.get(_pair_key(code))
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


@app.post("/agents/pair/confirm")
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


@app.post("/agents/pair/unpair")
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


@app.post("/agents/pair/poll")
def agent_pair_poll(req: AgentPairPollRequest):
    """Agent polls for the user's approval. Public — the agent has no
    token yet. Returns ``state="pending"`` until the user confirms;
    once approved, returns the fresh token exactly once and deletes the
    pair record so a second poll gets 404."""
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
