#!/usr/bin/env bash
# Populate the public download mirror with the Piper TTS Windows
# binary + the default Phase-1 voice. The Windows agent's TTS
# bootstrap pulls from:
#   {coordinator_url}/download/piper.exe
#   {coordinator_url}/download/piper-runtime.zip   (DLLs + espeak data)
#   {coordinator_url}/download/tts-voices/<slug>.onnx
#   {coordinator_url}/download/tts-voices/<slug>.onnx.json
#
# Run on the VPS as root:
#   sudo bash /opt/gamerai/infra/setup-tts-mirror.sh
#
# Idempotent: re-runs skip downloads when files are already present
# and non-empty. Set FORCE=1 to redownload.
#
# Companion to setup-image-mirror.sh (sd.cpp + diffusion models) and
# setup-mirror.sh (ollama + LLM weights). See voice-phase1 design
# memory for why Piper is the v1 choice.

set -euo pipefail

MIRROR_ROOT="${MIRROR_ROOT:-/var/www/downloads-chroot/uploads}"
TTS_VOICES_DIR="${MIRROR_ROOT}/tts-voices"

# Piper release. Pinning a tag so the agent's bootstrap doesn't break
# on upstream API changes. Override PIPER_BINARY_URL for a newer build
# (or the ARM64 build if a future contributor tier supports it).
#
# The Windows zip contains piper.exe + onnxruntime.dll + espeak-ng-data/.
# We re-stage it on the mirror as a single piper-runtime.zip the agent
# unpacks atomically (same shape as sd-bin.zip → sd.exe + DLLs).
PIPER_BINARY_URL="${PIPER_BINARY_URL:-https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip}"

# Voice catalog. Each entry is
#   "<slug>|<onnx-url>|<json-url>|<sample-rate>|<language>".
# Slug is the model name registered in coordinator/model_registry.py;
# the agent's TTS bootstrap downloads `<slug>.onnx` + `<slug>.onnx.json`.
# Phase 1 ships one voice (en_US-libritts-high) — DEFAULT_TTS_MODEL.
# Add more entries here when the model_registry catalog grows.
VOICE_CATALOG=(
  "piper:en_us-libritts-high|https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts/high/en_US-libritts-high.onnx|https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts/high/en_US-libritts-high.onnx.json|22050|en_US"
)

mkdir -p "${TTS_VOICES_DIR}"

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

# piper.exe arrives as a zip with piper.exe + onnxruntime DLL +
# espeak-ng-data/. Re-stage as piper-runtime.zip so the agent does
# a single download + extract instead of tracking each DLL.
PIPER_ZIP="${MIRROR_ROOT}/piper-runtime.zip"
fetch_if_missing "${PIPER_BINARY_URL}" "${PIPER_ZIP}" "piper windows zip"

# Stage each voice. Two files per voice (the .onnx weights + the
# .onnx.json config); both go straight into tts-voices/. The agent's
# TTS bootstrap pulls both by slug; missing the JSON would break the
# sample-rate read so we treat both as required (unlike the image
# sidecar, which is best-effort).
for entry in "${VOICE_CATALOG[@]}"; do
  IFS='|' read -r slug onnx_url json_url sample_rate language <<<"${entry}"
  # Filesystem-safe form of the slug — colon is allowed on Linux but
  # is a stream identifier on Windows NTFS, so we substitute. The
  # mirror URL still uses the colon-form slug; the agent reverses
  # the substitution locally. Keeps the model_registry name identical
  # to the on-mirror filename for grep-ability.
  fs_slug="${slug//:/__}"
  onnx_dest="${TTS_VOICES_DIR}/${fs_slug}.onnx"
  json_dest="${TTS_VOICES_DIR}/${fs_slug}.onnx.json"
  sidecar_dest="${TTS_VOICES_DIR}/${fs_slug}.json"

  fetch_if_missing "${onnx_url}" "${onnx_dest}" "${slug}.onnx"
  fetch_if_missing "${json_url}" "${json_dest}" "${slug}.onnx.json"

  # Per-voice sidecar with our own defaults — separate from the
  # upstream .onnx.json so we can carry GamerAI-specific knobs
  # (length_scale for pacing, noise_scale for variation) without
  # mutating the upstream config. Mirrors the image sidecar pattern.
  cat > "${sidecar_dest}" <<JSON
{
  "name": "${slug}",
  "family": "piper",
  "kind": "tts",
  "sample_rate": ${sample_rate},
  "language": "${language}",
  "default_length_scale": 1.0,
  "default_noise_scale": 0.667,
  "default_noise_w": 0.8,
  "license": "MIT"
}
JSON
  echo "wrote ${sidecar_dest}"
done

echo
echo "mirror tts contents:"
ls -lh "${PIPER_ZIP}" "${TTS_VOICES_DIR}"/*.onnx "${TTS_VOICES_DIR}"/*.json 2>/dev/null

echo
echo "verify from a client:"
echo "  curl -I https://ai.dallinlayton.com/download/piper-runtime.zip"
for entry in "${VOICE_CATALOG[@]}"; do
  IFS='|' read -r slug _ <<<"${entry}"
  fs_slug="${slug//:/__}"
  echo "  curl -I https://ai.dallinlayton.com/download/tts-voices/${fs_slug}.onnx"
done
echo
echo "TODO: Kokoro-82M as quality-tier voice (Phase 2)."
echo "      ~330 MB ONNX, better prosody, requires onnxruntime 1.16+."
