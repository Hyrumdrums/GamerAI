"""Per-machine uptime-schedule window math.

A machine (one ``member_tokens`` row) may declare a daily window during
which it's allowed to claim jobs, in its own IANA timezone. This module
is the pure, DB-free core: given the stored schedule fields and the
current instant, decide whether the machine is allowed to work right
now and, when it isn't, when the window next opens.

Window is stored as minutes-from-local-midnight. ``start == end`` is
treated as "all day" (a zero-width blackout is never what an operator
means). ``start < end`` is a same-day window; ``start > end`` is an
overnight wrap (e.g. 1320..360 = 22:00-06:00).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("coordinator.schedule")

MINUTES_PER_DAY = 24 * 60


def _local_now(tz_name: Optional[str], now: Optional[datetime]) -> Optional[datetime]:
    """Current wall-clock time in ``tz_name`` (DST-correct), or None when
    the tz can't be resolved — callers fail open (treat as allowed) so a
    bad tz never strands a machine offline. PATCH validates tz on the way
    in, so this is a defensive fallback, not a routine path."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        return now.astimezone(ZoneInfo(tz_name)) if tz_name else now
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("uptime schedule: unresolvable timezone %r — failing open", tz_name)
        return None


def valid_tz(tz_name: str) -> bool:
    """Whether ``tz_name`` resolves to a real IANA zone. Used by the
    PATCH endpoint so a bad tz is rejected at write time rather than
    silently failing open later."""
    try:
        ZoneInfo(tz_name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False


def in_window(cur_min: int, start_min: int, end_min: int) -> bool:
    """Is ``cur_min`` (minutes from midnight) inside [start, end)?"""
    start_min %= MINUTES_PER_DAY
    end_min %= MINUTES_PER_DAY
    if start_min == end_min:
        return True  # all-day
    if start_min < end_min:
        return start_min <= cur_min < end_min
    # Overnight wrap: allowed after start OR before end.
    return cur_min >= start_min or cur_min < end_min


def allowed_now(
    *,
    paused: bool,
    sched_enabled: bool,
    start_min: Optional[int],
    end_min: Optional[int],
    tz_name: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    """Whether a machine with this schedule may claim jobs right now.

    paused short-circuits everything (hard manual stop). With no enabled
    window (or an incomplete one), the machine is always allowed."""
    if paused:
        return False
    if not sched_enabled or start_min is None or end_min is None:
        return True
    local = _local_now(tz_name, now)
    if local is None:
        return True  # fail open on bad tz
    cur_min = local.hour * 60 + local.minute
    return in_window(cur_min, int(start_min), int(end_min))


def next_open_local(
    *,
    start_min: Optional[int],
    end_min: Optional[int],
    tz_name: Optional[str],
    now: Optional[datetime] = None,
) -> Optional[str]:
    """When the window next opens, as a local "HH:MM" string, for UI copy
    ("Sleeping until 01:00"). None when there's no meaningful next-open
    (no window, or already inside it)."""
    if start_min is None or end_min is None:
        return None
    local = _local_now(tz_name, now)
    if local is None:
        return None
    cur_min = local.hour * 60 + local.minute
    if in_window(cur_min, int(start_min), int(end_min)):
        return None
    start = int(start_min) % MINUTES_PER_DAY
    return f"{start // 60:02d}:{start % 60:02d}"
