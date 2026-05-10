# GamerAI dev test — Windows worker bootstrap.
#
# Prereqs (manual, one-time):
#   - Python 3.11+ (https://python.org)
#   - Ollama (https://ollama.com/download/windows)
#
# Run from the repo's windows-agent\ directory:
#   .\setup-dev.ps1 -Coordinator http://<lan-ip>:8000
#
# What it does:
#   1. Pulls the requested model via Ollama (skips if already present).
#   2. Creates a Python venv and installs agent dependencies.
#   3. Writes a config.json tuned for testing (5s idle, 90% cpu ceiling).
#   4. Points the agent at local Ollama and launches it in the foreground.

param(
    [string]$Coordinator = "http://192.168.1.135:8000",
    [string]$Model       = "llama3.1:8b"
)

$ErrorActionPreference = "Stop"

# 1. ensure model is pulled --------------------------------------------------
Write-Host "[1/4] checking model: $Model"
$present = (& ollama list 2>$null | Select-String -SimpleMatch $Model)
if (-not $present) {
    Write-Host "      not found locally - pulling (this can take several minutes)"
    & ollama pull $Model
} else {
    Write-Host "      already present"
}

# 2. venv + deps -------------------------------------------------------------
Write-Host "[2/4] python venv + deps"
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
. .\.venv\Scripts\Activate.ps1
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# 3. config.json -------------------------------------------------------------
Write-Host "[3/4] writing config.json (coordinator=$Coordinator, model=$Model)"
$config = @"
{
  "coordinator_url": "$Coordinator",
  "polling_interval_seconds": 5,
  "earnings_print_minutes": 10,
  "idle": {
    "min_input_idle_seconds": 5,
    "max_cpu_percent": 90,
    "cpu_sample_seconds": 2
  },
  "model": "$Model",
  "worker_id": null,
  "api_token": null
}
"@
Set-Content -Path config.json -Value $config -Encoding utf8

# 4. launch ------------------------------------------------------------------
Write-Host "[4/4] launching agent (Ctrl+C to stop)"
$env:OLLAMA_URL = "http://localhost:11434"
python agent.py
