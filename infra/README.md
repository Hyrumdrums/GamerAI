# infra/ — GamerAI MVP deployment

This directory deploys the GamerAI coordinator + worker stack onto a single
public VPS behind TLS. It is intentionally simple — one bash script, one
Caddyfile, one compose overlay — because the goal is **to vet the MVP with
real workers**, not to ship production infrastructure.

For Phase 2 (production AWS, multi-AZ, autoscaling, RDS) we'd swap this for
Terraform. Until then, this gets you a public `https://...` URL in about
ten minutes for ~$10/month.

---

## What you get

- A public, TLS-terminated coordinator at `https://<your-domain>/`
  (Let's Encrypt cert, auto-renewed by Caddy).
- One in-VPS worker running in mock mode so the system is alive even
  without external workers.
- Internal services (Redis, SQLite-backed coordinator, web UI) bound to
  `127.0.0.1` only — Caddy is the only thing on the public internet.
- Bearer-token auth on by default. The bootstrap generates a random
  `API_TOKEN` and writes it to `.env.prod`; the coordinator middleware
  (`shared/auth.py`) requires `Authorization: Bearer <API_TOKEN>` on every
  request except `/health`. Disable by setting `API_TOKEN=` (empty) in
  `.env.prod` and restarting — useful for testing.
- Idempotent re-runs: re-running the bootstrap is safe.

---

## Prerequisites (manual, once)

These are the only things you do by hand.

1. **A VPS.** Anything with Ubuntu 22.04+, 2 GB RAM, and a public IP works:
   - Hetzner CPX21 — €8/mo (cheapest sane option).
   - DigitalOcean basic droplet — $12/mo.
   - AWS Lightsail $10/mo bundle.
2. **A domain pointing at the VPS.** Add an `A` record for
   `coordinator.example.com` → your VPS's public IP. DNS propagation is
   usually under a minute.
3. **SSH access as root** (or a user with passwordless sudo).

That's it.

---

## Deploy in one command

From the VPS, as root:

```bash
curl -sSL https://raw.githubusercontent.com/Hyrumdrums/GamerAI/main/infra/bootstrap.sh \
  | sudo bash -s -- \
    --domain coordinator.example.com \
    --email you@example.com
```

The script:

1. Installs Docker Engine and the Compose plugin.
2. Configures `ufw` to allow only `:22`, `:80`, `:443`.
3. Clones this repo to `/opt/gamerai`.
4. Generates `/opt/gamerai/.env.prod` with a random `API_TOKEN`.
5. Brings up the stack with the production overlay.
6. Prints the URL, the worker token, and the redeploy command.

After it finishes:

```bash
curl https://coordinator.example.com/health
# => {"status":"ok"}
```

---

## Re-deploying after a code change

**The current preferred workflow is push-to-main + a manual deploy.sh.**
Quick, dirty, works. There is no full push-to-deploy yet.

1. Commit and `git push origin main`.
2. CI auto-publishes any `windows-agent/**` change — the
   `windows-agent build` workflow rebuilds `agent.exe` + the installer
   on a Windows runner and SFTPs them to `/var/www/downloads` on this
   VPS. Already-installed agents see the bumped `version.txt` and
   self-update (or `update` from the stdin command). Nothing to do.
3. Coordinator / UI / shared changes are NOT auto-deployed. SSH in and
   run:

   ```bash
   sudo /opt/gamerai/infra/deploy.sh
   ```

   That's just `git pull && docker compose up -d --build`.

End-to-end-test reality check: a feature that bumps the worker tool
surface (e.g. `tools=["search"]`) will 503 in prod until at least one
contributor's agent has self-updated to the new build. Force a
contributor to `update` (or restart it) if you need to validate
immediately.

If you want a real push-to-deploy for the coordinator, wire the same
SFTP-style SSH job over to a CI step that runs `deploy.sh`. We haven't
done it yet because the manual step is rare enough not to justify the
infrastructure.

---

## Watching prod logs during manual testing

The search-mode rewrite pipeline has a few moving parts (classifier →
parser → dispatcher → DDG → summarizer). When something looks wrong
in the UI, the fastest diagnosis path is to tail the structured
coordinator logs while you reproduce.

```bash
tools/watch-search-logs.sh                  # tail forever
tools/watch-search-logs.sh --since 5m       # backfill recent history
tools/watch-search-logs.sh --filter all     # show every line, not just search
```

Output is pretty-printed JSON colored by event. Each search submission
produces a `search_rewrite_enqueued` → `search_rewrite_parsed` →
(`search_rewrite_dispatched` | `search_rewrite_classified_skip` |
`search_rewrite_skip_overridden`) trail, with `original_prompt`,
`rewrite_output`, `decision`, and `final_query` inlined.

**Privacy heads-up:** those log lines contain user-typed prompts (and
the LLM-paraphrased query that came out of the classifier), truncated
to 200 chars each. See `docs/project-gaps.md` →
*"Coordinator logs contain raw user prompts"* for the rotation /
opt-out story.

For an already-finished job, the per-job snapshot is one HTTP call:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  https://ai.dallinlayton.com/api/admin/debug/job/<job_id>
```

Returns the DB row, JOB_RESULTS payload, pending rewrite linkage,
and the matched rewrite chat job with its parsed decision.

---

## Connecting workers

External gamer machines (anywhere on the internet) point their agent at
the public URL and provide the same `API_TOKEN` the bootstrap printed.

```jsonc
// windows-agent/config.json on the gamer's machine
{
  "coordinator_url": "https://coordinator.example.com",
  "api_token": "<paste API_TOKEN from .env.prod>"
}
```

(The agent also reads `API_TOKEN` from the environment; pick whichever is
more convenient.)

The in-VPS mock worker exists so the system is functional even before any
gamers join. It picks up the token from `.env.prod` automatically.

---

## Accessing the web UI privately

The customer dashboard (port 8080) is bound to `127.0.0.1` on the VPS.
SSH-tunnel it to your laptop:

```bash
ssh -L 8080:127.0.0.1:8080 root@coordinator.example.com
# now open http://localhost:8080 in your browser
```

This avoids exposing the unauthenticated dashboard to the public internet
during the MVP test. When the dashboard gets real auth, expose it through
Caddy on a subdomain.

---

## Auth

Auth is enforced inside the coordinator (see `shared/auth.py`), gated by
a single env var:

| `API_TOKEN`           | behaviour                                                |
| --------------------- | -------------------------------------------------------- |
| set (any non-empty)   | every request must carry `Authorization: Bearer <token>` |
| unset / empty         | auth disabled — open API (useful for tests + local dev)  |

`/health` is always open so uptime probes don't need the token.

The bootstrap script generates a random `API_TOKEN` and writes it to
`.env.prod`. To disable temporarily for testing:

```bash
sed -i 's|^API_TOKEN=.*|API_TOKEN=|' /opt/gamerai/.env.prod
sudo /opt/gamerai/infra/deploy.sh
```

Re-enable by writing a fresh token and redeploying:

```bash
sed -i "s|^API_TOKEN=.*|API_TOKEN=$(openssl rand -hex 32)|" \
  /opt/gamerai/.env.prod
sudo /opt/gamerai/infra/deploy.sh
```

The token is shared between the coordinator and every client (worker,
Windows agent, web/CLI client). Rotate by running the second snippet
above and updating each gamer's `windows-agent/config.json`.

---

## Common ops

```bash
# tail logs
docker compose -f /opt/gamerai/docker-compose.yml \
  -f /opt/gamerai/infra/docker-compose.prod.yml logs -f

# stop everything
docker compose -f /opt/gamerai/docker-compose.yml \
  -f /opt/gamerai/infra/docker-compose.prod.yml down

# back up the SQLite ledger
scp root@coordinator.example.com:/opt/gamerai/data/gamerai.db ./backup-$(date +%F).db

# rotate the API token
sed -i "s|^API_TOKEN=.*|API_TOKEN=$(openssl rand -hex 32)|" \
  /opt/gamerai/.env.prod
sudo /opt/gamerai/infra/deploy.sh
```

---

## When to graduate from this to Terraform / AWS

Stay on this setup until at least one of the following is true:

- 10+ active workers and the single VPS coordinator is the bottleneck.
- You're taking real money (PCI/SOC2 conversations begin).
- You need multi-region for latency to gamers.
- SQLite contention is causing measurable problems.

When that happens, this directory's contents become the spec for the
Phase 2 Terraform: ECS Fargate for the coordinator, ElastiCache for
Redis, RDS for the SQLite system-of-record, ACM for TLS, ALB instead
of Caddy.

---

## Files in this directory

| file                       | purpose                                                |
| -------------------------- | ------------------------------------------------------ |
| `bootstrap.sh`             | one-shot VPS installer (run as root, idempotent)       |
| `deploy.sh`                | re-pull + rebuild after a code change                  |
| `Caddyfile`                | TLS reverse proxy (auth lives in the coordinator)      |
| `docker-compose.prod.yml`  | overlay: adds Caddy, restricts internal ports          |
| `README.md`                | this file                                              |
