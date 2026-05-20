"""GamerAI Windows agent.

Runs in the background on a gamer's machine, picks up inference jobs from
the coordinator only when the system is idle, and tracks per-job earnings.

Usage:
    python agent.py                      # foreground, prints to console
    python agent.py --background         # log to file only, no console output
    python agent.py --config config.json
    python agent.py --once               # process at most one job, then exit
    python agent.py --status             # print local earnings totals and exit

This file is single-file by design so it can be packaged with:
    pyinstaller --onefile --name agent agent.py
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import psutil

IS_WINDOWS = platform.system() == "Windows"

# Human-readable agent version. Bump this in the same commit as any
# behavior change you want a contributor to be able to verify on
# their machine — it's surfaced in the console banner and in the
# periodic status line so a manually-launched agent prints the version
# the moment it starts. The CI-generated version.txt (short-sha +
# build timestamp) is still what the self-updater diffs against;
# AGENT_VERSION is just the human-facing label.
AGENT_VERSION = "1.0.1"

# ---------------------------------------------------------------------------
# Idle detection
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    import ctypes

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    def input_idle_seconds() -> float:
        """Seconds since the last keyboard / mouse event (Windows only)."""
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        return max(0.0, millis / 1000.0)

else:
    def input_idle_seconds() -> float:
        """Non-Windows fallback: pretend the user has been away forever
        so the CPU check is the only gate. Lets you dev/test on Linux/Mac."""
        return 1e9


def cpu_percent(sample_seconds: float) -> float:
    return psutil.cpu_percent(interval=sample_seconds)


# ---------------------------------------------------------------------------
# Power management (Windows-only)
# ---------------------------------------------------------------------------
# SetThreadExecutionState flags from winnt.h. ES_CONTINUOUS keeps the request
# active until we explicitly clear it; ES_SYSTEM_REQUIRED tells Windows the
# system is needed (resets the idle timer that triggers sleep).
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake_begin(log: logging.Logger) -> bool:
    """Ask Windows not to sleep while the agent is running. Returns True on
    success. No-op on non-Windows so the same code runs in dev."""
    if not IS_WINDOWS:
        return False
    try:
        rc = ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
        )
        if rc == 0:
            log.warning("SetThreadExecutionState failed; sleep may still occur")
            return False
        log.info("keep-awake on (preventing system sleep while online)")
        return True
    except Exception as e:
        log.warning("keep-awake setup failed: %s", e)
        return False


def keep_awake_end(log: logging.Logger) -> None:
    """Release the keep-awake request. Safe to call even if begin failed."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        log.info("keep-awake released")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Self-update
# ---------------------------------------------------------------------------
# version.txt is written by CI at build time (commit SHA + ISO timestamp)
# and bundled into the exe via PyInstaller --add-data. Read-from-bundle
# path uses sys._MEIPASS when frozen; falls back to a sibling file or
# the literal "dev" string when running from source.
def current_version() -> str:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "version.txt")
        candidates.append(Path(sys.executable).parent / "version.txt")
    candidates.append(Path(__file__).parent / "version.txt")
    for p in candidates:
        try:
            if p.exists():
                return p.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            continue
    return "dev"


def fetch_latest_version(base_url: str, timeout: float = 10.0) -> Optional[str]:
    """Read the latest published version string from /download/version.txt.
    Returns None on network failure so the agent keeps running."""
    url = f"{base_url.rstrip('/')}/download/version.txt"
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.text.strip() or None
    except Exception:
        return None


def _agent_exe_path() -> Optional[Path]:
    """Filesystem path of the running agent.exe when frozen; None when
    running from a .py source (in which case we never auto-update —
    devs use git pull)."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def _write_update_batch(exe: Path, new_exe: Path) -> Path:
    """Generate the post-exit batch that swaps the binary and relaunches.
    Lives next to the exe so it inherits the right working directory."""
    bat = exe.with_name("update.bat")
    # Quote-and-escape paths defensively; the directory may have spaces
    # (e.g. C:\Program Files\GamerAI Agent).
    bat.write_text(
        "@echo off\r\n"
        ":: Auto-generated by agent.exe self-update. Safe to delete.\r\n"
        ":: Step 1: give the parent agent time to exit cleanly.\r\n"
        "timeout /t 3 /nobreak >nul\r\n"
        ":: Step 2: force-kill any straggler agent.exe instances (defensive).\r\n"
        'taskkill /F /IM "agent.exe" >nul 2>nul\r\n'
        "timeout /t 1 /nobreak >nul\r\n"
        ":: Step 3: swap in the new binary. Retry once on lock failure.\r\n"
        f'move /Y "{new_exe}" "{exe}"\r\n'
        "if errorlevel 1 (\r\n"
        "  timeout /t 5 /nobreak >nul\r\n"
        f'  move /Y "{new_exe}" "{exe}"\r\n'
        ")\r\n"
        ":: Step 4: relaunch in --background and clean up the batch.\r\n"
        f'start "" "{exe}" --background\r\n'
        'del "%~f0"\r\n',
        encoding="ascii",
    )
    return bat


def _apply_update(
    base_url: str, exe: Path, log: logging.Logger, keep_awake_active: bool
) -> bool:
    """Download the published agent.exe, stage it next to the running
    binary, write update.bat, fire it as a detached process, and exit.

    Returns True if the update kicked off (caller should exit); False
    means we stayed put (download failed, etc.) and life continues."""
    new_url = f"{base_url.rstrip('/')}/download/agent.exe"
    staged = exe.with_name("agent.exe.new")
    log.info("self-update: downloading %s -> %s", new_url, staged)
    try:
        with httpx.Client(timeout=120.0) as c, c.stream("GET", new_url) as r:
            r.raise_for_status()
            with open(staged, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
    except Exception as e:
        log.warning("self-update: download failed: %s", e)
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    # Sanity-check the download is at least binary-shaped (non-empty
    # and starts with the PE 'MZ' header). Catches the case where
    # /download/agent.exe is briefly a 404 HTML page mid-deploy.
    try:
        with open(staged, "rb") as f:
            head = f.read(2)
        if head != b"MZ":
            log.warning("self-update: downloaded file is not a Windows PE; aborting")
            staged.unlink(missing_ok=True)
            return False
    except OSError as e:
        log.warning("self-update: could not inspect staged file: %s", e)
        return False

    try:
        bat = _write_update_batch(exe, staged)
    except OSError as e:
        log.warning("self-update: could not write update.bat (%s); aborting", e)
        staged.unlink(missing_ok=True)
        return False

    log.info("self-update: launching update.bat (%s) — exiting agent", bat)
    try:
        creationflags = 0
        if IS_WINDOWS:
            # Detach so the child survives our exit. CREATE_NEW_PROCESS_GROUP
            # + DETACHED_PROCESS make the batch run independently.
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            )
        subprocess.Popen(
            ["cmd.exe", "/c", str(bat)] if IS_WINDOWS else ["/bin/sh", str(bat)],
            close_fds=True,
            creationflags=creationflags,
        )
    except Exception as e:
        log.warning("self-update: could not launch update.bat: %s", e)
        return False

    # Best-effort release of keep-awake so the system can sleep again
    # if the new agent takes a moment to come up.
    if keep_awake_active:
        keep_awake_end(log)
    # The caller will sys.exit; we don't kill ourselves here in case the
    # caller wants to do its own cleanup first.
    return True


def updater_loop(
    cfg: "Config",
    log: logging.Logger,
    keep_awake_holder: dict,
    stop_event: threading.Event,
) -> None:
    """Background thread: poll the published version, trigger an update
    when ours is stale. ``keep_awake_holder`` is a mutable container so
    we can read the boolean from the main thread without races (a single
    flag is plenty here — we never write it from the updater)."""
    if not IS_WINDOWS:
        log.info("self-update: not on Windows — skipping update loop")
        return
    here = current_version()
    log.info("self-update: current version = %s, interval = %.1fh",
             here, cfg.update_check_interval_hours)
    # First check after a short initial delay so we don't hammer the
    # network the instant the agent boots.
    initial_delay = min(60.0, cfg.update_check_interval_hours * 3600.0 / 4)
    if stop_event.wait(initial_delay):
        return
    while not stop_event.is_set():
        latest = fetch_latest_version(cfg.coordinator_url)
        if latest and latest != here and latest != "":
            log.info("self-update: published version %s differs from running %s",
                     latest, here)
            exe = _agent_exe_path()
            if exe is None:
                log.info("self-update: not a frozen exe — skipping (dev mode)")
            else:
                fired = _apply_update(
                    cfg.coordinator_url, exe, log,
                    keep_awake_active=keep_awake_holder.get("active", False),
                )
                if fired:
                    # Tell main thread it's time to die.
                    keep_awake_holder["exit_requested"] = True
                    return
        stop_event.wait(cfg.update_check_interval_hours * 3600.0)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULTS = {
    "coordinator_url": "http://localhost:8000",
    "polling_interval_seconds": 5,
    "earnings_print_minutes": 10,
    "idle": {
        "min_input_idle_seconds": 60,
        "max_cpu_percent": 30,
        "cpu_sample_seconds": 2,
        # When true, if the user becomes active between job-claim and
        # inference-start the agent calls /jobs/abandon and forfeits
        # any pending earnings. Off by default: the existing behavior
        # is to drain (finish the in-flight job, *then* go offline).
        "override_drain": False,
    },
    "power": {
        # When true and running in --background mode, ask Windows not
        # to sleep while the agent is online. This is the "I've opted
        # my machine into the network" contract — autostart shortcuts
        # set --background, so this only fires for explicit
        # background-contributor mode. Foreground / --once never
        # touches power state.
        "keep_awake_while_online": True,
    },
    "update": {
        # Background self-update. Only active in --background mode
        # (autostart contributors). The agent polls
        # {coordinator_url}/download/version.txt every
        # check_interval_hours; on a version mismatch it pulls a fresh
        # agent.exe, writes update.bat, fires it detached, and exits.
        # Set enabled=false on a contributor's machine to pin the
        # version (e.g. for a developer running a custom build).
        "enabled": True,
        "check_interval_hours": 6,
    },
    "bootstrap": {
        # First-run inference bootstrap (Windows). If enabled, the
        # agent ensures Ollama + the default model are present before
        # entering the main loop. Sources artifacts from
        # {mirror_base_url}/download/* (defaults to coordinator_url),
        # falling back to `ollama pull` when the mirror is missing a
        # model. Best-effort: on failure the agent still runs and
        # returns mock inference (preserves pre-bootstrap behavior).
        "enabled": True,
        "model": "llama3.2:1b",
        "ollama_url": "http://localhost:11434",
        "mirror_base_url": None,  # null = use coordinator_url
    },
    "model": None,
    "worker_id": None,
    "api_token": None,
}


@dataclass
class Config:
    coordinator_url: str
    polling_interval: float
    earnings_print_seconds: float
    min_input_idle_seconds: float
    max_cpu_percent: float
    cpu_sample_seconds: float
    override_drain: bool
    keep_awake_while_online: bool
    update_enabled: bool
    update_check_interval_hours: float
    bootstrap_enabled: bool
    bootstrap_model: str
    bootstrap_ollama_url: str
    bootstrap_mirror_base_url: Optional[str]
    model: Optional[str]
    worker_id: Optional[str]
    api_token: Optional[str]

    @classmethod
    def load(cls, path: Optional[Path]) -> "Config":
        data = dict(DEFAULTS)
        if path and path.exists():
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            _deep_merge(data, user)
        idle = data["idle"]
        power = data.get("power", DEFAULTS["power"])
        update = data.get("update", DEFAULTS["update"])
        bootstrap = data.get("bootstrap", DEFAULTS["bootstrap"])
        # env overrides config so a single API_TOKEN export works
        # for ad-hoc testing without touching config.json.
        token = (os.getenv("API_TOKEN") or data.get("api_token") or "").strip()
        return cls(
            coordinator_url=str(data["coordinator_url"]).rstrip("/"),
            polling_interval=float(data["polling_interval_seconds"]),
            earnings_print_seconds=float(data["earnings_print_minutes"]) * 60.0,
            min_input_idle_seconds=float(idle["min_input_idle_seconds"]),
            max_cpu_percent=float(idle["max_cpu_percent"]),
            cpu_sample_seconds=float(idle["cpu_sample_seconds"]),
            override_drain=bool(idle.get("override_drain", False)),
            keep_awake_while_online=bool(
                power.get("keep_awake_while_online", True)
            ),
            update_enabled=bool(update.get("enabled", True)),
            update_check_interval_hours=float(
                update.get("check_interval_hours", 6)
            ),
            bootstrap_enabled=bool(bootstrap.get("enabled", True)),
            bootstrap_model=str(bootstrap.get("model", "llama3.2:1b")),
            bootstrap_ollama_url=str(
                bootstrap.get("ollama_url", "http://localhost:11434")
            ).rstrip("/"),
            bootstrap_mirror_base_url=(
                str(bootstrap["mirror_base_url"]).rstrip("/")
                if bootstrap.get("mirror_base_url")
                else None
            ),
            model=data.get("model"),
            worker_id=data.get("worker_id"),
            api_token=token or None,
        )


def _deep_merge(into: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            _deep_merge(into[k], v)
        else:
            into[k] = v


# ---------------------------------------------------------------------------
# Local state — worker_id + cumulative earnings, persisted next to config
# ---------------------------------------------------------------------------
def state_dir() -> Path:
    if IS_WINDOWS:
        base = os.getenv("APPDATA") or os.path.expanduser("~")
        d = Path(base) / "GamerAI"
    else:
        d = Path.home() / ".gamerai"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = state_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


STATE_PATH = state_dir() / "state.json"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        "worker_id": None,
        "api_token": None,
        "jobs": 0,
        "tokens": 0,
        "earnings_usd": 0.0,
    }


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def resolve_worker_id(cfg_worker_id: Optional[str], state: dict) -> str:
    if cfg_worker_id:
        return cfg_worker_id
    if state.get("worker_id"):
        return state["worker_id"]
    new_id = f"win-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    state["worker_id"] = new_id
    save_state(state)
    return new_id


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(background: bool) -> logging.Logger:
    log = logging.getLogger("gamerai.agent")
    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir() / "agent.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    if not background:
        try:
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(fmt)
            log.addHandler(stream)
        except Exception:
            pass

    return log


# ---------------------------------------------------------------------------
# Coordinator client
# ---------------------------------------------------------------------------
class Coordinator:
    def __init__(
        self,
        base_url: str,
        worker_id: str,
        log: logging.Logger,
        api_token: Optional[str] = None,
    ):
        self.base = base_url
        self.worker_id = worker_id
        self.log = log
        headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
        self.http = httpx.Client(timeout=30.0, headers=headers)

    def _post(self, path: str, body: dict, timeout: float = 10.0) -> Optional[dict]:
        try:
            resp = self.http.post(f"{self.base}{path}", json=body, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            self.log.warning("coordinator %s failed: %s", path, e)
            return None

    def _get(self, path: str) -> Optional[dict]:
        try:
            resp = self.http.get(f"{self.base}{path}", timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            self.log.warning("coordinator GET %s failed: %s", path, e)
            return None

    def register(self, capabilities: Optional[dict] = None) -> bool:
        body: dict = {"worker_id": self.worker_id}
        if capabilities:
            body["capabilities"] = capabilities
        for attempt in range(20):
            if self._post("/register", body) is not None:
                self.log.info(
                    "registered with coordinator at %s (capabilities=%s)",
                    self.base, capabilities or {},
                )
                return True
            time.sleep(min(2 * (attempt + 1), 15))
        self.log.error("could not register with coordinator")
        return False

    def heartbeat(self, status: str) -> None:
        self._post("/heartbeat", {"worker_id": self.worker_id, "status": status}, timeout=5)

    def next_job(self) -> Optional[dict]:
        out = self._post("/jobs/next", {"worker_id": self.worker_id}, timeout=10)
        return (out or {}).get("job")

    def claim(self, job_id: str) -> bool:
        return self._post("/jobs/claim", {"worker_id": self.worker_id, "job_id": job_id}) is not None

    def complete(self, payload: dict) -> Optional[dict]:
        return self._post("/jobs/complete", payload, timeout=30)

    def abandon(self, job_id: str) -> bool:
        """Voluntarily return a claimed job to the queue. Used in
        override-drain mode when the user becomes active between
        claim and inference. Coordinator requeues; another worker
        picks it up. Earnings are forfeited."""
        out = self._post(
            "/jobs/abandon",
            {"worker_id": self.worker_id, "job_id": job_id},
        )
        return bool((out or {}).get("ok"))

    def remote_earnings(self) -> Optional[dict]:
        return self._get(f"/earnings/{self.worker_id}")


# ---------------------------------------------------------------------------
# Inference bootstrap (Windows-only): install Ollama + default model
# ---------------------------------------------------------------------------
# Default chain on first-run is:
#   1. Probe ollama_url/api/tags — if it responds, Ollama is up.
#   2. Else find ollama.exe at known paths and start it detached.
#   3. Else download {mirror_base}/download/ollama-setup.exe and run /silent.
#   4. Poll up to BOOTSTRAP_OLLAMA_WAIT_SECONDS for the HTTP API.
#   5. Check /api/tags for the target model.
#   6. Else try the mirror: GET {mirror_base}/download/models/{slug}.gguf +
#      .Modelfile, then `ollama create <name> -f Modelfile`.
#   7. Else fall back to POST {ollama_url}/api/pull (uses Ollama's CDN).
#
# Best-effort: any step failing leaves the agent running with mock
# inference (the pre-bootstrap behavior). Idempotent — every step is a
# fast no-op when its precondition is already met.

BOOTSTRAP_OLLAMA_WAIT_SECONDS = 60.0
BOOTSTRAP_MODEL_PULL_TIMEOUT_SECONDS = 1800.0  # 30 min; small models well under
BOOTSTRAP_DOWNLOAD_CHUNK = 256 * 1024


def _model_slug(model: str) -> str:
    """`llama3.2:1b` -> `llama3.2-1b`. Used as the mirror filename stem."""
    return model.replace(":", "-").replace("/", "-")


def _ollama_responding(ollama_url: str, timeout: float = 2.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{ollama_url.rstrip('/')}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


def _find_ollama_exe() -> Optional[Path]:
    """Locate ollama.exe at the standard install paths. Returns None if
    not installed."""
    if not IS_WINDOWS:
        return None
    candidates = [
        Path(os.getenv("LOCALAPPDATA") or "") / "Programs" / "Ollama" / "ollama.exe",
        Path(os.getenv("ProgramFiles") or "C:/Program Files") / "Ollama" / "ollama.exe",
        Path(os.getenv("ProgramFiles(x86)") or "C:/Program Files (x86)")
            / "Ollama" / "ollama.exe",
    ]
    for p in candidates:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def _start_ollama_server(ollama_exe: Path, log: logging.Logger) -> bool:
    """Launch `ollama serve` detached so the API comes up. Ollama's
    installer normally drops a tray app that does this on login, but on
    a freshly-silent-installed box the user hasn't logged out/in yet.

    Ollama's default logging does NOT include prompts at INFO level, but
    setting OLLAMA_DEBUG=1 makes it dump prompt + response. We pin
    OLLAMA_DEBUG=0 explicitly when we spawn the server so the Ollama
    instance the agent installs cannot leak prompts to contributor-side
    logs even if the contributor has the env var set globally. This is
    a defense in depth on top of the community-tos.md clause forbidding
    contributor-side prompt logging — it costs us nothing and means
    fresh installs are safe by default."""
    if not IS_WINDOWS:
        return False
    safe_env = {**os.environ, "OLLAMA_DEBUG": "0"}
    try:
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
        subprocess.Popen(
            [str(ollama_exe), "serve"],
            close_fds=True,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=safe_env,
        )
        log.info(
            "bootstrap: launched ollama serve (%s) with OLLAMA_DEBUG=0",
            ollama_exe,
        )
        return True
    except Exception as e:
        log.warning("bootstrap: could not launch ollama serve: %s", e)
        return False


def _format_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _download_to(url: str, dest: Path, log: logging.Logger, label: str) -> bool:
    """Stream a URL to a temp file then rename atomically. Returns True
    only on a complete, non-empty download. Logs progress as percent +
    ETA when the server provides Content-Length (the usual case for a
    static file). Falls back to byte counts when Content-Length is
    absent (e.g. a chunked-encoded response)."""
    staged = dest.with_suffix(dest.suffix + ".part")
    log.info("bootstrap: downloading %s -> %s", url, dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()
        with httpx.Client(timeout=600.0, follow_redirects=True) as c:
            with c.stream("GET", url) as r:
                r.raise_for_status()
                total = 0
                try:
                    total = int(r.headers.get("content-length") or 0)
                except (TypeError, ValueError):
                    total = 0
                if total:
                    log.info(
                        "bootstrap: %s total size = %.1f MB",
                        label, total / (1024 * 1024),
                    )
                bytes_written = 0
                last_pct_bucket = 0
                last_log_time = time.time()
                with open(staged, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=BOOTSTRAP_DOWNLOAD_CHUNK):
                        f.write(chunk)
                        bytes_written += len(chunk)
                        now = time.time()
                        elapsed = max(now - started, 0.001)
                        rate = bytes_written / elapsed  # bytes/sec
                        if total > 0:
                            pct = bytes_written * 100.0 / total
                            pct_bucket = int(pct // 5)
                            should_log = (
                                pct_bucket > last_pct_bucket
                                or now - last_log_time >= 30
                            )
                            if should_log:
                                remaining = max(total - bytes_written, 0)
                                eta = remaining / rate if rate > 0 else 0
                                log.info(
                                    "bootstrap: %s %5.1f%% (%.0f / %.0f MB, %.1f MB/s) — ETA %s",
                                    label, pct,
                                    bytes_written / (1024 * 1024),
                                    total / (1024 * 1024),
                                    rate / (1024 * 1024),
                                    _format_eta(eta),
                                )
                                last_pct_bucket = pct_bucket
                                last_log_time = now
                        elif now - last_log_time >= 30:
                            log.info(
                                "bootstrap: %s %.0f MB downloaded (size unknown, %.1f MB/s)",
                                label, bytes_written / (1024 * 1024),
                                rate / (1024 * 1024),
                            )
                            last_log_time = now
    except Exception as e:
        log.warning("bootstrap: download of %s failed: %s", url, e)
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        if staged.stat().st_size == 0:
            staged.unlink(missing_ok=True)
            log.warning("bootstrap: %s download was empty", label)
            return False
        log.info(
            "bootstrap: %s complete (%.0f MB in %s)",
            label,
            staged.stat().st_size / (1024 * 1024),
            _format_eta(time.time() - started),
        )
        staged.replace(dest)
    except OSError as e:
        log.warning("bootstrap: could not finalize %s: %s", label, e)
        return False
    return True


def _install_ollama(mirror_base: str, log: logging.Logger) -> Optional[Path]:
    """Download OllamaSetup.exe from our mirror and run it silently.
    Returns the path to ollama.exe on success, else None."""
    if not IS_WINDOWS:
        return None
    setup_url = f"{mirror_base.rstrip('/')}/download/ollama-setup.exe"
    setup_dest = state_dir() / "ollama-setup.exe"
    if not _download_to(setup_url, setup_dest, log, "ollama-setup.exe"):
        return None
    log.info("bootstrap: running ollama installer (silent)")
    try:
        # Squirrel-based installer; /S is the silent flag.
        rc = subprocess.run(
            [str(setup_dest), "/S"],
            timeout=600,
            check=False,
        )
        log.info("bootstrap: ollama installer exited rc=%s", rc.returncode)
    except Exception as e:
        log.warning("bootstrap: ollama installer failed to run: %s", e)
        return None
    # Installer can take a moment to populate %LOCALAPPDATA%\Programs\Ollama.
    for _ in range(20):
        exe = _find_ollama_exe()
        if exe is not None:
            return exe
        time.sleep(1.0)
    log.warning("bootstrap: ollama.exe not found after install")
    return None


def _wait_for_ollama(
    ollama_url: str, log: logging.Logger,
    timeout: float = BOOTSTRAP_OLLAMA_WAIT_SECONDS,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ollama_responding(ollama_url):
            return True
        time.sleep(2.0)
    log.warning("bootstrap: ollama did not respond on %s within %.0fs",
                ollama_url, timeout)
    return False


def _model_present(ollama_url: str, model: str, log: logging.Logger) -> bool:
    """Check Ollama's /api/tags for an exact match of the model name.
    Ollama lists models as `name:tag` (e.g. `llama3.2:1b`)."""
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{ollama_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("bootstrap: /api/tags lookup failed: %s", e)
        return False
    for entry in data.get("models", []) or []:
        if entry.get("name") == model:
            return True
    return False


def _install_model_from_mirror(
    ollama_exe: Optional[Path],
    ollama_url: str,
    model: str,
    mirror_base: str,
    log: logging.Logger,
) -> bool:
    """Pull the model .gguf + Modelfile from our mirror and run
    `ollama create`. Returns True if the model is registered with
    Ollama after this. Mirror-side files live at:
        /download/models/<slug>.gguf
        /download/models/<slug>.Modelfile
    where slug = model with ':' -> '-'.
    """
    if ollama_exe is None:
        log.warning("bootstrap: no ollama.exe — cannot run `ollama create`")
        return False
    slug = _model_slug(model)
    base = mirror_base.rstrip("/")
    gguf_url = f"{base}/download/models/{slug}.gguf"
    modelfile_url = f"{base}/download/models/{slug}.Modelfile"

    # HEAD the gguf first to decide whether the mirror has this model
    # before we download a multi-GB blob that's actually a 404 HTML page.
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as c:
            head = c.head(gguf_url)
        if head.status_code != 200:
            log.info(
                "bootstrap: mirror does not have %s (HTTP %d); will fall back to ollama pull",
                slug, head.status_code,
            )
            return False
    except Exception as e:
        log.info("bootstrap: mirror HEAD failed (%s); will fall back to ollama pull", e)
        return False

    stage_dir = state_dir() / "bootstrap" / slug
    stage_dir.mkdir(parents=True, exist_ok=True)
    gguf_path = stage_dir / f"{slug}.gguf"
    modelfile_path = stage_dir / "Modelfile"

    # Upfront warning so the user doesn't think the agent has hung
    # mid-download. The .gguf is ~1 GB+ — at 10 Mbps that's ~15 min,
    # at 1 Mbps it's ~2 hours. Progress lines below give percent + ETA.
    log.info(
        "bootstrap: about to download the model weights (%s). This is "
        "the long step — typically 10-30 minutes on home internet, "
        "potentially over an hour on a slow connection. Please leave "
        "this window open; progress is logged below.",
        slug,
    )
    if not _download_to(gguf_url, gguf_path, log, f"{slug}.gguf"):
        return False
    if not _download_to(modelfile_url, modelfile_path, log, f"{slug}.Modelfile"):
        return False

    # Rewrite FROM line to absolute path so `ollama create` resolves
    # the gguf regardless of cwd.
    try:
        original = modelfile_path.read_text(encoding="utf-8")
        rewritten_lines = []
        for line in original.splitlines():
            if line.strip().lower().startswith("from "):
                rewritten_lines.append(f"FROM {gguf_path}")
            else:
                rewritten_lines.append(line)
        modelfile_path.write_text("\n".join(rewritten_lines) + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("bootstrap: could not normalize Modelfile path: %s", e)
        return False

    log.info("bootstrap: registering %s with Ollama via `ollama create`", model)
    try:
        result = subprocess.run(
            [str(ollama_exe), "create", model, "-f", str(modelfile_path)],
            timeout=BOOTSTRAP_MODEL_PULL_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            log.warning(
                "bootstrap: `ollama create` failed (rc=%d): %s",
                result.returncode, (result.stderr or "").strip()[:500],
            )
            return False
    except Exception as e:
        log.warning("bootstrap: `ollama create` raised: %s", e)
        return False
    return _model_present(ollama_url, model, log)


def _install_model_via_pull(
    ollama_url: str, model: str, log: logging.Logger,
) -> bool:
    """Fall back to Ollama's own CDN via POST /api/pull. Streams JSON
    progress lines; we use them to surface percent + ETA the same way
    the mirror path does. Ollama's stream emits `total` and `completed`
    on per-layer download events."""
    log.info("bootstrap: pulling %s via ollama /api/pull (CDN fallback)", model)
    log.info(
        "bootstrap: about to download the model weights from Ollama's CDN. "
        "This is the long step — typically 10-30 minutes on home "
        "internet, potentially over an hour on a slow connection. "
        "Please leave this window open; progress is logged below.",
    )
    started = time.time()
    try:
        with httpx.Client(timeout=BOOTSTRAP_MODEL_PULL_TIMEOUT_SECONDS) as c:
            with c.stream(
                "POST",
                f"{ollama_url.rstrip('/')}/api/pull",
                json={"name": model, "stream": True},
            ) as r:
                r.raise_for_status()
                last_log_time = time.time()
                last_pct_bucket = -1
                current_digest = ""
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    status = evt.get("status", "")
                    if status == "success":
                        log.info(
                            "bootstrap: ollama pull complete for %s (took %s)",
                            model, _format_eta(time.time() - started),
                        )
                        return True
                    if "error" in evt:
                        log.warning("bootstrap: ollama pull error: %s", evt["error"])
                        return False
                    total = int(evt.get("total") or 0)
                    completed = int(evt.get("completed") or 0)
                    digest = evt.get("digest", "")
                    now = time.time()
                    if total > 0 and completed >= 0:
                        pct = completed * 100.0 / total
                        pct_bucket = int(pct // 5)
                        digest_changed = digest != current_digest
                        if digest_changed:
                            current_digest = digest
                            last_pct_bucket = -1
                        should_log = (
                            pct_bucket > last_pct_bucket
                            or now - last_log_time >= 30
                            or digest_changed
                        )
                        if should_log:
                            elapsed = max(now - started, 0.001)
                            rate = completed / elapsed if completed > 0 else 0
                            eta = (total - completed) / rate if rate > 0 else 0
                            log.info(
                                "bootstrap: %s %5.1f%% (%.0f / %.0f MB) — ETA %s",
                                status, pct,
                                completed / (1024 * 1024),
                                total / (1024 * 1024),
                                _format_eta(eta),
                            )
                            last_pct_bucket = pct_bucket
                            last_log_time = now
                    elif status and now - last_log_time > 30:
                        log.info("bootstrap: ollama pull — %s", status)
                        last_log_time = now
    except Exception as e:
        log.warning("bootstrap: ollama pull failed: %s", e)
        return False
    return _model_present(ollama_url, model, log)


def bootstrap_inference(cfg: "Config", log: logging.Logger) -> Optional[str]:
    """Make sure Ollama + the default model are ready. Returns the
    working Ollama URL on success, or None on any failure (caller
    keeps running and falls back to mock inference).

    Skipped entirely on non-Windows and when bootstrap.enabled is
    false in config.json.
    """
    if not cfg.bootstrap_enabled:
        log.info("bootstrap: disabled in config — skipping")
        return None
    if not IS_WINDOWS:
        log.info("bootstrap: not on Windows — skipping (dev mode)")
        return None

    ollama_url = cfg.bootstrap_ollama_url
    mirror_base = cfg.bootstrap_mirror_base_url or cfg.coordinator_url
    model = cfg.bootstrap_model

    # Step 1-4: ensure Ollama is running.
    if _ollama_responding(ollama_url):
        log.info("bootstrap: ollama already running at %s", ollama_url)
    else:
        exe = _find_ollama_exe()
        if exe is None:
            exe = _install_ollama(mirror_base, log)
        if exe is None:
            log.warning("bootstrap: ollama not available — mock inference only")
            return None
        # Installer normally starts the tray-app server itself; if we got
        # here from an already-installed-but-not-running state we have
        # to kick it ourselves.
        if not _ollama_responding(ollama_url):
            _start_ollama_server(exe, log)
        if not _wait_for_ollama(ollama_url, log):
            return None

    # Step 5-7: ensure the model is present.
    if _model_present(ollama_url, model, log):
        log.info("bootstrap: model %s already installed", model)
        return ollama_url

    exe = _find_ollama_exe()
    if _install_model_from_mirror(exe, ollama_url, model, mirror_base, log):
        log.info("bootstrap: model %s ready (mirror)", model)
        return ollama_url
    if _install_model_via_pull(ollama_url, model, log):
        log.info("bootstrap: model %s ready (ollama CDN)", model)
        return ollama_url

    log.warning("bootstrap: could not install model %s — mock inference only", model)
    return None


# ---------------------------------------------------------------------------
# Inference (mock-only by default; real Ollama is opt-in)
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def run_inference(
    prompt: str,
    model: Optional[str],
    log: logging.Logger,
    messages: Optional[list] = None,
) -> dict:
    """Run a prompt (or chat-style ``messages`` list) against a local
    Ollama if OLLAMA_URL is set, otherwise return a mock response.

    When ``messages`` is provided the call goes to ``/api/chat`` so the
    model's chat template is applied (proper multi-turn behavior).
    Single-shot prompts keep the legacy ``/api/generate`` path."""
    ollama_url = os.getenv("OLLAMA_URL")
    use_model = model or os.getenv("MODEL") or "llama3.2:1b"
    if not ollama_url:
        time.sleep(0.5)
        text = f"[mock] {prompt[:200]}"
        return {
            "text": text,
            "prompt_tokens": estimate_tokens(prompt),
            "completion_tokens": estimate_tokens(text),
            "model": "mock",
        }
    try:
        with httpx.Client(timeout=600) as c:
            if messages:
                resp = c.post(
                    f"{ollama_url.rstrip('/')}/api/chat",
                    json={
                        "model": use_model,
                        "messages": messages,
                        "stream": False,
                    },
                )
            else:
                resp = c.post(
                    f"{ollama_url.rstrip('/')}/api/generate",
                    json={
                        "model": use_model,
                        "prompt": prompt,
                        "stream": False,
                    },
                )
            resp.raise_for_status()
            data = resp.json()
        if messages:
            text = (data.get("message") or {}).get("content", "")
        else:
            text = data.get("response", "")
        return {
            "text": text,
            "prompt_tokens": int(data.get("prompt_eval_count") or estimate_tokens(prompt)),
            "completion_tokens": int(data.get("eval_count") or estimate_tokens(text)),
            "model": use_model,
        }
    except Exception as e:
        log.warning("ollama call failed (%s); falling back to mock", e)
        text = f"[mock-fallback] {prompt[:200]}"
        return {
            "text": text,
            "prompt_tokens": estimate_tokens(prompt),
            "completion_tokens": estimate_tokens(text),
            "model": "mock",
        }


# ---------------------------------------------------------------------------
# Idle gate
# ---------------------------------------------------------------------------
def is_system_idle(cfg: Config) -> tuple[bool, str]:
    idle_for = input_idle_seconds()
    if idle_for < cfg.min_input_idle_seconds:
        return False, f"user active ({idle_for:.0f}s since last input)"
    cpu = cpu_percent(cfg.cpu_sample_seconds)
    if cpu >= cfg.max_cpu_percent:
        return False, f"cpu busy ({cpu:.1f}%)"
    return True, f"idle ({idle_for:.0f}s, cpu {cpu:.1f}%)"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def print_earnings(state: dict, log: logging.Logger) -> None:
    log.info(
        "Jobs completed: %d | Earnings: $%.6f",
        int(state.get("jobs", 0)),
        float(state.get("earnings_usd", 0.0)),
    )


# How often the main loop logs a "status: idle/busy/offline" line for
# the operator. This is in addition to the per-event lines (job claim,
# job complete, etc.) — the idea is that a quiet machine still shows a
# heartbeat in the console window every ~60s so you can tell at a
# glance that the agent is running and on what version.
STATUS_LINE_INTERVAL_SECONDS = 60.0


def log_status_line(
    log: logging.Logger, worker_id: str, status: str, reason: str = "",
) -> None:
    suffix = f" — {reason}" if reason else ""
    log.info(
        "status: %s | worker=%s | v%s (build %s)%s",
        status, worker_id, AGENT_VERSION, current_version(), suffix,
    )


def main_loop(
    cfg: Config,
    coord: Coordinator,
    state: dict,
    log: logging.Logger,
    once: bool,
    should_exit=lambda: False,
) -> None:
    last_heartbeat = 0.0
    last_earnings_print = time.time()
    # Periodic heartbeat-in-the-console: the operator wants to glance
    # at the agent window and immediately see status + version. We
    # log on every state transition AND every STATUS_LINE_INTERVAL_SECONDS
    # so a quiet machine still emits a line.
    last_status_line = 0.0
    last_logged_status: Optional[str] = None
    # Tracks whether the last main-loop iteration completed a real job.
    # Used to differentiate a plain "user became active" message from
    # "user became active right after a job completed" — the latter is
    # the graceful-drain case the README addendum advertises, and
    # surfacing it gives the contributor confidence that their last
    # work landed before the machine went offline.
    just_drained_job_id: Optional[str] = None

    def emit_status(current: str, reason: str = "") -> None:
        nonlocal last_status_line, last_logged_status
        now2 = time.time()
        if (
            current != last_logged_status
            or now2 - last_status_line >= STATUS_LINE_INTERVAL_SECONDS
        ):
            log_status_line(log, coord.worker_id, current, reason)
            last_logged_status = current
            last_status_line = now2

    while True:
        # The updater thread can signal a graceful exit when it has
        # kicked off the swap-and-restart batch. Check between
        # iterations so we never abandon an in-flight job mid-claim.
        if should_exit():
            log.info("self-update kicked in — exiting main loop for replacement")
            return

        now = time.time()
        if now - last_heartbeat > 5:
            coord.heartbeat("idle")
            last_heartbeat = now

        if now - last_earnings_print > cfg.earnings_print_seconds:
            print_earnings(state, log)
            last_earnings_print = now

        idle, reason = is_system_idle(cfg)
        if not idle:
            if just_drained_job_id is not None:
                log.info(
                    "user activity detected (%s) — last job %s complete, agent offline",
                    reason, just_drained_job_id,
                )
                just_drained_job_id = None
            coord.heartbeat("offline")
            last_heartbeat = time.time()
            emit_status("offline", reason)
            time.sleep(cfg.polling_interval)
            continue

        # Idle. If we just came off a job we'd previously logged the
        # drain message, clear the breadcrumb so the next user-active
        # transition doesn't reference a stale job_id.
        just_drained_job_id = None
        emit_status("idle", reason)

        did_work, processed_job_id = process_one(cfg, coord, state, log)
        if did_work:
            just_drained_job_id = processed_job_id
        if once and did_work:
            return
        if not did_work:
            time.sleep(cfg.polling_interval)


def process_one(
    cfg: Config, coord: Coordinator, state: dict, log: logging.Logger,
) -> tuple[bool, Optional[str]]:
    """Pop, claim, run, complete one job. Returns (did_work, job_id).
    The job_id is captured so the main loop can reference it in the
    next iteration's drain-visibility log line."""
    job = coord.next_job()
    if not job:
        return False, None
    job_id = job.get("job_id")
    prompt = job.get("prompt", "")
    log.info("job %s started", job_id)
    coord.claim(job_id)
    coord.heartbeat("busy")

    if cfg.override_drain:
        idle_now, reason = is_system_idle(cfg)
        if not idle_now:
            log.info(
                "override-drain: %s — abandoning job %s (earnings forfeited)",
                reason, job_id,
            )
            coord.abandon(job_id)
            coord.heartbeat("offline")
            return False, None

    started = time.time()
    try:
        result = run_inference(
            prompt,
            cfg.model or job.get("model"),
            log,
            messages=job.get("messages"),
        )
        duration = round(time.time() - started, 3)
        out = coord.complete(
            {
                "worker_id": coord.worker_id,
                "job_id": job_id,
                "text": result["text"],
                "model": result["model"],
                "prompt_tokens": result["prompt_tokens"],
                "completion_tokens": result["completion_tokens"],
                "duration_seconds": duration,
                "status": "complete",
            }
        )
        earnings = float((out or {}).get("earnings", 0.0))
        state["jobs"] = int(state.get("jobs", 0)) + 1
        state["tokens"] = int(state.get("tokens", 0)) + int(result["completion_tokens"])
        state["earnings_usd"] = round(float(state.get("earnings_usd", 0.0)) + earnings, 10)
        save_state(state)
        log.info(
            "job %s finished: %d tokens, $%.8f, %.2fs",
            job_id, result["completion_tokens"], earnings, duration,
        )
    except Exception as e:
        log.exception("job %s failed: %s", job_id, e)
        coord.complete(
            {
                "worker_id": coord.worker_id,
                "job_id": job_id,
                "text": "",
                "model": cfg.model or "unknown",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "duration_seconds": round(time.time() - started, 3),
                "status": "error",
                "error": str(e),
            }
        )
    finally:
        coord.heartbeat("idle")
    return True, job_id


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GamerAI Windows agent")
    here = Path(__file__).resolve().parent
    p.add_argument("--config", type=Path, default=here / "config.json")
    p.add_argument("--background", action="store_true",
                   help="suppress console output; log to file only")
    p.add_argument("--once", action="store_true",
                   help="process at most one job and exit (useful for tests)")
    p.add_argument("--status", action="store_true",
                   help="print local earnings totals and exit")
    p.add_argument("--version", action="store_true",
                   help="print agent version + build id and exit")
    return p.parse_args(argv)


def resolve_api_token(
    cfg_token: Optional[str],
    state: dict,
    background: bool,
) -> Optional[str]:
    """Token-resolution chain for first-run onboarding:

      env API_TOKEN   ← already merged into cfg.api_token by Config.load
        →  config.json["api_token"]
        →  state.json["api_token"]      (persisted from a prior first-run prompt)
        →  interactive prompt (foreground only)

    Returns the resolved token, or None when running in --background and
    no token is available (caller should error out — we can't prompt
    when there's no console).
    """
    if cfg_token:
        return cfg_token
    state_token = state.get("api_token")
    if state_token:
        return state_token
    if background:
        return None
    # First-run prompt. Most recruits land here exactly once.
    sys.stdout.write(
        "\n"
        "GamerAI agent first-run setup\n"
        "-----------------------------\n"
        "Paste the bearer token from your invite redemption page.\n"
        "Looks like:  gai_<64 hex chars>\n"
        "\n"
    )
    sys.stdout.flush()
    try:
        entered = input("token: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\naborted — no token entered.\n")
        return None
    if not entered.startswith("gai_"):
        sys.stderr.write(
            "that doesn't look like a GamerAI token (expected gai_<...>).\n"
            "edit %APPDATA%\\GamerAI\\state.json manually if you need to.\n"
        )
        return None
    state["api_token"] = entered
    save_state(state)
    sys.stdout.write(f"token saved to {STATE_PATH}\n\n")
    return entered


def _print_greeting(once: bool, status: bool) -> None:
    """First visible output. Runs before logging is wired up so a
    freshly double-clicked agent.exe shows text immediately instead
    of a blank console while PyInstaller finishes extracting itself.
    Suppressed for --background (no console) and --status (the user
    asked for a focused report).

    The version line is the load-bearing bit: after a self-update the
    contributor (or you, when debugging) should see the new version
    here without having to dig through %APPDATA% logs."""
    if once or status:
        return
    try:
        build = current_version()
        sys.stdout.write(
            "\n"
            f"GamerAI agent v{AGENT_VERSION} (build {build})\n"
            "Logs:    %APPDATA%\\GamerAI\\logs\\agent.log\n"
            "\n"
        )
        sys.stdout.flush()
    except Exception:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.version:
        print(f"GamerAI agent v{AGENT_VERSION} (build {current_version()})")
        return 0
    if not args.background:
        _print_greeting(args.once, args.status)
    cfg = Config.load(args.config if args.config.exists() else None)
    state = load_state()
    worker_id = resolve_worker_id(cfg.worker_id, state)
    log = setup_logging(args.background)

    if args.status:
        print(f"version:   v{AGENT_VERSION} (build {current_version()})")
        print(f"worker_id: {worker_id}")
        print(f"jobs:      {state.get('jobs', 0)}")
        print(f"tokens:    {state.get('tokens', 0)}")
        print(f"earnings:  ${float(state.get('earnings_usd', 0.0)):.6f}")
        return 0

    token = resolve_api_token(cfg.api_token, state, args.background)
    if not token:
        msg = (
            "no api_token configured. "
            "edit %APPDATA%\\GamerAI\\state.json and set \"api_token\", "
            "or run the agent in the foreground once and paste your token."
            if IS_WINDOWS else
            "no api_token configured. Set $API_TOKEN or edit ~/.gamerai/state.json"
        )
        log.error(msg)
        sys.stderr.write(msg + "\n")
        return 2
    cfg.api_token = token

    log.info(
        "GamerAI agent v%s (build %s) starting on %s — worker_id=%s",
        AGENT_VERSION, current_version(), platform.platform(), worker_id,
    )
    log.info("coordinator=%s polling=%ss idle threshold=%ss cpu<%s%% auth=%s",
             cfg.coordinator_url, cfg.polling_interval,
             cfg.min_input_idle_seconds, cfg.max_cpu_percent,
             "on" if cfg.api_token else "off")

    # Visibility-only check. If the contributor has OLLAMA_DEBUG=1 set
    # in the agent's environment they likely also have it set for the
    # Ollama process, which would log prompts. We can't strictly verify
    # Ollama's state without changing its API, so we log a warning here.
    # The community ToS forbids debug logging; this surfaces a concrete
    # signal for the admin during incident review.
    if (os.getenv("OLLAMA_DEBUG") or "0").strip() not in ("", "0", "false", "False"):
        log.warning(
            "OLLAMA_DEBUG is set in this agent's environment — Ollama may "
            "log prompts. This violates the community ToS; please unset it."
        )

    # First-run bootstrap: install Ollama + default model. Best-effort;
    # on failure we fall back to mock inference and keep running.
    # Skipped if OLLAMA_URL is already set in the environment, so devs
    # pointing at a remote/test Ollama keep that override.
    if not os.getenv("OLLAMA_URL"):
        if not args.background and cfg.bootstrap_enabled and IS_WINDOWS:
            try:
                sys.stdout.write(
                    "First-run setup: ensuring Ollama and the default model "
                    "are installed.\n"
                    "On a fresh machine this can take several minutes "
                    "(downloads ~2 GB). Subsequent launches are instant.\n"
                    "\n"
                )
                sys.stdout.flush()
            except Exception:
                pass
        ready_url = bootstrap_inference(cfg, log)
        if ready_url:
            os.environ["OLLAMA_URL"] = ready_url

    # Keep-awake is the "I've committed my machine" contract — only
    # enabled when the contributor opts into background mode (autostart
    # installer toggle) and the config knob is on. Foreground/--once
    # runs never touch power state.
    keep_awake_active = False
    if args.background and cfg.keep_awake_while_online:
        keep_awake_active = keep_awake_begin(log)
    elif args.background and not cfg.keep_awake_while_online:
        log.info("keep-awake off (power.keep_awake_while_online=false)")

    coord = Coordinator(cfg.coordinator_url, worker_id, log, cfg.api_token)
    # Advertise the model we can actually serve. The bootstrap above
    # either confirmed the model is loaded into Ollama or fell back to
    # mock — either way, the coordinator should know this worker is
    # eligible for jobs targeting bootstrap_model. (Coordinator-side
    # capability-aware routing is still on the deferred list; for now
    # this is informational and surfaces in /workers.)
    capabilities = (
        {"models": [cfg.bootstrap_model]}
        if cfg.bootstrap_enabled and os.getenv("OLLAMA_URL")
        else None
    )
    if not coord.register(capabilities=capabilities):
        if keep_awake_active:
            keep_awake_end(log)
        return 1

    # Self-update background thread. Only runs in --background mode
    # (the autostart-contributor path) and when the config knob is on.
    # Communicates with the main thread via stop_event + a tiny shared
    # holder dict: keep_awake.active is read-only for the updater; the
    # updater sets exit_requested when it kicks off the swap-and-restart.
    updater_thread: Optional[threading.Thread] = None
    stop_event = threading.Event()
    keep_awake_holder = {"active": keep_awake_active, "exit_requested": False}
    if args.background and cfg.update_enabled:
        updater_thread = threading.Thread(
            target=updater_loop,
            args=(cfg, log, keep_awake_holder, stop_event),
            name="gamerai-updater",
            daemon=True,
        )
        updater_thread.start()

    try:
        main_loop(
            cfg, coord, state, log,
            once=args.once,
            should_exit=lambda: keep_awake_holder.get("exit_requested", False),
        )
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        stop_event.set()
        try:
            coord.heartbeat("offline")
        except Exception:
            pass
        if keep_awake_holder.get("active") and not keep_awake_holder.get("exit_requested"):
            keep_awake_end(log)
        print_earnings(state, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
