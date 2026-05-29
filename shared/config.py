"""Centralized configuration. All env vars in one place."""
import os
from urllib.parse import urlparse


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --- redis ---
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


def redis_kwargs() -> dict:
    u = urlparse(REDIS_URL)
    return {
        "host": u.hostname or "redis",
        "port": u.port or 6379,
        "db": int((u.path or "/0").lstrip("/") or 0),
        "password": u.password,
    }


# --- coordinator ---
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://coordinator:8000")
DB_PATH = os.getenv("DB_PATH", "/data/gamerai.db")
JOB_TIMEOUT_SECONDS = _int("JOB_TIMEOUT_SECONDS", 120)
WORKER_TIMEOUT_SECONDS = _int("WORKER_TIMEOUT_SECONDS", 15)
REAPER_INTERVAL_SECONDS = _float("REAPER_INTERVAL_SECONDS", 5.0)
# If true, /generate (and retry) refuses to enqueue when no worker has
# heartbeated within WORKER_TIMEOUT_SECONDS. Prod sets this so users
# get an explicit "no community members available" message instead of
# their prompt sitting on the queue indefinitely. Off by default so
# tests and local-dev (which may run /generate before a worker has
# come online) keep working.
REQUIRE_LIVE_WORKER = _bool("REQUIRE_LIVE_WORKER", False)

# --- worker ---
WORKER_ID_OVERRIDE = os.getenv("WORKER_ID")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
MODEL = os.getenv("MODEL", "llama3.2:1b")
POLL_INTERVAL = _float("POLL_INTERVAL", 2.5)
HEARTBEAT_INTERVAL = _float("HEARTBEAT_INTERVAL", 5.0)
MOCK_INFERENCE = _bool("MOCK_INFERENCE", False)

# realism — simulated gamer machine
NETWORK_DELAY_MIN = _float("NETWORK_DELAY_MIN", 0.5)
NETWORK_DELAY_MAX = _float("NETWORK_DELAY_MAX", 3.0)
COLD_START_MIN = _float("COLD_START_MIN", 2.0)
COLD_START_MAX = _float("COLD_START_MAX", 8.0)
COLD_START_AFTER_IDLE = _float("COLD_START_AFTER_IDLE", 60.0)
AVAILABILITY_WINDOW = os.getenv("AVAILABILITY_WINDOW", "always")  # "always" | "HH-HH" UTC

# --- payouts ---
RATE_PER_TOKEN = _float("RATE_PER_TOKEN", 0.000005)
WORKER_SHARE = _float("WORKER_SHARE", 0.7)

# --- image accounting ---
# Cost of one "standard" image generation in image-unit terms. The
# coordinator credits this against the submitter's member_usage on every
# successful image job. A REAL column (member_usage.image_units) holds
# the running total so future config options (higher resolution, more
# steps, larger batches) can pre-multiply this base before crediting
# without a schema change. The per-member daily image quota is enforced
# in units, not raw image count, so a 4× cost image consumes 4 quota.
IMAGE_UNIT_COST_BASE = _float("IMAGE_UNIT_COST_BASE", 1.0)

# --- abuse / retry safety (all opt-in; 0 / unset disables) ---
IDEMPOTENCY_TTL_SECONDS = _int("IDEMPOTENCY_TTL_SECONDS", 86400)  # 24h
RATE_LIMIT_PER_MIN = _int("RATE_LIMIT_PER_MIN", 0)

# --- model registry (off by default = legacy "any model name accepted") ---
STRICT_MODELS = _bool("STRICT_MODELS", False)

# --- canaries (community-trust monitoring) ---
# Background task injects one canary every CANARY_INTERVAL_SECONDS, but
# only if there's at least one live worker AND at least one active
# canary seeded. Set to 0 to disable injection entirely.
CANARY_INTERVAL_SECONDS = _int("CANARY_INTERVAL_SECONDS", 600)  # 10 min
# Recent-window depth used to compute per-worker canary scores.
CANARY_SCORE_WINDOW = _int("CANARY_SCORE_WINDOW", 50)


# --- redis keys ---
# JOB_QUEUE is the legacy chat-only queue (kept as an alias for
# job_queue:chat so the worker container, which BLPOPs this exact
# name, doesn't need a config change to keep serving chat jobs).
JOB_QUEUE = "job_queue"
# Per-tool queue prefix. Image-capable workers pull from job_queue:image;
# chat-capable workers pull from job_queue (= job_queue:chat). The
# coordinator pushes to the right queue based on the request's tool
# field.
def job_queue_for(tool: str) -> str:
    """Return the Redis list key a job of *tool* should be queued on.
    Image jobs go to a dedicated queue so chat-only workers (the
    majority of the network) never pick one up and fail. Search jobs
    likewise get their own queue — a chat-only agent without the ddgs
    dependency installed would error out on tool=search. TTS jobs
    ride their own queue because (a) the agent's CPU TTS loop polls
    only this key, decoupled from the GPU chat/image loop so the two
    can run concurrently on the same worker (see voice-phase1 design),
    and (b) workers that haven't bootstrapped Piper must not accidentally
    BLPOP an audio job they can't fulfil."""
    if tool == "image":
        return "job_queue:image"
    if tool == "search":
        return "job_queue:search"
    if tool == "tts":
        return "job_queue:tts"
    return JOB_QUEUE
JOB_RESULTS = "job_results"
JOB_PROCESSING = "job_processing"
# Accumulated streaming text per in-flight job, keyed by job_id. Worker
# pushes the full running text on each /jobs/partial; the polling path
# reads from here while the job is mid-stream and falls back to
# JOB_RESULTS once it completes.
JOB_PARTIALS = "job_partials"

# Voice-mode chat: ordered list of synthesized audio chunks for an
# in-flight job. Stored as a Redis hash where field=seq (stringified
# int) and value=JSON {seq, audio_b64, audio_seconds}. Per-job keys
# (suffixed with :{job_id}) so cleanup is a single DELETE. The hash
# form lets us write idempotently if a chunk is retried, and reading
# is a single HGETALL on the polling read path.
JOB_AUDIO_CHUNKS = "job_audio_chunks"

# Tail-window cap on chat history shipped to the model. Prompt eval on
# a long context is the single biggest contributor to submit-to-first-
# token latency — small consumer models prefill at ~500-1500 tok/sec, so
# a 25K-token history costs 20-40s before the first generated token
# arrives. Newest turns are kept; older turns are dropped (or replaced
# by a summary once the summarizer fires). 4000 tokens ≈ 1000 words ≈
# 4-6 normal turns, generous for short-thread continuity without making
# prefill the dominant cost. Tunable via env if a contributor wants to
# trade latency for memory in a long thread.
import os as _os
MAX_HISTORY_TOKENS = int(_os.getenv("MAX_HISTORY_TOKENS", "4000"))
del _os

# Single source of truth for the PWA shell-cache version. The service
# worker's CACHE_VERSION and the chat-header version indicator both
# read this — bumping it here invalidates the cached JS for every
# browser on next page load AND surfaces the new number in the UI so
# operator and user can confirm at-a-glance which build is live.
# Format: bare integer string. Display widget renders "v1.0.{N}".
CLIENT_CACHE_VERSION = "18"
WORKER_REGISTRY = "worker_registry"
WORKER_HEARTBEATS = "worker_heartbeats"
WORKER_STATUS = "worker_status"
WORKER_EARNINGS = "worker_earnings"
WORKER_CAPABILITIES = "worker_capabilities"
# Redis hash mapping job_id -> canary_id for jobs currently in flight
# as canaries. Coordinator consults this on /jobs/complete to recognize
# canary results; workers never see this key.
CANARY_PENDING = "canary_pending"

# Redis hash mapping rewrite_chat_job_id -> JSON({image_job_id,
# image_envelope, original_prompt}). Set by /generate when an image
# job needs context-aware prompt rewriting (image submit inside a
# conversation that already has prior turns); consumed by
# /jobs/complete when the rewrite job lands — at which point the
# coordinator plugs the rewritten text into the stored image envelope
# and RPUSHes it onto the real image queue. The link entry is deleted
# after dispatch (or after a fallback-to-raw error path) so a stale
# rewrite_chat_job_id never leaks into the next request.
IMAGE_REWRITE_PENDING = "image_rewrite_pending"
# Same pattern as IMAGE_REWRITE_PENDING but for search jobs. The
# rewrite turns "try again" (a literal query that produces Aaliyah
# song results) into a context-aware query like "different recent
# news event" using the conversation history. Fires on follow-ups
# only (no rewrite on first-turn searches — same skip-paths as
# image rewrite). Consumed by /jobs/complete; deleted after dispatch.
SEARCH_REWRITE_PENDING = "search_rewrite_pending"
# Conversation-summary job linkage. Same pattern as the rewrite pending
# hashes: when /generate fires a "summarize the older turns" chat job,
# it stashes {conversation_id, through_seq} here keyed by the summary
# job's id. /jobs/complete looks the entry up and, on success, writes
# the result text to conversations.summary_text. Avoids threading a
# conversation_id back through the orphan job row.
SUMMARY_PENDING = "summary_pending"
# Reverse-detection marker. When the rewrite chat job classifies a
# follow-up as "not search-worthy" (a closure like "thanks" / "that's
# cool!" after a search thread), the dispatcher reroutes the job to
# the chat queue AND sets this flag so the eventual /jobs/complete
# can stamp `search_was_skipped: true` on the result. The client
# uses that flag to auto-uncheck the search box and reset the sticky
# state for the conversation. Cleaned up on /jobs/complete.
SEARCH_AUTO_DISABLED = "search_auto_disabled"
