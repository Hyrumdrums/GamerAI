# Operator's runbook

For the one person running prod (hi, Dallin). This page tells you where the
admin token lives, how to invite testers, and what to do when something needs
revoking. Read it cold before you start poking prod.

Code references in this doc are snapshots; if a line number looks wrong, grep
for the symbol — the surrounding code probably moved.

---

## 1. The auth model in 30 seconds

- **One admin**, seeded once from the `API_TOKEN` env var at coordinator
  startup (`coordinator/main.py:91-123`, `ensure_admin_seed()`).
- **N invitees**, each minted via a per-invite redemption flow. Every invitee
  ends up with their own bearer token tied back to whoever invited them
  (`members.parent_member_id`).
- **Token format:** `gai_<64 hex>` for member bearer tokens,
  `inv_<16 hex>` for one-shot invite redemption codes
  (`coordinator/member_auth.py:21-34`).
- **Storage:** only the SHA-256 hash of the raw token is stored
  (`members.token_hash` in `coordinator/db.py:55-66`,
  hashing in `coordinator/member_auth.py:37-38`). Raw tokens are never
  written to disk by the coordinator — if a token is lost, it's lost.

---

## 2. Where the admin token lives

On the prod VPS, in `/opt/gamerai/.env.prod`, on the `API_TOKEN=...` line.
The file was generated once by the bootstrap script
(`infra/bootstrap.sh:125-142` — `openssl rand -hex 32`) and is preserved
across re-runs.

```bash
ssh root@ai.dallinlayton.com 'grep API_TOKEN /opt/gamerai/.env.prod'
```

It is **not** in this git repo. If you lose it, you don't recover it —
you rotate (see §6).

---

## 3. Logging in as admin

1. Open `https://ai.dallinlayton.com/login`.
2. Paste the `API_TOKEN` value from §2 into the form. No `Bearer ` prefix —
   the form takes the raw token.
3. You're now logged in. Session cookie is `gai_session`, 30-day lifetime,
   `HttpOnly` + `Secure` (HTTPS-only) (`client/services/session.py:21-23`).
4. As admin you can reach the chat at `/`, plus `/dashboard`,
   `/admin/members`, and `/admin/invites`.

The same flow works for any member token, not just the admin one — invitees
log in the same way.

---

## 4. Minting an invite for a tester

There is no admin button for this yet (see §7). Use curl from your laptop:

```bash
# Pull the admin token off the VPS into a local shell var:
TOKEN=$(ssh root@ai.dallinlayton.com \
  'grep API_TOKEN /opt/gamerai/.env.prod | cut -d= -f2')

# Mint an invite. All fields except invitee_email are optional.
curl -X POST https://ai.dallinlayton.com/invites \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "invitee_email": "friend@example.com",
    "daily_quota_tokens": 50000,
    "expires_hours": 72,
    "notes": "prod beta tester"
  }'
```

Response shape:

```json
{
  "invite_id": "inv_id_<12 hex>",
  "code": "inv_<16 hex>",
  "daily_quota_tokens": 50000,
  "expires_at": 1715539200.0,
  ...
}
```

The URL to send your tester is:

```
https://ai.dallinlayton.com/invite/<code>
```

Endpoint: `POST /invites` (`coordinator/main.py:1275`). The
`InviteCreateRequest` body schema lives in `shared/models.py`.

Notes:
- `daily_quota_tokens: null` (or omitted) = unlimited. For a test invite,
  cap it (50k–200k output tokens is plenty for a chat session).
- `expires_hours: null` = never expires. Cap it for testers so a forgotten
  invite URL doesn't sit redeemable forever.
- Both admin and contributor members can mint invites
  (`coordinator/main.py:1275` and the role check inside).

---

## 5. What your tester sees

1. They open `https://ai.dallinlayton.com/invite/<code>` — public, no auth
   needed.
2. The redemption page shows who invited them, their daily cap, the ToS
   excerpt, and an "I accept the terms" checkbox
   (template: `client/templates/redeem.html.j2`).
3. They check the box, optionally enter an email, click **Accept and get my
   token**. Coordinator calls `POST /invites/{code}/accept`
   (`coordinator/main.py:1358`), atomically mints a new `members` row, and
   returns the raw `gai_<64 hex>` token in the response.
4. The success page shows the token **once**, with a Copy button
   (`client/templates/redeem_done.html.j2`). The token is not recoverable
   after this page closes.
5. They paste the token at `/login` and they're in.

Walk a tester through it once before launching at a wider group. If they
close the success page without copying, you have to revoke + re-issue.

---

## 6. Day-2 operations

### List members
- Browser: `https://ai.dallinlayton.com/admin/members` (admin only).
- API: `curl -H "Authorization: Bearer $TOKEN" https://ai.dallinlayton.com/admin/members`

### List invites
- Browser: `https://ai.dallinlayton.com/admin/invites`.
- API: `curl -H "Authorization: Bearer $TOKEN" "https://ai.dallinlayton.com/invites?all=true"`
  (admin) or `/invites` (your own — works for contributors too).

### Revoke an invite (still unredeemed)
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://ai.dallinlayton.com/invites/<code>/revoke
```
Endpoint: `coordinator/main.py:1410`. Once redeemed, the invite row is
locked — you have to revoke the resulting member instead (next item).

### Revoke a member (the only escape hatch right now)
There is **no HTTP endpoint** for member revocation yet. To kill a token,
SSH to the VPS and flip the row directly:

```bash
ssh root@ai.dallinlayton.com
docker compose -f /opt/gamerai/docker-compose.yml \
  exec coordinator python -c "
from coordinator.db import DB
import time
db = DB()
# look up by partial email or member_id; here, by email:
for r in db.list_members():
    if r['email'] == 'friend@example.com':
        db.revoke_member_by_token_hash(r['token_hash'], time.time())
        print('revoked', r['member_id'])
"
```

The `db.revoke_member_by_token_hash()` helper exists
(`coordinator/db.py`); only the HTTP wrapper is missing. Either ship that
wrapper or accept the SSH dance.

### Tail logs (from your laptop)

```bash
# Coordinator only — watch heartbeats, job dispatch, completions.
ssh root@ai.dallinlayton.com -t 'cd /opt/gamerai && docker compose logs -f coordinator'

# Worker — watch what a single contributor machine is doing.
ssh root@ai.dallinlayton.com -t 'cd /opt/gamerai && docker compose logs -f worker'

# Everything at once.
ssh root@ai.dallinlayton.com -t 'cd /opt/gamerai && docker compose logs -f'
```

Ctrl-C stops the stream. The `cd /opt/gamerai` is so Compose finds the
`docker-compose.yml`; without it you get `no configuration file provided`.
Service names: `coordinator`, `client`, `worker`, `redis`.

What you're looking for in coordinator logs:
- `"event":"worker_registered"` — a Windows agent just connected.
- `"event":"job_queued"` — a chat prompt was submitted.
- `"event":"job_claimed"` — a worker picked it up (includes `worker_id`).
- `"event":"job_complete"` / `"job_error"` — final state, includes
  duration + tokens.
- `POST /heartbeat` — workers ping every ~5s; if a worker stops, its
  pings stop appearing.

### Check who's connected

```bash
# Admin UI — easiest:
open https://ai.dallinlayton.com/dashboard

# One command from your laptop — JSON of every registered worker:
ssh root@ai.dallinlayton.com \
  'TOKEN=$(grep API_TOKEN /opt/gamerai/.env.prod | cut -d= -f2);
   curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/workers | jq'
```

Each worker row carries `status` (`idle` / `busy` / `offline`),
`last_seen` (unix ts), `total_jobs`, and earnings. A worker that's been
silent for more than `WORKER_TIMEOUT_SECONDS` (default 15s) is marked
offline by the coordinator on the next read.

### Rotate the admin token
Documented in `infra/README.md:147-156`. Summary:

```bash
ssh root@ai.dallinlayton.com
sed -i "s|^API_TOKEN=.*|API_TOKEN=$(openssl rand -hex 32)|" /opt/gamerai/.env.prod
/opt/gamerai/infra/deploy.sh   # or docker compose ... up -d
```

The admin row is re-seeded by `ensure_admin_seed()` on next startup —
the existing admin member's `token_hash` is updated to the new value,
so the old token stops working immediately.

### Turn auth off temporarily (do NOT do this on prod)
Documented in `infra/README.md:137-142`. Sets `API_TOKEN=` empty and
restarts. This opens `/generate` to the public internet — only useful
behind a firewall or when the box is briefly off-DNS for debugging.

---

## 6.5. Image generation operations

The MVP ships chat + image. Image-side has its own download surface
and its own failure modes worth knowing.

### What lives where

| Artifact | Path | Owner | When it changes |
|---|---|---|---|
| `sd.exe` + `stable-diffusion.dll` | `/var/www/downloads-chroot/uploads/{sd.exe,stable-diffusion.dll}` | `infra/setup-image-mirror.sh` | Only when bumping the pinned sd.cpp version |
| `sd-models/sd1.5.gguf` (~1.5 GB) | `/var/www/downloads-chroot/uploads/sd-models/sd1.5.gguf` | `infra/setup-image-mirror.sh` | Only when swapping the default image model |
| Generated PNGs | `/opt/gamerai/data/images/<job_id>.png` on the VPS | Coordinator, on `/jobs/complete` | One per image job; never rotated yet |

The mirror script runs as a one-shot on the VPS — it's not part of
the docker compose deploy. Re-run it whenever you bump the pinned
sd.cpp release or rotate the model file.

### Bootstrap or refresh the mirror

```bash
ssh root@ai.dallinlayton.com
sudo /opt/gamerai/infra/setup-image-mirror.sh
```

Idempotent — re-running skips downloads when files are non-empty.
Set `FORCE=1` to redownload. Defaults source the SD 1.5 GGUF from
a local file path (`/home/beargroup/ai/models/sd/...`); override
`DEFAULT_MODEL_SRC=<vps-path>` or `DEFAULT_MODEL_URL=<https://...>`
to source elsewhere if you don't already have the file on the box.

### Diagnosing an image job that errors

1. **Check whether image workers are online.** `/dashboard` lists
   per-worker advertised tools; `/workers` API returns the same as
   JSON. Look for `"tools":["chat","image"]` on at least one row.
2. **Have the contributor run `agent.exe --diagnose`** and paste
   the report. The "Image generation (sd.cpp)" section confirms
   `sd.exe`, `stable-diffusion.dll`, and `sd1.5.gguf` are all on
   their machine.
3. **If sd.exe rejects a CLI flag** (e.g. `error: invalid mode
   txt2img`), that's an agent-code/sd.cpp-version mismatch. The
   agent encodes the flag set for the pinned sd.cpp release; a
   future mirror bump needs `run_image_inference`'s argv updated
   too. See the comment block above that function for the
   coupling note.

### `/data/images` rotation

There is none yet. PNGs accumulate forever, ~1.5 MB each at the
default 512×512. At 100 images/day that's ~50 GB/year. Worth
adding a nightly job to drop images older than 90 days (or
archive to S3) once the network has enough volume to feel it.
Until then: `du -sh /opt/gamerai/data/images/` is the canary.

---

## 7. Things deferred (intentionally not in this doc / repo yet)

- **"+ Invite a friend" button on `/dashboard`.** Form: email, daily-quota
  slider, expiry. Calls `POST /invites`, displays the URL.
  ~half a day of work; we'll add it when the curl flow gets annoying.
- **HTTP endpoint for member revocation.** Wrap `db.revoke_member_by_token_hash`
  in a `POST /admin/members/<id>/revoke`. Small.
- **Google OAuth.** Design sketch in §8.

---

## 8. Google OAuth — design sketch (not implemented)

This is here so the next time someone says "let's add Google sign-in"
nobody redoes the discovery. Three changes from the current state:

### 8a. Session abstraction
Today, the `gai_session` cookie value **is** the raw bearer token
(`client/services/session.py:21-65`). Every page load re-validates it via
`GET /me` on the coordinator. To support OAuth, the cookie value becomes
an opaque session id (random 32 bytes) and a server-side
`sessions(session_id, member_id, created_at, expires_at, auth_method)`
table maps it to a member. Bearer-token logins also write a row there.
`_session_bearer()` becomes `_session_member()` and returns the resolved
member.

### 8b. OAuth callback
Two new routes on the client:
- `GET /oauth/google/start` — issues a CSRF `state` cookie, redirects to
  Google's consent screen.
- `GET /oauth/google/callback` — exchanges `code` for an id_token,
  verifies signature + audience, looks up or creates the `members` row
  (matching on `google_sub`), writes a `sessions` row, sets the cookie.

Use `google-auth-oauthlib` — small, vetted, already in pip.

### 8c. Member linkage
New columns on `members`: `auth_method TEXT NOT NULL DEFAULT 'bearer'`,
`google_sub TEXT`. Unique index on `(auth_method, google_sub)` so two
Google sign-ins for the same Google account collapse to one member row.

### Open decision when this lands
Does the existing invite flow keep working, or do invites become
Google-only? Recommended: **keep both.** Invites still mint a bearer
token (back-compat for the Windows agent + power users); the `/login`
page grows a "Sign in with Google" button as a parallel option. Lets
us A/B which flow people actually use.

### Explicitly out of scope even when implementation lands
- OIDC discovery for arbitrary IdPs.
- SAML.
- Magic-link email auth.
