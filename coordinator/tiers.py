"""Tier → daily allowance + uptime requirements.

Single source of truth for what BRONZE / SILVER / GOLD / PLATINUM each
get per day AND what each tier expects in contribution. Consumed by:

- ``/me`` — returns the inviting member's ``tier_quota`` so the account
  page can render "% of your daily allowance" tips on the invite form
  without a per-keystroke server round-trip.
- The invite form — uses ``tier_quota`` as the denominator for the
  helper text under the daily-cap inputs.
- ``/me/contributor-status`` — surfaces requirements vs. observed
  uptime so the host knows why they're at the tier they're at.
- The daily tier engine (coordinator.tier_engine) — promotes when a
  member meets the *next* tier's bar; demotes (after a 14-day grace)
  when below the *current* tier's bar. One step per day, BRONZE
  floor, PLATINUM ceiling.

PLATINUM is capped (not unlimited) so future API-key access can't
turn an unlimited account into an exfiltration vector. The numbers
are starting defaults — tune via PR; no env override is supported
because tier policy is a network-wide invariant, not a per-deployment
knob.
"""
from typing import Optional, TypedDict


class TierQuota(TypedDict):
    tokens: Optional[int]
    images: Optional[int]


class TierRequirement(TypedDict):
    min_hours_per_day: float
    min_days_per_week: int


TIER_QUOTAS: dict[str, TierQuota] = {
    "BRONZE":   {"tokens":    100_000, "images":   20},
    "SILVER":   {"tokens":    500_000, "images":  100},
    "GOLD":     {"tokens":  2_000_000, "images":  500},
    "PLATINUM": {"tokens": 10_000_000, "images": 2500},
}

# What each tier asks of its contributors. Numbers picked for an
# uptime-first promotion ladder:
#
# - BRONZE 2h × 4d (~32h/week) — light commitment, anyone with a PC
#   that's online after work most days.
# - SILVER 6h × 5d (~120h/week) — dedicated after-work contributor
#   or work-from-home machine left running.
# - GOLD 12h × 6d (~288h/week) — mostly-on contributor; the machine
#   is dedicated to GamerAI for at least half its waking life.
# - PLATINUM 20h × 7d (~588h/week) — always-on. Background
#   contribution on a machine that lives for this.
#
# At 50 tok/s effective throughput these produce 1×–3.6× the tier's
# cap in coverage. Coverage is the goal; outproduction is bonus.
TIER_REQUIREMENTS: dict[str, TierRequirement] = {
    "BRONZE":   {"min_hours_per_day":  2.0, "min_days_per_week": 4},
    "SILVER":   {"min_hours_per_day":  6.0, "min_days_per_week": 5},
    "GOLD":     {"min_hours_per_day": 12.0, "min_days_per_week": 6},
    "PLATINUM": {"min_hours_per_day": 20.0, "min_days_per_week": 7},
}

# Ordered ladder. Index = rank. Used by the tier engine to step up or
# down by exactly one position per evaluation.
TIER_LADDER: list[str] = ["BRONZE", "SILVER", "GOLD", "PLATINUM"]


def quota_for(tier: str) -> TierQuota:
    """Return the ``{tokens, images}`` allowance for a tier, defaulting
    to BRONZE for an unknown tier string. Defensive fallback so a typo
    or a legacy row that somehow holds a stale tier name doesn't 500
    the /me handler."""
    return TIER_QUOTAS.get(tier, TIER_QUOTAS["BRONZE"])


def requirements_for(tier: str) -> TierRequirement:
    """Return uptime requirements for a tier. Same defensive fallback
    as ``quota_for``."""
    return TIER_REQUIREMENTS.get(tier, TIER_REQUIREMENTS["BRONZE"])


def tier_rank(tier: str) -> int:
    """Position of ``tier`` in the promotion ladder. Unknown tiers map
    to 0 (BRONZE rank) so a corrupt row doesn't crash the engine."""
    try:
        return TIER_LADDER.index(tier)
    except ValueError:
        return 0


def tier_above(tier: str) -> Optional[str]:
    """The next tier up, or None if already at PLATINUM."""
    rank = tier_rank(tier)
    if rank >= len(TIER_LADDER) - 1:
        return None
    return TIER_LADDER[rank + 1]


def tier_below(tier: str) -> Optional[str]:
    """The next tier down, or None if already at BRONZE."""
    rank = tier_rank(tier)
    if rank <= 0:
        return None
    return TIER_LADDER[rank - 1]


def meets_requirements(
    tier: str,
    days_online: int,
    avg_hours_per_active_day: float,
) -> bool:
    """True iff a member's observed uptime meets ``tier``'s bar.
    Both axes must be satisfied — partial credit isn't a thing because
    the network needs both coverage (days) and capacity (hours)."""
    req = requirements_for(tier)
    return (
        days_online >= req["min_days_per_week"]
        and avg_hours_per_active_day >= req["min_hours_per_day"]
    )
