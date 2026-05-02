# GamerAI — Windows Agent

This is the **Windows worker agent** for the GamerAI distributed inference
network. Install it on your gaming PC and earn money when your machine is idle.

The agent is intentionally tiny and self-contained. It only talks HTTP to a
GamerAI coordinator — it does **not** need direct access to Redis or any
internal services.

---

## How it works

1. On launch the agent reads `config.json`, generates (or loads) a stable
   `worker_id`, and registers with the coordinator.
2. Every few seconds it checks whether your machine is **idle**:
   - no keyboard or mouse input for **60+ seconds**, AND
   - CPU usage below **30%**.
3. When idle, it asks the coordinator for the next job, runs inference, and
   submits the result. Earnings (USD per token) are credited to your
   `worker_id` and printed to the log every ~10 minutes.
4. The instant the user touches the keyboard/mouse — or CPU rises — the
   agent reports `offline` and stops accepting new jobs. The current job (if
   any) finishes; nothing is killed mid-inference.

All thresholds are tunable in `config.json`.

---

## Install (end user)

### Option A — bundled installer (recommended)

1. Download `GamerAI-Agent-Setup.exe`.
2. Run it. Defaults are fine for most users.
3. (Optional) Tick **"Run on Windows startup (background mode)"** during
   install to launch silently on every boot.
4. Edit `config.json` (Start menu → "Edit configuration") to point at your
   coordinator URL if it's not the default.

### Option B — standalone exe

1. Download `agent.exe` and `config.json` into the same folder.
2. Edit `config.json` and set `"coordinator_url"`.
3. Double-click `agent.exe` — or, for silent operation, create a shortcut
   with the argument `--background` and drop it in
   `shell:startup`.

### Option C — from source

```powershell
git clone <this repo>
cd GamerAI\windows-agent
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python agent.py
```

---

## How to run

| command                                | what it does                                  |
| -------------------------------------- | --------------------------------------------- |
| `agent.exe`                            | foreground — logs to console **and** file     |
| `agent.exe --background`               | no console output; rotating log file only     |
| `agent.exe --once`                     | process one job and exit (smoke test)         |
| `agent.exe --status`                   | print local earnings totals and exit          |
| `agent.exe --config C:\path\to\config.json` | override config path                     |

Logs live at:

```
%APPDATA%\GamerAI\logs\agent.log
```

State (`worker_id` + cumulative earnings) is persisted to:

```
%APPDATA%\GamerAI\state.json
```

---

## Configuration

`config.json` (defaults shown):

```json
{
  "coordinator_url": "http://localhost:8000",
  "polling_interval_seconds": 5,
  "earnings_print_minutes": 10,
  "idle": {
    "min_input_idle_seconds": 60,
    "max_cpu_percent": 30,
    "cpu_sample_seconds": 2
  },
  "model": null,
  "worker_id": null
}
```

- `coordinator_url` — full URL of the GamerAI coordinator. Use HTTPS in production.
- `polling_interval_seconds` — how often the agent checks for jobs / re-checks idleness.
- `earnings_print_minutes` — how often the agent logs an earnings summary.
- `idle.min_input_idle_seconds` — keyboard/mouse must be quiet at least this long.
- `idle.max_cpu_percent` — CPU must be below this for the agent to take work.
- `idle.cpu_sample_seconds` — sampling window for the CPU check.
- `model` — optional override for the Ollama model name.
- `worker_id` — leave `null` to auto-generate a stable ID (saved to state.json).

If you want to use a real local model instead of mock inference, install
[Ollama](https://ollama.com), `ollama pull llama3.2:1b`, and set the env var
`OLLAMA_URL=http://localhost:11434` before launching the agent.

---

## How earnings work

Every completed job credits your worker:

```
earnings_usd = completion_tokens * RATE_PER_TOKEN * WORKER_SHARE
```

Defaults on the platform: `RATE_PER_TOKEN = $0.000005`, `WORKER_SHARE = 0.7`.

The coordinator is the source of truth — your local `state.json` is just a
mirror so you can see totals without a network call. Verify against the
coordinator any time:

```
GET  https://<coordinator>/earnings/<worker_id>
```

Or run `agent.exe --status` to see what the agent has tracked locally.

Payouts in this MVP are **simulated only**. There is no real cash settlement
yet — see the main README's roadmap for Phase 3 (marketplace + payout rails).

---

## Building the .exe (developers)

From a Windows host with Python 3.11+:

```powershell
cd windows-agent
pip install -r requirements.txt
build.bat
```

That's a wrapper for:

```powershell
pyinstaller --onefile --name agent --add-data "config.json;." agent.py
```

Output: `dist\agent.exe`. Distribute that file together with `config.json`.

### Building the installer (optional)

1. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php).
2. After `build.bat` succeeds, run:
   ```powershell
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
   ```
3. Output: `Output\GamerAI-Agent-Setup.exe`.

The installer:
- copies `agent.exe`, `config.json`, and this README to `Program Files\GamerAI Agent`
- adds Start Menu shortcuts (foreground, background, edit config, view logs)
- optionally adds a desktop shortcut
- optionally adds a `--background` startup shortcut to `shell:startup`
- registers a normal Windows uninstall entry

---

## Safety notes (important)

Read this before installing on someone else's machine.

- **The agent runs arbitrary text generation prompts from the coordinator.**
  If you don't trust the coordinator operator, don't install the agent. They
  control what your GPU is asked to compute.
- **Idle gating is best-effort, not a hard guarantee.** A burst of inference
  can briefly spike CPU/GPU before the idle check next runs. Tune
  `min_input_idle_seconds` and `max_cpu_percent` higher if you notice any
  impact on games or video calls.
- **The agent uses HTTP only by default.** In production, set
  `coordinator_url` to an HTTPS endpoint. Otherwise prompts and results
  travel in cleartext.
- **No auth in the MVP.** Anyone with the coordinator URL can submit jobs
  that your machine may run. Run only against coordinators you control or
  trust until auth lands (Phase 2 in the main roadmap).
- **The agent does not run as a Windows service or with elevated privileges.**
  It runs in the current user's session. To stop it: close the window, or
  kill `agent.exe` from Task Manager. The Inno Setup uninstaller removes
  everything except your `state.json` (delete `%APPDATA%\GamerAI` to wipe
  the worker identity and totals).
- **No auto-updater.** If a security fix ships, you must reinstall manually.
- **Mock mode by default.** Without `OLLAMA_URL` set, the agent returns a
  stub response — useful for end-to-end testing without burning GPU cycles.
  Enable real inference only when you're ready.
- **Token counts can be approximate.** When the inference backend doesn't
  report exact token counts, the agent estimates `len(text) // 4`. Earnings
  are computed from those numbers; expect ±10% drift vs. a true tokenizer.

If anything looks wrong, kill the process and check
`%APPDATA%\GamerAI\logs\agent.log` — every job, error, and earnings update
is recorded there.
