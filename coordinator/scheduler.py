"""Background reaper: requeue jobs whose claim deadline has expired —
unless the claimant is still heartbeating with the matching job_id, in
which case the deadline gets extended.

The extension semantics mean a healthy worker doing long inference
(image generation, big chat) never has its job yanked out from under
it. The reaper only requeues when the worker has actually gone silent
(no heartbeat within WORKER_TIMEOUT_SECONDS) or has clearly moved on
(heartbeat carries a different job_id, e.g. after a worker restart
mid-job)."""
import json
import logging
import threading
import time
from typing import Optional

from shared.config import (
    JOB_PARTIALS,
    JOB_PROCESSING,
    JOB_QUEUE,
    JOB_TIMEOUT_SECONDS,
    REAPER_INTERVAL_SECONDS,
    WORKER_HEARTBEATS,
    WORKER_TIMEOUT_SECONDS,
)
from shared.config import job_queue_for

log = logging.getLogger("coordinator.scheduler")


def _read_heartbeat(redis_client, worker_id: str) -> tuple[float, Optional[str]]:
    """Mirror of coordinator.main._read_heartbeat — duplicated here so
    the reaper module doesn't have to import the FastAPI app. Returns
    ``(ts, job_id)`` and tolerates the legacy bare-float shape that
    upgrade-window heartbeats may have left behind."""
    raw = redis_client.hget(WORKER_HEARTBEATS, worker_id)
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


class Reaper(threading.Thread):
    daemon = True

    def __init__(self, redis_client, db, interval: float = REAPER_INTERVAL_SECONDS):
        super().__init__(name="reaper")
        self.r = redis_client
        self.db = db
        self.interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info(json.dumps({"event": "reaper_started", "interval_s": self.interval}))
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # never let the thread die
                log.exception("reaper tick failed: %s", e)
            self._stop.wait(self.interval)

    def _tick(self) -> None:
        now = time.time()
        entries = self.r.hgetall(JOB_PROCESSING) or {}
        for job_id, raw in entries.items():
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                self.r.hdel(JOB_PROCESSING, job_id)
                continue
            deadline = float(meta.get("deadline", 0))
            if not (deadline and deadline < now):
                continue
            worker_id = meta.get("worker_id")
            hb_ts, hb_job_id = _read_heartbeat(self.r, worker_id) if worker_id else (0.0, None)
            worker_fresh = hb_ts and (now - hb_ts) <= WORKER_TIMEOUT_SECONDS
            on_matching_job = hb_job_id == job_id
            if worker_fresh and on_matching_job:
                # Healthy worker, still on this job — extend the
                # deadline rather than reaping. Indefinite extension
                # is intentional: the user-side cancel button is what
                # bounds total wait now, not the reaper.
                self._extend(job_id, meta, now)
                continue
            self._requeue(job_id, meta)

    def _extend(self, job_id: str, meta: dict, now: float) -> None:
        meta["deadline"] = now + JOB_TIMEOUT_SECONDS
        self.r.hset(JOB_PROCESSING, job_id, json.dumps(meta))
        log.info(
            json.dumps(
                {
                    "event": "job_deadline_extended",
                    "job_id": job_id,
                    "worker_id": meta.get("worker_id"),
                    "new_deadline": meta["deadline"],
                }
            )
        )

    def _requeue(self, job_id: str, meta: dict) -> None:
        original = meta.get("job")
        if not original:
            self.r.hdel(JOB_PROCESSING, job_id)
            return
        log.warning(
            json.dumps(
                {
                    "event": "job_timeout_requeued",
                    "job_id": job_id,
                    "stale_worker": meta.get("worker_id"),
                }
            )
        )
        # Route to the original routing queue — an image job must land
        # on job_queue:image so chat-only workers don't pick it up and
        # error, and a smart-mode chat job (envelope carries
        # route="chat:smart") must go back to the pipeline head, not a
        # standard 3B chat worker.
        if isinstance(original, dict):
            tool = original.get("route") or original.get("tool") or "chat"
        else:
            tool = "chat"
        try:
            queue = job_queue_for(tool)
        except Exception:
            queue = JOB_QUEUE
        self.r.rpush(queue, json.dumps(original))
        self.r.hdel(JOB_PROCESSING, job_id)
        # Drop any partial text from the dead worker so the retry
        # worker's fresh output replaces it cleanly.
        self.r.hdel(JOB_PARTIALS, job_id)
        self.db.requeue_job(job_id)
