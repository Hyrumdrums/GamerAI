#!/usr/bin/env bash
# Populate the public download mirror with the stable-diffusion.cpp
# Windows binary + the default image-generation model. The Windows
# agent's image bootstrap pulls from:
#   {coordinator_url}/download/sd.exe
#   {coordinator_url}/download/sd-models/<slug>.gguf
#   {coordinator_url}/download/sd-models/<slug>.json   (metadata)
#
# Run on the VPS as root:
#   sudo bash /opt/gamerai/infra/setup-image-mirror.sh
#
# Idempotent: re-runs skip downloads when files are already present
# and non-empty. Set FORCE=1 to redownload.
#
# Companion to setup-mirror.sh (which handles Ollama + LLM weights).

set -euo pipefail

MIRROR_ROOT="${MIRROR_ROOT:-/var/www/downloads-chroot/uploads}"
SD_MODELS_DIR="${MIRROR_ROOT}/sd-models"
SD_BINARY="${MIRROR_ROOT}/sd.exe"

# stable-diffusion.cpp release. Pinning a specific tag so the agent's
# bootstrap doesn't break on an upstream API change. Vulkan build is
# the broadest first-shot pick — runs on NVIDIA, AMD, and Intel GPUs
# without a separate CUDA runtime download. Override SD_BINARY_URL to
# the cuda12-x64 build for NVIDIA-only fleets (bundle cudart-sd-bin-
# win-cu12-x64.zip too).
SD_BINARY_URL="${SD_BINARY_URL:-https://github.com/leejet/stable-diffusion.cpp/releases/download/master-637-ef92a00/sd-master-ef92a00-bin-win-vulkan-x64.zip}"

# Small SD 1.5 model — ~1.5 GB Q4_0 GGUF. The smallest credible model
# that produces recognizable images. Default for first-run installs;
# SDXL ships later for higher-VRAM contributors.
#
# DEFAULT_MODEL_SRC is a local file path; we copy it into the mirror
# rather than re-downloading from HF (where the leejet/stable-
# diffusion-v1-5 repo is currently auth-gated — re-fetching there
# would 401). Override DEFAULT_MODEL_URL=<https://...> to fall back
# to a network source.
DEFAULT_SLUG="${DEFAULT_SLUG:-sd1.5}"
DEFAULT_MODEL_SRC="${DEFAULT_MODEL_SRC:-/home/beargroup/ai/models/sd/stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf}"
DEFAULT_MODEL_URL="${DEFAULT_MODEL_URL:-}"

mkdir -p "${SD_MODELS_DIR}"

fetch_if_missing() {
  local url="$1"
  local dest="$2"
  local label="$3"
  if [[ "${FORCE:-0}" != "1" && -s "${dest}" ]]; then
    echo "skip ${label} (already present: $(du -h "${dest}" | cut -f1))"
    return 0
  fi
  echo "fetching ${label}..."
  curl --fail --location --progress-bar -o "${dest}.part" "${url}"
  mv "${dest}.part" "${dest}"
  echo "ok   ${label} ($(du -h "${dest}" | cut -f1))"
}

# sd.exe arrives as a zip with sd.exe + a few DLLs. We extract them flat
# into the mirror root so the agent can grab the whole bundle as a
# single archive — simpler than tracking each DLL individually.
SD_ZIP="${MIRROR_ROOT}/sd-bin.zip"
fetch_if_missing "${SD_BINARY_URL}" "${SD_ZIP}" "stable-diffusion.cpp windows zip"

# Re-package the upstream zip into a flat sd.zip the agent unpacks into
# its own install dir. Skipped when an already-extracted sd.exe is
# present and the zip hasn't been refetched.
if [[ ! -s "${SD_BINARY}" || "${FORCE:-0}" == "1" ]]; then
  if command -v unzip >/dev/null 2>&1; then
    tmp="$(mktemp -d)"
    unzip -q "${SD_ZIP}" -d "${tmp}"
    # Upstream renamed `sd.exe` → `sd-cli.exe` in the master-637+ builds
    # and also ships a separate `sd-server.exe`. Find either; prefer
    # sd.exe if present (older tag), fall back to sd-cli.exe. We
    # rename the binary to `sd.exe` on the mirror so the agent's
    # download URL (/download/sd.exe) stays stable across upstream
    # renames.
    found=""
    for cand in sd.exe sd-cli.exe; do
      hit="$(find "${tmp}" -name "${cand}" -print -quit)"
      if [[ -n "${hit}" ]]; then
        found="${hit}"
        break
      fi
    done
    if [[ -z "${found}" ]]; then
      echo "could not find sd.exe or sd-cli.exe inside ${SD_ZIP}" >&2
      ls -R "${tmp}" >&2
      exit 1
    fi
    found_dir="$(dirname "${found}")"
    # Copy every sibling (DLLs, license txts) flat into the mirror,
    # then rename the binary to sd.exe.
    cp -f "${found_dir}"/* "${MIRROR_ROOT}/"
    if [[ "$(basename "${found}")" != "sd.exe" ]]; then
      mv -f "${MIRROR_ROOT}/$(basename "${found}")" "${MIRROR_ROOT}/sd.exe"
    fi
    rm -rf "${tmp}"
    echo "extracted sd.exe + DLLs into ${MIRROR_ROOT}"
  else
    echo "unzip not installed; cannot extract sd-bin.zip" >&2
    exit 1
  fi
fi

# Stage the GGUF: prefer a local file (no re-download of a 1.5 GB
# blob we already have); fall back to URL if local source is missing
# and a URL was provided.
MODEL_DEST="${SD_MODELS_DIR}/${DEFAULT_SLUG}.gguf"
if [[ "${FORCE:-0}" != "1" && -s "${MODEL_DEST}" ]]; then
  echo "skip ${DEFAULT_SLUG}.gguf (already present: $(du -h "${MODEL_DEST}" | cut -f1))"
elif [[ -s "${DEFAULT_MODEL_SRC}" ]]; then
  echo "copying ${DEFAULT_SLUG}.gguf from ${DEFAULT_MODEL_SRC}..."
  cp -f "${DEFAULT_MODEL_SRC}" "${MODEL_DEST}.part"
  # Sanity-check GGUF magic bytes before promoting the staged file —
  # catches `cp` from a wrong-format file (a safetensors blob
  # masquerading as gguf would silently break sd.cpp).
  if [[ "$(head -c 4 "${MODEL_DEST}.part" | od -An -c | tr -d ' ')" != "GGUF" ]]; then
    echo "error: ${DEFAULT_MODEL_SRC} is not a GGUF file (magic mismatch)" >&2
    rm -f "${MODEL_DEST}.part"
    exit 1
  fi
  mv "${MODEL_DEST}.part" "${MODEL_DEST}"
  echo "ok   ${DEFAULT_SLUG}.gguf ($(du -h "${MODEL_DEST}" | cut -f1))"
elif [[ -n "${DEFAULT_MODEL_URL}" ]]; then
  fetch_if_missing "${DEFAULT_MODEL_URL}" "${MODEL_DEST}" "${DEFAULT_SLUG}.gguf"
else
  echo "error: ${DEFAULT_MODEL_SRC} not found and DEFAULT_MODEL_URL is empty" >&2
  echo "       set DEFAULT_MODEL_SRC=<local-path> or DEFAULT_MODEL_URL=<https://...>" >&2
  exit 1
fi

# Sidecar JSON the agent reads to know defaults (size, steps, license).
# Kept minimal — extend when adding SDXL or other models.
cat > "${SD_MODELS_DIR}/${DEFAULT_SLUG}.json" <<JSON
{
  "name": "${DEFAULT_SLUG}",
  "family": "stable-diffusion-1.5",
  "kind": "image",
  "default_width": 512,
  "default_height": 512,
  "default_steps": 20,
  "license": "CreativeML Open RAIL-M"
}
JSON
echo "wrote ${SD_MODELS_DIR}/${DEFAULT_SLUG}.json"

echo
echo "mirror image contents:"
ls -lh "${SD_BINARY}" "${SD_MODELS_DIR}/${DEFAULT_SLUG}.gguf" \
       "${SD_MODELS_DIR}/${DEFAULT_SLUG}.json" 2>/dev/null

echo
echo "verify from a client:"
echo "  curl -I https://ai.dallinlayton.com/download/sd.exe"
echo "  curl -I https://ai.dallinlayton.com/download/sd-models/${DEFAULT_SLUG}.gguf"
echo
echo "TODO: SDXL ships once the small model is proven end-to-end."
echo "      Source: /home/beargroup/ai/image-generation/models/checkpoints/sd_xl_base_1.0.safetensors"
