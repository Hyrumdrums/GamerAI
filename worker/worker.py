import json
import logging
import os
import socket
import time
import uuid
from typing import Optional

import httpx
import redis

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://coordinator:8000")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
MODEL = os.getenv("MODEL", "llama3.2:1b")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2.5"))
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "5"))
RATE_PER_TOKEN = float(os.getenv("RATE_PER_TOKEN", "0.000005"))
WORKER_SHARE = float(os.getenv("WORKER_SHARE", "0.7"))
MOCK_INFERENCE = os.getenv("MOCK_INFERENCE", "false").lower() == "true"

JOB_QUEUE = "job_queue"
JOB_RESULTS = "job_results"
WORKER_EARNINGS = "worker_earnings"

WORKER_ID = f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{WORKER_ID}] %(levelname)s %(message)s",
)
log = logging.getLogger("worker")


def connect_redis() -> redis.Redis:
    while True:
        try:
            client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            client.ping()
            return client
        except redis.RedisError as e:
            log.warning("redis not ready (%s) — retrying", e)
            time.sleep(2)


def register(http: httpx.Client) -> None:
    for attempt in range(20):
        try:
            http.post(f"{COORDINATOR_URL}/register", json={"worker_id": WORKER_ID}, timeout=5)
            log.info("registered with coordinator")
            return
        except httpx.HTTPError as e:
            log.warning("register attempt %d failed: %s", attempt + 1, e)
            time.sleep(2)
    log.error("could not register with coordinator; continuing anyway")


def heartbeat(http: httpx.Client) -> None:
    try:
        http.post(f"{COORDINATOR_URL}/heartbeat", json={"worker_id": WORKER_ID}, timeout=3)
    except httpx.HTTPError as e:
        log.debug("heartbeat failed: %s", e)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


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
    payload = {"model": use_model, "prompt": prompt, "stream": False}
    resp = http.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=600)
    resp.raise_for_status()
    data = resp.json()
    text = data.get("response", "")
    prompt_tokens = data.get("prompt_eval_count") or estimate_tokens(prompt)
    completion_tokens = data.get("eval_count") or estimate_tokens(text)
    return {
        "text": text,
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "model": use_model,
    }


def update_earnings(r: redis.Redis, tokens: int, earnings: float) -> None:
    raw = r.hget(WORKER_EARNINGS, WORKER_ID)
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"earnings": 0.0, "jobs": 0, "tokens": 0}
    else:
        data = {"earnings": 0.0, "jobs": 0, "tokens": 0}
    data["earnings"] = round(float(data.get("earnings", 0.0)) + earnings, 10)
    data["jobs"] = int(data.get("jobs", 0)) + 1
    data["tokens"] = int(data.get("tokens", 0)) + tokens
    data["worker_id"] = WORKER_ID
    data["updated_at"] = time.time()
    r.hset(WORKER_EARNINGS, WORKER_ID, json.dumps(data))


def process_job(r: redis.Redis, http: httpx.Client, job: dict) -> None:
    job_id = job["job_id"]
    prompt = job["prompt"]
    model = job.get("model")
    started = time.time()
    log.info("claimed job %s", job_id)

    try:
        result = run_inference(prompt, model, http)
        tokens = int(result["completion_tokens"])
        earnings = tokens * RATE_PER_TOKEN * WORKER_SHARE
        payload = {
            "job_id": job_id,
            "status": "complete",
            "worker_id": WORKER_ID,
            "model": result["model"],
            "text": result["text"],
            "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": tokens,
            "earnings": round(earnings, 10),
            "duration_seconds": round(time.time() - started, 3),
        }
        r.hset(JOB_RESULTS, job_id, json.dumps(payload))
        update_earnings(r, tokens, earnings)
        log.info(
            "completed %s — tokens=%d earnings=%.8f duration=%.2fs",
            job_id, tokens, earnings, payload["duration_seconds"],
        )
    except Exception as e:
        log.exception("job %s failed: %s", job_id, e)
        r.hset(
            JOB_RESULTS,
            job_id,
            json.dumps(
                {
                    "job_id": job_id,
                    "status": "error",
                    "worker_id": WORKER_ID,
                    "error": str(e),
                }
            ),
        )


def main() -> None:
    log.info("starting (model=%s mock=%s)", MODEL, MOCK_INFERENCE)
    r = connect_redis()
    http = httpx.Client()
    register(http)
    last_heartbeat = 0.0

    while True:
        now = time.time()
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            heartbeat(http)
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
        process_job(r, http, job)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("shutting down")
