"""Server-side per-member bearer-token auth.

Distinct from ``shared.auth`` (client-facing header generation, single
shared API_TOKEN). This module is the coordinator-side lookup: take a
raw bearer token off an inbound request, hash it, find the matching
member row.

Token format: ``gai_<64 hex>``. Tokens are random secrets; we never
store the raw token, only ``sha256(token)``. The first time an admin
runs ``python -m coordinator.admin create-member`` they get the raw
token printed to stdout; thereafter it's the holder's responsibility.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Optional

TOKEN_PREFIX = "gai_"
_TOKEN_BYTES = 32  # → 64 hex chars after the prefix


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_hex(_TOKEN_BYTES)


INVITE_PREFIX = "inv_"
_INVITE_BYTES = 8  # → 16 hex chars; ~64 bits entropy, plenty for one-shot codes


def generate_invite_code() -> str:
    return INVITE_PREFIX + secrets.token_hex(_INVITE_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def parse_bearer(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


@dataclass(frozen=True)
class Member:
    member_id: str
    email: Optional[str]
    role: str
    parent_member_id: Optional[str]
    tier: str
    daily_quota_tokens: Optional[int]
    revoked_at: Optional[float]
    created_at: float
    tos_accepted_at: Optional[float] = None
    tos_version: Optional[str] = None


def _row_to_member(row) -> Member:
    keys = row.keys()
    tos_accepted = row["tos_accepted_at"] if "tos_accepted_at" in keys else None
    tos_version = row["tos_version"] if "tos_version" in keys else None
    return Member(
        member_id=row["member_id"],
        email=row["email"],
        role=row["role"],
        parent_member_id=row["parent_member_id"],
        tier=row["tier"],
        daily_quota_tokens=(
            int(row["daily_quota_tokens"])
            if row["daily_quota_tokens"] is not None
            else None
        ),
        revoked_at=float(row["revoked_at"]) if row["revoked_at"] is not None else None,
        created_at=float(row["created_at"]),
        tos_accepted_at=float(tos_accepted) if tos_accepted is not None else None,
        tos_version=tos_version,
    )


def lookup_member_by_token(db, raw_token: str) -> Optional[Member]:
    """Resolve a raw bearer token to a Member, or None if not found / revoked.

    The raw_token is hashed before the DB hit, so callers can pass the
    string straight from the Authorization header.
    """
    if not raw_token:
        return None
    row = db.get_member_by_token_hash(hash_token(raw_token))
    if row is None:
        return None
    if row["revoked_at"] is not None:
        return None
    return _row_to_member(row)


def tokens_match(a: str, b: str) -> bool:
    """Constant-time string compare. Use when comparing two known-secret
    strings (not exposed to inbound requests today, but handy for tests)."""
    return hmac.compare_digest(a, b)
