"""Server-side per-member bearer-token auth.

Distinct from ``shared.auth`` (client-facing header generation, single
shared API_TOKEN). This module is the coordinator-side lookup: take a
raw bearer token off an inbound request, hash it, find the matching
member row.

Token format: ``gai_<64 hex>``. Tokens are random secrets; we never
store the raw token, only ``sha256(token)``. The first time an admin
runs ``python -m coordinator.admin create-member`` they get the raw
token printed to stdout; thereafter it's the holder's responsibility.

Password hashes are argon2id (via ``argon2-cffi``). Token rotation on
each u/p login is intentional — sessions on other devices die when
the user re-logs-in. A multi-device session table is on the deferred
list (see ``docs/auth-design.md``).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHash,
    VerificationError,
    VerifyMismatchError,
)

TOKEN_PREFIX = "gai_"
_TOKEN_BYTES = 32  # → 64 hex chars after the prefix

# Username constraints. Kept loose enough for casual handles ("dallin",
# "alex_h", "rkc-1") while excluding shell-special and url-special
# characters that would force escaping in admin tooling.
USERNAME_MIN_LEN = 3
USERNAME_MAX_LEN = 32
_USERNAME_OK = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_-."
)

# Password is enforced at signup; we don't store a copy anywhere, so the
# length floor is the only structural check. argon2id has no useful
# maximum but a 1KB cap stops obvious DoS-by-megabyte-password.
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 1024


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


# Single, module-level hasher. argon2-cffi recommends reuse so the
# expensive parameter detection runs once per process.
_PH = PasswordHasher()


def validate_username(username: str) -> str:
    """Normalize and validate a candidate username. Returns the cleaned
    value, raises ValueError with a user-facing message on failure."""
    if username is None:
        raise ValueError("username is required")
    name = username.strip()
    if len(name) < USERNAME_MIN_LEN:
        raise ValueError(
            f"username must be at least {USERNAME_MIN_LEN} characters"
        )
    if len(name) > USERNAME_MAX_LEN:
        raise ValueError(
            f"username must be at most {USERNAME_MAX_LEN} characters"
        )
    bad = [c for c in name if c not in _USERNAME_OK]
    if bad:
        raise ValueError(
            "username may only contain letters, digits, underscore, "
            "hyphen, and dot"
        )
    return name


def validate_password(password: str) -> str:
    if password is None:
        raise ValueError("password is required")
    if len(password) < PASSWORD_MIN_LEN:
        raise ValueError(
            f"password must be at least {PASSWORD_MIN_LEN} characters"
        )
    if len(password) > PASSWORD_MAX_LEN:
        raise ValueError(
            f"password must be at most {PASSWORD_MAX_LEN} characters"
        )
    return password


def hash_password(password: str) -> str:
    """Return an argon2id encoded hash including salt + parameters.
    Callers should ``validate_password`` first so the failure surfaces
    as a 400 rather than a hash on weak input."""
    return _PH.hash(password)


def verify_password(password: str, encoded: Optional[str]) -> bool:
    """Constant-time-ish password check. Returns False for an empty
    stored hash (member who never set credentials) so callers don't
    have to special-case the bootstrap state."""
    if not encoded:
        return False
    try:
        return _PH.verify(encoded, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


@dataclass(frozen=True)
class Member:
    member_id: str
    email: Optional[str]
    role: str
    parent_member_id: Optional[str]
    tier: str
    daily_quota_tokens: Optional[int]
    daily_quota_images: Optional[int]
    revoked_at: Optional[float]
    created_at: float
    tos_accepted_at: Optional[float] = None
    tos_version: Optional[str] = None
    username: Optional[str] = None
    has_password: bool = False
    password_set_at: Optional[float] = None


def _row_to_member(row) -> Member:
    keys = row.keys()
    tos_accepted = row["tos_accepted_at"] if "tos_accepted_at" in keys else None
    tos_version = row["tos_version"] if "tos_version" in keys else None
    username = row["username"] if "username" in keys else None
    password_hash = row["password_hash"] if "password_hash" in keys else None
    password_set_at = (
        row["password_set_at"] if "password_set_at" in keys else None
    )
    daily_image_cap_raw = (
        row["daily_quota_images"] if "daily_quota_images" in keys else None
    )
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
        daily_quota_images=(
            int(daily_image_cap_raw) if daily_image_cap_raw is not None else None
        ),
        revoked_at=float(row["revoked_at"]) if row["revoked_at"] is not None else None,
        created_at=float(row["created_at"]),
        tos_accepted_at=float(tos_accepted) if tos_accepted is not None else None,
        tos_version=tos_version,
        username=username,
        has_password=bool(password_hash),
        password_set_at=(
            float(password_set_at) if password_set_at is not None else None
        ),
    )


def lookup_member_by_token(db, raw_token: str) -> Optional[Member]:
    """Resolve a raw bearer token to a Member, or None if not found / revoked.

    Checks the additive ``member_tokens`` table first (per-agent and per-
    CLI tokens added after the multi-token slice landed), then falls
    back to the single ``members.token_hash`` that holds the web-session
    credential. The raw_token is hashed before the DB hit, so callers
    can pass the string straight from the Authorization header.
    """
    if not raw_token:
        return None
    th = hash_token(raw_token)
    secondary_member_id = db.lookup_member_id_by_token_hash_in_tokens_table(th)
    if secondary_member_id is not None:
        row = db.get_member(secondary_member_id)
        if row is None or row["revoked_at"] is not None:
            return None
        return _row_to_member(row)
    row = db.get_member_by_token_hash(th)
    if row is None:
        return None
    if row["revoked_at"] is not None:
        return None
    return _row_to_member(row)


def tokens_match(a: str, b: str) -> bool:
    """Constant-time string compare. Use when comparing two known-secret
    strings (not exposed to inbound requests today, but handy for tests)."""
    return hmac.compare_digest(a, b)
