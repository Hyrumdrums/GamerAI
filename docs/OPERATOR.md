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
   excerpt, and a form that collects **username + email + password +
   confirm + ToS checkbox** (template: `client/templates/redeem.html.j2`).
3. They fill it in and click **Create my account**. The web layer posts
   to the coordinator's `POST /invites/{code}/accept`, which atomically
   mints a new `members` row (username, argon2 password hash, fresh
   bearer token) and returns the token to the web layer.
4. The web layer drops the token in the `gai_session` HttpOnly cookie
   and 303-redirects to `/`. The token is never shown — friends should
   never have to copy-paste.
5. From then on the tester signs in at `/login` with their username +
   password. The session cookie carries the bearer for every request.

Walk a tester through it once before launching at a wider group. If they
forget their password, see the recovery note in `docs/auth-design.md` —
host-can-reset-link is deliberately deferred until an email service is
wired up.

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
| `sd-models/dreamshaperXL-lightning.gguf` (~5 GB) | `/var/www/downloads-chroot/uploads/sd-models/dreamshaperXL-lightning.gguf` | `infra/setup-image-mirror.sh` | Current default; ship a new GGUF here to swap |
| `sd-models/dreamshaper8.gguf` (~0.9 GB) | `/var/www/downloads-chroot/uploads/sd-models/dreamshaper8.gguf` | `infra/setup-image-mirror.sh` | Kept as a legacy 4 GB-tier fallback; do not delete |
| `sd-models/sd1.5.gguf` (~1.5 GB) | `/var/www/downloads-chroot/uploads/sd-models/sd1.5.gguf` | `infra/setup-image-mirror.sh` | Vanilla baseline kept for debugging |
| Generated PNGs | `/opt/gamerai/data/images/<job_id>.png` on the VPS | Coordinator, on `/jobs/complete` | One per image job; never rotated yet |

The mirror script runs as a one-shot on the VPS — it's not part of
the docker compose deploy. Re-run it whenever you bump the pinned
sd.cpp release or add/rotate a model file.

### Bootstrap or refresh the mirror

```bash
ssh root@ai.dallinlayton.com
sudo /opt/gamerai/infra/setup-image-mirror.sh
```

Idempotent — re-running skips downloads when files are non-empty.
Set `FORCE=1` to redownload. Per-model source files live under
`/home/beargroup/ai/models/sd/` on the VPS; the script copies them
into the mirror and writes a sidecar JSON (`<slug>.json`) carrying
the per-model defaults (steps, cfg scale, sampler, native resolution).
If a model's source file is missing, the script warns and skips that
entry — safe to commit a catalog row before staging the file.

### Rolling out the SDXL Lightning default

Default since v1.1.27. `coordinator/model_registry.DEFAULT_IMAGE_MODEL`
points at `dreamshaperXL-lightning`, and the agent's `DEFAULTS` dict
in `windows-agent/agent.py` ships the same slug. Existing
contributors auto-migrate on their next agent restart: the bootstrap
notices `<slug>.gguf` is missing, downloads it, and
`_sweep_stale_image_models` deletes the previous model GGUF/sidecar
from disk so each agent only carries one model at a time. The VPS
mirror keeps every entry — `dreamshaper8` stays online as a fallback
GGUF a contributor can pin via `bootstrap.image_model` in their
local `config.json` if their card is below the 6 GB tier.

Deploying a new GGUF on the VPS:

```bash
# 1. Stage the GGUF on the box (the catalog row's <local-src>
#    column tells you where it expects to find each one):
scp dreamshaperXL_lightning-Q4_K_M.gguf \
    root@ai.dallinlayton.com:/home/beargroup/ai/models/sd/

# 2. Pull the matching coordinator code so the agent default,
#    registry, and seeder all reference the same slug:
ssh root@ai.dallinlayton.com 'cd /opt/gamerai && git pull && \
  docker compose up -d --build coordinator'

# 3. Re-run the mirror seeder. It copies the new GGUF in,
#    writes the sidecar JSON (steps=6, cfg=1.5, sampler=euler,
#    1024×1024), and leaves the prior models in place:
ssh root@ai.dallinlayton.com 'sudo /opt/gamerai/infra/setup-image-mirror.sh'

# 4. Verify from a client:
curl -I https://ai.dallinlayton.com/download/sd-models/dreamshaperXL-lightning.gguf
curl -I https://ai.dallinlayton.com/download/sd-models/dreamshaperXL-lightning.json
```

Sourcing the GGUF: SDXL Lightning is widely converted to GGUF on
Hugging Face (e.g. `city96/DreamShaper-XL-Lightning-GGUF` or any
equivalent Q4_K_M Lightning fine-tune). Confirm the magic bytes
read `GGUF`; the seeder rejects mis-typed files automatically.

Rollback: bump `DEFAULT_IMAGE_MODEL` back to `dreamshaper8` in
`coordinator/model_registry.py` and the matching `DEFAULTS` slug in
`windows-agent/agent.py`, redeploy. Contributors' next agent restart
re-downloads the old GGUF from the mirror — no manual cleanup
needed.

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

There is none yet. PNGs accumulate forever; the SDXL Lightning
default emits ~2.5–3 MB PNGs at 1024×1024 (vs ~1.5 MB at the legacy
512×512). At 100 images/day that's ~100 GB/year. Worth
adding a nightly job to drop images older than 90 days (or
archive to S3) once the network has enough volume to feel it.
Until then: `du -sh /opt/gamerai/data/images/` is the canary.

---

## 6.55 Contributor download mirror — keep it seeded

Three scripts in `infra/` keep `/var/www/downloads/` populated with
the binaries every Windows agent pulls during its first-run
bootstrap chain:

| script                       | what it seeds                                   | size  |
| ---------------------------- | ----------------------------------------------- | ----- |
| `setup-mirror.sh`            | Ollama installer + default llama3.2:1b GGUF     | ~2 GB |
| `setup-image-mirror.sh`      | sd.exe + DLLs + default SDXL Lightning GGUF     | ~6 GB |
| `setup-tts-mirror.sh`        | Piper runtime + default voice ONNX              | ~100 MB |

**`bootstrap.sh` does NOT run any of these automatically** (deliberate
— ~8 GB bandwidth per VPS is steep and the binaries occasionally
pin to specific upstream versions). After every fresh bootstrap,
the operator must run all three.

What goes wrong if you skip one: the contributor agent's bootstrap
chain 404s on the missing files, falls back to `mock` for that tool,
and registers with capabilities that exclude it. Specifically:

- **Skip `setup-mirror.sh`** (the one we hit in prod on 2026-05-26):
  Ollama installer 404s → no chat model → agent registers
  `tools=["image"]` or `tools=["image", "tts"]` only → coordinator
  routes chat jobs to other workers, OR (pre-`d8eedd7`) the partial
  agent claims chat jobs from the queue and answers `[mock] ...`.
- **Skip `setup-image-mirror.sh`**: sd.exe 404s → image bootstrap
  fails → loud "PARTIAL CONTRIBUTOR" banner in the agent log (the
  user-facing direction we already documented) → no image jobs.
- **Skip `setup-tts-mirror.sh`**: voice ONNX 404s → no TTS → voice
  feature degraded for invitees of that contributor.

### Verifying the mirror after seeding

```bash
DOMAIN=ai.dallinlayton.com
for p in \
  /download/ollama-setup.exe \
  /download/models/llama3.2-1b.gguf \
  /download/sd.exe \
  /download/sd-models/dreamshaperXL-lightning.gguf \
  /download/piper-runtime.zip \
  /download/tts-voices/piper__en_us-libritts-high.onnx; do
  printf "%-65s %s\n" "$p" "$(curl -sI "https://$DOMAIN$p" | head -1)"
done
```

All six should print `HTTP/2 200`. If any 404s, run the matching
`setup-*-mirror.sh` script again.

### Refreshing a single asset after a model bump

Each script reads `FORCE=1` to redownload existing files. E.g. to
bump the chat model after editing `setup-mirror.sh`'s
`MODEL_GGUF_URL`:

```bash
sudo MODEL_SLUG=llama3.2-3b FORCE=1 \
  bash /opt/gamerai/infra/setup-mirror.sh
```

---

## 6.6 PWA + Push notifications

Since `pwa-refactor.txt` Phase 3/4/6, the web client is an installable
Progressive Web App with an offline shell and Web Push delivery on
image / TTS job completion.

### What an installed user gets
- App icon on their home screen (manifest at `/static/manifest.webmanifest`).
- App-shell renders offline (`client/static/sw.js` precaches everything
  needed and falls back to `/offline` for navigations the cache misses).
- Push notification when an image / TTS job they submitted completes
  (in-app `notifications` row always; browser push only if they
  granted permission AND VAPID is configured on the coordinator).

### Where the VAPID keypair lives on prod

```
/opt/gamerai/.secrets/                   chmod 700, gitignored
├── vapid_private.pem                chmod 600 — never read by hand
├── vapid_public.pem                 PEM form of the public key
└── application_server_key.txt       base64url public key (= VAPID_PUBLIC_KEY env)
```

`docker-compose.yml` bind-mounts `./.secrets` → `/secrets:ro` inside
the coordinator. `.env.prod` carries:

```
VAPID_PUBLIC_KEY=<copied from application_server_key.txt>
VAPID_PRIVATE_KEY_PATH=/secrets/vapid_private.pem  (container path)
VAPID_SUBJECT=mailto:hyrumdrums@gmail.com
```

`infra/bootstrap.sh` generates this whole tree automatically on first
run, so a brand-new VPS bootstraps with push delivery live. Existing
servers that pre-date Phase 6 need a one-time backfill — same recipe
as bootstrap.sh §6b, or see `infra/README.md` "Push notifications +
VAPID" for a copy-paste.

### Verifying push is armed

```bash
curl https://ai.dallinlayton.com/api/notifications/vapid-key
# expected: {"key":"BX..."} (~88 base64url chars)
# {"key":null} means push is disabled — check coordinator logs:
ssh root@ai.dallinlayton.com 'docker logs gamerai-coordinator 2>&1 | grep -i vapid'
```

### Rotating the keypair

**This invalidates every existing browser subscription** — installed
PWAs all lose push until each user re-subscribes from the banner.
Only do this if the private key has leaked, or you're intentionally
cycling. Full recipe in `infra/README.md` "Push notifications +
VAPID → Rotating the keypair".

### Notification triggers (what fires a push)

Today, two events:
- `image_done` — `coordinator/main.py:/jobs/complete` fires this after
  a successful image job, with the submitter as the recipient.
- `voice_done` — same handler, for TTS jobs.

Chat completions deliberately don't push (they stream visibly).
Members can opt out per category via `PUT /notifications/preferences`.

### Service worker versioning

`client/static/sw.js` has a `CACHE_VERSION` constant (currently
`'v2'`). **Bump it on any deploy that changes a precached asset**
(any CSS, vendored JS, or `client/static/js/*.js` change). Without
the bump, returning users keep serving the old cached assets even
after the new SW installs. `git diff origin/main -- 'client/static/**'`
is the canary — if any file in there changed, bump the version.

The new SW takes over on the next page load via `skipWaiting()` +
`clients.claim()`. The old shell cache is dropped in the `activate`
handler.

### Per-user data

Push subscriptions and notification preferences are member-scoped:

| table                       | what                                                |
| --------------------------- | --------------------------------------------------- |
| `push_subscriptions`        | one row per (member_id, browser/device endpoint)    |
| `notifications`             | persistent in-app row for every send (read/pushed)  |
| `notification_preferences`  | per-category opt-out (default = enabled)            |

Schema in `coordinator/db.py` `_SCHEMA`; CRUD methods in the same
file; endpoints in `coordinator/notifications.py`.

---

## 6.7 Working with Claude — the commit → push → deploy loop

This repo is solo-maintained. The standing workflow Claude (or any
agent operating on Dallin's behalf) is authorized to run end-to-end,
without per-step approval, once a change is finished and on `main`:

1. **Commit.** Stage specific files (not `git add -A`). HEREDOC body.
   Trailer:
   ```
   Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
   ```
   New commit per logical unit — never `--amend` a pushed commit.
2. **Push to `origin main`.** No PR for solo work.
3. **Watch CI.** `gh run watch <run_id> --exit-status`. Block on
   green. If red, fix the root cause and recommit before deploying —
   never `--no-verify` past a failing hook.
4. **Deploy.** From the laptop:
   ```bash
   ssh -i ~/.ssh/id_ed25519_gamerai root@ai.dallinlayton.com \
     'cd /opt/gamerai && bash infra/deploy.sh'
   ```
   The script does `git pull --ff-only` → `docker compose ... up -d
   --build` → restart `gamerai-caddy`. Allow ~5 min for cold image
   builds.
5. **Smoke-check** the touched surface area:
   ```bash
   curl -fsS -o /dev/null -w "%{http_code}\n" https://ai.dallinlayton.com/
   ```
   A 303 on `/` is expected (login redirect for anonymous curl).
   Add the specific routes/assets the change touched (e.g.
   `/sw.js`, `/static/js/<new_module>.js`, `/api/notifications/vapid-key`).

Skip steps 4–5 when the change is docs-only (`*.md`,
`pwa-refactor.txt`, `todo.txt`) — those are read from the repo, not
the VPS.

Force-pushes to `main`, secret rotation, and prod data mutation are
**not** covered by the standing authorization — get a thumbs-up
before each of those.

---

## 6.8 Agent update signing

The Windows agent auto-updates itself (checks `/download/version.txt`
every ~6h, downloads `/download/agent.exe`, swaps, relaunches — no user
prompt). Without signing, anyone who can write to the download host (or
the CI upload key) could push arbitrary code to every contributor PC.
The agent verifies an **Ed25519 signature** of `agent.exe` before
swapping, with the private key held only as a CI secret — so owning the
VPS is not enough to forge an update.

**State of play.** Signing is *armed* in code but *off* until you
provision keys: `UPDATE_PUBLIC_KEY` in `windows-agent/agent.py` is empty,
so agents fall back to the SHA-256 sidecar (integrity, not authenticity)
and log a warning each update. Turn it on once:

```bash
# 1. Generate the keypair locally (NEVER on the VPS).
python tools/gen_agent_signing_key.py

# 2. Paste the printed PUBLIC key into windows-agent/agent.py:
#       UPDATE_PUBLIC_KEY = "<base64 public key>"

# 3. Add the printed PRIVATE seed as a GitHub Actions repo secret:
#       Settings → Secrets and variables → Actions → New repository secret
#       Name:  AGENT_SIGNING_KEY
#       Value: <base64 private seed>
#    Also stash it in your password manager. Do NOT commit it; do NOT
#    copy it to the VPS.

# 4. Commit the agent.py change and push. CI signs agent.exe, publishes
#    agent.exe.sig, and bumps version.txt.
```

**Ordering matters / the interlock.** The build workflow *refuses to
build* if `UPDATE_PUBLIC_KEY` is set but the `AGENT_SIGNING_KEY` secret
is missing — otherwise it would ship an agent that fail-closed-refuses
every future update. So add the secret (step 3) **before or with** the
commit that embeds the key (step 2). If you want a staged rollout, you
can add the secret first and ship signatures while `UPDATE_PUBLIC_KEY`
is still empty (CI signs whenever the secret is present); flip
enforcement on in a later commit by filling in the key.

**Transition.** Agents on the current (unsigned) build update to the
first signed build over the normal SHA path. From that build on, they
enforce the signature. The first signed build must carry a valid
`agent.exe.sig` — guaranteed by CI once the secret is set.

**Rotation.** Generate a new pair, embed the new public key, update the
secret, and ship. Agents pick up the new key as part of that (SHA-or-old-
key-verified) update, then enforce the new key thereafter. Losing the
private seed means you can't publish further signed updates — recover by
rotating.

**Not covered (future).** This is publisher-key signing, not OS-level
Authenticode — SmartScreen/AV still see an unsigned PE. Authenticode is
the next step and needs a paid code-signing cert; it's complementary,
not a replacement, for the update-payload signature above.

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
