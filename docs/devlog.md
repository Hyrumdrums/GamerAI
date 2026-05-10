# Developer log

Chronological record of meaningful changes, decisions, and gotchas. Most-recent
entries on top. Skim for context before resuming work.

---

## 2026-05-09 — Strategy refresh: AI toolbox, not chat

Reframing the product from "distributed chat API" to "distributed AI toolbox"
of independent, retryable, latency-tolerant jobs. The architecture we already
built (job-based, queued, capability-aware on the data layer) supports this
without redesign — it just needs schema extensions and a second worker type.

### What changed in the docs

- README § 1 — toolbox framing + "Tools supported" table (chat MVP; image +
  search next; docs/code expansion; music/voice later; video and frontier
  training out of scope).
- README § 14 — old Phase 3 ("marketplace + dynamic pricing") split into
  **3a (multi-tool foundations)** and **3b (marketplace dynamics)**. 3a is
  now the next-up phase after the Windows-worker test.
- README § 10 — API table notes the planned `job_type` field on `/generate`
  and per-tool queue routing.
- `business.md` — added a "Supported tools" section, updated roadmap to
  reflect public deploy + toolbox.
- `docs/project-gaps.md` — capability-aware routing promoted from Phase 4
  to Phase 3a.

### Schema sketch (not yet implemented)

```
GenerateRequest:
  job_type: "chat" | "image" | "search"  (default "chat" for back-compat)
  params:   discriminated union of ChatParams | ImageParams | SearchParams

WorkerCapabilities:
  + tools: List[ToolType]  (e.g. ["chat", "image"])

Redis:
  job_queue:chat, job_queue:image, job_queue:search   (was: job_queue)

DB jobs table:
  + job_type TEXT NOT NULL DEFAULT 'chat'
  + result_blob TEXT   (base64/URL for non-text outputs)
```

Worker side: `BLPOP` over `[job_queue:t for t in capabilities.tools]`. No
new component, no broker rewrite — implicit routing via queue subscription.

Search runs *inside the coordinator* (no GPU lift); it fetches results, then
dispatches an internal chat job with augmented context. Image generation is
a real new worker mode (SDXL-class, ~8–12 GB VRAM).

### Sequence (not commitments — pending decision)

1. Real Windows worker test on chat (the milestone we were already heading
   toward — needed first, regardless of toolbox plan).
2. Phase 3a: `job_type` + capability routing schema, behind back-compat
   shim. ~3 days.
3. Web-search tool (centralized, no new worker type). ~1 day.
4. Image-gen worker mode + SDXL backend on Windows agent. ~1 week.
5. Unified toolbox UI (slash-commands or tabs).

### Open questions

- Earnings ledger needs per-tool rates (per-token doesn't apply to images).
  Cleanest schema: rename `total_tokens` → `total_units` and add a `unit`
  column. Defer until image gen actually ships.
- Quality variance across heterogeneous workers (1B vs 13B chat) is a real
  UX risk under the "abstract model complexity" principle. May need to
  expose a tier hint (Fast / Better / Best) instead of pretending the
  network is uniform.

---

## 2026-05-08 — Real inference on the VPS

VPS is no longer mock-mode. Currently serving `llama3.2:1b` from the in-VPS
worker over public TLS at `https://ai.dallinlayton.com/`.

### What changed on the box

```bash
# 1. Brought up the previously profile-gated ollama service
docker compose --env-file .env.prod \
  -f docker-compose.yml -f infra/docker-compose.prod.yml \
  --profile local-inference up -d ollama

# 2. Pulled the model (1.3 GB)
docker exec gamerai-ollama ollama pull llama3.2:1b

# 3. Flipped /opt/gamerai/.env.prod
MOCK_INFERENCE=false   # was true

# 4. Recreated worker so it picks up the new env
docker compose --env-file .env.prod \
  -f docker-compose.yml -f infra/docker-compose.prod.yml \
  --profile local-inference up -d worker
```

`infra/deploy.sh` doesn't include `--profile local-inference`. If you re-deploy
via the script, ollama won't come back up automatically. Either re-run the
profile-aware command above, or extend `deploy.sh` to detect when MOCK_INFERENCE
is false and pass the profile.

### Performance baseline

| Model | Hardware | Latency (38-prompt → 65-completion) | Throughput |
|---|---|---|---|
| `llama3.2:1b` | CPX21, 3 vCPU AMD, no GPU | ~10 s | ~6.5 tok/s |

Adequate for pipeline validation. Way too slow for customer-facing serving;
real inference is supposed to come from gamer machines.

### Resource state with model loaded

```
RAM total:    3.7 GB
Used:         1.9 GB  (ollama 1.94 GB / 3.7 GB — model weights resident)
Available:    1.5 GB
Swap:         none
```

Headroom is fine for the current workload. **Do not pull a 3B or 7B model on
this box** — it will OOM. Larger models go on gamer machines, not the VPS.

### Followups worth doing

- Decide whether the VPS keeps running real inference, or reverts to mock once
  external workers are connecting. Argument for reverting: VPS isn't supposed
  to be a worker, and the 1.9 GB of resident weight competes with future
  coordinator load. Argument for keeping: a no-workers-online state still has
  *something* to answer prompts.
- Teach `infra/deploy.sh` to honor a flag (or read `.env.prod`) so re-deploys
  don't silently lose the ollama service.
- Add a swap file (~2 GB) so an OOM doesn't kill the coordinator.

---

## 2026-05-07 — First public deploy

**Live:** https://ai.dallinlayton.com (Let's Encrypt cert, auto-renewed by Caddy)

### Infrastructure

| Thing | Value |
|---|---|
| Provider | Hetzner Cloud |
| Server | `gamerai-mvp` — CPX21 (3 vCPU AMD, 4 GB RAM, 80 GB SSD) |
| Region | Ashburn, VA (`ash`) |
| Public IP | `5.161.253.156` |
| Cost | ~€8.21/mo |
| OS | Ubuntu 22.04 |
| Firewall | Hetzner Cloud firewall `gamerai-public` (22/80/443 in) + ufw on box |
| Domain | `ai.dallinlayton.com` (Namecheap A record → public IP, TTL 300) |
| TLS | Let's Encrypt via Caddy 2 |

### Access

| Use | Key | Path |
|---|---|---|
| SSH to VPS | `id_ed25519_gamerai` | `~/.ssh/id_ed25519_gamerai` (local), `~/.ssh/authorized_keys` (VPS) |
| GitHub deploy key (read-only) | `gamerai-github-deploy` | local + `/root/.ssh/github_deploy` on VPS |
| Hetzner API context | local CLI | `~/.config/hcloud/cli.toml`, context `gamerai` |
| API_TOKEN (bearer auth) | random 64-hex | `/opt/gamerai/.env.prod` on VPS only — never committed |

### Stack on the box

`/opt/gamerai/` is the canonical install. Bootstrap clones to there.

```
gamerai-redis         redis:7-alpine                   127.0.0.1:6379  internal
gamerai-coordinator   gamerai-coordinator:latest       127.0.0.1:8000  internal (proxied via Caddy)
gamerai-worker-1      gamerai-worker:latest            (no ports)      MOCK_INFERENCE=true
gamerai-client        gamerai-client:latest            127.0.0.1:8080  internal (web UI, SSH-tunnel to view)
gamerai-caddy         caddy:2-alpine                   0.0.0.0:80,443  public
```

Real inference comes from external gamer machines connecting in, not the VPS.
The in-VPS worker is mock-mode so the system is alive before any gamers join.

### Decisions

- **Private repo + deploy key, not public.** Bootstrap was originally designed
  to `curl raw.githubusercontent...` anonymously; that returned 404 because
  the repo is private. Chose deploy key over going public — gives us the
  option to put real secrets / keys in the repo later without flag-day
  retrofitting.
- **Hetzner over DO/Lightsail.** ~½ the price for the same RAM/CPU, 20 TB
  bandwidth included, no managed services we need yet.
- **No IaC for now.** Manual VPS create (or `infra/hcloud-create.sh`) +
  idempotent bash bootstrap. Will graduate to Terraform when we have ≥2 boxes
  or want push-button rebuilds.
- **bearer-token auth on by default.** `API_TOKEN` is generated by bootstrap
  and required on every request except `/health`. Disable by emptying it in
  `.env.prod` and re-running `infra/deploy.sh`.

### Bugs caught and fixed during the first deploy

These all bit during real bootstrap; recording so the next deploy doesn't.

1. **`worker.depends_on.ollama` errored out as "undefined service"** when
   `ollama` was profile-gated to `local-inference` and that profile wasn't
   active. Compose v2 doesn't auto-prune depends_on for profiled services.
   Fix: drop the dep entirely (worker connects to ollama lazily and prod
   runs mock-mode). Commit `b890ac3`.
2. **Compose v2 *appends* port lists across base + overlay** instead of
   replacing. Result: every internal service tried to bind both
   `0.0.0.0:PORT` (from base) and `127.0.0.1:PORT` (from prod overlay) and
   failed with "address already in use". Fix: `ports: !override` in the
   prod overlay. Compose ≥ 2.24 supports this. Commit `6d3cf06`.
3. **Bootstrap couldn't `git clone` from a private repo** anonymously.
   Fix: bootstrap now supports `git@github.com:` URLs and expects a
   read-only deploy key at `/root/.ssh/github_deploy` (placed
   out-of-band by `scp` before bootstrap runs). Persists into
   `core.sshCommand` so `infra/deploy.sh` keeps working without env-var
   plumbing. Commit `138f5f4`.

### Gotchas worth remembering

- **`uid=lxd` (999) inside a redis:7-alpine container is normal.** The
  redis user inside the alpine image happens to be uid 999, which on
  the host's `/etc/passwd` resolves to the dormant `snap.lxd` system
  user. Looks alarming in `ps`; isn't.
- **`bootstrap.sh` curl one-liner is now broken** while the repo is
  private. Run-from-VPS workflow is `scp bootstrap.sh + deploy key` →
  run locally on the box. Public again later → restore the one-liner.
- **Hetzner CPX21 has no GPU.** Don't try to enable real inference on
  the VPS. Real inference comes from external workers.
- **Compose `!override` requires v2.24+.** Hetzner's docker-compose-plugin
  on Ubuntu 22.04 ships the right version (verified v5.1.3).

### How to redeploy after a code change

```bash
ssh -i ~/.ssh/id_ed25519_gamerai root@5.161.253.156
sudo /opt/gamerai/infra/deploy.sh
```

That's `git pull && docker compose up -d --build`. Idempotent.

### How to rebuild the box from scratch

```bash
# Local: provision VPS (idempotent — re-uses existing if name matches)
infra/hcloud-create.sh

# Local: copy deploy key + bootstrap
scp -i ~/.ssh/id_ed25519_gamerai \
  ~/.ssh/gamerai-github-deploy \
  root@<IP>:/root/.ssh/github_deploy
scp -i ~/.ssh/id_ed25519_gamerai \
  infra/bootstrap.sh root@<IP>:/root/bootstrap.sh

# VPS:
ssh -i ~/.ssh/id_ed25519_gamerai root@<IP>
sudo /root/bootstrap.sh \
  --domain ai.dallinlayton.com \
  --email hyrumdrums@gmail.com
```

### Where the API_TOKEN lives

Generated by `infra/bootstrap.sh` into `/opt/gamerai/.env.prod` (mode 600).
Only on the box. If you lose it, rotate:

```bash
ssh -i ~/.ssh/id_ed25519_gamerai root@5.161.253.156 \
  'sed -i "s|^API_TOKEN=.*|API_TOKEN=$(openssl rand -hex 32)|" /opt/gamerai/.env.prod && /opt/gamerai/infra/deploy.sh'
```

Then update every client (Windows agent's `config.json`, web/CLI clients).

---

## 2026-05-06 — Pitch branch merged into MVP branch

Merged 9 commits from `claude/add-startup-pitch-7SKv9` into the MVP branch.
+3133 lines, 32 files. Big additions:

- `business.md` — startup pitch
- `docs/ai-for-beginners.md`, `docs/project-gaps.md`
- `research/big-models-feasibility.md`
- `infra/` — VPS deploy kit (bootstrap, Caddy, prod overlay)
- `LICENSE`, `.github/workflows/ci.yml`, `requirements-dev.txt`
- `shared/auth.py` — bearer-token auth, gated by `API_TOKEN`
- `coordinator/idempotency.py`, `coordinator/rate_limit.py`,
  `coordinator/model_registry.py` + tests + e2e tests

### Conflicts and how they were resolved

- `client/web.py` — pitch branch wrapped every coordinator call in a `_client()`
  helper that injects `auth_headers()`. Local branch had a wholesale dashboard
  rewrite that bypassed it. Resolution: kept the dashboard rewrite, layered
  the `_client()` helper into every endpoint (including the new `/earnings`
  call). All proxy endpoints now go through auth.
- `docker-compose.yml` — pitch added `API_TOKEN` env vars to coordinator/worker/
  client; local added an `ollama` service. Auto-merge clean except for the
  worker block, where local removed `extra_hosts: host.docker.internal` (no
  longer needed once ollama is in-cluster) but pitch added `API_TOKEN` env.
  Kept both sides.
- `.gitignore` — kept both `ollama-models/` (local) and `.env.prod` (pitch).

### Tests after merge

44/44 pass (auth, coordinator e2e, idempotency, model_registry, rate_limit).

---

## Earlier — MVP foundations

Before this log existed, the MVP was built up across three commits:

- `1165ab4` — initial distributed-inference MVP (coordinator, worker, client,
  redis-backed queue, basic earnings calc).
- `b537813` — SQLite store, scheduler, web UI, observability, gamer-realism
  knobs (cold-start delays, network jitter, availability windows).
- `c5128fa` — Windows worker agent (gamer install simulation).

Then the parallel `claude/add-startup-pitch-7SKv9` branch added auth, infra/,
docs, and tests (see merge entry above).
