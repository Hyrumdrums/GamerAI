"""Idle detection — windows-agent/agent.py god-file split. Pure
cut/paste: every function body is unchanged from agent.py, only its
location moved. See windows-agent/agent.py for the entry point that
imports these names back.

This is the first section split out, deliberately — it's the cheapest
possible verification that a PyInstaller --onefile build still traces
a plain `from gamerai_agent.idle import ...` correctly before cutting
up the rest of the file into gamerai_agent/.
"""
import platform
import subprocess
from typing import Optional

import psutil

IS_WINDOWS = platform.system() == "Windows"

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


# CREATE_NO_WINDOW so the per-poll nvidia-smi probe doesn't flash a
# console window on a tray-mode (hidden-console) agent. 0 on non-Windows.
_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0  # type: ignore[attr-defined]


def gpu_busy_percent(timeout: float = 2.0) -> Optional[float]:
    """Max GPU utilization across NVIDIA GPUs as a percent, via
    nvidia-smi. Returns None when nvidia-smi is absent or errors
    (AMD / integrated / dev box) — callers treat None as "unknown,
    don't block" and lean on the game-process gate instead.

    Utilization (not free VRAM) is the signal on purpose: a game pegs
    the GPU to ~100%, while our own Ollama model sitting resident
    between jobs (keep-alive) reports ~0% util even though it still
    holds VRAM. Gating on free VRAM would false-positive on our own
    warm model and break the back-to-back chat streak; gating on
    utilization does not."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    vals: list[float] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            vals.append(float(line))
        except ValueError:
            continue
    return max(vals) if vals else None


def gpu_hardware_info(timeout: float = 3.0) -> tuple[Optional[str], Optional[float]]:
    """Best-effort GPU model name + VRAM (GB) for the /register
    capabilities payload. Informational only — unlike gpu_busy_percent,
    nothing gates on this, so a wrong or missing answer just means the
    dashboard shows "—" instead of a hardware label.

    Two-tier lookup, cheapest/most-accurate first:

    1. ``nvidia-smi --query-gpu=name,memory.total`` — this fleet's
       chat/image backends prefer CUDA, so most contributor boxes have
       it. Reports real VRAM in MiB.
    2. WMI, via ``Get-CimInstance Win32_VideoController``, name only —
       covers AMD/Intel boxes (the vulkan sd.cpp backend) where
       nvidia-smi is absent. VRAM is deliberately NOT read from this
       path: WMI's AdapterRAM is a 32-bit field that silently
       wraps/truncates around 4 GB on modern cards, so a WMI-only VRAM
       number would frequently just be wrong. Better to report nothing
       than a number we know can lie.

    Multi-GPU boxes: only the first-listed adapter is reported (KISS —
    almost every contributor rig is single-GPU, and this is a display
    label, not a scheduling input)."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW_FLAGS,
        )
        if out.returncode == 0 and out.stdout.strip():
            first_line = out.stdout.strip().splitlines()[0]
            name_part, _, mib_part = first_line.partition(",")
            name = name_part.strip()
            if name:
                vram_gb: Optional[float] = None
                try:
                    vram_gb = round(float(mib_part.strip()) / 1024.0, 1)
                except ValueError:
                    pass
                return name, vram_gb
    except (OSError, subprocess.SubprocessError):
        pass

    if not IS_WINDOWS:
        return None, None
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_VideoController | "
                "Where-Object { $_.Name -notmatch 'Basic|Remote' } | "
                "Select-Object -First 1 -ExpandProperty Name)",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW_FLAGS,
        )
        name = out.stdout.strip()
        if out.returncode == 0 and name:
            return name, None
    except (OSError, subprocess.SubprocessError):
        pass
    return None, None


def active_game_process(process_names: list[str]) -> Optional[str]:
    """Name of the first running process matching ``process_names``
    (case-insensitive), else None. Used to detect an active game / VR
    session that GetLastInputInfo can't see — VR controllers never
    register as keyboard/mouse input, so a player standing still reads
    as idle to the input gate. Matching on the SteamVR/Oculus runtime
    processes (which run for the whole session) is the reliable
    cross-vendor signal."""
    if not process_names:
        return None
    wanted = {n.lower() for n in process_names}
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in wanted:
                return name
    except (psutil.Error, OSError):
        return None
    return None
