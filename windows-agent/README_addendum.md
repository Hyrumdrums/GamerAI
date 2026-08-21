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
   submits the result. Tokens served are credited to your contribution
   ledger and drive your tier (BRONZE → PLATINUM) on the network. Tier
   determines your monthly quota for using the network's AI suite plus
   how many invitees you can grant access to.
4. The instant the user touches the keyboard/mouse — or CPU rises — the
   agent reports `offline` and stops accepting new jobs. The current job (if
   any) finishes; nothing is killed mid-inference.

All thresholds are tunable in `config.json`.

### Power draw is demand-driven, not uptime-driven

A common worry: "if I leave the agent online overnight, will my power bill
explode?" No — power scales with paid demand, not uptime:

| State | GPU draw (marginal) |
|---|---:|
| Online, no jobs | ~0 W |
| Online, model warm (Ollama keep-alive window) | ~30 W |
| Active inference | ~250–400 W |

So "leave it on overnight" costs near-zero on a quiet network.
Contributors burning real power are also serving real demand — and, at
GOLD+ in the opt-in paid pool, earning per-token bonuses that offset
that power cost.

---

## Install (end user)

### Option A — bundled installer (recommended)

1. Download `GamerAI-Agent-Setup.exe`.
2. Run it. Defaults are fine for most users.
3. (Optional) Tick **"Run on Windows startup (tray mode)"** during
   install to launch silently on every boot with a system tray icon.
4. Edit `config.json` (Start menu → "Edit configuration") to point at your
   coordinator URL if it's not the default.

### Option B — standalone exe

1. Download `agent.exe` and `config.json` into the same folder.
2. Edit `config.json` and set `"coordinator_url"`.
3. Double-click `agent.exe` — or, for silent operation, create a shortcut
   with the argument `--tray` and drop it in `shell:startup`. The
   agent will appear as a small icon in the system tray overflow.

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

| command                                | what it does                                                       |
| -------------------------------------- | ------------------------------------------------------------------ |
| `agent.exe`                            | foreground — visible console, logs to console **and** file         |
| `agent.exe --tray`                     | tray mode: hidden console + system tray icon (autostart default)   |
| `agent.exe --background`               | deprecated alias for `--tray`, kept for one release                |
| `agent.exe --once`                     | process one job and exit (smoke test)                              |
| `agent.exe --status`                   | print local earnings totals and exit                               |
| `agent.exe --config C:\path\to\config.json` | override config path                                          |

Logs live at:

```
%APPDATA%\GamerAI\logs\agent.log
```

State (`worker_id` + cumulative earnings) is persisted to:

```
%APPDATA%\GamerAI\state.json
```

---

## Tray mode UX

`--tray` (the autostart default) hides the console window and surfaces a
small system tray icon. Right-click the icon for:

- **Show console** — surfaces the live log window with every line since
  boot still in scrollback. Double-click does the same.
- **Hide console** — slides it back into the tray.
- **Open log file** — opens `%APPDATA%\GamerAI\logs\agent.log` in the
  system default text editor (Notepad).
- **GamerAI Agent v*x.y.z*** — disabled item, shows the running version.
- **Exit** — graceful shutdown: heartbeats `offline`, releases keep-awake,
  prints earnings, then exits.

### "Where did my tray icon go?"

Windows 10/11 hide new tray icons in the **overflow chevron** (the small
`^` next to the clock) by default. To promote the GamerAI icon to the
always-visible row, drag it out of the overflow flyout, or
**Settings → Personalization → Taskbar → Other system tray icons**.
This is a Windows policy — apps cannot force-promote themselves.

### "Hide console" doesn't hide anything on Windows 11

On Windows 11 (22H2+), **Windows Terminal is the default host for console
apps**, and `GetConsoleWindow()` returns the handle of conhost's invisible
ConPTY surface, not the actual Windows Terminal tab you see on screen —
`ShowWindow(..., SW_HIDE)` hides a window nobody was looking at, so the
console appears to stay put. Pre-Windows-11 (and Windows 11 with the
legacy console host, not Windows Terminal, set as default) hides fine,
since `GetConsoleWindow()` there *is* the visible window.

There's no reliable, side-effect-free fix from inside the app: locating
"the" Windows Terminal window from a console subprocess isn't something
Win32 exposes cleanly, and a wrong guess risks hiding some *other* window
of the user's (Windows Terminal is usually one process for all of a
user's terminal windows/tabs). Workaround for now: set **Settings →
Privacy & security → For developers → Terminal → Default terminal
application** to *Windows Console Host* instead of *Windows Terminal* —
tray hide/show then behaves like pre-11. Revisit a real fix (e.g.
relaunching tray mode as a fully consoleless `CREATE_NO_WINDOW` child,
trading away the "Show console" menu item) if this keeps coming up.

### Notifications

The agent uses Windows Action Center toasts for:

- **First-run token missing** — fires once at startup with a prompt to
  open the console and paste your token.
- **Updated** — fires once after an auto-update successfully relaunches
  into a newer version (e.g. "Now running v1.1.13").

### Single-instance behavior

If you double-click `agent.exe` while the agent is already running, the
existing instance's console pops to front instead of starting a second
worker. This is handled by binding `127.0.0.1:48591` — a quick
`netstat -ano | findstr 48591` confirms exactly one listener.

### Windows Terminal caveat

If you set **Windows Terminal** as your default terminal app, double-clicking
`agent.exe` directly from Explorer may host the console under `wt.exe`,
whose window can't be hidden via `GetConsoleWindow()`. The autostart
shortcut runs the exe through the standard Windows shell loader, which
always uses conhost — so the boot path is unaffected. If you hit the
issue manually, switch the default terminal back to "Windows Console
Host" or run the agent via the Start Menu shortcut.

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

## How contribution accounting works

Two parallel ledgers track what your agent does on the network:

### 1. Contribution ledger (every contributor)

Every completed job credits your contributor record with:
- **tokens contributed** — how much work your GPU served the network
- **tokens consumed** — how much you (and your invitees) drew from the
  network's shared pool

These two numbers drive your **tier** (BRONZE → PLATINUM). The
coordinator promotes/demotes contributors nightly based on uptime,
capability, and the ratio of claimed jobs to online time. Tier sets your
monthly quota, invite slots, and eligibility for the paid pool.

The coordinator is the source of truth — your local `state.json` is just
a mirror so you can see totals without a network call. Verify against
the coordinator any time:

```
GET  https://<coordinator>/earnings/<worker_id>
```

Or run `agent.exe --status` to see what the agent has tracked locally.

### 2. Bonus payouts (opt-in, GOLD+)

Once the **paid customer layer** ships (Phase 3b.ii in the main README
roadmap), GOLD+ contributors can opt into serving paid jobs. When you
do:

```
bonus_usd = paid_completion_tokens * paid_rate_per_token * 0.80
```

80% of the paid customer's per-token price goes to whichever
contributor served the job. 20% goes to the platform to cover
coordinator infrastructure and future development.

Bonus payouts are **simulated only** in today's MVP — there is no real
cash settlement yet. The infrastructure to issue real payouts (Stripe
Connect, ACH, 1099s) ships with Phase 3b.ii. See the main README's
roadmap.

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
pyinstaller --onefile --name agent ^
    --add-data "config.json;." ^
    --add-data "tray.ico;." ^
    --collect-submodules pystray ^
    --icon "tray.ico" ^
    agent.py
```

Output: `dist\agent.exe`. Distribute that file together with `config.json`
and `tray.ico` (or rely on the bundled installer, which carries both).

### Building the installer (optional)

1. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php).
2. After `build.bat` succeeds, run:
   ```powershell
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
   ```
3. Output: `Output\GamerAI-Agent-Setup.exe`.

The installer:
- copies `agent.exe`, `config.json`, `tray.ico`, and this README to
  `Program Files\GamerAI Agent`
- adds Start Menu shortcuts (foreground, tray, edit config, view logs)
- optionally adds a desktop shortcut
- optionally adds a `--tray` startup shortcut to `shell:startup`
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
- **Auth is the membership gate.** Once membership ships (Phase 3b.i),
  the `api_token` in `config.json` IS your contributor identity — it's
  how the coordinator credits your tier and accounts for the people you
  invite. Do not share it; do not run without it. Today (pre-membership),
  the token is optional; do not run against an unauthenticated
  coordinator you don't control.
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
