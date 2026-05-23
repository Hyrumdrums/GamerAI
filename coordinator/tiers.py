"""Tier → daily allowance mapping.

Single source of truth for what BRONZE / SILVER / GOLD / PLATINUM each
get per day. Consumed in three places:

- ``/me`` returns the inviting member's ``tier_quota`` so the account
  page can render "% of your daily allowance" tips on the invite form
  without a per-keystroke server round-trip.
- The invite form itself uses ``tier_quota`` as the denominator for the
  helper text under the daily-cap inputs (so "50,000" reads as
  "≈50% of your BRONZE daily allowance" instead of a bare number).
- The (future) tier-promotion engine will read this table when it
  flips ``members.tier`` to also write the matching defaults into
  ``members.daily_quota_tokens`` / ``members.daily_quota_images``.

These numbers are the *display denominator* today, not the quota
enforcer. The enforcer still reads the explicit per-member columns
set at invite time or by admin override (coordinator/main.py /generate
quota gate). Once the promotion engine ships, those columns get
populated from this table on every tier change, closing the loop.

``None`` means "unlimited" — used for PLATINUM and for admin members
regardless of tier.
"""
from typing import Optional, TypedDict


class TierQuota(TypedDict):
    tokens: Optional[int]
    images: Optional[int]


TIER_QUOTAS: dict[str, TierQuota] = {
    "BRONZE":   {"tokens": 100_000,   "images": 20},
    "SILVER":   {"tokens": 500_000,   "images": 100},
    "GOLD":     {"tokens": 2_000_000, "images": 500},
    "PLATINUM": {"tokens": None,      "images": None},
}


def quota_for(tier: str) -> TierQuota:
    """Return the ``{tokens, images}`` allowance for a tier, defaulting
    to BRONZE for an unknown tier string. Defensive fallback so a typo
    or a legacy row that somehow holds a stale tier name doesn't 500
    the /me handler."""
    return TIER_QUOTAS.get(tier, TIER_QUOTAS["BRONZE"])
