#!/usr/bin/env bash
#
# GamerAI — tear down the Hetzner VPS and everything we created on it.
#
# Idempotent: safe to re-run; missing resources are skipped without error.
#
# What this deletes:
#   - Hetzner server (the spending part — stops the meter)
#   - Hetzner firewall
#   - Hetzner SSH key (project-side; your local key is untouched)
#   - GitHub deploy key (if gh + GH_REPO are available)
#
# What this does NOT delete:
#   - Local SSH keys (~/.ssh/id_ed25519_gamerai, ~/.ssh/gamerai-github-deploy)
#   - Local hcloud context (the API token in ~/.config/hcloud/cli.toml)
#   - DNS records (e.g. ai.dallinlayton.com A record) — clean those manually
#   - Local known_hosts entries
#
# Usage:
#   ./infra/hcloud-destroy.sh           # uses default names from hcloud-create.sh
#   NAME=gamerai-staging ./infra/hcloud-destroy.sh
#
set -euo pipefail

NAME="${NAME:-gamerai-mvp}"
SSH_KEY_NAME="${SSH_KEY_NAME:-gamerai-deploy}"
FW_NAME="${FW_NAME:-gamerai-public}"
GH_REPO="${GH_REPO:-Hyrumdrums/GamerAI}"
GH_DEPLOY_KEY_TITLE="${GH_DEPLOY_KEY_TITLE:-gamerai-mvp-vps}"

log() { printf '\033[1;36m[hcloud]\033[0m %s\n' "$*"; }

command -v hcloud >/dev/null || { echo "ERROR: hcloud CLI not installed" >&2; exit 1; }

# ---------- 1. server (highest priority — stops billing) ----------
if hcloud server describe "$NAME" >/dev/null 2>&1; then
  log "deleting server '$NAME'..."
  hcloud server delete "$NAME"
else
  log "server '$NAME' not found — skipping."
fi

# ---------- 2. firewall ----------
if hcloud firewall describe "$FW_NAME" >/dev/null 2>&1; then
  log "deleting firewall '$FW_NAME'..."
  hcloud firewall delete "$FW_NAME"
else
  log "firewall '$FW_NAME' not found — skipping."
fi

# ---------- 3. project SSH key ----------
if hcloud ssh-key describe "$SSH_KEY_NAME" >/dev/null 2>&1; then
  log "deleting Hetzner SSH key '$SSH_KEY_NAME'..."
  hcloud ssh-key delete "$SSH_KEY_NAME"
else
  log "SSH key '$SSH_KEY_NAME' not found — skipping."
fi

# ---------- 4. GitHub deploy key (best-effort) ----------
if command -v gh >/dev/null; then
  KEY_ID=$(gh api "/repos/$GH_REPO/keys" --jq ".[] | select(.title == \"$GH_DEPLOY_KEY_TITLE\") | .id" 2>/dev/null || true)
  if [[ -n "$KEY_ID" ]]; then
    log "deleting GitHub deploy key '$GH_DEPLOY_KEY_TITLE' (id $KEY_ID)..."
    gh api -X DELETE "/repos/$GH_REPO/keys/$KEY_ID" >/dev/null
  else
    log "GitHub deploy key '$GH_DEPLOY_KEY_TITLE' not found — skipping."
  fi
else
  log "gh CLI not available — skipping GitHub deploy key cleanup."
fi

echo
echo "============================================================"
log "teardown complete. Hetzner billing stopped."
echo
echo "  Manual cleanup you may still want to do:"
echo "    - Remove DNS A record  (e.g. ai.dallinlayton.com)"
echo "    - Remove known_hosts entry:  ssh-keygen -R <old-ip>"
echo "    - Rotate the Hetzner API token in the console"
echo "============================================================"
