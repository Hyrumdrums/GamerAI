"""Canary prompts: integrity monitoring for the contributor network.

A canary is a normal-looking prompt with a known answer. The coordinator
periodically injects one into the job queue, indistinguishable to the
worker from a customer prompt. When the response comes back, we verify
it contains all the required tokens (case-insensitive substring match).
Repeated misses for a given worker_id indicate a substituted or
tampered-with model.

The worker never sees a canary marker — that mapping (job_id ->
canary_id) lives in Redis only on the coordinator side.
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
    CANARY_PENDING,
    JOB_QUEUE,
)

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
    ):
        super().__init__(name="canary-injector")
        self.r = redis_client
        self.db = db
        self.interval = float(interval)
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
        canaries = self.db.list_active_canaries()
        if not canaries:
            return
        canary = self._rng.choice(canaries)
        self._inject(canary)

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
        self.r.rpush(JOB_QUEUE, json.dumps(envelope))
        log.info(json.dumps({
            "event": "canary_injected",
            "job_id": job_id,
            "canary_id": canary["canary_id"],
        }))
