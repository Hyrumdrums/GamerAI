"""Canary prompts: integrity monitoring for the contributor network.

A canary is a normal-looking prompt with a known answer. The coordinator
periodically injects one into the job queue, indistinguishable to the
worker from a customer prompt. When the response comes back, we verify
it contains all the required tokens (case-insensitive substring match).
Repeated misses for a given worker_id indicate a substituted or
tampered-with model.

The worker never sees a canary marker — that mapping (job_id ->
canary_id) lives in Redis only on the coordinator side.

Injection is gated on real traffic (CANARY_MIN_REAL_JOBS): on an idle
network a canary would be the *only* job a worker sees, trivially
identifiable and wasteful of a canary slot besides. /generate INCRs
CANARY_REAL_JOBS_SINCE for each customer-facing job; this module reads
it, and only fires once real traffic has crossed the threshold.

Injection is also gated on tool liveness (shared/worker_liveness.py):
the traffic counter above is tool-agnostic, so real chat:smart/image/
search jobs alone can satisfy it while zero workers actually serve
plain "chat" — the tool every canary envelope targets. Without this
second gate, canaries queue up behind nothing and never get consumed.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import uuid

from shared.config import (
    CANARY_INTERVAL_SECONDS,
    CANARY_MIN_REAL_JOBS,
    CANARY_PENDING,
    CANARY_REAL_JOBS_SINCE,
    job_queue_for,
)
from shared.worker_liveness import has_live_worker_for_tool

log = logging.getLogger("coordinator.canaries")


def _required_tokens(canary_row) -> list[str]:
    try:
        return [str(t).lower() for t in json.loads(canary_row["required_tokens"])]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def verify_response(canary_row, response_text: str) -> bool:
    """Case-insensitive substring match: every required token must
    appear at least once. The worker's exact phrasing doesn't matter —
    only that they produced a response that *contains* the expected
    canonical tokens. Picking canaries with stable factual answers
    keeps this robust to sampling variance."""
    if not response_text:
        return False
    haystack = response_text.lower()
    for needle in _required_tokens(canary_row):
        if needle and needle not in haystack:
            return False
    return True


class CanaryInjector(threading.Thread):
    """Periodically picks an active canary and pushes it onto the job
    queue as if a customer had submitted it. The job_id -> canary_id
    mapping is recorded in Redis (``CANARY_PENDING``) so the coordinator
    can recognize the response when it comes back via /jobs/complete.
    """
    daemon = True

    def __init__(
        self,
        redis_client,
        db,
        interval: float = CANARY_INTERVAL_SECONDS,
        min_real_jobs: int = CANARY_MIN_REAL_JOBS,
    ):
        super().__init__(name="canary-injector")
        self.r = redis_client
        self.db = db
        self.interval = float(interval)
        self.min_real_jobs = int(min_real_jobs)
        self._stop = threading.Event()
        self._rng = random.Random()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if self.interval <= 0:
            log.info(json.dumps({"event": "canary_injector_disabled"}))
            return
        log.info(json.dumps({
            "event": "canary_injector_started",
            "interval_s": self.interval,
        }))
        # Stagger the first injection so deploys don't all fire at second 0.
        self._stop.wait(min(self.interval, 30.0) * self._rng.random())
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log.exception("canary tick failed: %s", e)
            self._stop.wait(self.interval)

    def _tick(self) -> None:
        # Skip the tick entirely on an idle network — no point spending
        # a canary slot when there's been no real traffic to hide it in
        # (or no workers around to have submitted any). Doesn't reset
        # the counter: a quiet stretch just lets real jobs accumulate
        # toward the threshold for the next tick.
        if self.min_real_jobs > 0:
            real_jobs = int(self.r.get(CANARY_REAL_JOBS_SINCE) or 0)
            if real_jobs < self.min_real_jobs:
                return
        # Canary envelopes are pinned to tool="chat" (see _inject). Real
        # traffic on ANY tool satisfies the gate above, so without this
        # check a chat:smart-only network (a live worker, just not one
        # that serves plain "chat") looks identical to a busy, healthy
        # one — and canaries keep piling into a queue nobody is
        # draining. This is exactly what produced a ~68-day, ~9,900-job
        # backlog in prod (2026-06-13 to 2026-08-20) once the only
        # online workers advertised chat:smart/tts instead of chat.
        if not has_live_worker_for_tool(self.r, "chat"):
            return
        canaries = self.db.list_active_canaries()
        if not canaries:
            return
        canary = self._rng.choice(canaries)
        self._inject(canary)
        self.r.set(CANARY_REAL_JOBS_SINCE, 0)

    def _inject(self, canary) -> None:
        job_id = str(uuid.uuid4())
        # The envelope shape MUST match what /generate pushes for real
        # prompts — same keys, same types. If a canary envelope has a
        # different shape from a real one (e.g. missing submitted_at),
        # a malicious worker can fingerprint canaries and selectively
        # cheat. Keep these aligned with the /generate path.
        now = time.time()
        # Canary envelopes pin tool="chat" — the canary system targets
        # chat workers (image-canary support would need a known-good
        # PNG fingerprint per prompt; not yet built). Including the
        # field keeps real and canary envelopes shape-identical so a
        # malicious worker can't fingerprint canaries by absence.
        envelope = {
            "job_id": job_id,
            "prompt": canary["prompt"],
            "model": canary["model"],
            "submitted_at": now,
            "tool": "chat",
        }
        # Record canary mapping FIRST, then push to queue. If push fails
        # we leave a dangling entry (harmless — it just never gets
        # verified). If we pushed first and the mapping write failed,
        # we'd treat a real worker's honest response as an
        # un-verifiable canary.
        self.r.hset(CANARY_PENDING, job_id, canary["canary_id"])
        # Submitted_by NULL → no real customer attribution.
        self.db.insert_job(
            job_id=job_id,
            prompt=canary["prompt"],
            model=canary["model"],
            submitted_at=now,
            submitted_by_member_id=None,
        )
        # Route through job_queue_for("chat") instead of the bare
        # JOB_QUEUE constant. They resolve to the same Redis key today
        # (JOB_QUEUE *is* the legacy chat alias), but going through the
        # helper means a future rename of the alias won't silently
        # strand canaries on an unconsumed queue.
        self.r.rpush(job_queue_for("chat"), json.dumps(envelope))
        log.info(json.dumps({
            "event": "canary_injected",
            "job_id": job_id,
            "canary_id": canary["canary_id"],
        }))
