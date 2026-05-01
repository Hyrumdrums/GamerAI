import json
import logging
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from redis_client import (
    JOB_QUEUE,
    JOB_RESULTS,
    WORKER_EARNINGS,
    WORKER_HEARTBEATS,
    WORKER_REGISTRY,
    get_client,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [coordinator] %(levelname)s %(message)s",
)
log = logging.getLogger("coordinator")

app = FastAPI(title="GamerAI Coordinator", version="0.1.0")
r = get_client()

WORKER_TIMEOUT_SECONDS = 15


class GenerateRequest(BaseModel):
    prompt: str
    model: Optional[str] = None


class GenerateResponse(BaseModel):
    job_id: str


class RegisterRequest(BaseModel):
    worker_id: str


@app.get("/health")
def health():
    try:
        r.ping()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"redis unavailable: {e}")


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt required")

    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "prompt": req.prompt,
        "model": req.model,
        "submitted_at": time.time(),
    }
    r.rpush(JOB_QUEUE, json.dumps(job))
    log.info("queued job %s (queue depth=%d)", job_id, r.llen(JOB_QUEUE))
    return GenerateResponse(job_id=job_id)


@app.get("/result/{job_id}")
def result(job_id: str):
    raw = r.hget(JOB_RESULTS, job_id)
    if raw is None:
        return {"job_id": job_id, "status": "pending"}
    data = json.loads(raw)
    data.setdefault("status", "complete")
    return data


@app.post("/register")
def register(req: RegisterRequest):
    r.sadd(WORKER_REGISTRY, req.worker_id)
    r.hset(WORKER_HEARTBEATS, req.worker_id, time.time())
    log.info("registered worker %s", req.worker_id)
    return {"ok": True}


@app.post("/heartbeat")
def heartbeat(req: RegisterRequest):
    r.hset(WORKER_HEARTBEATS, req.worker_id, time.time())
    return {"ok": True}


def _active_workers() -> list[dict]:
    workers = r.smembers(WORKER_REGISTRY) or set()
    heartbeats = r.hgetall(WORKER_HEARTBEATS) or {}
    now = time.time()
    out = []
    for w in workers:
        last = float(heartbeats.get(w, 0) or 0)
        out.append(
            {
                "worker_id": w,
                "last_heartbeat": last,
                "alive": (now - last) < WORKER_TIMEOUT_SECONDS,
                "seconds_since_heartbeat": round(now - last, 2) if last else None,
            }
        )
    return out


@app.get("/workers")
def workers():
    return {"workers": _active_workers()}


@app.get("/earnings")
def earnings():
    raw = r.hgetall(WORKER_EARNINGS) or {}
    parsed = {}
    total = 0.0
    for worker_id, payload in raw.items():
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = {"earnings": float(payload), "jobs": 0, "tokens": 0}
        parsed[worker_id] = data
        total += float(data.get("earnings", 0.0))
    return {"workers": parsed, "total_earnings": round(total, 8)}
