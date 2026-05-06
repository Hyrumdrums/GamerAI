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

# --- abuse / retry safety (all opt-in; 0 / unset disables) ---
IDEMPOTENCY_TTL_SECONDS = _int("IDEMPOTENCY_TTL_SECONDS", 86400)  # 24h
RATE_LIMIT_PER_MIN = _int("RATE_LIMIT_PER_MIN", 0)

# --- model registry (off by default = legacy "any model name accepted") ---
STRICT_MODELS = _bool("STRICT_MODELS", False)


# --- redis keys ---
JOB_QUEUE = "job_queue"
JOB_RESULTS = "job_results"
JOB_PROCESSING = "job_processing"
WORKER_REGISTRY = "worker_registry"
WORKER_HEARTBEATS = "worker_heartbeats"
WORKER_STATUS = "worker_status"
WORKER_EARNINGS = "worker_earnings"
WORKER_CAPABILITIES = "worker_capabilities"
