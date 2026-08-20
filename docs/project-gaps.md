# Project Gaps — High-Level Review

> Honest accounting of what's missing in GamerAI as of this branch.
> Organized by severity and category. Cross-references to the roadmap
> in the main README and the strategic research docs.

The MVP works end-to-end locally and has a one-command public deploy.
The gaps below are what stand between **"runs on my machine"** and
**"a stranger could submit a job, a different stranger's GPU could
process it, and money could change hands honestly."**

---

## Severity legend

- **🔴 Critical** — blocks any external user touching the system.
- **🟡 Important** — blocks real customers / real workers / real money.
- **🟢 Later** — improves quality, scale, or developer experience.

---

## 1. Security & Trust

### ~~🔴 No auth on the coordinator API~~ — done

Resolved. `shared/auth.py` is the single source of truth, gated by the
`API_TOKEN` env var. Set → bearer required on every request except
`/health`; unset → no-op (default for local dev and tests). All
clients (worker, Windows agent, web UI, CLI) automatically include the
header when the token is set. The bootstrap generates a random token at
deploy time. See `infra/README.md` for the runbook.

### 🟢 No membership identity / tier accounting — identity + invites done

Slices 1 & 2 (2026-05-11) shipped the identity + invite layers:

- `members` table with role / parent / token_hash / tier /
  daily_quota_tokens.
- `member_usage` daily rollup + **daily-quota enforcement** on
  `/generate` (429 when over cap).
- `jobs.submitted_by_member_id` attribution.
- Per-token bearer auth (`coordinator/member_auth.py` + middleware in
  `coordinator/main.py`).
- `invites` table with atomic accept (single BEGIN IMMEDIATE that
  validates + inserts the member + stamps the invite).
- `POST /invites`, `GET /invites`, `GET /invites/<code>` (public),
  `POST /invites/<code>/accept` (public), `POST /invites/<code>/revoke`
  (admin).
- `GET /admin/members` admin-only roster.
- Admin CLI: `create-member`, `list-members`, `revoke`,
  `create-invite`, `list-invites`, `revoke-invite`.
- Public redemption page at `/invite/<code>` in `client/web.py` plus
  `/admin/members` and `/admin/invites` admin browser views.
- `GET /me` returns identity + today's usage.
- Backwards compatible: existing `API_TOKEN` clients are auto-seeded
  as the admin member.

Severity downgraded 🔴 → 🟢. The system can distinguish callers,
gate consumption per cap, and onboard new invitees via copy-paste URL
without admin involvement. Remaining work is not blocking external
membership testing — it's polish:

- **Worker → member link.** A contributor's `worker_id` is still
  unconnected to their `member_id`. Earnings credit lives on
  `worker_id` only. Add `owner_member_id` on `/register` to
  consolidate contributor earnings per person, not per GPU. ~½ day.
- **Tier auto-promotion engine.** Everyone stays BRONZE unless
  bumped by hand. Daily cron driven by uptime + claim rate. ~1 day.
- **Per-member auth on the web UI itself.** `client/web.py` talks
  to the coordinator as admin for every viewer; Alice would see
  admin data. Add cookie-session login against member tokens.
- **Caddy basic_auth on the web UI** — required before opening
  the web UI beyond an SSH tunnel.
- **SMTP-delivered invites** (Resend / Postmark) — copy-paste URL
  is the slice-2 cut, deferred until first usability complaint.

### 🔴 No prompt safety / content controls

A customer can submit anything. A worker is asked to compute it. Both
sides could be unhappy: customers could be denied service for valid
prompts a worker refuses; workers could be asked to generate content
that violates their local laws or comfort.

**Fix:** worker-side `block_categories` config, coordinator-side
prompt-class tagging, opt-in pools per category. Same model as the
privacy tier in Phase 5.

### 🟡 No worker output verification

The worker is trusted to report honest output and honest token counts.
A malicious worker could return garbage and earn money for it.

**Fix:** Phase 3 roadmap item; quickest win is k-of-n consensus on a
random sample of jobs (run 5% of jobs on two workers, compare).

### 🔴 Open security findings (2026-05-13 review) — next up

A focused security pass surfaced three real, exploitable issues plus
three high-severity items worth fixing this week. Listed here so we
don't lose track; the **🔴 critical** items are the immediate next
slice.

**🔴 1. Canary detection leak.** `/jobs/next` and the queue-pushed
job envelope include `submitted_by_member_id`. Canaries have this
`null` (no human submitter). A worker can read its own incoming
jobs, spot the null submitter, recognize it's a canary, pass that
one cleanly, and cheat on real prompts. Defeats the integrity
slice we just shipped. **Fix:** drop the field from the
worker-facing payload. ~15 min.

**🔴 2. Worker→member link missing.** `/jobs/complete`,
`/jobs/claim`, `/jobs/abandon`, `/heartbeat` accept any `worker_id`
in the request body. Any authenticated member can spoof completions
against another worker's `worker_id`, attributing earnings to
someone else or returning bogus responses to other members'
prompts. **Fix:** stamp `owner_member_id` on the `workers` table at
`/register` time; reject calls where the authenticated member
doesn't own the `worker_id` they reference. ~½ day. (Already noted
elsewhere in this doc as polish; review elevated it to immediate.)

**🔴 3. Markdown XSS surface in the chat UI.** Assistant responses
go through `marked.parse()` with raw HTML passthrough. A contributor
running a tampered model could emit `<img src=x onerror=...>` and
execute arbitrary JS in the user's browser. Canaries don't catch
this (they check required tokens are present, not that HTML is
absent). **Fix:** sanitize the marked output (DOMPurify, or
escape-HTML option). ~30 min.

**🟡 4. CDN scripts without Subresource Integrity.**
`<script src="https://cdn.jsdelivr.net/.../marked.min.js">` has no
`integrity=` attribute on the chat UI or the ToS page. CDN compromise
→ arbitrary JS injected into authenticated pages. **Fix:** pin a
version + add SRI hash, or self-host `marked.min.js` from
`/static/`. ~30 min.

**🟡 5. No prompt length cap on `/generate`.** A single request can
submit an unboundedly large prompt; gets stored in SQLite and
shoveled to a worker. Cheap DoS. **Fix:** `MAX_PROMPT_BYTES` env
guard. ~15 min.

**🟡 6. No rate limit on `/login`.** 256-bit tokens make brute force
infeasible in practice, but the principle is wrong and a real
pentest will flag it. **Fix:** enable `RATE_LIMIT_PER_MIN` in
production, or hook `/login` specifically. ~30 min.

Plus the medium items from the review (cookie value IS the bearer,
agent state.json plaintext, no admin audit log, no token rotation
policy) — flagged but accepted for now, see devlog for the full
review.

### ~~🟡 Web UI dashboard has no auth~~ — done

Resolved by the 2026-05-12 browser-auth slice. `client/web.py` now
gates `/`, `/dashboard`, `/admin/*`, and `/api/*` on a session
cookie (`gai_session`) seeded by `/login`. Admin pages additionally
require `role=admin` from the session's bearer. Public surface
(`/invite/<code>`, `/tos`) stays open. Caddy now forwards all of the
above to the web UI on the public domain — the dashboard is no
longer SSH-tunnel-only.

### ~~🟡 No customer chat UI / persistent conversations~~ — done

Resolved by the 2026-05-12 chat-UI rewrite. `INDEX_HTML` is now a
two-column layout (sidebar of past conversations, message pane,
auto-growing composer). Multi-turn context handled via the
`conversation_id` on `/generate`; coordinator concatenates prior
turns server-side and auto-appends new turns to the conversation on
`/jobs/complete`. Markdown rendering for assistant messages via
`marked.js`. Remaining ChatGPT-style polish (streaming, stop button,
mobile responsive, multi-model picker) is on the followups list.

### 🟡 Workers can read every job

Every worker connects to the same Redis queue (`BLPOP`) and sees all
prompts in the queue header at minimum. There is no per-job ACL or
encryption-at-rest in the queue.

**Fix:** route through the coordinator (`/jobs/next` API) instead of
direct Redis access. Already partially done — the worker has
`/jobs/claim` and `/jobs/complete` calls but still pops from Redis
directly. Tighten this loop.

### 🟢 No TLS between worker and Redis

Local-only today. Once we have remote workers, the Caddy+coordinator
hop is TLS but the worker→Redis path is not. Worth fixing once Redis
is no longer co-resident with the coordinator (Phase 2b ElastiCache).

### 🟡 Secrets stored as plaintext in `.env.prod`

The `API_TOKEN` admin bearer and any future per-service secrets
live as plain values in `/opt/gamerai/.env.prod` on the VPS, file-
permissioned to root. Adequate for a single-machine MVP with one
operator; doesn't scale to a Phase 2b AWS deploy where multiple
services + auto-scaling need shared secret access.

**Fix when Phase 2b lands:** AWS Parameter Store (or Secrets
Manager — Parameter Store is cheaper and sufficient for this use
case). IAM-scoped read access from each service's task role; rotation
becomes `aws ssm put-parameter --overwrite` + a service restart.
Document the rotation runbook in `OPERATOR.md` when this ships.

Also consider: per-component secrets (the worker shouldn't need
the admin bearer; it has its own per-worker token) once the
worker-identity migration happens.

### 🟡 VPS holds an outbound deploy key to GitHub

The production VPS has `/root/.ssh/github_deploy` — a private key that
authenticates to `git@github.com:Hyrumdrums/GamerAI.git` for the
`/opt/gamerai` checkout. `infra/deploy.sh` (and ad-hoc `git pull` on
the box) use this key to bring in new source. Standard self-hosted
deploy pattern, but inconsistent with the *safer* pattern we already
use for `agent.exe` — that one is CI-built and SFTP-pushed to a
chrooted `gamerai-uploads` user that can only write into
`/var/www/downloads-chroot/uploads`. The VPS has zero outbound
credentials in the agent.exe path; we should aim for the same in the
container-deploy path.

Blast radius today depends on the GitHub-side configuration:
- If `github_deploy` is registered as a per-repo **Deploy Key** with
  "Allow write access" UNCHECKED → VPS compromise leaks a private-repo
  clone (bad, not catastrophic).
- If it's a personal user SSH key OR a write-enabled deploy key →
  VPS compromise = push to repo = supply-chain compromise of every
  downstream contributor's `agent.exe`. Verify which it is.

**Fix when container registry lands:** Build coordinator + worker
images in CI, push to GHCR (or similar). `deploy.sh` becomes a
`docker compose pull && docker compose up -d` — no git checkout on the
VPS, no key on the VPS. Static mirror assets (GGUFs, sd.exe, ollama
installer) follow the same SFTP-chroot path that agent.exe uses today.
End state: VPS has no outbound credentials to any source-of-truth
system. Discussed 2026-05-22 during the DreamShaper mirror rollout.

### 🟡 No push-to-deploy for the coordinator — manual SSH step

Today the windows-agent half of a release auto-ships (CI builds the
exe + SFTPs it to the VPS download chroot), but the coordinator / UI
half doesn't. After pushing main, *someone* still has to SSH in and
run `sudo /opt/gamerai/infra/deploy.sh`. The current workflow has
Claude do the SSH step from the user's workstation — quick, dirty,
works — but the *control plane* for that deploy lives on a workstation
SSH key (`id_ed25519_gamerai`), which means there's no audit trail in
GitHub for "what code is currently running in prod" and a stale
workstation key would silently keep working.

**Fix when we wire it up:** A GitHub Action on push-to-main that
either (a) `docker compose pull`s from GHCR (paired with the GHCR
migration in the gap above), or (b) SFTPs a tarball of the build into
the same chrooted upload path agent.exe uses, then triggers a small
`pull-and-restart` script that's already on disk. Either way: prod
holds **zero** outbound credentials to GitHub. GH Actions is the only
thing that touches the VPS; the deploy key on `/root/.ssh/` goes
away; deploy attempts show up in the Actions log instead of bash
history on whichever workstation typed the command. Pairs naturally
with the GHCR migration above — same direction, just covers the
*invocation* side as well as the *source* side.

### 🟢 No persistent client-side cache for *conversation history* (offline read)

The chat UI keeps an in-memory `Map` of loaded conversation messages
so switching back and forth between threads in the same tab is
instant. **Page refresh wipes it** — every new tab pays one
`/api/conversations` + one `/api/conversations/<id>` round trip
before any text appears.

**Status (2026-05-25):** PWA shell-cache is now LIVE — see
`pwa-refactor.txt` Phases 3 + 4. The service worker
(`client/static/sw.js`) precaches all CSS / JS / icons / fonts /
the offline fallback at install; navigations to uncached pages
render `/offline` when the network is down. So the **app shell**
opens offline. What still doesn't is the *conversation data* —
nothing's in IndexedDB yet, so an offline user opening the app
sees the shell but no past messages.

Two things still missing:

- **Reload-resilience.** A slow coordinator still leaves history
  blank on first paint (the shell loads from cache; the
  `/api/conversations` round-trip is still gated on a live
  network).
- **Offline read of past conversations.** Past prompts/responses
  aren't cached anywhere durable.

**Fix candidates (in order of cost):**

1. `localStorage` (per-conversation cache, plaintext). ~½ day. Cheap
   and easy. Major issue: it stores prompts + responses in plaintext
   in the user's browser. That's *fine* against the threats the
   user already knows about ("admins might read my DB"), but it
   **breaks the symmetry** with the Phase 3b.iii encrypted-history
   plan: we'd be claiming server-side encryption while the same
   data sits unencrypted on disk a few clicks away. Skip until
   we've decided we don't care, or until #2 ships.
2. IndexedDB + client-side decryption pairing with Phase 3b.iii.
   When the encryption work derives a key from the bearer for
   server-side ciphertext, the same key works for IndexedDB write.
   The user's bearer is the cryptographic gate everywhere. ~2 days
   on top of the encryption work. **Likely paired with
   `pwa-refactor.txt` Phase 5** (queued sends via Background Sync) —
   both need IndexedDB and the queue half is partially blocked on
   the data-cache decision.
3. ~~PWA shell~~ — **DONE** (Phases 3+4, commits `101449b`
   `c0ca395`).

Recommend: bundle #1 and #2 together, ship neither until Phase
3b.iii lands so the privacy story stays consistent. Decision
captured here so the next time someone asks "why don't we cache?"
we don't accidentally invent the wrong shortcut.

### 🟡 Stored prompt history is plaintext at rest (Phase 3b.iii)

`jobs.prompt` (and `jobs.result`) in the SQLite ledger are stored as
plaintext today. A user's full conversation history is recoverable by
anyone who can read the DB file: a coordinator-host compromise, a
backup leak, or an admin doing curiosity reads. The truncation
alternative would break the ChatGPT-style "see your past
conversations" feature, which is load-bearing for a chat suite.

**Fix:** encrypt prompts + responses at rest with a key derived from
the user's bearer token, never stored.

```
On submit:
  k = HKDF(bearer_token, salt="prompt-encryption-v1")
  ciphertext, iv = AES-GCM-encrypt(k, prompt)
  → jobs.prompt_ciphertext + jobs.prompt_iv,  jobs.prompt = NULL

On read:
  bearer = Authorization header (already required)
  k = HKDF(bearer, ...)
  plaintext = AES-GCM-decrypt(k, ciphertext, iv)
```

A DB dump reveals AES-GCM ciphertext, not prompts. Same pattern
Bitwarden / 1Password use for vaults. **Three known costs:**

- **The worker still sees plaintext during inference** (it has to —
  the model can't run on ciphertext). This protects stored history,
  not in-flight prompts. Real in-flight confidentiality is the
  Phase 5 client-side embedding tier.
- **Bearer rotation breaks history.** Mitigation: store a wrapped
  per-user vault key that is itself AES-encrypted under HKDF(bearer);
  on rotation, re-wrap with the new bearer. One small extra table.
- **Admin can't grep prompts** even for moderation. That's a feature
  under the ToS framing — admins should not be able to read prompts —
  but it means abuse triage needs user-reported flags that include
  the decrypted copy.

Scope estimate: ~2–3 days. Slotted into **Phase 3b.iii (trust &
verification)** alongside the canary system and (eventually) k-of-n
consensus. Not blocking near-term recruitment because the founder is
the only admin and the trust circle is friends-only; lands before
opening the network to strangers at scale.

---

## 2. Reliability & Ops

### 🔴 No backups, no DR

`infra/README.md` says "scp the SQLite file." That's not a backup
strategy, that's a memo. A reboot in the middle of a write window
could corrupt the ledger.

**Fix:** add a nightly cron in the prod compose that runs
`sqlite3 .backup` to a timestamped file; rotate weekly; optional
off-host upload (S3 / B2 / Hetzner Storage Box).

### 🟡 No monitoring / alerting

`/metrics` exists. Nothing scrapes it. We won't know the coordinator
is down unless we look.

**Fix:** simplest: Uptime Kuma in the prod compose, hits `/health`
every 30s. Better: Grafana Cloud free tier scraping `/metrics`. Best:
add OTLP exporter and ship to a hosted observability stack.

### ~~🟡 No rate limiting~~ — done

Resolved. `coordinator/rate_limit.py` implements a per-IP fixed-window
limiter, controlled by `RATE_LIMIT_PER_MIN` (0 / unset = disabled).
Plumbed into the coordinator middleware after auth; honors
`X-Forwarded-For` from Caddy.

### ~~🟡 No idempotency on `/generate`~~ — done

Resolved. Customers can pass an `Idempotency-Key` header on `/generate`;
the same key returns the same `job_id` for 24h (configurable via
`IDEMPOTENCY_TTL_SECONDS`). Implemented in `coordinator/idempotency.py`.
No-op when no header is sent.

### 🟡 Single point of failure

One VPS holds Redis, the coordinator, the SQLite ledger, and Caddy.
Any of: kernel update, disk full, OOM kill, unattended-upgrade reboot
mid-job — takes the whole system down.

**Fix:** acceptable for MVP test. Documented in the graduation
criteria; resolved by Phase 2b AWS multi-AZ.

### 🟡 A 503 silently destroys the user's message — no retry, no memory of what was asked

Observed live (2026-08-20) during the canary-backlog incident above, but
this recurs on *any* transient "no worker available" gap, not just that
one. Transcript:

```
user:      tallest building in the world?
assistant: No community members are available right now. Please try again in a few minutes.
user:      now?
assistant: No community members are available right now. Please try again in a few minutes.
user:      now?
assistant: I'm available now. What can I help you with?
user:      answer my question
assistant: You didn't ask a question. This conversation just started. What's on your mind?
```

The original question never comes back. What's actually happening,
traced through `client/static/js/chat.js` and `coordinator/main.py`:

1. **The composer clears on submit, not on success.** `textarea.value = ''`
   runs immediately after the optimistic user-bubble is appended
   (`chat.js` submit handler), before the `/api/generate` fetch even
   fires. The instant "Send" is pressed, the original prompt text is
   gone from the input — there's nothing to recover it from even if the
   user wanted to hit "retry."
2. **A 503 leaves zero trace, by design.** `_ensure_live_worker_or_503()`
   runs in `coordinator/main.py:generate()` *before* any `db.insert_job`
   or message-row write, specifically "so a 503 leaves no orphan
   job/message rows" (see the comment at `main.py:1822`). Reasonable for
   keeping the ledger clean, but it means the failed turn isn't
   recoverable from the server either — a page refresh mid-outage loses
   it completely, and even within the same tab there's no stored copy.
3. **No retry affordance on the error bubble.** The 503 handler
   (`chat.js` ~line 534-559) renders the error text into the assistant
   bubble and re-enables the composer — that's it. No "retry" button, no
   automatic resubmission. The `body` object with the original prompt is
   still sitting in the closure scope at that point in the code but
   nothing offers to resend it.

Net effect: the user's only path forward is to *remember and manually
retype* their actual question. In the transcript above they
(reasonably) typed a quick "now?" instead — which is contentless on its
own, and since the real question was never persisted anywhere, the
assistant has genuinely no way to know what "now?" refers to once a
worker comes online. The conversation looks broken/confused, but the
model never actually forgot anything — the question just never reached
it.

**Fix (client-side only, no backend design change needed):**
- Don't clear the textarea until the fetch succeeds; restore the exact
  prompt text into it on any error path (network failure and 503 both
  already fall into an existing `catch`/`!gr.ok` branch — just don't
  clobber the input).
- Give the error bubble a "retry" button that re-POSTs the same `body`
  already captured in the submit handler's closure — one tap, exact
  same prompt, no retyping. `chat.js`'s own comment at line ~352
  ("eventual error state can offer a retry button") shows this was
  anticipated for the mid-stream-interruption case; it just was never
  built for the submit-time 503 case.

**Heavier alternative (changes the backend contract, not recommended
first):** persist the user's turn before the live-worker check with a
terminal `status="failed_no_worker"` instead of skipping the write
entirely. Survives a refresh, but reverses the "no orphan rows on 503"
decision — the client-side fix above solves the observed symptom
without that trade-off.

Scope estimate: ~1-2 hours for the client-only fix.

### 🟢 No log retention strategy

Logs go to stdout / `docker logs`. Restarts wipe history. Useful for
debugging today, useless for compliance or analysis later.

**Fix:** ship to a hosted log store (Grafana Loki, Better Stack,
Axiom) when we add monitoring.

### 🟡 Coordinator logs contain raw user prompts

The search-rewrite debug instrumentation (added 2026-05-23 after the
Kevin O'Leary panic-recant bug needed an SSH+SQLite dump to
diagnose) logs the user's `original_prompt`, the classifier's
`rewrite_output`, the `parsed_value`, and the `final_query` — each
truncated to 200 chars but still containing arbitrary user input.
These hit `docker logs gamerai-coordinator` and are tailable by
anyone with root on the VPS (today: the dev workstation + Claude
when running deploy.sh via SSH).

What's exposed in plain text right now:
- The user's literal prompt for every search submission
- A short window of the LLM's rewrite output (also user-derived)
- The final search query sent to DDG / Bing / etc.

The chat-job paths log only metadata (`job_id`, tokens, durations),
not message bodies, so the exposure is specifically the search
debug instrumentation.

**Fix path, in order:**
1. Configure Docker log rotation on the VPS so logs don't grow
   unbounded — current default is "until restart," which leaks
   weeks of prompts in worst case. Add `daemon.json` settings:
   `log-driver: json-file`, `max-size: 10m`, `max-file: 3`. One-time
   change on the bootstrap script.
2. Add a `LOG_PROMPTS=false` env switch on the coordinator that
   demotes the search-rewrite debug fields to an opt-in. Default
   to true while we're actively debugging the small-model rewrite
   reliability, flip to false once the pipeline is stable.
3. When we ship to a hosted log store (see preceding gap), pick one
   with field-level redaction so we can keep the IDs but drop the
   prompt content from archived logs.

**Trigger to revisit:** any of: first external user lands; first
prompt that's medical / legal / financial / explicit in nature; ToS
clause about not storing prompt content (we don't have one yet —
see "🟡 Stored prompt history is plaintext at rest" above for the
related on-disk story).

### ~~🟢 No CI / CD~~ — done

Resolved. `.github/workflows/ci.yml` runs the unittest suite,
validates that `docker-compose.yml` and the prod overlay merge
cleanly, and syntax-checks the bash scripts on every push and PR.

### 🟡 Windows agent: stowaway-instance failure mode

Discovered during the 2026-05-22 v1.1.21 → v1.1.22 rollout: a
contributor's box ended up running **two** fully-initialized agent
processes simultaneously, with one (Instance A, tray-mode, hidden
console) bound to the IPC port and the other (Instance B, foreground,
visible console) operating as an unenforced duplicate. The duplicate
then propagated its no-tray state forward into the next auto-update
cycle, leaving v1.1.22 running in foreground mode with no tray icon
and no single-instance protection.

Three interacting causes — all in `windows-agent/agent.py`:

1. **TOCTOU race in `_claim_single_instance` (lines 278–320).** The
   `sock.bind()` + `sock.listen(4)` succeeds before the IPC accept loop
   gets wired up later in `main()`. A second process's `bind()` fails
   correctly, but its handshake probe hits a listening-but-not-accepting
   socket and times out, triggering the "surrender enforcement" branch
   at line 316. Both processes proceed as "first instance."

2. **Single-instance check only runs in tray mode** (gated on
   `tray_active` at line 3415). A foreground/args-less launch
   doesn't even try to bind, so it coexists silently with the tray
   instance. Should run unconditionally on Windows, with an explicit
   `--dev-multi` (or similar) opt-out for devs.

3. **No-tray inheritance through `update.bat`.** `relaunch_args =
   ["--tray"] if args.tray else []` at line 3694 means a foreground
   Instance B that processes the user's `update` command will
   relaunch the next version in foreground too, perpetuating the
   stowaway across updates.

And the deterministic trigger:

4. **`installer.iss` Start Menu shortcut has empty arguments.**
   Clicking "GamerAI Agent" from the Start Menu while the autostart
   instance is running spawns a fresh foreground Instance B every
   time. Should pass `--background` (or `--tray`) like the autostart
   shortcut does.

**Fix when this surfaces again:** Wire #4 first (one-line installer
change — fixes the visible footgun). Then #1 (move accept-loop into
`_claim_single_instance` before it returns, closing the handshake
window). Then #2 (run single-instance unconditionally on Windows).
#3 might just go away once #1 + #2 + #4 hold; if not, add an
install-path heuristic so update.bat reasserts `--tray` when the exe
lives under `%LOCALAPPDATA%\Programs\GamerAI Agent\`. Each piece is
small; the trick is shipping them in an order that doesn't leave a
worse intermediate state.

---

## 3. Product completeness

### ~~🔴 Worker capability registration missing~~ — done (data layer + routing)

`/register` now accepts an optional `capabilities` body
(`vram_gb`, `gpu_model`, `bandwidth_class`, `models[]`, `tools[]`,
`notes`). Capabilities are stored in Redis and surfaced on `/workers`.
Existing workers that send only `worker_id` keep working — capabilities
are additive (`tools[]` defaults to `["chat"]`).

Capability-aware **routing** also done as of the 2026-05-20 image-
generation slice: per-tool Redis queues (`job_queue`, `job_queue:image`),
`/jobs/next` accepts a `tool` field and pops from the matching queue,
and `_ensure_live_worker_or_503` is tool-aware so image submissions
return a 503 specific to "no image workers" instead of falling through
to a chat worker.

Still pending: **load balancing across tools** — see next section.

### 🟢 No load balancing across multi-tool agents

A dual-capable agent (chat + image) today polls chat queue first, then
image queue if chat is empty. That's adequate for the small-network
phase but leaves three gaps the bigger network will surface:

- **Warm-model affinity (partially done, 2026-05-20).** Each agent
  now persists its last-served tool in `state.json` and polls *that*
  queue first on the next tick, so a chat streak keeps the LLM warm
  in VRAM and an image streak keeps sd.cpp warm. Coordinator state
  is untouched — affinity lives entirely in the agent. Sufficient
  while the network is demand-shaped (chat traffic dominates → most
  agents serve mostly chat and only flip when a chat lull lets an
  image job through). Will *not* prevent thrashing once chat and
  image queues are routinely non-empty in parallel.

- **Soft pinning (Phase 2 — deferred).** Coordinator records each
  registered agent's GPU class + contributor preference and pins
  the agent to one tool at registration time. Two viable signals:
  (a) contributor opts in via `config.json` (`agent.role: "image"`),
  (b) coordinator infers from advertised VRAM (`vram_gb >= 12` →
  eligible to specialize on image). No dynamic flipping; promotion
  requires an agent restart. Plumbing is small and gives a reliable
  starting role without committing to a real balancer.

- **Dynamic rebalancing (Phase 3 — deferred until ≥50 contributors).**
  Coordinator measures per-queue depth + worker utilization, demotes
  idle image workers to chat (and vice versa, *subject to VRAM
  asymmetry — see below*) above a hysteresis threshold. Requires
  careful design around: (1) not yanking a busy worker mid-job;
  (2) charging swap cost (~10-30s sd.cpp cold start) only when the
  new role keeps the worker busy long enough to amortize it;
  (3) per-contributor opt-outs for specialists; (4) flap dampening
  on bursty demand.

**GPU asymmetry caveat.** A naive "70% chat / 30% image at rest" ratio
glosses over a real constraint: a contributor running an 8 GB GPU on
7B chat *cannot* run SDXL (needs ~12 GB). Conversion is one-way —
big GPUs can always fall back to chat, but small GPUs are chat-only.
Any rebalancer needs per-agent VRAM-aware eligibility, not a global
ratio. The natural breakdown is something like: small GPUs (4-8 GB) →
chat-only or sd1.5-only; big GPUs (12 GB+) → swappable. The current
capability-advertisement system already exposes the data needed; the
balancer just hasn't been written.

### ~~🔴 Model registry missing~~ — done (catalog + validation)

`coordinator/model_registry.py` now contains a curated catalog of
known models (Llama 3 family, Mistral, Mixtral, DeepSeek-V3, Qwen,
Phi, mock). Each entry tracks family, total/active params, min VRAM
at INT4, and license. Surfaced via `GET /models`.

Set `STRICT_MODELS=true` to make `/generate` reject unknown model
names with 400. Default is off (any string accepted) so tests and
local dev with custom models keep working.

Still pending: per-model pricing, license-aware routing, shard plans
for big models. Same Phase 4 boundary as routing above.

### 🟡 No customer SDK

Customers use raw curl. That's fine for the founder. It's not fine
for indie devs who are our Phase 1 ICP.

**Fix:** thin Python + JS clients (`pip install gamerai`,
`npm i gamerai`) that wrap `/generate` + `/result` polling, expose an
OpenAI-compatible `chat.completions.create` interface, and handle
auth/retries.

### 🟡 No streaming / SSE on `/result`

Customers must poll. Adds latency, wastes their CPU and our requests.

**Fix:** add a Server-Sent Events stream on the coordinator that
forwards tokens as the worker produces them. Cheap once worker
reports per-token.

### 🟡 No batch endpoint

Async/batch is our positioned strength, but we don't actually ship a
batch API. Customers have to fan out one-job-at-a-time.

**Fix:** `POST /generate/batch` with a list of prompts; returns a
`batch_id`; per-job results retrievable as they complete or as a zip
when done.

### 🟡 Token counts are estimates

`len(text) // 4` when Ollama doesn't report. Earnings drift ±10%.

**Fix:** load the model's tokenizer in the worker and use it for
counts. Adds a few MB per worker, removes the drift.

### 🟢 Auto-update for the Windows agent

Today: gamer manually downloads new exe. With even mild adoption that
becomes a maintenance nightmare and a security liability (no way to
ship a fix).

**Fix:** signed binaries + on-startup version check + self-replace.
Or, easier, ship via Squirrel / NSIS auto-updater.

### 🟢 No customer dashboard

Web UI shows worker / job state. Customers have no view of their
spend, key, usage trend, or limits.

**Fix:** post Phase 2b. Currently irrelevant — there are no
customers.

### 🟡 No image input (vision) on chat

The chat tool takes text only. ChatGPT, Copilot, and Gemini all accept
screenshots, photos, diagrams, receipts, whiteboard code. This is the
single most-cited capability gap when comparing the suite head-to-head —
a casual user reaches for "drop a picture in chat" before they reach for
"better chat model."

**Fix:** add a `chat-vlm` capability (either a new tool, or a `chat` job
with an optional `image_b64` param) plus a matching Redis queue.
Llama 3.2 11B Vision is the obvious first model — ~10–12 GB VRAM at Q4,
so it lives on the SDXL-class contributor tier, not the entry tier.
Workers with the weights + VRAM advertise `tools=["chat","chat-vlm"]`;
coordinator routes accordingly. UI gets a paperclip on the composer.

Scope estimate: ~2–3 days for routing + UI + agent bootstrap. Pulls the
README Phase 4 vision item forward into Phase 3 — the perceived-IQ gap
from "can't see images" is bigger than the gap from "small chat model."

### 🟡 No document upload (PDF / DOCX / CSV)

Paired with vision: ChatGPT lets you drop a PDF and ask "summarize this."
We have no document path at all. The natural shape isn't a new tool — it
mirrors the existing search-prepend: extract text on the coordinator,
prepend to the chat prompt as fenced context, dispatch as a normal chat
job. No new worker capability needed.

**Fix:** `POST /uploads` accepting PDF / DOCX / TXT / MD / CSV up to a cap
(5 MB to start). Extract with `pypdf` + `python-docx` + `pandas.read_csv`.
Store extracted text against the conversation; `/generate` for that
conversation prepends it inside a `<<document>> … <</document>>` fence.

Explicitly out of scope for v1: image-only PDFs (needs OCR — Tesseract or
PaddleOCR sidecar), structured spreadsheet QA (needs a code-execution
sandbox), >20-page docs (needs chunking + retrieval). Ship the simple
path first; revisit each only after first user complaint.

Scope estimate: ~2 days. Slot alongside the vision work — "drag a thing
into chat" is one UX gesture either way.

### 🟡 Piper cold-starts on every TTS job — agent should keep it warm

Each `tool=tts` job triggers a fresh `subprocess.run([piper.exe, ...])`
in `windows-agent/agent.py:run_tts_inference` (line ~3607). Every spawn
reloads the voice ONNX model + initializes ONNX Runtime — roughly
1–2 seconds of dead time per job before any audio comes back.

Voice mode used to dispatch one TTS job per sentence to chase "time to
first audio" while the chat response was still streaming. The earlier-
on-2026-05-25 chunker (paragraph + soft-buffer heuristic) reduced a
multi-bullet response from ~7 jobs to ~3, but each chunk still paid the
full piper.exe cold-start, so audible 1–2s gaps between chunks
remained. Later the same day we reverted the chunker entirely and
collapsed voice mode onto the single-job-per-message path the read-
aloud button uses (see `chat.js` — `voiceMode` is now just an auto-
trigger for `onReadAloudClick` on completion). Cold-start is now paid
once per response instead of once per chunk, amortized over a full WAV
with natural Piper inter-utterance prosody. The trade-off the user
accepted: longer time-to-first-audio (have to wait for the whole
response to generate + synth) for smooth delivery.

Warm-Piper is still the eventual lower-bound on TTS latency — it'd cut
~1–2s off every read-aloud request, manual or auto-fired — but it's no
longer load-bearing for the "conversational pauses" problem.

**Fix:** spawn `piper.exe` once when the tts loop starts, attach to
its stdin/stdout, write `<text>\n` per job and read the WAV from
stdout. The `--output-raw` flag returns raw PCM on stdout; we wrap it
into a WAV header on the agent before base64-encoding. Single process
across many jobs → one model load per agent lifetime.

Two known costs:
- Process-lifecycle management. If piper.exe crashes mid-job, the loop
  needs to detect (stdout EOF or non-WAV bytes), restart, and retry
  the current job. Today's spawn-per-job pattern hides this naturally.
- Concurrency: the tts loop is single-threaded, so one warm process
  is sufficient. If we ever parallelize tts (multi-voice or batched
  requests on a beefier CPU), we'd need a small process pool.

Scope estimate: ~1 day for the warm-process refactor + tests; ~½ day
for crash-recovery hardening. Slot when TTS volume warrants it.

### ~~🟢 Read-aloud is auto-fired in voice mode; want a per-message button in chat mode~~ — done (2026-05-25)

Resolved. `client/static/js/chat.js` now hangs a faded speaker icon
off every completed assistant text bubble (history-loaded and freshly-
streamed). First tap submits a `tool=tts` job for the whole bubble,
spinner during synthesis, audio plays when ready. Result is cached
against `message_id` so re-taps replay from memory without re-billing
the user's `voice_minutes` (in-memory only — page refresh re-bills).
Tap on the playing button stops; tap on a different button stops the
prior and plays the new one. Audio also stops when the user navigates
to another conversation, starts a new chat, or deletes the active
conversation. Voice mode is untouched and still streams during mic-on
conversations.

### 🟡 No cross-session memory per account — KISS, explicit only

Persistent memory across conversations is a real perceived-IQ gap vs.
ChatGPT, which remembers your name / projects / preferences across weeks.
But the *automatic* memory ChatGPT does — quietly summarizing past chats
and re-injecting them — is the wrong fit for us. It's noisy, opaque,
debug-hostile, and a privacy footgun under the membership rule
(contributors see whatever ends up in the system prompt).

**Fix:** explicit memory only.

- New table `member_memory(member_id, key_or_id, body, created_at)`.
- Parsing rule on `/generate`: if the chat message starts with
  `remember ` (case-insensitive), strip the prefix, persist the remainder
  as a memory entry, reply with a one-line confirmation, **don't** fire a
  chat job.
- On every chat/search `/generate`, prepend the member's memories to the
  system prompt as a fenced block:
  ``<<memory>>\n- <entry 1>\n- <entry 2>\n<</memory>>``.
- `/account` shows current memories with a per-row delete button. No
  edit — delete + re-add.
- Hard cap: 50 entries or 2 KB total. Over the cap → reject new
  additions with a "delete some first" message. No silent truncation.

What this deliberately *doesn't* do:

- No automatic extraction from chat history (opaque, debug-hostile,
  privacy-risky).
- No embeddings / semantic search over memories (premature at <50
  short entries — they fit in the system prompt for free).
- No summarization of past conversations (different concern; lives with
  the existing `conversation_id` context, not here).

Scope estimate: ~1 day. Pairs naturally with Phase 3b.iii encrypted-
prompts-at-rest — the memory column is just another field to encrypt
under the same per-user key.

### 🟢 Search mode rides free `ddgs` — runway is huge but not infinite

The search tool routes through `ddgs` (formerly `duckduckgo-search`),
which is a scraping client, not an official API. No key, no quota
tier, no SLA. Every DDG call comes from the **worker's** IP, not
the coordinator's, so the rate-limit math benefits from the
distributed contributor fleet.

The agent uses **backend rotation** (`duckduckgo, mojeek, brave, bing,
yahoo, startpage`) plus a **per-worker 10-minute TTL cache**
(`cachetools`), so the effective ceiling is:

```
≈ N_contributors × N_backends × ~30 q/min/IP
≈ N_contributors × 180–240 q/min
```

Stage estimates:
- **Today (1 contributor):** ~240 q/min sustained → ~50 active
  searchers. Not close to the wall.
- **Early growth (10 contributors):** ~2,400 q/min → ~500 active
  searchers.
- **Scaling pain (~1,000 contributors):** ~240,000 q/min → ~50,000
  active searchers. The free runway ends here, not before.

**When we migrate:** Brave Search API is the most LLM-friendly paid
option (~$3-5/1000 queries; generous free tier of 2,000/mo). Drop-in
replacement for the `_ddg_search` call. Tavily is the agent-native
alternative if we want returned summaries instead of raw hits.

**Watch for, before the wall:**
- A single hot worker getting slammed by the coordinator's warm-
  routing for search. Mitigation: weighted-random routing for
  `tool=search` instead of last-tool affinity. Cheap fix when
  symptoms appear.
- DDG / Bing HTML shape changes breaking ddgs. The maintainers chase
  this with point releases; we'd need to bump the pin promptly.

**Skipped infra options (with reasoning):**
- Self-hosted SearXNG → too heavy to bundle; requires a sidecar.
- Public free proxy lists → unreliable + untrusted exit nodes.
- Tor as fallback → our multi-IP fleet already IS the proxy network.

---

## 4. Engineering hygiene

### ~~🟡 Limited automated tests~~ — done for MVP scope

`tests/` now has **44 test cases** across six files:

* `test_auth.py` — bearer-auth module (9 cases).
* `test_idempotency.py` — idempotency store (7 cases).
* `test_rate_limit.py` — rate limiter (7 cases).
* `test_model_registry.py` — model catalog and strict-mode (11 cases).
* `test_coordinator_e2e.py` — end-to-end FastAPI tests against
  ``fakeredis`` + a tempfile SQLite DB. Covers: `/health`, `/models`,
  job round-trip with earnings credit, idempotency through the HTTP
  layer, capability registration round-trip, and reaper requeue (10
  cases).

Open follow-ups (smaller priority): two-worker contention test,
explicit auth-on test through the HTTP layer, real-Redis CI smoke.

### 🟡 No type checking in CI

`shared/models.py` uses Pydantic, but nothing enforces types across
the codebase. Subtle bugs (status string typos, key-name drift) slip
through.

**Fix:** add `mypy --strict` to the pre-commit and CI pipelines.

### 🟡 SQLite under concurrency is not a tested path

The coordinator is single-process today, so write contention is a
non-issue. The minute we scale horizontally (multiple coordinator
replicas), SQLite breaks.

**Fix:** acknowledged in the graduation criteria. Plan: switch to
Postgres in Phase 2b, not before.

### 🟢 Shared schemas live in `shared/` but only the Python services
use them

When we ship the JS SDK, schemas drift. Worth generating both Python
and TS types from a single source (e.g., a small JSON schema or
`pydantic` → `pydantic-to-typescript`).

### 🟢 Dockerfiles are unpinned

`FROM python:3.11` not `FROM python:3.11.9-slim-bookworm@sha256:...`.
Builds aren't reproducible; CVE introduction is silent.

---

## 5. Business / go-to-market

### 🔴 No invite / membership flow

The system has no signup, no invite, no per-member identity. Under the
community-powered model the contributor IS the first user, so we need:
contributor onboarding (the Windows agent installer is most of this),
the Alice → Bob invitee flow (contributor invites by email + sets cap),
and a host admin UI for managing invitees / seeing one's own tier.

**Fix:** Phase 3b.i critical-path work. The agent installer covers most
of the contributor side; the invite + admin UI is the new build.

### 🔴 No payout rails — for the paid-revenue → bonus flow (Phase 3b.ii)

Severity preserved, scope reframed. Under contribute-to-use, free
contributors are not paid — they're rewarded with tier-based access.
But once the **paid customer layer** ships, 80% of paid revenue flows
back to opt-in GOLD+/PLATINUM contributors who served the paid jobs.
That flow needs real money rails.

**Fix:** Stripe Connect for monthly contributor payouts; minimum
threshold ($25); 1099 handling. Required for Phase 3b.ii, not before.

### 🟡 No tier structure

Severity preserved, scope expanded. Tier structure spans both sides of
the network:

- **Contributor tiers** (BRONZE → PLATINUM) gate quota, invite slots,
  paid-pool opt-in eligibility. Driven by uptime + capability +
  claimed-jobs-per-hour metric.
- **Paid customer tiers** (CASUAL / DEVELOPER / ENTERPRISE) gate pricing
  shape, SLA, and privacy-routing options.
- **Per-model pricing** for paid customers (a chat-13B call should cost
  more than chat-7B; SDXL images priced separately).

**Fix:** ships alongside the member identity work in 3b.i, then
expands for paid pricing in 3b.ii.

### 🟡 No legal: ToS, privacy policy — partial

Code now licensed under PolyForm Noncommercial 1.0.0 (see `LICENSE`) —
switched from the original Apache-2.0 pick ahead of going public, so
free/noncommercial use (reading, running, forking, learning from it)
stays open with no gate, but a company wanting to actually deploy or
sell on top of it needs to come talk to the copyright holder first.
Still missing under the
community-powered framing:
- **Community ToS + acceptable-use policy** — covers all members
  (contributors and invitees), not just paid customers.
- **Contributor agreement** — covers the "your GPU runs prompts you
  didn't write, including from invitees you don't know" risk, plus the
  opt-in paid-pool participation contract.
- **Paid-customer commercial ToS** — Phase 3b.ii prerequisite.
- **Privacy policy** — must be honest about the membership rule (your
  prompts traverse the contributor network, not specifically your
  inviter's GPU). See README § 5 privacy framing note.

Pull from Termly / iubenda templates before recruiting non-friends.

### 🟢 No marketing surface

No landing page, no docs site, no demo video. Fine while the founder
is the only customer; required before recruiting beyond the friend
circle.

---

## 6. Strategic / forward-looking

These are documented in `research/big-models-feasibility.md` and the
README roadmap. Listing them here for completeness.

- Big-model support (Petals → EXO) — Phase 4 in the roadmap.
- Privacy tiers (client-side embedding, vetted pools, TEE) — Phase 5.
- Energy-aware routing — Phase 4 long-tail.

### Agents tier — paid-customer-only, needs PLATINUM contributors

Multi-step autonomous agents (ChatGPT Operator, Claude computer-use,
Copilot Pages agentic flows) are the highest-token-consumption category
in the market — a single agent task can burn 100K+ tokens on planning,
tool calls, retries, reflection. Two reasons it's not a free contributor
perk here:

1. **Token cost.** A free contributor's daily quota would evaporate in
   one agent run. Agents are inherently a paid-customer product.
2. **Model requirements.** Reliable agents need a real coding / tool-use
   model — DeepSeek-Coder-V2, Qwen 2.5 Coder 32B, Llama 3.3 70B class.
   That's 20–40 GB VRAM at Q4, i.e. the PLATINUM contributor tier
   (3090 / 4090 / 5090 / A6000). Basic-GPU fleet can't serve these jobs
   at all.

**Plan:**

- Ships in Phase 3b.ii (paid customer layer) or later, never sooner.
- DEVELOPER paid tier gets agent access. Bill per *step* (planning,
  tool-call, reflection), not per token, so customers can predict cost.
- Route to a separate `job_queue:agent` served only by PLATINUM
  contributors who (a) opt into the paid pool, (b) have advertised a
  coding-class model, (c) passed the 1-week reliability proof.
- Higher per-step bonus payouts — agent steps are slower, more VRAM-
  hungry, and lower-turnover than chat tokens. Premium reflects that.

Flagging now so we don't accidentally promise free agents to
contributors. Public framing: free contributor toolbox = chat + image +
search + read-aloud + (eventually) vision + docs + memory; **paid
customers** get agents and frontier-model inference (the Phase 4
Petals/EXO work).

---

## 7. Suggested 30-day priority list

If you ran a focused sprint on the items above, this is the order I'd
work them in. Highest leverage first.

1. ~~**Ship bearer-auth end-to-end**.~~ ✅ Done.
2. ~~**Idempotency keys + rate limit + GitHub Actions CI.**~~ ✅ Done.
3. ~~**License the code.**~~ ✅ Done — PolyForm Noncommercial 1.0.0
   (originally Apache-2.0; switched pre-launch, see § 5 "No legal" above).
4. ~~**Coordinator integration tests** (job round-trip, reaper requeue,
   idempotency, capabilities).~~ ✅ Done — `test_coordinator_e2e.py`
   with ``fakeredis``.
5. ~~**Worker capability registration + model registry** (data layer).~~
   ✅ Done — capability-aware *routing* deferred to Phase 4.
6. **Uptime Kuma (or equivalent) hitting `/health`.** **~½ day.**
7. **SQLite nightly backup cron with weekly rotation.** **~½ day.**
8. ~~**Member identity + tier accounting**~~ — slice 1 done
   (2026-05-11). Per-member tokens, jobs recorded against the
   submitter, daily usage rollup. Tier auto-evaluation deferred to a
   later slice. ✅ Done.
9. ~~**Invite / admin flow (Alice → Bob)**~~ — slice 2 done
   (2026-05-11). `invites` table, copy-paste invite URL, quota
   enforcement on `/generate`, minimal admin web UI in `client/`.
   ✅ Done.
10. **Community ToS + contributor agreement** from a template. **~½ day.**
11. **Recruit 3 real gamer contributors and run end-to-end.** The
    actual experiment. **~½ day, plus calendar time.**

Notably *not* on this list anymore: per-token pricing rollout, Stripe
Connect, customer signup, marketing landing page, Python SDK. Those all
move down to Phase 3b.ii once the membership/tier engine is in place —
selling paid access to a network that doesn't yet have a member identity
or a tier ladder is putting the cart before the horse.

That's ~10 working days of engineering plus the recruitment / community
items. After that, reassess: is anyone using it? If yes, push into the
paid-customer layer (3b.ii) and the big-model plan in Phase 4. If no,
the gap is in distribution and recruitment, not the platform.
