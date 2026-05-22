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

# Image model catalog. Each entry is "<slug>|<local-src>|<family>|<steps>|<cfg>|<sampler>".
# The agent's image bootstrap downloads `<slug>.gguf` + `<slug>.json`;
# `<slug>.json` carries the per-model defaults (steps, cfg scale, sampler)
# the agent feeds to sd.exe. LCM-distilled models need step≈8 / cfg≈1.5 /
# sampler=lcm; vanilla SD1.5 wants step≈20 / cfg≈7 / sampler=euler_a.
#
# Local-src files are copied (not re-downloaded from HF) since the
# upstream repos are sometimes auth-gated. The script bails per-entry
# if a source file is missing — easier to add models incrementally.
MODEL_CATALOG=(
  "dreamshaper8|/home/beargroup/ai/models/sd/dreamshaper_8LCM-iq4_nl-imv2.gguf|dreamshaper-8-lcm|8|1.5|lcm"
  "sd1.5|/home/beargroup/ai/models/sd/stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf|stable-diffusion-1.5|20|7.0|euler_a"
)

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

# Stage each entry in the catalog. Prefers local files (no re-download
# of a 1.5 GB blob we already have) and writes a sidecar JSON the agent
# reads for per-model defaults (steps / cfg / sampler).
for entry in "${MODEL_CATALOG[@]}"; do
  IFS='|' read -r slug src family steps cfg sampler <<<"${entry}"
  dest="${SD_MODELS_DIR}/${slug}.gguf"
  sidecar="${SD_MODELS_DIR}/${slug}.json"

  if [[ "${FORCE:-0}" != "1" && -s "${dest}" ]]; then
    echo "skip ${slug}.gguf (already present: $(du -h "${dest}" | cut -f1))"
  elif [[ -s "${src}" ]]; then
    echo "copying ${slug}.gguf from ${src}..."
    cp -f "${src}" "${dest}.part"
    # Sanity-check GGUF magic bytes before promoting the staged file —
    # catches `cp` from a wrong-format file (a safetensors blob
    # masquerading as gguf would silently break sd.cpp).
    if [[ "$(head -c 4 "${dest}.part" | od -An -c | tr -d ' ')" != "GGUF" ]]; then
      echo "error: ${src} is not a GGUF file (magic mismatch)" >&2
      rm -f "${dest}.part"
      exit 1
    fi
    mv "${dest}.part" "${dest}"
    echo "ok   ${slug}.gguf ($(du -h "${dest}" | cut -f1))"
  else
    echo "warn ${slug}: source missing at ${src} — skipping" >&2
    continue
  fi

  cat > "${sidecar}" <<JSON
{
  "name": "${slug}",
  "family": "${family}",
  "kind": "image",
  "default_width": 512,
  "default_height": 512,
  "default_steps": ${steps},
  "default_cfg_scale": ${cfg},
  "default_sampler": "${sampler}",
  "license": "CreativeML Open RAIL-M"
}
JSON
  echo "wrote ${sidecar}"
done

echo
echo "mirror image contents:"
ls -lh "${SD_BINARY}" "${SD_MODELS_DIR}"/*.gguf "${SD_MODELS_DIR}"/*.json 2>/dev/null

echo
echo "verify from a client:"
echo "  curl -I https://ai.dallinlayton.com/download/sd.exe"
for entry in "${MODEL_CATALOG[@]}"; do
  IFS='|' read -r slug _ <<<"${entry}"
  echo "  curl -I https://ai.dallinlayton.com/download/sd-models/${slug}.gguf"
done
echo
echo "TODO: SDXL ships once a high-VRAM contributor tier exists."
echo "      Source: /home/beargroup/ai/image-generation/models/checkpoints/sd_xl_base_1.0.safetensors"
