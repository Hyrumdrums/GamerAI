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
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import psutil

IS_WINDOWS = platform.system() == "Windows"

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

    def register(self) -> bool:
        for attempt in range(20):
            if self._post("/register", {"worker_id": self.worker_id}) is not None:
                self.log.info("registered with coordinator at %s", self.base)
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

    def remote_earnings(self) -> Optional[dict]:
        return self._get(f"/earnings/{self.worker_id}")


# ---------------------------------------------------------------------------
# Inference (mock-only by default; real Ollama is opt-in)
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def run_inference(prompt: str, model: Optional[str], log: logging.Logger) -> dict:
    """Run the prompt against a local Ollama if OLLAMA_URL is set, otherwise mock."""
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
            resp = c.post(
                f"{ollama_url.rstrip('/')}/api/generate",
                json={"model": use_model, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            data = resp.json()
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
def process_one(cfg: Config, coord: Coordinator, state: dict, log: logging.Logger) -> bool:
    job = coord.next_job()
    if not job:
        return False

    job_id = job.get("job_id")
    prompt = job.get("prompt", "")
    log.info("job %s started", job_id)
    coord.claim(job_id)
    coord.heartbeat("busy")

    started = time.time()
    try:
        result = run_inference(prompt, cfg.model or job.get("model"), log)
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
    return True


def print_earnings(state: dict, log: logging.Logger) -> None:
    log.info(
        "Jobs completed: %d | Earnings: $%.6f",
        int(state.get("jobs", 0)),
        float(state.get("earnings_usd", 0.0)),
    )


def main_loop(cfg: Config, coord: Coordinator, state: dict, log: logging.Logger, once: bool) -> None:
    last_heartbeat = 0.0
    last_earnings_print = time.time()

    while True:
        now = time.time()
        if now - last_heartbeat > 5:
            coord.heartbeat("idle")
            last_heartbeat = now

        if now - last_earnings_print > cfg.earnings_print_seconds:
            print_earnings(state, log)
            last_earnings_print = now

        idle, reason = is_system_idle(cfg)
        if not idle:
            coord.heartbeat("offline")
            last_heartbeat = time.time()
            time.sleep(cfg.polling_interval)
            continue

        did_work = process_one(cfg, coord, state, log)
        if once and did_work:
            return
        if not did_work:
            time.sleep(cfg.polling_interval)


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


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = Config.load(args.config if args.config.exists() else None)
    state = load_state()
    worker_id = resolve_worker_id(cfg.worker_id, state)
    log = setup_logging(args.background)

    if args.status:
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

    log.info("agent starting on %s — worker_id=%s", platform.platform(), worker_id)
    log.info("coordinator=%s polling=%ss idle threshold=%ss cpu<%s%% auth=%s",
             cfg.coordinator_url, cfg.polling_interval,
             cfg.min_input_idle_seconds, cfg.max_cpu_percent,
             "on" if cfg.api_token else "off")

    coord = Coordinator(cfg.coordinator_url, worker_id, log, cfg.api_token)
    if not coord.register():
        return 1

    try:
        main_loop(cfg, coord, state, log, once=args.once)
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        try:
            coord.heartbeat("offline")
        except Exception:
            pass
        print_earnings(state, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
