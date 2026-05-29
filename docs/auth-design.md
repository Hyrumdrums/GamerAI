# Authentication design (Phase 3b.i)

This is the design + threat-model writeup for the username/password
sign-in slice, the invite-redeem flow, and the agent browser-handoff
pairing. It complements the high-level roadmap in `README.md` §14 and
the operational runbook in `docs/OPERATOR.md`.

If you are looking for "how do I run the existing flow," read
`OPERATOR.md`. If you are looking for "why is the flow shaped this
way and what is *not* implemented yet," you are in the right file.

## What's in scope (shipped)

- **Per-member u/p sign-in.** Every member has a `username`,
  `password_hash` (argon2id), and `email` on their `members` row.
  `POST /login` validates and returns a fresh bearer token. The
  bearer is stamped into a single `gai_session` HttpOnly cookie on
  the web side.
- **Invite-redeem creates the account.** `POST /invites/{code}/accept`
  is the only signup path. The form collects username + password +
  email + ToS-accepted; the member row is inserted atomically with
  the invite-accept update so a duplicate-username concurrent redeem
  rolls back cleanly (409 to the caller). The web layer auto-logs-in
  with the returned token; the friend never sees a raw bearer.
- **Account page.** `/account` shows the member's own info, their
  host (one-line honesty about host powers), a friends list (admin
  only in v1), and a placeholder "This PC" section for paired
  agents.
- **Agent pairing (browser handoff).** The Windows agent runs
  `agent --pair`, which calls `POST /agents/pair/start`. The
  coordinator returns a secret `pair_code` (the agent polls with it),
  a short human-typeable `user_code`, and a verification URL with **no
  secret in it**. The agent prints the `user_code` on screen and opens
  the default browser to `/agent/pair`; the signed-in user types the
  `user_code` to approve (`POST /agents/pair/confirm`). The
  out-of-band code is the anti-phishing control: a code in the URL
  (the original design) let anyone who called `/start` mail a victim a
  one-click approval link and capture a token in the victim's name.
  The agent polls `/agents/pair/poll` with the secret `pair_code` and
  picks the token up exactly once. The user's account gains a new
  per-agent row in the `member_tokens` table — pairing does *not*
  rotate the primary web bearer, so web sessions on other devices keep
  working.
- **Multi-token support.** A new `member_tokens` table holds
  per-agent / per-CLI bearers. `lookup_member_by_token` checks this
  table first, then falls back to `members.token_hash` (the single
  primary credential rotated by `/login`).
- **Token-paste fallback.** `/login/token` (collapsible on the login
  page) accepts a raw bearer. Kept so the existing token-only admin
  can sign in once and run the `set-credentials` CLI to migrate.

## What's deferred (and why)

These are not bugs — each one was deliberately punted in this slice.
The "why" is the load-bearing part; if a future you (or future me)
forgets the reasoning, the temptation is to grab the simple fix and
re-introduce the original failure mode.

### Password recovery — current state and end state

**Today (no email service):** there is no self-service recovery. A
member who forgets their password contacts their host out of band;
the host revokes the member row and reissues a fresh invite under
the same email. The friend redeems, picks a new password, and is
back in. Conversation history is lost (`purge_conversation` runs on
revoke). For friends-and-family beta this is acceptable but lossy —
and it doesn't rescue the host themselves losing access, since the
admin / a root contributor has no host above them.

**Email collected at signup is the mitigation.** Username +
**required** + **unique** email are persisted on every member
row from the redemption flow. The `members.email` partial unique
index (case-insensitive) is what makes "reset to alice@example.com"
unambiguous — without uniqueness, two members with the same address
collapse the reset target. Cost of enforcing later, after
inconsistent rows accumulate, is high; cost of enforcing at signup
is one partial index.

**Why this matters for the pyramid:** the network is a tree —
Alice → Bob → Carol — and a stuck account at any depth is a dead
node. A locked-out Bob means Carol's "Your host" link points at a
ghost AND her recovery escalation is broken (her host can't be
reached to revoke + reissue). Without an email recovery channel
that goes around the host, deep-tree contributors are fragile.

**End state (when email service is wired):**

1. **Self-service email reset** — standard form: enter email, click
   the magic link, set new password. Sender will be Postmark / SES /
   Resend / similar; not yet picked.
2. **Host-attested recovery** — the right shape for the "email
   bounces too" edge case, and the long-term answer that ties
   recovery back into the social graph that's the whole point of
   the network. The host, who already vouched for the invitee at
   signup, can attest "yes that's still Bob, accept this recovery
   request" — gated by the host's own active session, logged on
   the invitee's account history, with the host's own role and
   tier providing the audit trail. Combined with email reset,
   this means a stuck account is recoverable iff *either* the
   email works *or* the host is reachable.
3. **Bounce monitoring** — once email is sending, flag rows whose
   reset attempts bounce; surface them on the admin dashboard so a
   silently-dead recovery channel doesn't go undetected for months.

The host-attested path is the load-bearing piece for the
pyramid: it's what keeps Carol recoverable when Bob is unreachable
*and* her own email has rotted. Don't ship email reset without it,
or we'll have replaced "host-only recovery" with "email-only
recovery" — different single point of failure, same fragility.

### Host-can-send-password-reset-link

**What was considered:** the host could press a button on their
`/account → Friends` row to mint a single-use reset link for an
invitee, then hand-deliver it over Slack / SMS / etc. That dodges
the need for an email service while still giving forgotten-password
recovery.

**Why deferred:** **cascading takeover.** Today only one admin can
invite, so the blast radius is bounded — admin already owns every
invitee. But the moment Phase 3b.i tier-gating ships and contributors
can invite their own friends, the graph deepens:

> Alice (admin) → Bob (contributor) → Carol (invitee)

If Alice can reset Bob's password, she logs in as Bob and resets
Carol's password. Each layer of the network becomes implicitly
takeover-able by any ancestor. That's a far worse trust model than
the docs' "your host vouches for you and can adjust quota / revoke
access" — it would let a single compromised root take over the
entire downstream tree silently.

**What replaces it:** email-based reset, gated by the invitee's
email (collected at signup). This needs a real email service
(Postmark / SES / Resend / etc.) which we have decided is not worth
wiring up before friends-and-family beta.

**How an invitee recovers their account before email service ships:**
contact the host out of band; the host can `revoke` their member row
and issue a fresh invite under the same email. The friend redeems
the new invite, picks a new password, and is back in. The old data
(conversations, etc.) is lost, but for friends-and-family that's
acceptable.

### Tier-gated per-contributor invite slots

**What was considered:** every contributor at BRONZE+ gets N invite
slots; SILVER gets more; etc., consistent with the README §5 tier
ladder.

**Why deferred:** the tier promotion engine doesn't exist yet (Phase
3b.i, same slice). Until tiers move dynamically, gating on them is
either fixed-constants (which the tier engine will override anyway)
or trivially false ("everyone's at BRONZE so everyone gets 1 slot,
fine"). The right time to wire tier-gated invite quotas is when
`tier` actually changes for non-admin members.

**Today's behavior:** only the admin (role=admin) can create invites
from `/account`. Other roles see a "want to host? run an agent"
placeholder. Coordinator side, `POST /invites` checks role and 403s
contributors and invitees.

### Google OAuth (or any other federated identity)

**What was considered:** "sign in with Google" as a second identity
path alongside u/p.

**Why deferred:** the user case ("some friends won't want Google
OAuth") argues for u/p as the *primary*, not "u/p AND Google." Adding
Google later is a small additive change to the login + redeem
templates plus a `/auth/google` callback that creates / links a
member. Doing both up front doubles the test matrix and adds an
account-linking edge case (user signs up with u/p, later wants to
add Google → which is the source of truth?). Wait for a concrete
member to ask.

### Multi-device session table

**What was considered:** each /login mints a fresh row in a
`sessions` table; logging out kills one row; force-sign-out-everywhere
kills all rows for a member.

**Why deferred:** today `/login` rotates `members.token_hash`, so
logging in on a new device kicks the old. That's annoying for power
users on phone + laptop but it's *safe* — and a stolen session
cookie is invalidated by the legitimate user simply signing in
again. Adding a sessions table is straightforward (we already have
`member_tokens` for the secondary path) but until a real user
complains, the cheap behavior wins.

**Note on agent pairing:** this is precisely the reason pairing
goes through `member_tokens` rather than rotating the primary
bearer. Pairing creates a new credential alongside the web session;
the web session doesn't die.

### Participation verification

The "is this contributor actually contributing, or just claiming
they are" question. Mentioned in the user's original message as
"circle back later." Not part of this slice. The canary system
(`coordinator/canaries.py`) is the substrate; tier promotion will
consume claimed-jobs-per-hour + canary pass rate when it ships.

## Threat model summary

| Threat | Mitigation today |
|---|---|
| Bearer token exfil via XSS | `gai_session` is `HttpOnly` so page JS can't read it |
| CSRF on /login or /me/password | `SameSite=Lax` cookie + same-origin forms |
| Account enumeration via /login | Single "invalid credentials" detail for unknown-username and bad-password |
| Brute-force /login | Existing per-IP rate limiter (`RATE_LIMIT_PER_MIN`); add a per-username limiter when traffic warrants |
| Stolen session cookie | u/p login by the real owner mints a fresh token, invalidating the stolen one |
| Pair-code interception | 5-minute TTL + explicit confirm-in-browser by an already-signed-in user + one-shot delivery (Redis record deleted on first /poll) |
| Cascading host takeover | **deferred mitigation** — host-reset waits for email service (see above) |
| Argon2 parameter drift | `argon2-cffi` default profile; rehash-on-login when needed (not implemented yet — add a `_PH.check_needs_rehash` call in `/login` when we tune cost) |

## Database shape

```sql
members (
    member_id, email, role, parent_member_id,
    token_hash UNIQUE,                  -- primary wire bearer; rotated by /login
    username UNIQUE WHERE NOT NULL,     -- u/p login handle
    password_hash,                      -- argon2id encoded
    password_set_at,
    tier, daily_quota_tokens, revoked_at,
    created_at, last_active_at,
    tos_accepted_at, tos_version
)

member_tokens (
    token_hash PRIMARY KEY,             -- additional bearers (paired agents,
    member_id,                          -- future CLIs); never the web session
    label, created_at, last_used_at
)
```

`lookup_member_by_token(raw_token)` returns the member by checking
`member_tokens` first, then `members.token_hash`. Both paths apply the
same `revoked_at` filter on the parent `members` row.

## Flows

### Invite redemption

```
host /account  --[POST /invites]-->  coordinator
                                     creates invite row
                                     returns code

host                          shares invite URL out of band
  └ /invite/<code>            (no email service in v1)

invitee /invite/<code>  --[GET /invites/<code>]-->  coordinator
                                                    returns inviter + cap
                        ←-- 200 with form fields

invitee POSTs username + email + password + tos_accepted
                       --[POST /invites/<code>/accept]-->
                                                    atomically:
                                                      - validate invite redeemable
                                                      - reject duplicate username (409)
                                                      - hash password (argon2)
                                                      - insert members row
                                                      - mark invite accepted
                                                    returns gai_<token>

web stamps session cookie + 303 to /
```

### Sign-in

```
user /login   --[POST /login]-->   coordinator
                                   verify_password(stored)
                                   rotate members.token_hash
                                   return new gai_<token>

web stamps session cookie + 303 to next_path
```

### Agent pairing

```
agent  --[POST /agents/pair/start]-->  coordinator
                                       store {state:pending, user_code, ttl=300} in Redis
                                       index user_code -> pair_code in Redis
                                       return {pair_code, user_code, verification_url}
                                       (verification_url has NO secret)

agent prints user_code + webbrowser.open(verification_url)

user  /agent/pair                    web: require session, show code-entry form

user types the user_code shown by the agent and submits
       --[POST /agent/pair {user_code}]--> web --[POST /agents/pair/confirm {user_code}]-->
                                                              coordinator: resolve user_code->pair_code,
                                                              mint gai_, insert into member_tokens,
                                                              store raw in Redis {state:approved}

agent polls:  POST /agents/pair/poll {pair_code}
                                                              if approved:
                                                                delete Redis record
                                                                return {token, member_id}

agent persists token in state.json
```

### Agent unpair

Triggered by either the Windows uninstaller (`[UninstallRun] agent.exe
--unpair`) or a manual run on demand. The order is intentional: hit
the network first so the credential is revoked even if the local
wipe fails, then NULL the on-disk copy so a recovered state.json
holds nothing useful.

```
agent --unpair  --[POST /agents/pair/unpair]-->  coordinator
                                                 delete member_tokens row
                                                 (only matching this bearer's hash —
                                                  never touches members.token_hash)
                                                 return {deleted: true|false}

agent then NULLs api_token in state.json
```

The `[UninstallRun]` step runs **before** Inno's `[UninstallDelete]`,
so agent.exe still exists when the unpair fires. A second
`[UninstallRun]` step does `taskkill /F /IM agent.exe /T` to reap the
tray instance that's usually still alive when a user uninstalls
without closing first; `[Setup] CloseApplications=force` asks
Restart Manager first for a friendly close where supported.

### Web-side unpair from /account

The account page's "Paired machines" section lists each row in
`member_tokens` for the caller (label, paired date, last-used). The
per-row Unpair button POSTs to `/account/machines/<id>/unpair` →
`/me/machines/<id>/unpair` on the coordinator → `delete_member_token`.
The agent on the unpaired PC keeps running until it next tries to
hit the coordinator, at which point it 401s and stops claiming work.

## Files of interest

- `coordinator/db.py` — schema + member_tokens helpers
- `coordinator/member_auth.py` — argon2 wrappers + Member dataclass + lookup
- `coordinator/main.py` — `/login`, `/me/password`, `/me/friends`,
  `/me/machines`, `/agents/pair/*` (start/info/confirm/poll/unpair)
- `coordinator/admin.py` — `set-credentials`, `set-email` CLIs
- `client/routes/auth.py` — `/login`, `/login/token`, `/logout`
- `client/routes/invites.py` — public `/invite/<code>` redemption
- `client/routes/account.py` — `/account`, `/account/machines/<id>/unpair`,
  `/account/invites/*`, `/agent/pair`, `/contribute`
- `client/templates/redeem.html.j2` — invitee signup form
- `client/templates/account.html.j2` — host + invitee account view
- `client/templates/agent_pair.html.j2` — browser-handoff confirm
- `client/templates/contribute.html.j2` — public onboarding pitch +
  Windows install TL;DR (linked from the topbar CTA and from
  /account's empty-state)
- `windows-agent/agent.py` — `--pair`, `--unpair` subcommands;
  `run_pair_flow`, `run_unpair_flow`
- `windows-agent/installer.iss` — `[UninstallRun]` invokes
  `--unpair` then `taskkill`; `[UninstallDelete]` wipes
  `%APPDATA%\GamerAI` and `%LOCALAPPDATA%\GamerAI`
- `tests/test_password_auth.py` — u/p, /login, /me/password
- `tests/test_pairing.py` — agent pairing + unpair + /me/machines
- `tests/test_web_ui_smoke.py` — web-side coverage for all of the above

## Production status (as of 1.2.3)

End-to-end verified live on ai.dallinlayton.com:

- Invite redemption → u/p account creation → auto-login (no token reveal)
- u/p sign-in → session cookie → /me + /account work
- Per-host invite + friends list on /account (admin-only in v1)
- Agent first-run auto-pairs via browser handoff (no token-paste anywhere)
- Paired-machines list on /account with per-row Unpair button
- Windows uninstall fires `--unpair` → server-side revocation +
  `taskkill` of zombie tray + full `%APPDATA%\GamerAI` wipe
- Topbar `Contribute and invite friends` CTA hides itself once a
  machine is paired (`paired_machines_count > 0` on /me)
