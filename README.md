# GamerAI — Distributed Inference MVP

A fully local, containerized prototype of a distributed AI inference network
with per-job payouts. The goal is to model the moving parts of a real GPU
marketplace — coordinator, job queue, worker registry, payouts — without
involving cloud infrastructure yet.

Everything runs with one command:

```bash
docker compose up --build
```

## Overview

In a distributed inference network, idle GPUs (think gamers' machines) earn
money by running inference jobs submitted by clients. A central **coordinator**
accepts prompts and queues them; **workers** poll the queue, run the model
locally (via Ollama), return the result, and get credited per token.

This MVP simulates that loop end-to-end on a single host.

## Architecture

```
                 ┌──────────────────┐
   client ──▶    │   coordinator    │   FastAPI, port 8000
                 │  (REST + queue)  │
                 └──────┬───────────┘
                        │  rpush / hset
                        ▼
                 ┌──────────────────┐
                 │      redis       │   queue + results + earnings
                 └──────┬───────────┘
                blpop  / hget
                        ▲
        ┌───────────────┼───────────────┐
        │               │               │
   ┌────┴───┐      ┌────┴───┐      ┌────┴───┐
   │ worker │      │ worker │      │ worker │   --scale worker=N
   └────┬───┘      └────┬───┘      └────┬───┘
        └──────────── Ollama ───────────┘
              (host.docker.internal:11434)
```

### Services

| service       | role                                          | port |
| ------------- | --------------------------------------------- | ---- |
| `coordinator` | FastAPI app: submit jobs, fetch results       | 8000 |
| `redis`       | job queue, result store, registry, earnings   | 6379 |
| `worker`      | polls queue, runs inference, updates earnings | —    |

### Redis keys

| key                 | type | purpose                                  |
| ------------------- | ---- | ---------------------------------------- |
| `job_queue`         | list | FIFO queue of pending jobs (JSON blobs)  |
| `job_results`       | hash | `job_id -> result JSON`                  |
| `worker_registry`   | set  | known worker IDs                         |
| `worker_heartbeats` | hash | `worker_id -> last heartbeat ts`         |
| `worker_earnings`   | hash | `worker_id -> earnings JSON`             |

## Setup

### 1. Install Docker

Install Docker Desktop (Mac/Windows) or Docker Engine + Compose plugin (Linux).

### 2. Install and run Ollama (on the host)

The workers call Ollama on the host at `host.docker.internal:11434`.

```bash
# https://ollama.com/download
ollama serve &
ollama pull llama3.2:1b
```

You can override the model via env var:

```bash
MODEL=mistral docker compose up --build
```

If you don't want to install Ollama, run the workers in mock mode:

```bash
MOCK_INFERENCE=true docker compose up --build
```

### 3. Bring up the stack

```bash
docker compose up --build
```

### 4. Scale workers

```bash
docker compose up --build --scale worker=3
```

Each worker registers itself with a unique `worker_id` and shows up in
`GET /workers`.

## Usage

Submit a job:

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Explain GPUs simply"}'
# => {"job_id":"a3f1..."}
```

Poll for the result:

```bash
curl http://localhost:8000/result/a3f1...
```

While pending you'll get `{"status":"pending"}`; once a worker finishes you get:

```json
{
  "job_id": "a3f1...",
  "status": "complete",
  "worker_id": "worker-abc-1f2e3d",
  "model": "llama3.2:1b",
  "text": "...",
  "prompt_tokens": 6,
  "completion_tokens": 84,
  "earnings": 0.000000294,
  "duration_seconds": 1.42
}
```

List active workers:

```bash
curl http://localhost:8000/workers
```

View earnings:

```bash
curl http://localhost:8000/earnings
```

### CLI

A tiny pure-stdlib CLI is included:

```bash
python client/cli.py submit "Explain GPUs simply"
python client/cli.py result <job_id>
python client/cli.py wait "Write a haiku about Redis"
python client/cli.py workers
python client/cli.py earnings
```

## Earnings

Each completed job credits the worker that handled it:

```
earnings = completion_tokens * RATE_PER_TOKEN * WORKER_SHARE
```

Defaults:

- `RATE_PER_TOKEN = 0.000005`
- `WORKER_SHARE  = 0.7`

Cumulative totals (earnings, jobs, tokens) are stored per worker in the
`worker_earnings` Redis hash and exposed via `GET /earnings`.

## API

| method | path             | description                     |
| ------ | ---------------- | ------------------------------- |
| POST   | `/generate`      | `{prompt}` → `{job_id}`         |
| GET    | `/result/{id}`   | result JSON or `{status:pending}` |
| GET    | `/workers`       | registered workers + liveness   |
| GET    | `/earnings`      | per-worker earnings + total     |
| POST   | `/register`      | worker self-registration        |
| POST   | `/heartbeat`     | worker liveness ping            |
| GET    | `/health`        | redis ping                      |

## Limitations

- No real GPU scheduling — any worker takes any job
- No fault tolerance — if a worker dies mid-job the job is lost
- No batching — one prompt per job
- No auth — local only
- Token counts fall back to `len(text) // 4` if Ollama doesn't report them
- No persistence beyond Redis's default config

## Migrating to AWS later

The compose layout maps cleanly onto cloud primitives:

- `redis` → ElastiCache
- `coordinator` → ECS/Fargate or App Runner behind ALB
- `worker` → EC2 GPU instances (or external GPU providers) running the same image
- `OLLAMA_URL` becomes per-worker localhost
- `COORDINATOR_URL` becomes the ALB DNS name

Nothing in the code assumes a single host beyond the default `host.docker.internal`
URL for Ollama.

## File structure

```
.
├── coordinator/
│   ├── main.py
│   ├── redis_client.py
│   ├── requirements.txt
│   └── Dockerfile
├── worker/
│   ├── worker.py
│   ├── requirements.txt
│   └── Dockerfile
├── client/
│   └── cli.py
├── docker-compose.yml
└── README.md
```
