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

docker compose \
  --env-file .env.prod \
  -f docker-compose.yml \
  -f infra/docker-compose.prod.yml \
  up -d --build

echo "[gamerai] redeploy complete."
