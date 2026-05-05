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

### 🔴 No auth on the coordinator API

Every endpoint is open. Anyone who learns the URL can submit jobs,
register a fake worker, or read every other worker's earnings. The
Caddyfile has a commented bearer-auth variant, the bootstrap generates
a `WORKER_TOKEN`, but **no client code sends the header**.

**Fix:** ~30 lines. Add an optional `Authorization: Bearer ...` middleware
to the coordinator (skip on `/health`); pass `WORKER_TOKEN` through the
worker (`worker/worker.py`), the Windows agent (`windows-agent/agent.py`),
and the web/CLI client. Then flip the auth-gated handle in the Caddyfile.

### 🔴 No customer identity / API keys

There's no concept of "who submitted this job." The `WORKER_TOKEN` above
gates *access*, not *identity*. We can't bill, rate-limit, or attribute
abuse without per-customer keys.

**Fix:** add a tiny `api_keys` table; require `X-API-Key` on `/generate`
and `/result`; record `customer_id` on every job row.

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

### 🟡 Web UI dashboard has no auth

Even bound to localhost (current default), once we expose it through
Caddy on a subdomain it leaks worker IDs, earnings, and job prompts to
anyone who guesses the URL.

**Fix:** Caddy `basic_auth` is one line and good enough until we have
real customer accounts.

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

### 🟡 No rate limiting

A buggy customer (or a bored attacker) can drain the worker pool by
submitting an unbounded number of jobs. Coordinator has no per-key or
per-IP rate limit.

**Fix:** Caddy can do basic IP rate limiting today. Per-key limits
land with the API-key work above.

### 🟡 No idempotency on `/generate`

Customer retries (network blip, timeout) create duplicate jobs and
double-charge.

**Fix:** accept an optional `Idempotency-Key` header; cache `(key →
job_id)` for 24h; return the same `job_id` on retries.

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

### 🟢 No CI / CD

`.github/` doesn't exist. Every push is "hope I didn't break it."
Tests don't exist either, so CI couldn't run anything yet.

**Fix:** after the test suite below, add a GitHub Actions workflow
that runs `pytest` + a smoke test of `docker compose up`.

---

## 3. Product completeness

### 🔴 Worker capability registration missing

A worker registers with just an ID. The coordinator has no idea if
the worker can run a 70B model or only a 1B one. Required before any
big-model work.

**Fix:** add `vram_gb`, `gpu_model`, `bandwidth_class`, `model_list`
to `/register`. Schedulers can then route by capability.

### 🔴 Model registry missing

`model` is a free-form string in `/generate`. No metadata, no
licensing, no shard plan, no min-VRAM. Required before serving more
than one model.

**Fix:** small `models` table with id, family, parameters, license,
min_vram, shard_plan, price_per_1m_in, price_per_1m_out. Phase 4a
prerequisite.

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

### 🔴 Zero automated tests

No `pytest`, no smoke test, no fixtures. Every refactor risks
breaking the loop and we'd find out only by clicking through the UI.

**Fix:** start with three integration tests:
1. Submit a mock-inference job, assert `complete` and tokens > 0.
2. Kill a worker mid-job, assert reaper requeues.
3. Two workers, one job, assert exactly one completes it.

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

### 🔴 No payout rails

Earnings are tracked, not paid. A gamer has zero incentive to install
the agent until we can write them money.

**Fix:** start with Stripe Connect for ACH payouts; minimum threshold
($25); 1099 handling.

### 🟡 No pricing tier structure

Single `RATE_PER_TOKEN` flat across all models. We can't charge more
for 70B than for 1B.

**Fix:** per-model pricing in the model registry above.

### 🟡 No customer signup flow

The system has no /signup, no /login, no Stripe customer creation, no
quota assignment. To get to first revenue we need at minimum a sign-up
form that mints an API key and a Stripe customer.

**Fix:** small Phase 2b scope. Could outsource to Clerk + Stripe
billing portal in a weekend.

### 🟡 No legal: ToS, privacy policy, license file

`README.md` says `License: TBD`. No terms for customers, no agreement
for workers (they're literally running arbitrary computation for
strangers).

**Fix:** Apache-2.0 or MIT for the code (pick one); template ToS +
privacy from Termly / iubenda; explicit worker agreement covering the
"you run prompts you didn't write" risk.

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

1. **Ship bearer-auth end-to-end** (coordinator middleware + worker +
   agent + flip the Caddyfile). Removes the open-API risk. **~1 day.**
2. **First three integration tests + GitHub Actions.** Removes the
   "every change might break it" risk. **~1 day.**
3. **Worker capability registration + model registry.** Unblocks
   everything in Phase 4 and lets us safely host >1 model. **~1–2 days.**
4. **Caddy IP rate limit + idempotency keys on `/generate`.** Cheap
   abuse prevention. **~½ day.**
5. **Uptime Kuma (or equivalent) hitting `/health`.** Now we know
   when it's down. **~½ day.**
6. **SQLite nightly backup cron with weekly rotation.** Now we don't
   lose the ledger to a single bad reboot. **~½ day.**
7. **License the code (Apache-2.0) + draft worker agreement + ToS.**
   Required before any payouts. **~1 day with templates.**
8. **Python SDK** (thin wrapper, OpenAI-compatible signature).
   Lowers the bar for first paying users. **~1–2 days.**
9. **Recruit 3 real gamer workers and run end-to-end with auth on.**
   This is the actual experiment. **~½ day, plus calendar time.**

That's ~10 working days of engineering plus the recruitment / business
items. After that, reassess: is anyone using it? If yes, push into the
big-model plan in Phase 4. If no, the gap is in distribution and
pricing, not the platform.
