"""Minimal in-process event bus.

KISS on purpose: a dict of event name -> subscriber callables, called
synchronously and inline with the emitting request. That's fine here
because every subscriber (see admin_alerts.py) is itself best-effort
and fast (a single Resend POST with a short timeout) — there's no
queue, no retry, no cross-process delivery. If a subscriber ever needs
to survive coordinator restarts or fan out across processes, that's a
real queue (Redis stream, etc.), not a bigger version of this file.

The coordinator runs as a single uvicorn process (see Dockerfile — no
--workers flag), so a plain in-memory dict is safe: there's only ever
one process's subscriber list to worry about.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

log = logging.getLogger("gamerai.events")

Handler = Callable[[dict], None]

_subscribers: dict[str, list[Handler]] = defaultdict(list)


def subscribe(event: str, handler: Handler) -> None:
    _subscribers[event].append(handler)


def emit(event: str, **data) -> None:
    """Fire ``event`` to every subscriber with ``data`` as the payload.
    A handler that raises is logged and skipped — one broken
    subscriber must never break the request that triggered the
    event (e.g. signup failing because an email alert threw)."""
    for handler in _subscribers.get(event, ()):
        try:
            handler(data)
        except Exception:
            log.exception("event handler failed", extra={"event": event})
