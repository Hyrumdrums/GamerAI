"""Admin CLI for the membership layer.

Usage (run inside the coordinator container, where the SQLite DB is mounted):

    docker exec -it gamerai-coordinator python -m coordinator.admin create-member \\
        --email alice@example.com --role contributor

    docker exec -it gamerai-coordinator python -m coordinator.admin list-members

    docker exec -it gamerai-coordinator python -m coordinator.admin revoke \\
        --token gai_abc123...

`create-member` prints the new bearer token to stdout exactly once.
There is no recovery — losing it means revoke + reissue.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import uuid
from typing import Optional

from coordinator import member_auth
from coordinator.db import DB

VALID_ROLES = ("admin", "contributor", "invitee")
VALID_TIERS = ("BRONZE", "SILVER", "GOLD", "PLATINUM")

# Public URL where invitees redeem their codes. Override at deploy time
# if the host changes; falls back to a placeholder for local dev.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")


def _new_member_id() -> str:
    return "mem_" + uuid.uuid4().hex[:12]


def _resolve_parent(db: DB, parent_token: Optional[str]) -> Optional[str]:
    if not parent_token:
        return None
    row = db.get_member_by_token_hash(member_auth.hash_token(parent_token))
    if row is None:
        print("error: --parent-token does not match any member", file=sys.stderr)
        sys.exit(2)
    if row["revoked_at"] is not None:
        print("error: --parent-token is revoked", file=sys.stderr)
        sys.exit(2)
    return row["member_id"]


def cmd_create_member(args: argparse.Namespace) -> None:
    db = DB()
    parent_id = _resolve_parent(db, args.parent_token)
    token = member_auth.generate_token()
    member_id = _new_member_id()
    db.create_member(
        member_id=member_id,
        email=args.email,
        role=args.role,
        parent_member_id=parent_id,
        token_hash=member_auth.hash_token(token),
        tier=args.tier,
        daily_quota_tokens=args.daily_quota_tokens,
    )
    print(f"member_id={member_id}")
    print(f"role={args.role}")
    print(f"tier={args.tier}")
    if args.email:
        print(f"email={args.email}")
    if parent_id:
        print(f"parent_member_id={parent_id}")
    if args.daily_quota_tokens is not None:
        print(f"daily_quota_tokens={args.daily_quota_tokens}")
    print(f"token={token}")
    print("Save this token now — it is not stored and cannot be recovered.")


def cmd_list_members(args: argparse.Namespace) -> None:
    db = DB()
    rows = db.list_members()
    if not rows:
        print("(no members)")
        return
    fmt = "{:<22} {:<12} {:<10} {:<28} {:<22} {:<8}"
    print(fmt.format("member_id", "role", "tier", "email", "parent", "revoked"))
    for row in rows:
        print(
            fmt.format(
                row["member_id"],
                row["role"],
                row["tier"],
                row["email"] or "-",
                row["parent_member_id"] or "-",
                "yes" if row["revoked_at"] else "no",
            )
        )


def cmd_set_credentials(args: argparse.Namespace) -> None:
    """Set or rotate a member's username + password. Used by the founding
    admin to claim u/p sign-in after running with token-only auth, and as
    an out-of-band recovery path while email-based reset is on hold (see
    docs/auth-design.md)."""
    db = DB()
    row = db.get_member_by_token_hash(member_auth.hash_token(args.token))
    if row is None:
        print("error: --token does not match any member", file=sys.stderr)
        sys.exit(2)
    if row["revoked_at"] is not None:
        print("error: that member is revoked", file=sys.stderr)
        sys.exit(2)

    try:
        clean_username = member_auth.validate_username(args.username)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    # Prevent a UNIQUE violation by checking up front; the CLI is the
    # only path that picks a username on someone else's behalf, so a
    # clear error here beats a SQL exception.
    existing = db.get_member_by_username(clean_username)
    if existing is not None and existing["member_id"] != row["member_id"]:
        print(
            f"error: username {clean_username!r} is taken by {existing['member_id']}",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.password:
        password = args.password
    else:
        password = getpass.getpass("password: ")
        confirm = getpass.getpass("confirm:  ")
        if password != confirm:
            print("error: passwords did not match", file=sys.stderr)
            sys.exit(2)
    try:
        member_auth.validate_password(password)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    db.set_member_credentials(
        member_id=row["member_id"],
        username=clean_username,
        password_hash=member_auth.hash_password(password),
        when=time.time(),
    )
    print(f"member_id={row['member_id']}")
    print(f"username={clean_username}")
    print("password set; sign in at /login")


def cmd_set_email(args: argparse.Namespace) -> None:
    """Set or update a member's email. Keyed by username so the admin
    can update their own (or anyone's) email without first fetching the
    current bearer — useful for the founding admin who has no email on
    their seeded row, and as the recovery prep step before email-based
    reset ships."""
    db = DB()
    row = db.get_member_by_username(args.username)
    if row is None:
        print(
            f"error: no member with username {args.username!r}",
            file=sys.stderr,
        )
        sys.exit(2)
    if row["revoked_at"] is not None:
        print("error: that member is revoked", file=sys.stderr)
        sys.exit(2)
    email = (args.email or "").strip()
    if not email or "@" not in email:
        print("error: --email must be a valid address", file=sys.stderr)
        sys.exit(2)
    ok, reason = db.set_member_email(row["member_id"], email)
    if not ok:
        if reason == "email_taken":
            print(
                f"error: {email!r} is already in use by another member",
                file=sys.stderr,
            )
        else:
            print(f"error: {reason or 'unknown'}", file=sys.stderr)
        sys.exit(2)
    print(f"member_id={row['member_id']}")
    print(f"username={args.username}")
    print(f"email={email}")


def cmd_revoke(args: argparse.Namespace) -> None:
    db = DB()
    ok = db.revoke_member_by_token_hash(
        member_auth.hash_token(args.token), time.time()
    )
    if not ok:
        print("error: token did not match any active member", file=sys.stderr)
        sys.exit(2)
    print("revoked")


def cmd_create_invite(args: argparse.Namespace) -> None:
    db = DB()
    parent = _resolve_parent(db, args.contributor_token)
    if parent is None:
        print("error: --contributor-token required", file=sys.stderr)
        sys.exit(2)
    contributor = db.get_member(parent)
    if contributor["role"] not in ("admin", "contributor"):
        print("error: contributor token must belong to a contributor or admin",
              file=sys.stderr)
        sys.exit(2)
    email = (args.email or "").strip()
    if not email or "@" not in email:
        print(
            "error: --email is required (every member needs a recovery "
            "address on file)",
            file=sys.stderr,
        )
        sys.exit(2)
    now = time.time()
    expires_at = now + args.expires_hours * 3600.0 if args.expires_hours else None
    code = member_auth.generate_invite_code()
    invite_id = "inv_id_" + uuid.uuid4().hex[:12]
    db.create_invite(
        invite_id=invite_id,
        code=code,
        contributor_member_id=parent,
        daily_quota_tokens=args.daily_quota_tokens,
        invitee_email=args.email,
        expires_at=expires_at,
        notes=args.notes,
        created_at=now,
    )
    print(f"invite_id={invite_id}")
    print(f"code={code}")
    print(f"contributor_member_id={parent}")
    if args.daily_quota_tokens is not None:
        print(f"daily_quota_tokens={args.daily_quota_tokens}")
    if args.email:
        print(f"invitee_email={args.email}")
    if expires_at is not None:
        print(f"expires_at={expires_at}")
    print(f"redemption_url={PUBLIC_BASE_URL.rstrip('/')}/invite/{code}")


def cmd_list_invites(args: argparse.Namespace) -> None:
    db = DB()
    if args.contributor_token:
        parent = _resolve_parent(db, args.contributor_token)
        rows = db.list_invites_by_contributor(parent)
    else:
        rows = db.list_all_invites()
    if not rows:
        print("(no invites)")
        return
    fmt = "{:<22} {:<10} {:<22} {:<10} {:<10}"
    print(fmt.format("code", "state", "contributor", "quota", "accepted"))
    now = time.time()
    for row in rows:
        if row["revoked_at"]:
            state = "revoked"
        elif row["accepted_at"]:
            state = "accepted"
        elif row["expires_at"] and row["expires_at"] < now:
            state = "expired"
        else:
            state = "open"
        print(
            fmt.format(
                row["code"],
                state,
                row["contributor_member_id"],
                str(row["daily_quota_tokens"] or "-"),
                "yes" if row["accepted_at"] else "no",
            )
        )


def cmd_revoke_invite(args: argparse.Namespace) -> None:
    db = DB()
    if not db.revoke_invite_by_code(args.code, time.time()):
        print(
            "error: invite not found, already accepted, or already revoked",
            file=sys.stderr,
        )
        sys.exit(2)
    print("revoked")


# --------------------------- canaries -------------------------------

# Seed list. Pick prompts with stable, narrowly-phrased factual answers
# so honest models will reliably produce the required tokens regardless
# of sampling temperature. Avoid prompts where a correct answer could
# legitimately omit any required token (e.g. "What planet do we live
# on?" might be answered "we live here" without naming Earth).
DEFAULT_CANARIES = [
    {
        "prompt": "Reply with the single word that names our planet: ",
        "required_tokens": ["earth"],
    },
    {
        "prompt": "What is two plus three? Reply with only the digit.",
        "required_tokens": ["5"],
    },
    {
        "prompt": "Name the largest ocean on Earth in one word.",
        "required_tokens": ["pacific"],
    },
    {
        "prompt": "In what year did humans first land on the Moon? Reply with the year only.",
        "required_tokens": ["1969"],
    },
    {
        "prompt": "What is the chemical symbol for water? Reply with the symbol only.",
        "required_tokens": ["h2o"],
    },
]


def cmd_seed_canaries(args: argparse.Namespace) -> None:
    db = DB()
    model = args.model
    existing = {row["prompt"]: row for row in db.list_active_canaries()}
    seeded = 0
    skipped = 0
    for entry in DEFAULT_CANARIES:
        prompt = entry["prompt"]
        if prompt in existing:
            skipped += 1
            continue
        db.create_canary(
            canary_id="can_" + uuid.uuid4().hex[:12],
            prompt=prompt,
            required_tokens_json=json.dumps(entry["required_tokens"]),
            model=model,
            active=True,
        )
        seeded += 1
    print(f"seeded={seeded} skipped={skipped} model={model}")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m coordinator.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create-member", help="issue a new member token")
    p_create.add_argument("--email", default=None)
    p_create.add_argument("--role", required=True, choices=VALID_ROLES)
    p_create.add_argument(
        "--parent-token",
        default=None,
        help="raw bearer of an existing contributor (for invitees)",
    )
    p_create.add_argument("--tier", default="BRONZE", choices=VALID_TIERS)
    p_create.add_argument(
        "--daily-quota-tokens",
        type=int,
        default=None,
        help="per-day output token cap; omit for unlimited",
    )
    p_create.set_defaults(fn=cmd_create_member)

    p_list = sub.add_parser("list-members", help="dump member table")
    p_list.set_defaults(fn=cmd_list_members)

    p_revoke = sub.add_parser("revoke", help="revoke a member by their token")
    p_revoke.add_argument("--token", required=True)
    p_revoke.set_defaults(fn=cmd_revoke)

    p_se = sub.add_parser(
        "set-email",
        help="set or update a member's email (recovery channel for "
             "future email-based password reset)",
    )
    p_se.add_argument(
        "--username", required=True,
        help="member to update (case-insensitive)",
    )
    p_se.add_argument(
        "--email", required=True,
        help="email address; must be unique among members (case-insensitive)",
    )
    p_se.set_defaults(fn=cmd_set_email)

    p_sc = sub.add_parser(
        "set-credentials",
        help="set or rotate a member's username + password (u/p sign-in)",
    )
    p_sc.add_argument(
        "--token", required=True,
        help="raw bearer of the member to update",
    )
    p_sc.add_argument(
        "--username", required=True,
        help="login handle; letters, digits, _-. only",
    )
    p_sc.add_argument(
        "--password", default=None,
        help="new password; omitted to prompt interactively (safer for shell history)",
    )
    p_sc.set_defaults(fn=cmd_set_credentials)

    p_inv = sub.add_parser("create-invite", help="create an invite for an outsider")
    p_inv.add_argument(
        "--contributor-token",
        required=True,
        help="raw bearer of the contributor doing the inviting",
    )
    p_inv.add_argument(
        "--email", required=True,
        help="invitee's email (required — every member needs a "
             "recovery address on file)",
    )
    p_inv.add_argument(
        "--daily-quota-tokens",
        type=int,
        default=None,
        help="per-day output token cap for the invitee; omit for unlimited",
    )
    p_inv.add_argument(
        "--expires-hours",
        type=float,
        default=None,
        help="invite expiry window in hours; omit for no expiry",
    )
    p_inv.add_argument("--notes", default=None)
    p_inv.set_defaults(fn=cmd_create_invite)

    p_li = sub.add_parser("list-invites", help="dump invite table")
    p_li.add_argument(
        "--contributor-token",
        default=None,
        help="filter to invites this contributor created",
    )
    p_li.set_defaults(fn=cmd_list_invites)

    p_ri = sub.add_parser("revoke-invite", help="revoke an unredeemed invite")
    p_ri.add_argument("--code", required=True)
    p_ri.set_defaults(fn=cmd_revoke_invite)

    p_canary = sub.add_parser(
        "seed-canaries",
        help="install the default factual-answer canary prompts",
    )
    p_canary.add_argument(
        "--model",
        default="llama3.2:1b",
        help="model name to target; should match what most contributors run",
    )
    p_canary.set_defaults(fn=cmd_seed_canaries)

    return ap


def main(argv: Optional[list[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
