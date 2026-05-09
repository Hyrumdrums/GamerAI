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

# If real inference is enabled (MOCK_INFERENCE=false), bring the in-VPS
# ollama service along too — otherwise compose would tear it down on every
# re-deploy because it's behind the local-inference profile.
PROFILE_ARGS=()
if grep -q '^MOCK_INFERENCE=false' .env.prod 2>/dev/null; then
  PROFILE_ARGS=(--profile local-inference)
fi

docker compose \
  --env-file .env.prod \
  -f docker-compose.yml \
  -f infra/docker-compose.prod.yml \
  "${PROFILE_ARGS[@]}" \
  up -d --build

echo "[gamerai] redeploy complete."
