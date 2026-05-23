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

from shared.auth import auth_headers
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


# Minimum wall-clock between /jobs/partial pushes for real Ollama
# streams. Tight enough that tokens feel live, loose enough that we
# don't hammer the coordinator with one POST per token. The mock
# branch overrides this and flushes per character so a dev watching
# the UI sees a visible type-in effect even when the underlying
# response is tiny.
PARTIAL_FLUSH_INTERVAL = 0.25

# A fixed lorem-ipsum-flavored body the mock streams back regardless
# of the input prompt. Deliberately NOT an echo of the prompt — that
# was confusing in multi-turn dev tests where the prepended chat
# history looked like the model was leaking it back. This is long
# enough that a 200ms client poll captures several visible chunks.
_MOCK_RESPONSE = (
    "Streaming demo response: lorem ipsum dolor sit amet, consectetur "
    "adipiscing elit. Sed do eiusmod tempor incididunt ut labore et "
    "dolore magna aliqua. Ut enim ad minim veniam, quis nostrud "
    "exercitation ullamco laboris nisi ut aliquip ex ea commodo "
    "consequat. Duis aute irure dolor in reprehenderit in voluptate "
    "velit esse cillum dolore eu fugiat nulla pariatur."
)
# Per-character wall-clock during the mock stream. 2.5ms ⇒ the full
# response above takes about a second — fast enough that it doesn't
# stall the demo, slow enough that the client poll loop sees multiple
# intermediate states and renders a real type-in effect.
_MOCK_CHAR_DELAY = 0.0025


def run_inference(
    prompt: str,
    model: Optional[str],
    http: httpx.Client,
    on_partial=None,
    messages: Optional[list[dict]] = None,
) -> dict:
    """Generate a response, streaming intermediate text via ``on_partial``
    (called with the FULL accumulated text, not a delta). The final
    return is the same shape as before so /jobs/complete callers don't
    need to change.

    When ``messages`` is provided the call is routed to Ollama's
    ``/api/chat`` endpoint, so the model's own chat template is applied
    instead of feeding it a hand-rolled ``User:/Assistant:`` transcript.
    Single-shot jobs (canaries, /generate without a conversation) keep
    the legacy ``/api/generate`` path."""
    if MOCK_INFERENCE:
        # Push every character — no throttle. The mock is a UX demo,
        # not a load test, and flushing per char makes the streaming
        # animation visible regardless of how the client paces polls.
        text = ""
        for ch in _MOCK_RESPONSE:
            text += ch
            time.sleep(_MOCK_CHAR_DELAY)
            if on_partial:
                on_partial(text)
        return {
            "text": text,
            "prompt_tokens": estimate_tokens(prompt),
            "completion_tokens": estimate_tokens(text),
            "model": "mock",
        }
    use_model = model or MODEL
    text = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    last_flush = 0.0
    if messages:
        endpoint = f"{OLLAMA_URL}/api/chat"
        payload = {"model": use_model, "messages": messages, "stream": True}
    else:
        endpoint = f"{OLLAMA_URL}/api/generate"
        payload = {"model": use_model, "prompt": prompt, "stream": True}
    with http.stream("POST", endpoint, json=payload, timeout=600) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            # /api/chat streams {"message": {"role": "assistant",
            # "content": "..."}, "done": ...}; /api/generate streams
            # {"response": "...", "done": ...}. Read whichever shape
            # the current chunk carries.
            token = (
                (chunk.get("message") or {}).get("content", "")
                if messages
                else chunk.get("response", "")
            )
            if token:
                text += token
            if chunk.get("done"):
                prompt_tokens = chunk.get("prompt_eval_count")
                completion_tokens = chunk.get("eval_count")
                break
            now = time.time()
            if on_partial and (now - last_flush) >= PARTIAL_FLUSH_INTERVAL:
                on_partial(text)
                last_flush = now
    if on_partial:
        on_partial(text)
    return {
        "text": text,
        "prompt_tokens": int(prompt_tokens or estimate_tokens(prompt)),
        "completion_tokens": int(completion_tokens or estimate_tokens(text)),
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
    # Capture the claim_token the coordinator mints at /jobs/claim. We
    # thread it through partial/complete so the coordinator can reject
    # a stale completer (e.g. the reaper requeued and another worker
    # has since picked the job up). The in-VPS worker BLPOPs jobs
    # straight from Redis rather than going through /jobs/next, so
    # /jobs/claim is still its first network contact with the
    # coordinator for the job.
    claim_resp = post(
        http, "/jobs/claim",
        {"worker_id": WORKER_ID, "job_id": job_id},
    )
    claim_token: Optional[str] = (claim_resp or {}).get("claim_token")

    def push_partial(text: str) -> None:
        # Fire-and-forget: a dropped partial isn't worth retrying since
        # the next one carries the full text anyway, and the final
        # /jobs/complete is the source of truth.
        body = {"worker_id": WORKER_ID, "job_id": job_id, "text": text}
        if claim_token is not None:
            body["claim_token"] = claim_token
        post(http, "/jobs/partial", body, timeout=3)

    try:
        maybe_simulate_cold_start(last_job_finished)
        simulate_network_delay()
        result = run_inference(
            job["prompt"],
            job.get("model"),
            http,
            on_partial=push_partial,
            messages=job.get("messages"),
        )
        duration = round(time.time() - started, 3)
        complete_body = {
            "worker_id": WORKER_ID,
            "job_id": job_id,
            "text": result["text"],
            "model": result["model"],
            "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"],
            "duration_seconds": duration,
            "status": "complete",
        }
        if claim_token is not None:
            complete_body["claim_token"] = claim_token
        post(http, "/jobs/complete", complete_body, timeout=30)
        log.info(
            "complete tokens=%d duration=%.2fs",
            result["completion_tokens"],
            duration,
            extra={"event": "complete", "job_id": job_id},
        )
    except Exception as e:
        duration = round(time.time() - started, 3)
        log.exception("job failed: %s", e, extra={"event": "error", "job_id": job_id})
        complete_body = {
            "worker_id": WORKER_ID,
            "job_id": job_id,
            "text": "",
            "model": job.get("model") or MODEL,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "duration_seconds": duration,
            "status": "error",
            "error": str(e),
        }
        if claim_token is not None:
            complete_body["claim_token"] = claim_token
        post(http, "/jobs/complete", complete_body, timeout=30)
    return time.time()


def main() -> None:
    log.info(
        "starting model=%s mock=%s availability=%s",
        MODEL, MOCK_INFERENCE, AVAILABILITY_WINDOW,
        extra={"event": "starting"},
    )
    r = connect_redis()
    http = httpx.Client(headers=auth_headers())
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
