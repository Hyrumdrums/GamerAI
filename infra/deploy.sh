#!/usr/bin/env bash
#
# Re-deploy GamerAI on a host that's already been bootstrapped.
# Pulls the latest commit on the current branch and rebuilds containers.
#
# Usage (from the VPS):
#   sudo /opt/gamerai/infra/deploy.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

git pull --ff-only

# Two profile toggles, matched against the env file:
#   - MOCK_INFERENCE=true → bring up the in-VPS worker under
#     `mock-worker` (the worker container is profile-gated in
#     docker-compose.prod.yml).
#   - MOCK_INFERENCE=false → bring up `local-inference` (the in-VPS
#     ollama) so the worker has a real model to call.
#   - MOCK_INFERENCE unset (default prod) → neither profile, in-VPS
#     worker stays down, only external gamer machines serve jobs.
# Without this, compose would tear the appropriate profile down on
# every re-deploy.
PROFILE_ARGS=()
if grep -q '^MOCK_INFERENCE=true' .env.prod 2>/dev/null; then
  PROFILE_ARGS=(--profile mock-worker)
elif grep -q '^MOCK_INFERENCE=false' .env.prod 2>/dev/null; then
  PROFILE_ARGS=(--profile local-inference)
fi

docker compose \
  --env-file .env.prod \
  -f docker-compose.yml \
  -f infra/docker-compose.prod.yml \
  "${PROFILE_ARGS[@]}" \
  up -d --build

# Caddy bind-mounts a *file* (infra/Caddyfile) rather than a directory.
# `git pull` replaces the file by rename, which orphans the inode the
# Caddy container is mounted against — the container keeps serving the
# pre-pull config even after `docker compose up --build`. Restart it
# unconditionally so Caddyfile edits actually land.
docker restart gamerai-caddy >/dev/null

echo "[gamerai] redeploy complete."
