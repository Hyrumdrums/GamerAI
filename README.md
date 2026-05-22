# GamerAI

> **A community-powered AI suite — chat, images, and web-augmented answers —
> where contributing a gaming PC earns you tier-based access for yourself and
> the people you invite.**

This repo is a fully local, containerized MVP. One command brings up a
coordinator, Redis queue, SQLite store, a web UI, and any number of worker
nodes that simulate contributor machines.

The MVP today serves **two tools** — chat and image generation — with the
architecture job-based and per-tool capability-routed (agents advertise
`tools=["chat","image"]` at registration; the coordinator queues each
job to the matching per-tool Redis queue and only image-capable workers
pick up image jobs). Web-augmented answers and additional tools are
additive on top of this same plumbing — see § 14 (Roadmap).

**Membership is contribute-to-use.** When you run the agent, your GPU serves
jobs from the network's shared queue anonymously — not just your own
invitees. A paid customer layer (Phase 3b+) funds bonus payouts to opt-in
contributors who serve paying users, but free contributors always get
priority. See § 5 (Economics).

```bash
docker compose up --build
# open http://localhost:8080
```

Live: https://ai.dallinlayton.com — see [`docs/devlog.md`](docs/devlog.md) for
deploy details, decisions, and operational runbook.

---

## 1. Product overview

GamerAI is a **community-powered AI suite** running on a network of
contributor gaming PCs. Three actors:

- **Contributors** install a small agent and advertise the tools their
  hardware can run (chat, image generation, eventually doc/code/voice).
  When the machine is idle, the agent serves jobs from the network's
  **shared queue** — anonymously from the contributor's POV. In return,
  contributors get tier-based access to the network's full AI suite for
  themselves and the people they invite. Tier (BRONZE → PLATINUM) is
  earned by uptime + capability + actual jobs served, not paid for.
- **Invitees** are non-contributing friends, family, household members
  invited by a contributor. Their usage comes from the inviter's quota;
  the inviter sets their cap. Invitees do not need their own hardware.
- **Paid customers** (Phase 3b+, not MVP) — CASUAL households,
  DEVELOPER per-token API users, ENTERPRISE volume customers — pay for
  access. Paid revenue funds bonus payouts to opt-in PRO/PLATINUM
  contributors who serve paying jobs, and a capped share covers
  coordinator infrastructure.

**The coordinator** runs job routing, the tier engine, accounting, and
centralized helpers (e.g. web search). It is the only platform-owned
component; everything else (compute, data, models) lives on contributor
machines. The platform never extracts from contributors — paid revenue
only ever flows to bonus payouts + infra costs.

The network is fully **per-job and per-tool**: every completed job is
priced in its natural unit (tokens for chat, images for SDXL, requests
for search) and credited to the contributor's ledger.

### Tools

| Status   | Tool          | Model class            | Why it fits a distributed network |
| -------- | ------------- | ---------------------- | --------------------------------- |
| **MVP, live** | Chat          | 7B–13B (currently 1B for VPS demo) | Independent jobs, latency-tolerant, low VRAM |
| Next     | Web-augmented answers | small chat + search API | No GPU lift; centralized; biggest perceived-IQ bump for small models |
| Next     | Image generation | SDXL-class (~8–12 GB VRAM) | Independent jobs, async-friendly, high demo wow |
| Expansion | Document tools (summarize, rewrite, chunked analysis) | 7B–13B | High retention, same hardware envelope as chat |
| Expansion | Coding assistant | 7B–13B | Frequent, small jobs, plays to the chat envelope |
| Later    | Music generation (MusicGen) | varies | Async / queued; longer runtimes |
| Later    | Voice (batch STT/TTS) | varies | Real-time voice deliberately out of scope |
| Out of scope | Tightly-coupled multi-node, real-time low-latency, frontier training | — | Network constraints make these unwise |

The principle behind the list: pick tools that are **independent, retryable,
and tolerant of moderate latency**, because that is the shape of jobs a
heterogeneous gamer network can serve well. See [`business.md`](business.md)
for the strategic framing.

## 2. Problem statement

Two facts about the AI market today:

1. **Centralized inference is expensive.** OpenAI, Anthropic, and the major
   cloud GPUs charge a premium that reflects scarcity, not marginal cost.
2. **Massive amounts of compute sit idle.** There are tens of millions of
   gaming PCs with capable GPUs that run at <5% utilization most of the day.

The gap between #1 and #2 is the opportunity.

## 3. Solution

A **contribute-to-use community network** with an optional paid layer:

- A **coordinator** that accepts jobs, queues them, dispatches to idle
  contributors, tracks per-member contribution + consumption, and
  enforces tier-based quotas. Eventually routes paid customer jobs to
  the opt-in pool.
- **Contributor agents** running on consumer hardware. They poll for
  jobs from the shared queue, run inference locally (Ollama / llama.cpp
  / vLLM), submit results, and earn tier-based access in return.
- **Members and invitees** that submit prompts via REST or a UI; in a
  future phase, paid customers do the same with separately metered
  access.

This MVP simulates the entire loop on a single host using Docker Compose.
Nothing in the architecture assumes a single host beyond the default
`host.docker.internal` URL workers use to reach Ollama.

## 4. Architecture

```
                        ┌──────────────────────┐
   browser ──────▶      │     client (web)     │   FastAPI UI, port 8080
   $ python client.py   │  + CLI (host-side)   │
                        └──────────┬───────────┘
                                   │ REST
                                   ▼
                        ┌──────────────────────┐
                        │     coordinator      │   FastAPI, port 8000
                        │  ─ /generate         │
                        │  ─ /result/{id}      │     ┌────────────┐
                        │  ─ /workers          │ ──▶ │   SQLite   │  system of record
                        │  ─ /earnings         │     │  /data/*.db│
                        │  ─ /metrics          │     └────────────┘
                        │  ─ reaper thread     │
                        └──────────┬───────────┘
                                   │ rpush / hset / blpop
                                   ▼
                        ┌──────────────────────┐
                        │        redis         │   queue + claim deadlines
                        └──────────┬───────────┘
                                   ▲
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
   ┌────┴───┐                ┌────┴───┐                  ┌────┴───┐
   │ worker │                │ worker │                  │ worker │   --scale worker=N
   │  idle  │                │  busy  │                  │offline │
   └────┬───┘                └────┬───┘                  └────────┘
        └──────── Ollama (host.docker.internal:11434) ─────────┘
```

### Services

| service       | role                                                    | port |
| ------------- | ------------------------------------------------------- | ---- |
| `coordinator` | REST API, job dispatch, SQLite write-through, reaper    | 8000 |
| `redis`       | job queue, in-flight claims, fast worker registry       | 6379 |
| `worker`      | claims jobs, runs inference, simulates gamer realism    | —    |
| `client`      | minimal web UI + proxy to coordinator                   | 8080 |

### Storage layers

| store    | role                                                          |
| -------- | ------------------------------------------------------------- |
| Redis    | hot path: queue, in-flight claim deadlines, status, hot cache |
| SQLite   | system of record: jobs, workers, earnings (write-through)     |

The coordinator is the only writer to SQLite. Workers go through the
coordinator's `/jobs/claim` and `/jobs/complete` endpoints.

### Scheduler

Jobs are pulled (`BLPOP`) by workers — but workers only poll while their local
state is `idle` and they are within their availability window, so jobs are
naturally only claimed by idle workers. When a worker claims a job, the
coordinator records a deadline in Redis (`job_processing` hash). A background
**reaper thread** scans for expired deadlines and **requeues** the job so it
isn't lost if a worker disappears mid-job.

## 5. Economics

The economics are **three layers stacked**: free contributor-tier access
forms the foundation, an optional paid customer layer funds the
coordinator and bonus payouts, and PRO/PLATINUM contributors can opt
into earning from paid jobs.

### Layer 1 — Contribute compute, earn tiered access (MVP)

Contributing is free; access is tiered by what you actually contribute.

| Tier | Criteria (target) | Benefits |
|---|---|---|
| **BRONZE** | Agent installed, intermittent uptime | Full toolbox; small monthly quota; 1 invite slot |
| **SILVER** | ~4 hrs/day average uptime | 5× quota; 3 invites; queue priority over BRONZE |
| **GOLD** | ~12 hrs/day; multi-tool capable | 20× quota; 10 invites; eligible for paid-pool opt-in |
| **PLATINUM** | ~20+ hrs/day; high-VRAM card | Effectively unlimited; first dibs on new tools; full paid-pool participation |

Tier promotion is **uncapped meritocracy** and **low-friction**: anyone
with a 4090 and a 24/7 availability toggle can hit PLATINUM tier on day
one. The status loop should never feel gated.

**Paid-pool eligibility is decoupled from tier promotion.** The opt-in
toggle for serving paid customer jobs only appears after the agent has
demonstrated **1 week of sustained uptime + minimum claim rate**. Tier
gets you the status; reliability proof gets you the earnings.

Tier maintenance requires **both uptime AND actual jobs served** — an
agent that idles online while refusing jobs (a fork, for example) falls
down the ladder. The coordinator measures claimed-jobs-per-hour as the
source of truth.

### Layer 2 — Paid customer tiers (Phase 3b+; not MVP launch)

| Tier | Audience | Latency | Pricing shape |
|---|---|---|---|
| **CASUAL** | Households without a gaming PC | Realtime | Flat monthly fee, generous-but-capped quota |
| **DEVELOPER** | App builders | <30s realtime | Per-token API; ~$1.50/1M tokens (between Haiku $1.25/1M and self-hosted) |
| **BATCH** | Bulk workloads (embeddings, doc summarization, classification) | <24h | ~$0.75/1M tokens — scheduled into low-utilization windows |
| **ENTERPRISE** | Companies | SLA-defined | Volume contract + dedicated worker pool + privacy-tier routing |

**BATCH** is the supply-soak lever: when network utilization is low,
batch jobs fill the slack instead of requiring more advertising. AWS
Spot Instances as the proven analog (50–70% discount for time-flexible
work; most enterprise AI workloads are batch-friendly).

Paid customer demand is served from a **separate priority queue** that
only contributors at GOLD+ who **opt in** can see. Free contributor
tiers are never degraded by paid demand — if paid demand exceeds opt-in
supply, paid customers see queue delays or capped service, not
contributors.

### Layer 3 — Bonus payouts to opt-in contributors

Paid revenue distribution:

```
80% → contributor who served the paid job (per-token payout)
20% → platform (coordinator infra + future development)
```

This **aligns incentives**: adding paid customers grows the prize pool,
which attracts more PLATINUM uptime, which grows total network
capacity, which benefits free contributors too. The platform never
extracts from contributor activity — only from paid activity, capped.

### Realistic earnings by GPU class

Honest numbers for what a contributor actually nets after electricity,
at US-median $0.16/kWh and the $1.50/1M-tokens × 80% split:

| GPU | Per 1M tokens (margin) | 1 hr/day active | 3 hr/day | 8 hr/day saturated |
|---|---:|---:|---:|---:|
| Basic (RTX 3060, 170 W, 30 tok/s) | $0.95 (79%) | $3/mo | $9/mo | **$24/mo** |
| Mid (RTX 4070, 200 W, 70 tok/s) | $1.07 (89%) | $8/mo | $22/mo | **$58/mo** |
| High (RTX 4090, 450 W, 100 tok/s) | $1.00 (83%) | $11/mo | $32/mo | **$87/mo** |

Per-token margin holds at 60–90% even on basic GPUs in expensive
electricity territory. But **idle overhead bites basic GPUs hard** — a
3060 left loaded 24/7 burns ~$3.50/mo in idle power, which can wipe a
light-demand month. The demand-driven uptime signal (see § 14 Phase
3b.ii) is load-bearing for basic-GPU profitability, not optional.

Practical framing:
- **Basic-GPU pitch**: free AI for you + invitees; near-zero power cost
  when demand is low; occasional Netflix-sub bonus when network is busy.
- **High-end-GPU pitch**: real secondary income at saturation
  (~$80–90/mo even in California), plus community status.

### Sustainability target

Coordinator infra: ~€8/month today; ~$50/month at 1k users; ~$200/month
at 10k users. Break-even at the $1.50/1M DEVELOPER tier:

- $50/month coordinator = 33M tokens/month of paid usage
- 33M tokens at 50 tok/s = ~6 hours/day of one PLATINUM contributor in
  the paid pool

Translation: **two paying developer customers + one PLATINUM
contributor covers the founder's coordinator bill indefinitely.** A
year-one milestone, not a unicorn target — the explicit answer to "how
does the founder stop self-hosting at a loss."

## 6. Contributor value proposition

Why someone runs the agent on their gaming PC. The dial that controls
each of these is **uptime + capability + jobs served** (i.e. your tier):

- **Be the host.** You're the person who runs AI for your household,
  friend group, D&D group, coworking space — whoever you invite.
- **Your own access** to the full toolbox, tiered by what you
  contribute. PLATINUM contributors effectively never hit their cap.
- **Status that compounds.** Tier badges on the leaderboard; first
  access to new tools as they ship; longer Ollama keep-alive windows so
  your latency stays low.
- **Opt-in paid-pool bonuses at GOLD+.** Earn per-token payouts on paid
  customer jobs you serve. Power consumption scales with paid demand,
  and so do the payouts — power bill and bonus are correlated, not
  decoupled.
- **No exclusivity.** Leave the network at any time; in-flight jobs are
  automatically requeued by the reaper. Your tier drops over time if
  you stop contributing, but rejoining is one-click.

**Power draw scales with demand, not uptime.** A contributor's marginal
power cost: ~0 W when no jobs are arriving, ~30 W during the Ollama
keep-alive window after a recent job, ~250–400 W during active
inference. Leaving the agent online overnight on a quiet network costs
near-zero; bursts of real power happen when there are real users (and,
at GOLD+, real payouts).

## 7. User experience

Two distinct audiences, both reaching the same coordinator:

### For contributors and their invitees (the default audience)

- **No subscription.** You're already paying with idle GPU cycles.
- **Prompts handled by community-contributed GPUs**, not OpenAI's or
  Anthropic's data centers. Your data is not used to train anyone's
  model.
- **Full toolbox in one place** — chat, image, search, and whatever
  ships next, behind a unified UI.

> **Privacy framing — honest version.** Under the membership rule, your
> prompts traverse the contributor network (not the public internet,
> not a hyperscaler) but they do flow through random contributors'
> GPUs, not specifically your inviter's machine. For sensitive
> prompts, the Phase 5 client-side embedding tier removes raw text
> from the wire — that's the answer to "is this private *enough*."

### For paid customers (Phase 3b+)

- **Undercut Anthropic Haiku on price** — gaming-PC supply is
  structurally cheaper than data-center supply.
- **Async-friendly API**, no rate limits beyond the size of the opt-in
  PRO/PLATINUM pool.
- **Opt-in privacy-tier routing** for enterprise: pin jobs to vetted
  worker pools, client-side embedding for the strictest cases.

## 8. Limitations (honest)

This is an MVP. Don't ship it to customers as-is.

- **Higher latency than centralized providers.** Cold-start and network
  delays are simulated for realism — they're real on consumer hardware.
- **Lower reliability than a hyperscaler.** No multi-region failover, no
  durable replication, no SLO. Workers can disappear mid-job (handled by
  requeue, but customers see latency spikes).
- **No batching.** One prompt per request. Real systems batch aggressively.
- **No auth / billing.** Local-only; everyone is trusted.
- **Token counts are approximated** when Ollama doesn't report them
  (`len(text) // 4`). Production would use the model's actual tokenizer.
- **No proof-of-work.** The MVP trusts workers to report honest output and
  honest token counts. A real network needs result verification (consensus,
  challenge-response, watermarking).
- **Local-only.** No TLS, no public ingress, no cloud yet.

## 9. Local setup

### Prerequisites

- Docker Desktop (Mac/Windows) or Docker Engine + Compose plugin (Linux).
- Optional: [Ollama](https://ollama.com) on the host. Without it, run in
  mock mode — see below.

### Run with real inference (Ollama)

```bash
# on the host
ollama serve &
ollama pull llama3.2:1b

# from this repo
docker compose up --build
```

Switch model: `MODEL=mistral docker compose up --build`.

### Run without Ollama (mock mode)

```bash
MOCK_INFERENCE=true docker compose up --build
```

### Scale workers

```bash
docker compose up --build --scale worker=3
```

Each worker registers with a unique ID and shows up in `GET /workers`.

### Try it

**Web UI:** open <http://localhost:8080>.

**CLI:**

```bash
python client/client.py "Explain GPUs simply"
python client/client.py --workers
python client/client.py --earnings
python client/client.py --metrics
python client/client.py --result <job_id>
```

**curl:**

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Explain GPUs simply"}'
# => {"job_id":"…"}

curl http://localhost:8000/result/<job_id>
curl http://localhost:8000/workers
curl http://localhost:8000/earnings
curl http://localhost:8000/metrics
```

### Production deploy (MVP test)

To stand up a public coordinator that real gamer machines can connect to,
spin up an Ubuntu VPS (Hetzner CPX21 ~€8/mo works), point a domain at it,
and run the one-shot installer:

```bash
curl -sSL https://raw.githubusercontent.com/Hyrumdrums/GamerAI/main/infra/bootstrap.sh \
  | sudo bash -s -- --domain coordinator.example.com --email you@example.com
```

The script installs Docker, configures `ufw`, clones the repo, generates a
`WORKER_TOKEN`, and brings up the stack with Caddy in front of it
(automatic Let's Encrypt TLS). Total time: ~10 minutes.

See `infra/README.md` for the full runbook including auth-on procedure,
backups, and graduation criteria for moving to Terraform / AWS.

## 10. API

| method | path                       | description                                           |
| ------ | -------------------------- | ----------------------------------------------------- |
| POST   | `/generate`                | `{prompt, model?}` → `{job_id}`. Optional `Idempotency-Key` header makes retries safe. |
| GET    | `/result/{job_id}`         | result JSON (status: `pending`/`running`/`complete`/`error`) |
| GET    | `/workers`                 | list of workers + status, last_seen, totals, capabilities |
| GET    | `/earnings`                | per-worker `{worker_id, total_tokens, total_usd}`    |
| GET    | `/earnings/{worker_id}`    | single worker earnings record                         |
| GET    | `/metrics`                 | totals, completed, avg latency, queue depth, etc.    |
| GET    | `/models`                  | catalog of known models + strict-mode flag           |
| POST   | `/register`                | worker self-registration; optional `capabilities` body (`vram_gb`, `gpu_model`, `tools[]`, `models[]`) |
| POST   | `/heartbeat`               | worker liveness + status (`idle`/`busy`/`offline`)   |
| POST   | `/jobs/claim`              | worker reports it has claimed a job                   |
| POST   | `/jobs/complete`           | worker submits result; coordinator credits earnings   |
| GET    | `/health`                  | redis ping                                            |

> Multi-tool API (planned, see § 14 Phase 3). `/generate` will accept a
> `job_type` field (`chat` | `image` | `search`) discriminating a typed
> `params` block; legacy `{prompt, model}` payloads will still work as
> implicit `job_type=chat`. Routing is via per-tool Redis queues
> (`job_queue:chat`, `job_queue:image`, …) that workers subscribe to based
> on their advertised `tools[]` capability.

## 11. Configuration

All services read from environment variables (see `shared/config.py`).

| var                       | default                          | service     |
| ------------------------- | -------------------------------- | ----------- |
| `REDIS_URL`               | `redis://redis:6379/0`           | all         |
| `DB_PATH`                 | `/data/gamerai.db`               | coordinator |
| `JOB_TIMEOUT_SECONDS`     | `120`                            | coordinator |
| `WORKER_TIMEOUT_SECONDS`  | `15`                             | coordinator |
| `COORDINATOR_URL`         | `http://coordinator:8000`        | worker, client |
| `WORKER_ID`               | auto-generated                   | worker      |
| `MODEL`                   | `llama3.2:1b`                    | worker      |
| `OLLAMA_URL`              | `http://host.docker.internal:11434` | worker   |
| `MOCK_INFERENCE`          | `false`                          | worker      |
| `AVAILABILITY_WINDOW`     | `always` (or `HH-HH` UTC)        | worker      |
| `NETWORK_DELAY_MIN/MAX`   | `0.5` / `3.0` seconds            | worker      |
| `COLD_START_MIN/MAX`      | `2.0` / `8.0` seconds            | worker      |
| `RATE_PER_TOKEN`          | `0.000005`                       | platform    |
| `WORKER_SHARE`            | `0.7`                            | platform    |
| `API_TOKEN`               | _unset_ (auth disabled)          | all         |
| `RATE_LIMIT_PER_MIN`      | `0` (disabled)                   | coordinator |
| `IDEMPOTENCY_TTL_SECONDS` | `86400`                          | coordinator |
| `STRICT_MODELS`           | `false` (any model name accepted) | coordinator |

## 12. Project structure

```
.
├── coordinator/           FastAPI app, SQLite, scheduler
│   ├── main.py
│   ├── db.py
│   ├── scheduler.py
│   ├── redis_client.py
│   ├── requirements.txt
│   └── Dockerfile
├── worker/                worker daemon (gamer simulation)
│   ├── worker.py
│   ├── requirements.txt
│   └── Dockerfile
├── client/                CLI + web UI
│   ├── client.py          ← `python client/client.py "prompt"`
│   ├── web.py             ← FastAPI web UI on :8080
│   ├── requirements.txt
│   └── Dockerfile
├── shared/                shared schemas + config
│   ├── config.py
│   └── models.py
├── infra/                 placeholder for future Terraform/CDK
├── data/                  SQLite volume (gitignored)
├── docker-compose.yml
└── README.md
```

## 13. Observability

- **Structured JSON logs** from coordinator and worker include `service`,
  `worker_id`, `job_id`, and `event` fields. Pipe through `jq`:

  ```bash
  docker compose logs -f worker | jq 'select(.event == "complete")'
  ```

- **`GET /metrics`** returns counts and average latency in JSON, suitable
  for scraping into a dashboard.

## 14. Roadmap

### Phase 1 — local MVP (current)

- [x] Coordinator + Redis + workers running locally
- [x] SQLite write-through; per-job payout ledger
- [x] Reaper-based timeout requeue
- [x] Web UI + CLI client
- [x] Structured logs + `/metrics`
- [x] Simulated gamer realism (network delay, cold start, availability)

### Phase 2 — public deployment + real GPU nodes

**Phase 2a — single-VPS MVP test (current)**

- [x] One-shot VPS bootstrap script (`infra/bootstrap.sh`)
- [x] Caddy-fronted TLS via Let's Encrypt
- [x] Production docker-compose overlay; internal services on localhost only
- [x] `.env.prod` with generated `WORKER_TOKEN` (auth-ready, opt-in)
- [ ] Bearer-auth wired through worker + Windows agent code
- [ ] Real worker installer (one-line `curl | sh`, with auto-update)
- [ ] Connect 3–5 real gamer machines and validate end-to-end loop

**Phase 2b — production AWS (after MVP signal)**

- [ ] Terraform under `infra/` (VPC, ECS Fargate, ElastiCache, RDS)
- [ ] API keys for customers, signed registration for workers
- [ ] Move SQLite → Postgres / DynamoDB
- [ ] CloudWatch / OTLP log + metric ingestion
- [ ] Multi-region for latency to gamers

The VPS path keeps us shipping; Terraform comes when at least one of these
is true: 10+ active workers, real money flowing, multi-region latency
needs, or SQLite contention.

### Phase 3 — AI toolbox + community plumbing + paid layer

The strategic reframing: the platform is not a chat API, it's a *job-based
toolbox* of independent, retryable, latency-tolerant AI workloads served
by a contributor network. Phase 3 turns the chat-only MVP into a multi-
tool product, adds membership + tier infrastructure, then layers in the
optional paid customer revenue.

**Phase 3a — multi-tool foundations**

- [x] `tool` on `/generate` (`chat` | `image`) with an optional
      `image: {width, height, steps, seed, negative_prompt}` params
      block. Legacy `{prompt, model}` payloads keep working as
      implicit `tool=chat`. (Shipped 2026-05-20.)
- [x] Worker `capabilities.tools[]` registration; coordinator routes
      via per-tool Redis queues (`job_queue`, `job_queue:image`).
      Workers call `/jobs/next` with a `tool` field per tick and
      receive only matching jobs. (Shipped 2026-05-20.)
- [x] DB schema: `jobs.tool` column added; `messages.image_path`
      column for image attachments; `/data/images/<job_id>.png` for
      generated PNG storage; ownership-checked `/images/<name>`
      route. (Shipped 2026-05-20.)
- [x] Image-generation worker mode — Windows agent bootstraps
      `sd.exe` + `stable-diffusion.dll` + `sd1.5.gguf` from the
      mirror on first run, advertises `tools=["chat","image"]`, and
      runs `stable-diffusion.cpp` as a subprocess on image jobs.
      Returns PNG via base64 in `/jobs/complete`. (Shipped
      2026-05-21 end-to-end at agent v1.1.6.)
- [x] Unified "toolbox" UI — Chat/Image toggle in the existing chat
      composer (no separate route). Image messages render as inline
      `<img>` bubbles via `/api/images/<name>` proxy. (Shipped
      2026-05-20.)
- [ ] Web-search tool — runs *server-side* on the coordinator.
      Fetches results from a search API, prepends them to the prompt
      as context, then dispatches a chat job. No new worker type
      needed; biggest perceived-intelligence boost for small models.
- [ ] SDXL on the mirror — currently MVP ships SD 1.5 (1.5 GB Q4_0).
      SDXL (~6.5 GB) is registered in the model catalog but the
      mirror only serves it when promoted. See
      `infra/setup-image-mirror.sh` TODO.
- [ ] Per-image-job pricing — image earnings are flat-rated as
      ~200-token equivalents for MVP. Real per-image rates ship
      alongside paid-customer pricing in Phase 3b.ii.
- [ ] Image canaries — chat canaries cover model-swap detection
      for chat workers; image equivalent would need a known-good
      PNG fingerprint per prompt. Currently image worker outputs
      are trusted (PNG-magic + size cap only).

**Phase 3b — community plumbing first, paid layer second**

The strategic order: community/tier infrastructure ships before the
paid layer. Without tiers and invites, the paid pool has nothing to
opt into; without membership accounting, there's no fair way to gate
quotas.

**3b.i — Membership and tier engine**

- [ ] Member token issuance (replaces the single shared API token).
      Tokens identify a contributor; coordinator records them on every
      job for tier accounting.
- [ ] Tier promotion engine — measures uptime + capability + claimed-
      jobs-per-hour; promotes/demotes contributors across BRONZE →
      PLATINUM nightly.
- [ ] Per-tier quota enforcement on `/generate`. Free quota = sum of
      contributor's own + each invitee's remaining allowance.
- [ ] Invitee/invite flow — contributor invites by email; sets cap
      (% of own quota or absolute tokens); revokes/adjusts anytime.
- [ ] Host admin UI — manage invitees, see your tier, see jobs
      contributed vs. jobs consumed.

**3b.ii — Paid customer layer**

- [ ] Paid customer onboarding (signup, billing, API key issuance).
      Four tiers: CASUAL flat-fee, DEVELOPER realtime per-token,
      BATCH non-realtime per-token, ENTERPRISE volume contracts.
- [ ] Paid-job priority queue, separate from the contributor queue.
      BATCH scheduler that fills jobs into low-utilization windows.
- [ ] Opt-in toggle on the contributor agent — GOLD+ contributors can
      enable serving paid jobs (gated on 1-week reliability proof).
- [ ] Bonus payout ledger (per-token earnings for paid jobs served).
- [ ] Stripe Connect (or equivalent) for monthly contributor payouts.
- [ ] **Supply-demand signal loop** — utilization-driven acquisition
      triggers:

      | Util | State | Action |
      |---|---|---|
      | <50% | Spare | Paid-customer acquisition (BATCH campaigns, dev forums) |
      | 50–70% | Steady | No action |
      | 70% | Yellow | Ops attention; DEVELOPER discount campaigns |
      | 85% | Tight | Dashboard alert to offline GOLD+ contributors: "Usage is growing — consider adjusting your uptime to reach the next tier" |
      | 90%+ | Surge | New-signup pricing surge; cap CASUAL signups |

      Two-direction acquisition runs against the loop: low utilization
      triggers paid-customer marketing (HN, r/MachineLearning, API
      aggregator listings); high utilization triggers contributor
      recruiting, **geographically targeted** to fix the time-of-day
      anti-correlation (paid demand peaks 9–6 weekdays, gamer supply
      peaks overnight/weekends — recruit EU/APAC contributors to fill
      US business hours).

**3b.iii — Trust & verification**

Demoted from Phase 3 critical-path under the tier-based meritocracy
model — bad actors fall down the ladder organically. Still worth
shipping when there's real volume:

- [ ] Dynamic pricing based on supply/demand
- [ ] Worker reputation scoring (independent of tier, e.g. "did the
      response satisfy the user")
- [ ] Result verification (challenge jobs, k-of-n consensus on a
      random sample)
- [ ] Customer dashboards, billing history, invoicing

### Phase 4 — frontier-model support (big-model expansion)

Goal: serve frontier-class open models (Llama 3.1 405B, DeepSeek-V3 / R1,
Mixtral 8x22B, Llama 3.2 Vision) on top of the same gamer-GPU network.

Strategy is staged: start by reselling an existing public swarm to prove
demand, then bring the engine in-house once we have enough workers to form
private pipeline groups.

**Phase 4a — Petals-backed big-model tier**

- [ ] Wrap [Petals](https://petals.dev/) as a new worker type. Customer
      jobs targeting frontier models route into the public swarm; small-
      model jobs continue on our native Ollama path.
- [ ] Add a `model_class` field (`small` / `frontier`) to the model
      registry and per-class pricing (frontier tier ~5–10× small tier).
- [ ] Worker capability registration: VRAM, bandwidth class, locale.
      Required so the coordinator only sends a 70B+ request to a worker
      that can actually serve it.
- [ ] Draft-model speculative decoding for frontier requests to cut
      end-to-end latency 2–3×.

**Phase 4b — EXO-backed private pipelines**

- [ ] Replace Petals dependency with [EXO](https://github.com/exo-explore/exo)
      under the hood. Customer jobs run on private pipeline groups
      assembled from *our* gamer workers, not anonymous swarm members.
- [ ] Pipeline-group scheduling as a first-class coordinator primitive:
      bind N workers into an ephemeral group with shared health/reaper
      semantics, keep groups warm across jobs to amortize cold-start.
- [ ] Worker-to-worker activation routing (WebSockets / QUIC). Coordinator
      stays on the control plane; activations flow worker-to-worker.
- [ ] Peer-to-peer weight distribution (SHARDCAST-style) so adding a new
      model doesn't saturate platform egress.

This stages the risk: Phase 4a proves paid customers will pay for big-
model inference through our coordinator without us building any of the
hard parts; Phase 4b is what makes us a real network instead of a Petals
reseller.

See `research/big-models-feasibility.md` for the underlying analysis.

### Phase 5 — privacy tiers

The community-powered model has a baseline privacy story: prompts
traverse the contributor network, not a hyperscaler — no training-on-
prompts, no surveillance harvesting. But under the membership rule
(contributors serve the shared queue, not just their own invitees),
strangers' GPUs do see prompts in cleartext. That's fine for most
casual use; it's not fine for enterprise customers or sensitive
prompts.

Phase 4 adds an additional privacy gap when pipeline-parallel inference
ships: the worker that runs the embedding layer sees the customer's
raw prompt. Middle workers see hidden-state vectors (not human-readable,
but theoretically invertible). The last worker sees the output logits.

We won't match a hyperscaler's "your data never leaves our datacenter"
story by default, but we can offer tiered privacy that's good enough for
most workloads — and better than centralized providers for some. The
**client-side embedding tier** (below) is the load-bearing item for
enterprise paid customers and any contributor who wants a real privacy
guarantee.

- [ ] **Standard tier (default).** TLS in transit, prompts handled in
      worker memory only, agent never writes prompts to disk, ephemeral
      session keys per job.
- [ ] **Private tier — client-side tokenization + embedding.** The
      customer SDK runs the tokenizer and embedding layer locally and
      sends *embeddings* into the pipeline, not raw text. No worker on
      the network sees the prompt as text. Output logits are returned
      to the client and decoded locally. Cheap to implement, large
      privacy win.
- [ ] **Vetted-pool tier.** KYC'd workers, reputation-gated, locale-
      pinned (e.g., US-only, EU-only), audit log per job. Customers pay
      a premium and pick the pool. Same model as private cloud regions.
- [ ] **TEE tier (future).** Route jobs only to workers with confidential-
      compute-capable GPUs (NVIDIA H100/H200 confidential mode, and
      consumer cards as the feature trickles down). Hardware attestation
      proves the worker can't observe the prompt or weights.
- [ ] **Output redaction.** Coordinator-side optional pass that strips
      common PII patterns from outputs before returning to the customer
      (defense in depth, not a primary control).
- [ ] **No-log audit mode.** For sensitive customers, the coordinator
      stores only the billing record (job ID, token counts, worker IDs)
      — not prompts, not outputs, not intermediate state.

The client-side-embedding approach is the high-leverage one: it changes
"strangers' GPUs see your prompts" to "strangers' GPUs see vectors that
look like noise." That's the answer to the obvious "would you trust
this?" objection from enterprise customers.

## 15. License

Apache-2.0. See `LICENSE` for the full text.
