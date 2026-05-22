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

### 🟢 No persistent client-side cache (offline / reload-resilience)

The chat UI keeps an in-memory `Map` of loaded conversation messages
so switching back and forth between threads in the same tab is
instant. **Page refresh wipes it** — every new tab pays one
`/api/conversations` + one `/api/conversations/<id>` round trip
before any text appears. Two things this *doesn't* give us:

- **Reload-resilience.** If the coordinator is slow or briefly
  unreachable, the user sees a blank UI even though their history
  exists on disk in their browser nowhere.
- **Offline read.** A user on a flaky train connection can't review
  a past conversation while the network is dropping.

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
   on top of the encryption work.
3. PWA shell (service worker, offline cache, install-to-home-screen).
   ~3–5 days. Real offline mode. Only worth it after we have real
   users complaining.

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

### 🟢 No log retention strategy

Logs go to stdout / `docker logs`. Restarts wipe history. Useful for
debugging today, useless for compliance or analysis later.

**Fix:** ship to a hosted log store (Grafana Loki, Better Stack,
Axiom) when we add monitoring.

### ~~🟢 No CI / CD~~ — done

Resolved. `.github/workflows/ci.yml` runs the unittest suite,
validates that `docker-compose.yml` and the prod overlay merge
cleanly, and syntax-checks the bash scripts on every push and PR.

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

Code now licensed Apache-2.0 (see `LICENSE`). Still missing under the
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

---

## 7. Suggested 30-day priority list

If you ran a focused sprint on the items above, this is the order I'd
work them in. Highest leverage first.

1. ~~**Ship bearer-auth end-to-end**.~~ ✅ Done.
2. ~~**Idempotency keys + rate limit + GitHub Actions CI.**~~ ✅ Done.
3. ~~**License the code (Apache-2.0).**~~ ✅ Done.
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
