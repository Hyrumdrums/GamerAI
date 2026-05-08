#!/usr/bin/env bash
#
# GamerAI — provision a Hetzner Cloud VPS for the MVP coordinator.
#
# Idempotent: re-running re-uses the existing SSH key, firewall, and server
# (matched by name) instead of creating duplicates. Prints the public IP at
# the end so you can DNS-record it.
#
# Prereqs:
#   - hcloud CLI authenticated (~/.config/hcloud/cli.toml)
#   - SSH public key at $SSH_PUB_KEY (default: ~/.ssh/id_ed25519_gamerai.pub)
#
# Usage:
#   ./infra/hcloud-create.sh
#
# Override defaults via env:
#   NAME=gamerai-staging LOCATION=hil ./infra/hcloud-create.sh
#
set -euo pipefail

NAME="${NAME:-gamerai-mvp}"
TYPE="${TYPE:-cpx21}"               # 3 vCPU AMD, 4 GB RAM, 80 GB SSD, ~€8/mo
IMAGE="${IMAGE:-ubuntu-22.04}"
LOCATION="${LOCATION:-ash}"         # Ashburn, VA — closest US east-coast option
SSH_KEY_NAME="${SSH_KEY_NAME:-gamerai-deploy}"
SSH_PUB_KEY="${SSH_PUB_KEY:-$HOME/.ssh/id_ed25519_gamerai.pub}"
FW_NAME="${FW_NAME:-gamerai-public}"

log() { printf '\033[1;36m[hcloud]\033[0m %s\n' "$*"; }

[[ -f "$SSH_PUB_KEY" ]] || { echo "ERROR: $SSH_PUB_KEY not found" >&2; exit 1; }
command -v hcloud >/dev/null || { echo "ERROR: hcloud CLI not installed" >&2; exit 1; }

# ---------- 1. SSH key ----------
if hcloud ssh-key describe "$SSH_KEY_NAME" >/dev/null 2>&1; then
  log "SSH key '$SSH_KEY_NAME' already exists — re-using."
else
  log "uploading SSH key '$SSH_KEY_NAME' ($SSH_PUB_KEY)..."
  hcloud ssh-key create --name "$SSH_KEY_NAME" --public-key-from-file "$SSH_PUB_KEY"
fi

# ---------- 2. Firewall (22/80/443 only) ----------
if hcloud firewall describe "$FW_NAME" >/dev/null 2>&1; then
  log "firewall '$FW_NAME' already exists — re-using."
else
  log "creating firewall '$FW_NAME'..."
  hcloud firewall create --name "$FW_NAME"
  for port in 22 80 443; do
    hcloud firewall add-rule "$FW_NAME" \
      --direction in --protocol tcp --port "$port" \
      --source-ips 0.0.0.0/0 --source-ips ::/0 \
      --description "allow tcp/$port"
  done
fi

# ---------- 3. Server ----------
if hcloud server describe "$NAME" >/dev/null 2>&1; then
  log "server '$NAME' already exists — re-using."
else
  log "creating server '$NAME' ($TYPE, $IMAGE, $LOCATION)..."
  hcloud server create \
    --name "$NAME" \
    --type "$TYPE" \
    --image "$IMAGE" \
    --location "$LOCATION" \
    --ssh-key "$SSH_KEY_NAME" \
    --firewall "$FW_NAME"
fi

IP=$(hcloud server ip "$NAME")

# ---------- 4. Summary ----------
echo
echo "============================================================"
log "ready."
echo
echo "  Name:        $NAME"
echo "  Type:        $TYPE  ($(hcloud server-type describe "$TYPE" -o format='{{.Cores}}c {{.Memory}}GB {{.Disk}}GB'))"
echo "  Location:    $LOCATION"
echo "  Public IP:   $IP"
echo
echo "  Next steps:"
echo "    1. Add DNS:  ai.dallinlayton.com  A  $IP  (TTL 300)"
echo "    2. Wait ~60s, then verify:  dig +short ai.dallinlayton.com"
echo "    3. SSH in:   ssh -i ${SSH_PUB_KEY%.pub} root@$IP"
echo "    4. Bootstrap (from VPS as root):"
echo "       curl -sSL https://raw.githubusercontent.com/Hyrumdrums/GamerAI/main/infra/bootstrap.sh \\"
echo "         | sudo bash -s -- --domain ai.dallinlayton.com --email <your-email>"
echo "============================================================"
