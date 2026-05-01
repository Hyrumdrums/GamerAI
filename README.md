# GamerAI

> **A distributed AI inference marketplace where idle gaming PCs earn money running models for paying customers.**

This repo is a fully local, containerized MVP of that marketplace. One command
brings up a coordinator, Redis queue, SQLite store, a web UI, and any number
of worker nodes that simulate gamer machines.

```bash
docker compose up --build
# open http://localhost:8080
```

---

## 1. Product overview

GamerAI is a **distributed inference marketplace**.

- **Workers** (gamers) install a small client on their machine. When idle,
  the machine joins the network and runs inference jobs. They earn USD per
  token of output.
- **Customers** (developers, apps, end users) submit prompts via the API or
  web UI. Their requests are dispatched to the cheapest available worker.
- **The platform** runs the coordinator, queue, and ledger, and takes a
  percentage of each transaction.

The network is fully **per-job**: payouts are computed on every completed
inference and credited to the worker's earnings ledger.

## 2. Problem statement

Two facts about the AI market today:

1. **Centralized inference is expensive.** OpenAI, Anthropic, and the major
   cloud GPUs charge a premium that reflects scarcity, not marginal cost.
2. **Massive amounts of compute sit idle.** There are tens of millions of
   gaming PCs with capable GPUs that run at <5% utilization most of the day.

The gap between #1 and #2 is the opportunity.

## 3. Solution

A **two-sided marketplace** with:

- A **coordinator** that accepts jobs, queues them, dispatches to idle workers,
  and tracks per-job payouts.
- **Worker nodes** running on consumer hardware. They poll for jobs, run
  inference locally (Ollama / llama.cpp / vLLM in production), and submit
  results.
- **Customers** that submit prompts via REST or a UI.

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

## 5. Business model

### Revenue

Customers pay per token. Default reference pricing baked into the MVP:

```
$5 per 1M tokens   →   RATE_PER_TOKEN = $0.000005 / token
```

(For comparison: GPT-4o is ~$10/1M output tokens, Claude Haiku is ~$1.25/1M.
GamerAI targets the value tier with consumer GPUs.)

### Costs

The platform pays workers a fixed share per token:

```
WORKER_SHARE = 0.7      # gamer keeps 70%
PLATFORM     = 0.3      # platform keeps 30%
```

### Margin

```
worker payout per token   = $0.000005 * 0.70 = $0.0000035
platform margin per token = $0.000005 * 0.30 = $0.0000015
```

At 1B tokens/month: **$1,500/month gross margin** before infra costs (which
are minimal — coordinator + Redis + DB; the GPUs are not on the platform's
balance sheet).

## 6. Worker value proposition

- **Passive income** from a machine that's already on.
- **Zero work when idle** — workers respect an availability window and skip
  inference outside it.
- **Transparent earnings** — every completed job credits the ledger; check
  `GET /earnings/{worker_id}` or the dashboard at any time.
- **No exclusivity** — workers can leave the network at any time; in-flight
  jobs are automatically requeued by the reaper.

## 7. Customer value proposition

- **Cheaper inference** — consumer GPUs at scale undercut hyperscaler pricing
  for many workloads.
- **Scalable background processing** — async-friendly API, no rate limits
  beyond the size of the worker pool.
- **Optional privacy angle** — open-source models running on independent
  nodes; the platform never sees the model weights and customers can pin
  jobs to vetted worker pools (future).

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

## 10. API

| method | path                       | description                                           |
| ------ | -------------------------- | ----------------------------------------------------- |
| POST   | `/generate`                | `{prompt, model?}` → `{job_id}`                       |
| GET    | `/result/{job_id}`         | result JSON (status: `pending`/`running`/`complete`/`error`) |
| GET    | `/workers`                 | list of workers + status, last_seen, totals          |
| GET    | `/earnings`                | per-worker `{worker_id, total_tokens, total_usd}`    |
| GET    | `/earnings/{worker_id}`    | single worker earnings record                         |
| GET    | `/metrics`                 | totals, completed, avg latency, queue depth, etc.    |
| POST   | `/register`                | worker self-registration                              |
| POST   | `/heartbeat`               | worker liveness + status (`idle`/`busy`/`offline`)   |
| POST   | `/jobs/claim`              | worker reports it has claimed a job                   |
| POST   | `/jobs/complete`           | worker submits result; coordinator credits earnings   |
| GET    | `/health`                  | redis ping                                            |

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

### Phase 2 — AWS deployment + real GPU nodes

- [ ] Terraform under `infra/` (VPC, ECS Fargate, ElastiCache, RDS)
- [ ] TLS + auth (API keys for customers, signed registration for workers)
- [ ] Real worker installer (one-line `curl | sh` for gamers, with auto-update)
- [ ] Move SQLite → Postgres / DynamoDB
- [ ] CloudWatch / OTLP log + metric ingestion

### Phase 3 — marketplace + dynamic pricing

- [ ] Multiple model tiers with per-tier pricing
- [ ] Dynamic pricing based on supply/demand
- [ ] Worker reputation + slashing for bad output
- [ ] Result verification (challenge jobs, k-of-n consensus)
- [ ] Customer dashboards, billing, invoicing
- [ ] Worker payout rails (Stripe Connect / crypto)

## 15. License

TBD.
