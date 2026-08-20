"""Shared "is a live worker actually serving this tool" check.

Used by both the /generate live-worker gate (coordinator/main.py) and
the canary injector (coordinator/canaries.py). Keeping this in one
place matters: before this module existed, canaries.py had no
liveness awareness at all and kept injecting probes for tool="chat"
into an unconsumed queue for ~68 days while only chat:smart/tts
workers were online — a two-copy version of this check could drift
the same way again.
"""
from __future__ import annotations

import json
import time

from shared.config import WORKER_CAPABILITIES, WORKER_HEARTBEATS, WORKER_TIMEOUT_SECONDS


def _heartbeat_ts(r, worker_id: str) -> float:
    raw = r.hget(WORKER_HEARTBEATS, worker_id)
    if not raw:
        return 0.0
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return float(data.get("ts", 0) or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


def worker_advertises_tool(r, worker_id: str, tool: str) -> bool:
    """Missing/legacy capabilities default to chat-only. An explicit
    empty tools list is honored as "serves nothing" (a smart-pipeline
    backend lending its GPU via rpc-server)."""
    raw = r.hget(WORKER_CAPABILITIES, worker_id)
    if not raw:
        return tool == "chat"
    try:
        caps = json.loads(raw)
    except json.JSONDecodeError:
        return tool == "chat"
    tools = caps.get("tools")
    if tools is None:
        tools = ["chat"]
    return tool in tools


def has_live_worker_for_tool(r, tool: str) -> bool:
    """True if any worker that heartbeated within WORKER_TIMEOUT_SECONDS
    advertises ``tool``."""
    now = time.time()
    heartbeats = r.hgetall(WORKER_HEARTBEATS) or {}
    for worker_id in heartbeats:
        ts = _heartbeat_ts(r, worker_id)
        if not ts or (now - ts) > WORKER_TIMEOUT_SECONDS:
            continue
        if worker_advertises_tool(r, worker_id, tool):
            return True
    return False
