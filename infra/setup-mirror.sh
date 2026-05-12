#!/usr/bin/env bash
# Populate the public download mirror at /var/www/downloads/ with the
# Ollama Windows installer and the default model files. The Windows
# agent's first-run bootstrap pulls from
#   {coordinator_url}/download/ollama-setup.exe
#   {coordinator_url}/download/models/<slug>.gguf
#   {coordinator_url}/download/models/<slug>.Modelfile
# so this script is what makes those URLs resolve.
#
# Run on the VPS as root:
#   sudo bash /opt/gamerai/infra/setup-mirror.sh
#
# Idempotent: re-runs skip downloads when files are already present
# and non-empty. Set FORCE=1 to redownload.

set -euo pipefail

MIRROR_ROOT="${MIRROR_ROOT:-/var/www/downloads-chroot/uploads}"
MODELS_DIR="${MIRROR_ROOT}/models"
OLLAMA_INSTALLER="${MIRROR_ROOT}/ollama-setup.exe"
MODEL_NAME="${MODEL_NAME:-llama3.2:1b}"
MODEL_SLUG="${MODEL_SLUG:-llama3.2-1b}"
MODEL_GGUF="${MODELS_DIR}/${MODEL_SLUG}.gguf"
MODEL_MODELFILE="${MODELS_DIR}/${MODEL_SLUG}.Modelfile"

OLLAMA_INSTALLER_URL="${OLLAMA_INSTALLER_URL:-https://ollama.com/download/OllamaSetup.exe}"
# Public HF mirror of the Meta llama-3.2 1B Instruct weights at Q8_0.
# Q8_0 matches Ollama's default quantization for llama3.2:1b (~1.3 GB).
MODEL_GGUF_URL="${MODEL_GGUF_URL:-https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q8_0.gguf}"

mkdir -p "${MODELS_DIR}"

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

fetch_if_missing "${OLLAMA_INSTALLER_URL}" "${OLLAMA_INSTALLER}" "ollama installer"
fetch_if_missing "${MODEL_GGUF_URL}" "${MODEL_GGUF}" "${MODEL_SLUG} gguf"

# Modelfile is small enough to ship inline. The agent rewrites the FROM
# line to an absolute path before passing it to `ollama create`, so the
# relative path here is just a placeholder.
cat > "${MODEL_MODELFILE}" <<'MODELFILE'
# llama3.2:1b — bundled with the GamerAI agent first-run bootstrap.
# FROM line is rewritten to an absolute path by the agent at install time.
FROM ./llama3.2-1b.gguf

TEMPLATE """<|begin_of_text|><|start_header_id|>{{ if .System }}system<|end_header_id|>

{{ .System }}<|eot_id|>{{ end }}<|start_header_id|>user<|end_header_id|>

{{ .Prompt }}<|eot_id|><|start_header_id|>assistant<|end_header_id|>

"""

PARAMETER stop "<|start_header_id|>"
PARAMETER stop "<|end_header_id|>"
PARAMETER stop "<|eot_id|>"
PARAMETER temperature 0.7
MODELFILE
echo "wrote ${MODEL_MODELFILE}"

echo
echo "mirror contents:"
ls -lh "${OLLAMA_INSTALLER}" "${MODEL_GGUF}" "${MODEL_MODELFILE}"

echo
echo "verify from a client:"
echo "  curl -I https://ai.dallinlayton.com/download/ollama-setup.exe"
echo "  curl -I https://ai.dallinlayton.com/download/models/${MODEL_SLUG}.gguf"
