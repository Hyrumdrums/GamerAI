#!/usr/bin/env bash
#
# GamerAI — one-shot VPS bootstrap.
#
# Provisions a fresh Ubuntu 22.04+ VPS to run the GamerAI MVP coordinator
# behind TLS. Idempotent — safe to re-run.
#
# Usage (from the VPS as root):
#
#   curl -sSL https://raw.githubusercontent.com/Hyrumdrums/GamerAI/main/infra/bootstrap.sh \
#     | sudo bash -s -- --domain coordinator.example.com --email you@example.com
#
# Optional flags:
#   --branch <name>    git branch to deploy (default: main)
#   --repo   <url>     git repo URL (default: this repo)
#   --dir    <path>    install directory (default: /opt/gamerai)
#
set -euo pipefail

DOMAIN=""
EMAIL=""
BRANCH="${BRANCH:-main}"
REPO="${REPO:-git@github.com:Hyrumdrums/GamerAI.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/gamerai}"
DEPLOY_KEY="${DEPLOY_KEY:-/root/.ssh/github_deploy}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email)  EMAIL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --repo)   REPO="$2"; shift 2 ;;
    --dir)    INSTALL_DIR="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -z "$DOMAIN" ]] && { echo "ERROR: --domain is required" >&2; exit 1; }
[[ -z "$EMAIL"  ]] && { echo "ERROR: --email is required (for Let's Encrypt)" >&2; exit 1; }
[[ "$EUID" -eq 0 ]] || { echo "ERROR: must run as root (use sudo)" >&2; exit 1; }

log() { printf '\033[1;36m[gamerai]\033[0m %s\n' "$*"; }

log "domain=$DOMAIN email=$EMAIL branch=$BRANCH dir=$INSTALL_DIR"

# ---------- 1. base packages ----------
log "installing prerequisites..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg git openssl ufw

# ---------- 1b. swap (cheap OOM insurance on a 4 GB box) ----------
SWAPFILE="${SWAPFILE:-/swapfile}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-2048}"
if ! swapon --show=NAME --noheadings | grep -qx "$SWAPFILE"; then
  log "creating ${SWAP_SIZE_MB}MB swap at $SWAPFILE..."
  fallocate -l "${SWAP_SIZE_MB}M" "$SWAPFILE" 2>/dev/null \
    || dd if=/dev/zero of="$SWAPFILE" bs=1M count="$SWAP_SIZE_MB" status=none
  chmod 600 "$SWAPFILE"
  mkswap "$SWAPFILE" >/dev/null
  swapon "$SWAPFILE"
  if ! grep -q "^$SWAPFILE " /etc/fstab; then
    echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
  fi
fi

# ---------- 2. firewall ----------
if command -v ufw >/dev/null; then
  log "configuring firewall (allow 22, 80, 443)..."
  ufw --force reset >/dev/null
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow 22/tcp  >/dev/null
  ufw allow 80/tcp  >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
fi

# ---------- 3. docker ----------
if ! command -v docker >/dev/null; then
  log "installing Docker Engine + Compose plugin..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

# ---------- 4. clone or update repo ----------
# If REPO is an SSH URL we expect a deploy key at $DEPLOY_KEY (placed there
# out-of-band — the bootstrap can't pull it down because the repo is private).
log "syncing repo at $INSTALL_DIR..."
GIT_ENV=()
if [[ "$REPO" == git@* ]]; then
  [[ -f "$DEPLOY_KEY" ]] || { echo "ERROR: $DEPLOY_KEY missing — scp the deploy private key first" >&2; exit 1; }
  chmod 600 "$DEPLOY_KEY"
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  ssh-keyscan -t ed25519,rsa github.com >> /root/.ssh/known_hosts 2>/dev/null
  sort -u -o /root/.ssh/known_hosts /root/.ssh/known_hosts
  GIT_ENV=(env "GIT_SSH_COMMAND=ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes")
fi
if [[ -d "$INSTALL_DIR/.git" ]]; then
  "${GIT_ENV[@]}" git -C "$INSTALL_DIR" fetch --quiet origin
  "${GIT_ENV[@]}" git -C "$INSTALL_DIR" checkout --quiet "$BRANCH"
  "${GIT_ENV[@]}" git -C "$INSTALL_DIR" pull --ff-only --quiet origin "$BRANCH"
else
  "${GIT_ENV[@]}" git clone --quiet --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
fi
# Persist the deploy key into the repo's git config so future deploy.sh runs
# don't need GIT_SSH_COMMAND in the environment.
if [[ ${#GIT_ENV[@]} -gt 0 ]]; then
  git -C "$INSTALL_DIR" config core.sshCommand "ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes"
fi

# ---------- 5. env file (preserves existing token on re-run) ----------
ENV_FILE="$INSTALL_DIR/.env.prod"
if [[ ! -f "$ENV_FILE" ]]; then
  log "generating $ENV_FILE..."
  API_TOKEN=$(openssl rand -hex 32)
  # Redis auth (defense-in-depth; Redis is also localhost-bound). Both
  # the redis container and the python clients read REDIS_PASSWORD, so a
  # generated value here is enforced end-to-end on first boot. Existing
  # deploys are NOT retrofitted — see the else branch / docs/OPERATOR.md.
  REDIS_PASSWORD=$(openssl rand -hex 24)
  cat > "$ENV_FILE" <<EOF
# generated by infra/bootstrap.sh — do not commit
DOMAIN=$DOMAIN
EMAIL=$EMAIL
API_TOKEN=$API_TOKEN
REDIS_PASSWORD=$REDIS_PASSWORD
# Refuse to enqueue jobs when no real worker is heartbeating. Without
# this, prompts would sit on the queue indefinitely. The web UI
# surfaces the 503 as "No community members are available right now".
REQUIRE_LIVE_WORKER=true
# To exercise the stack end-to-end without a real worker (e.g.
# a staging walkthrough), uncomment the next two lines AND start
# compose with --profile mock-worker. Leaving MOCK_INFERENCE unset
# keeps the in-VPS worker out of the deploy entirely.
# MOCK_INFERENCE=true
# REQUIRE_LIVE_WORKER=false
EOF
  chmod 600 "$ENV_FILE"
else
  log "$ENV_FILE already exists — leaving in place"
  # keep existing token; refresh domain/email in case they changed
  sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|"  "$ENV_FILE"
  sed -i "s|^EMAIL=.*|EMAIL=$EMAIL|"     "$ENV_FILE"
fi

# ---------- 6. bring services up ----------
log "starting services (this builds images on first run)..."
cd "$INSTALL_DIR"
# Two profile toggles, decided by the env file:
#   - MOCK_INFERENCE=true → bring up the in-VPS worker under the
#     `mock-worker` profile so it picks up jobs with the mock.
#   - MOCK_INFERENCE=false → bring up `local-inference` (the in-VPS
#     ollama) so the in-VPS worker has a real model to call.
#   - MOCK_INFERENCE unset (default prod) → neither profile; the
#     in-VPS worker stays down and jobs only run on connected
#     external gamer machines.
PROFILE_ARGS=()
if grep -q '^MOCK_INFERENCE=true' "$ENV_FILE" 2>/dev/null; then
  PROFILE_ARGS=(--profile mock-worker)
elif grep -q '^MOCK_INFERENCE=false' "$ENV_FILE" 2>/dev/null; then
  PROFILE_ARGS=(--profile local-inference)
fi
docker compose \
  --env-file "$ENV_FILE" \
  -f docker-compose.yml \
  -f infra/docker-compose.prod.yml \
  "${PROFILE_ARGS[@]}" \
  up -d --build

# ---------- 6b. VAPID keys (Phase 6 of pwa-refactor.txt) ----------
# Web Push needs a per-deployment EC keypair. Without it, push
# subscriptions still record but no actual browser notifications are
# delivered. We generate once at first bootstrap and leave the keys
# alone on subsequent runs — rotating invalidates every existing
# subscription, which is a deliberate operator action (see docs/OPERATOR.md).
SECRETS_DIR="$INSTALL_DIR/.secrets"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"
if [[ ! -f "$SECRETS_DIR/vapid_private.pem" ]]; then
  log "generating VAPID keypair (one-time)..."
  # `vapid --gen` writes private_key.pem + public_key.pem into the
  # CWD inside the container, then prints the base64url public key
  # ("Application Server Key") to stdout. The coordinator image
  # already has pywebpush + the `vapid` CLI; reusing it avoids
  # installing py-vapid on the host.
  docker run --rm \
    -v "$SECRETS_DIR:/work" \
    -w /work \
    --entrypoint vapid \
    gamerai-coordinator:latest --gen --applicationServerKey \
    > /tmp/vapid.out 2>&1
  mv "$SECRETS_DIR/private_key.pem" "$SECRETS_DIR/vapid_private.pem"
  mv "$SECRETS_DIR/public_key.pem"  "$SECRETS_DIR/vapid_public.pem"
  grep "Application Server Key" /tmp/vapid.out | sed 's/^.*= //' \
    > "$SECRETS_DIR/application_server_key.txt"
  chmod 600 "$SECRETS_DIR/vapid_private.pem"
  rm /tmp/vapid.out
fi

# Append VAPID env to .env.prod if not already configured. Subject
# defaults to the operator's --email so the Push service has a real
# contact channel if our notifications start misbehaving.
if ! grep -q '^VAPID_PUBLIC_KEY=' "$ENV_FILE"; then
  log "wiring VAPID into $ENV_FILE..."
  PUBKEY=$(cat "$SECRETS_DIR/application_server_key.txt")
  cat >> "$ENV_FILE" <<EOF

# ---------- VAPID (Phase 6 of pwa-refactor.txt) ----------
# Public Application Server Key, served at /notifications/vapid-key.
VAPID_PUBLIC_KEY=$PUBKEY
# Path resolved INSIDE the coordinator container; ./.secrets is bind-
# mounted to /secrets:ro by docker-compose.yml.
VAPID_PRIVATE_KEY_PATH=/secrets/vapid_private.pem
VAPID_SUBJECT=mailto:$EMAIL
EOF
  # Recreate just the coordinator container so it picks up the new env
  # vars (we built + started above without them, so push was a no-op
  # for the first ~30s of life).
  log "restarting coordinator with VAPID enabled..."
  docker compose \
    --env-file "$ENV_FILE" \
    -f docker-compose.yml \
    -f infra/docker-compose.prod.yml \
    up -d --no-deps coordinator >/dev/null
fi

# ---------- 7. summary ----------
TOKEN=$(grep '^API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
echo
echo "============================================================"
log "deployment complete!"
echo
echo "  Coordinator URL:  https://$DOMAIN"
echo "  Health check:     https://$DOMAIN/health"
echo "  Worker token:     $TOKEN"
echo
echo "  Web UI (private): ssh -L 8080:127.0.0.1:8080 root@<this-host>"
echo "                    then open http://localhost:8080"
echo
echo "  Re-deploy:        $INSTALL_DIR/infra/deploy.sh"
echo "  Logs:             docker compose -f $INSTALL_DIR/docker-compose.yml \\"
echo "                      -f $INSTALL_DIR/infra/docker-compose.prod.yml logs -f"
echo
echo "  NOTE: bearer auth is ON (API_TOKEN set in .env.prod)."
echo "        Workers/agents must send 'Authorization: Bearer <token>'"
echo "        — see infra/README.md for client config."
echo
echo "  PWA + Push:       Service worker, manifest, and push delivery are"
echo "                    live by default. The VAPID keypair lives in"
echo "                    $INSTALL_DIR/.secrets/ (gitignored, chmod 600)."
echo "                    Public key is also in .env.prod for reference."
echo "                    To rotate, see docs/OPERATOR.md \"§ Push notifications\"."
echo
echo "  ⚠  REQUIRED next step — seed contributor mirrors (~8 GB total):"
echo "       sudo bash $INSTALL_DIR/infra/setup-mirror.sh        # Ollama + chat model"
echo "       sudo bash $INSTALL_DIR/infra/setup-image-mirror.sh  # sd.cpp + SDXL"
echo "       sudo bash $INSTALL_DIR/infra/setup-tts-mirror.sh    # Piper + voice"
echo "     Without these, every contributor agent that connects falls into a"
echo "     mock-inference partial-contributor state for the missing tool."
echo "     See infra/README.md \"Mandatory follow-up\" for the verification curl."
echo "============================================================"
