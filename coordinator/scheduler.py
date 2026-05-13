"""Background reaper: requeue jobs whose claim deadline has expired."""
import json
import logging
import threading
import time

from shared.config import (
    JOB_PARTIALS,
    JOB_PROCESSING,
    JOB_QUEUE,
    REAPER_INTERVAL_SECONDS,
)

log = logging.getLogger("coordinator.scheduler")


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
            if deadline and deadline < now:
                self._requeue(job_id, meta)

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
        self.r.rpush(JOB_QUEUE, json.dumps(original))
        self.r.hdel(JOB_PROCESSING, job_id)
        # Drop any partial text from the dead worker so the retry
        # worker's fresh output replaces it cleanly.
        self.r.hdel(JOB_PARTIALS, job_id)
        self.db.requeue_job(job_id)
