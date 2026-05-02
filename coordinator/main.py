"""Coordinator: REST API + Redis queue + SQLite write-through + reaper."""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from coordinator.db import DB
from coordinator.redis_client import get_client
from coordinator.scheduler import Reaper
from shared.config import (
    JOB_PROCESSING,
    JOB_QUEUE,
    JOB_RESULTS,
    JOB_TIMEOUT_SECONDS,
    RATE_PER_TOKEN,
    WORKER_EARNINGS,
    WORKER_HEARTBEATS,
    WORKER_REGISTRY,
    WORKER_SHARE,
    WORKER_STATUS,
    WORKER_TIMEOUT_SECONDS,
)
from shared.models import (
    GenerateRequest,
    GenerateResponse,
    HeartbeatRequest,
    JobClaimRequest,
    JobCompleteRequest,
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
_reaper: Reaper | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reaper
    _reaper = Reaper(r, db)
    _reaper.start()
    log.info("coordinator ready", extra={"event": "startup"})
    try:
        yield
    finally:
        if _reaper:
            _reaper.stop()


app = FastAPI(title="GamerAI Coordinator", version="0.2.0", lifespan=lifespan)


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


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt required")

    job_id = str(uuid.uuid4())
    submitted_at = time.time()
    job = {
        "job_id": job_id,
        "prompt": req.prompt,
        "model": req.model,
        "submitted_at": submitted_at,
    }
    db.insert_job(job_id, req.prompt, req.model, submitted_at)
    r.rpush(JOB_QUEUE, json.dumps(job))
    log.info(
        "queued job",
        extra={"event": "job_queued", "job_id": job_id},
    )
    return GenerateResponse(job_id=job_id)


@app.get("/result/{job_id}")
def result(job_id: str):
    raw = r.hget(JOB_RESULTS, job_id)
    if raw:
        data = json.loads(raw)
        data.setdefault("status", "complete")
        return data
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "worker_id": row["worker_id"],
        "model": row["model"],
        "text": row["result"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "earnings": row["earnings"],
        "duration_seconds": row["duration_seconds"],
        "attempts": row["attempts"],
        "error": row["error"],
    }


# ---------- worker lifecycle ----------
@app.post("/register")
def register(req: WorkerIdent):
    now = time.time()
    r.sadd(WORKER_REGISTRY, req.worker_id)
    r.hset(WORKER_HEARTBEATS, req.worker_id, now)
    r.hset(WORKER_STATUS, req.worker_id, "idle")
    db.upsert_worker(req.worker_id, "idle", now)
    log.info(
        "worker registered",
        extra={"event": "worker_registered", "worker_id": req.worker_id},
    )
    return {"ok": True}


@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest):
    now = time.time()
    r.hset(WORKER_HEARTBEATS, req.worker_id, now)
    r.hset(WORKER_STATUS, req.worker_id, req.status)
    db.upsert_worker(req.worker_id, req.status, now)
    return {"ok": True}


@app.post("/jobs/next")
def next_job(req: WorkerIdent):
    """HTTP-only job pickup for remote agents (e.g. the Windows gamer install).
    Pops one job from the queue and returns it; the agent should immediately
    POST /jobs/claim with the returned job_id."""
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
def claim(req: JobClaimRequest):
    """Worker reports it has claimed a job. Coordinator records processing entry + DB row."""
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


@app.post("/jobs/complete")
def complete(req: JobCompleteRequest):
    """Worker submits result. Coordinator writes Redis result, earnings, SQLite row."""
    now = time.time()
    tokens = int(req.completion_tokens or 0)
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
    if req.status == "complete" and tokens > 0:
        db.add_earnings(req.worker_id, tokens, earnings)
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
            }
        )
    return {"workers": out}


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
