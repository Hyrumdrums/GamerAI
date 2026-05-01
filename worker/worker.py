"""Worker: pulls jobs, simulates a gamer machine, posts results to coordinator."""
import datetime as dt
import json
import logging
import random
import socket
import time
import uuid
from typing import Optional

import httpx
import redis

from shared.config import (
    AVAILABILITY_WINDOW,
    COLD_START_AFTER_IDLE,
    COLD_START_MAX,
    COLD_START_MIN,
    COORDINATOR_URL,
    HEARTBEAT_INTERVAL,
    JOB_QUEUE,
    MOCK_INFERENCE,
    MODEL,
    NETWORK_DELAY_MAX,
    NETWORK_DELAY_MIN,
    OLLAMA_URL,
    POLL_INTERVAL,
    WORKER_ID_OVERRIDE,
    redis_kwargs,
)

WORKER_ID = WORKER_ID_OVERRIDE or f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"


# ---------- structured logging ----------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": "worker",
            "worker_id": WORKER_ID,
            "message": record.getMessage(),
        }
        for k in ("job_id", "event"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        return json.dumps(payload)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("worker")


# ---------- availability ----------
def _parse_window(spec: str) -> Optional[tuple[int, int]]:
    if not spec or spec.lower() in ("always", "any", ""):
        return None
    try:
        a, b = spec.split("-")
        return int(a) % 24, int(b) % 24
    except (ValueError, TypeError):
        return None


_WINDOW = _parse_window(AVAILABILITY_WINDOW)


def is_available(now: Optional[dt.datetime] = None) -> bool:
    if _WINDOW is None:
        return True
    start, end = _WINDOW
    h = (now or dt.datetime.utcnow()).hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end  # wraps midnight


# ---------- redis ----------
def connect_redis() -> redis.Redis:
    while True:
        try:
            client = redis.Redis(decode_responses=True, **redis_kwargs())
            client.ping()
            return client
        except redis.RedisError as e:
            log.warning("redis not ready: %s", e, extra={"event": "redis_retry"})
            time.sleep(2)


# ---------- coordinator HTTP ----------
def post(http: httpx.Client, path: str, body: dict, timeout: float = 5.0) -> Optional[dict]:
    try:
        resp = http.post(f"{COORDINATOR_URL}{path}", json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        log.warning("coordinator %s failed: %s", path, e)
        return None


def register(http: httpx.Client) -> None:
    for attempt in range(20):
        if post(http, "/register", {"worker_id": WORKER_ID}) is not None:
            log.info("registered", extra={"event": "registered"})
            return
        time.sleep(2)
    log.error("failed to register; continuing")


def heartbeat(http: httpx.Client, status: str) -> None:
    post(http, "/heartbeat", {"worker_id": WORKER_ID, "status": status}, timeout=3)


# ---------- inference ----------
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def run_inference(prompt: str, model: Optional[str], http: httpx.Client) -> dict:
    if MOCK_INFERENCE:
        time.sleep(0.5)
        text = f"[mock-{WORKER_ID}] echo: {prompt[:200]}"
        return {
            "text": text,
            "prompt_tokens": estimate_tokens(prompt),
            "completion_tokens": estimate_tokens(text),
            "model": "mock",
        }
    use_model = model or MODEL
    resp = http.post(
        f"{OLLAMA_URL}/api/generate",
        json={"model": use_model, "prompt": prompt, "stream": False},
        timeout=600,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("response", "")
    return {
        "text": text,
        "prompt_tokens": int(data.get("prompt_eval_count") or estimate_tokens(prompt)),
        "completion_tokens": int(data.get("eval_count") or estimate_tokens(text)),
        "model": use_model,
    }


# ---------- gamer-machine realism ----------
def simulate_network_delay() -> None:
    delay = random.uniform(NETWORK_DELAY_MIN, NETWORK_DELAY_MAX)
    if delay > 0:
        time.sleep(delay)


def maybe_simulate_cold_start(last_job_finished: Optional[float]) -> None:
    if last_job_finished is None or (time.time() - last_job_finished) > COLD_START_AFTER_IDLE:
        delay = random.uniform(COLD_START_MIN, COLD_START_MAX)
        log.info("cold start %.2fs", delay, extra={"event": "cold_start"})
        time.sleep(delay)


# ---------- main loop ----------
def process_job(r: redis.Redis, http: httpx.Client, job: dict, last_job_finished: Optional[float]) -> float:
    job_id = job["job_id"]
    started = time.time()
    log.info("claimed", extra={"event": "claimed", "job_id": job_id})
    post(http, "/jobs/claim", {"worker_id": WORKER_ID, "job_id": job_id})

    try:
        maybe_simulate_cold_start(last_job_finished)
        simulate_network_delay()
        result = run_inference(job["prompt"], job.get("model"), http)
        duration = round(time.time() - started, 3)
        post(
            http,
            "/jobs/complete",
            {
                "worker_id": WORKER_ID,
                "job_id": job_id,
                "text": result["text"],
                "model": result["model"],
                "prompt_tokens": result["prompt_tokens"],
                "completion_tokens": result["completion_tokens"],
                "duration_seconds": duration,
                "status": "complete",
            },
            timeout=30,
        )
        log.info(
            "complete tokens=%d duration=%.2fs",
            result["completion_tokens"],
            duration,
            extra={"event": "complete", "job_id": job_id},
        )
    except Exception as e:
        duration = round(time.time() - started, 3)
        log.exception("job failed: %s", e, extra={"event": "error", "job_id": job_id})
        post(
            http,
            "/jobs/complete",
            {
                "worker_id": WORKER_ID,
                "job_id": job_id,
                "text": "",
                "model": job.get("model") or MODEL,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "duration_seconds": duration,
                "status": "error",
                "error": str(e),
            },
            timeout=30,
        )
    return time.time()


def main() -> None:
    log.info(
        "starting model=%s mock=%s availability=%s",
        MODEL, MOCK_INFERENCE, AVAILABILITY_WINDOW,
        extra={"event": "starting"},
    )
    r = connect_redis()
    http = httpx.Client()
    register(http)

    last_heartbeat = 0.0
    last_job_finished: Optional[float] = None

    while True:
        now = time.time()

        if not is_available():
            if now - last_heartbeat > HEARTBEAT_INTERVAL:
                heartbeat(http, "offline")
                last_heartbeat = now
            time.sleep(min(POLL_INTERVAL * 2, 10))
            continue

        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            heartbeat(http, "idle")
            last_heartbeat = now

        item = r.blpop(JOB_QUEUE, timeout=int(POLL_INTERVAL))
        if not item:
            continue

        _, raw = item
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("dropping malformed job payload")
            continue

        heartbeat(http, "busy")
        last_job_finished = process_job(r, http, job, last_job_finished)
        heartbeat(http, "idle")
        last_heartbeat = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("shutdown")
