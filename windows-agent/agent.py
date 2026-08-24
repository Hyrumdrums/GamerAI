"""GamerAI Windows agent.

Runs in the background on a gamer's machine, picks up inference jobs from
the coordinator only when the system is idle, and tracks per-job earnings.

Usage:
    python agent.py                      # foreground, prints to console
    python agent.py --tray               # tray mode: hidden console + system tray icon
    python agent.py --background         # deprecated alias for --tray
    python agent.py --config config.json
    python agent.py --once               # process at most one job, then exit
    python agent.py --status             # print local job stats and exit

This file is single-file by design so it can be packaged with:
    pyinstaller --onefile --name agent agent.py
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
import logging.handlers
import os
import platform
import queue
import random
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
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
AGENT_VERSION = "1.4.1"

# Base64-encoded Ed25519 PUBLIC key used to verify self-update payloads.
# When this is non-empty, the self-updater REQUIRES a valid signature on
# the downloaded agent.exe (fail-closed: no signature, a bad signature,
# or a missing crypto lib all abort the update) — this is what stops a
# compromised download server from pushing arbitrary code to the fleet.
# The matching private seed is held only as the CI secret
# AGENT_SIGNING_KEY and never touches the VPS, so owning the download
# host is no longer enough to forge an update.
#
# Left empty by default so an un-provisioned deploy keeps updating via
# the SHA-256 sidecar (integrity, not authenticity). To turn signing on:
#   1. python tools/gen_agent_signing_key.py
#   2. paste the printed public key here
#   3. add the printed private seed as the repo secret AGENT_SIGNING_KEY
#   4. commit — CI then publishes agent.exe.sig and refuses to build if
#      this key is set but the secret is missing.
# See docs/OPERATOR.md "Agent update signing".
UPDATE_PUBLIC_KEY = ""

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
# Tray mode (Windows-only)
# ---------------------------------------------------------------------------
# Hidden-console + system tray icon for autostart users (v1.1.12). The
# console window is preserved so its scrollback retains the full session
# log, but it's hidden until the user clicks "Show console" in the tray
# menu. Single-instance + inter-process signalling binds 127.0.0.1:48591
# (the Spotify/Discord pattern) — works across conhost/Windows Terminal
# hosts and gives us a free IPC channel for future CLI subcommands.
#
# See windows-agent/README_addendum.md for the user-facing UX notes.
SW_HIDE = 0
SW_RESTORE = 9

SINGLE_INSTANCE_PORT = 48591   # localhost-only dynamic-range port
IPC_HANDSHAKE = "OK GAMERAI"   # second-instance sanity check (port collision)


def _set_aumid() -> None:
    """Set an explicit AppUserModelID so the (unhidden) console window groups
    under the same taskbar identity as our toast notifications. No-op on
    older Windows or non-Windows."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "com.gamerai.agent"
        )
    except Exception:
        pass


def _get_console_hwnd() -> int:
    if not IS_WINDOWS:
        return 0
    try:
        return int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    except Exception:
        return 0


def _hide_console(hwnd: int) -> None:
    if not (IS_WINDOWS and hwnd):
        return
    try:
        ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass


def _print_welcome_banner() -> None:
    """First-launch banner shown in the visible console after an
    auto-update (or any version transition). Without it, the post-
    update tray flash + 10 s pause feels like the agent broke — the
    Windows test pass surfaced this UX gap. Pairs with
    _schedule_delayed_hide so the user has time to read it."""
    try:
        build = current_version()
        banner = (
            "\n"
            "============================================================\n"
            "============================================================\n"
            "\n"
            f"     Welcome to GamerAI Agent v{AGENT_VERSION}!\n"
            f"     Build {build}\n"
            "\n"
            "     This console will hide in 10 seconds.\n"
            "     Right-click the tray icon to show it again any time.\n"
            "\n"
            "============================================================\n"
            "============================================================\n"
            "\n"
        )
        sys.stdout.write(banner)
        sys.stdout.flush()
    except Exception:
        pass


def _schedule_delayed_hide(hwnd: int, delay_seconds: float = 10.0) -> None:
    """Hide the console after ``delay_seconds`` on a daemon thread.
    Used in the welcome-banner path so the user gets a full read of
    the banner before the console disappears."""
    if not (IS_WINDOWS and hwnd):
        return

    def _hide_later():
        time.sleep(delay_seconds)
        try:
            ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
        except Exception:
            pass
    threading.Thread(
        target=_hide_later, name="gamerai-welcome-hide", daemon=True,
    ).start()


def _schedule_post_pair_hide(hwnd: int, log: "logging.Logger | None" = None) -> None:
    """First-run-after-pairing UX: print "press any key to hide" and
    block (on a daemon thread) until the user acknowledges, then hide
    the console. The agent is fully running in the background while
    we wait — heartbeat, job polling, everything — so the user is
    looking at a live, reassuring console while they decide they're
    done watching it.

    Falls back to the welcome-banner-style fixed 30 s delay if msvcrt
    isn't available (non-Windows dev runs) or if stdin isn't a TTY
    (output redirected). Either way the console eventually disappears.

    Distinct from _schedule_delayed_hide (post-update welcome banner,
    fixed 10 s) — this one is interactive and only fires when the user
    just paired."""
    if not (IS_WINDOWS and hwnd):
        return

    def _wait_then_hide():
        # Give /register + first heartbeat ~3 s to land so the log
        # lines settle before we print the prompt — otherwise the
        # "press any key" line scrolls off the top before the user
        # reads it.
        time.sleep(3.0)
        try:
            sys.stdout.write(
                "\n"
                "------------------------------------------------------------\n"
                "  Pairing complete. Agent is online and serving the network.\n"
                "  Press any key to send this console to the tray.\n"
                "  (Right-click the tray icon to bring it back any time.)\n"
                "------------------------------------------------------------\n"
            )
            sys.stdout.flush()
        except Exception:
            pass
        try:
            import msvcrt  # type: ignore[import-not-found]
            # Block until a keypress. msvcrt.getch is a blocking read
            # against the real console buffer, so a hidden console
            # won't satisfy it — the user has to actually focus this
            # window and press a key.
            msvcrt.getch()
        except Exception:
            # No msvcrt (dev run on non-Windows) or stdin yanked from
            # under us — fall back to a longer fixed delay so the
            # console doesn't linger forever.
            time.sleep(30.0)
        try:
            ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
            if log is not None:
                log.info("console hidden after first-run pair confirmation")
        except Exception:
            pass

    threading.Thread(
        target=_wait_then_hide,
        name="gamerai-post-pair-hide",
        daemon=True,
    ).start()


def _persist_console_hidden(hwnd: int, duration_seconds: float = 2.0) -> None:
    """Background-thread re-hide loop. PyInstaller's --onefile bootstrap
    creates the conhost window asynchronously, so a single ShowWindow(
    SW_HIDE) at main() entry sometimes runs before the window is fully
    realized — by which point our hide is a no-op and the console then
    pops visible. Poll IsWindowVisible for ``duration_seconds`` and
    re-hide on any flicker. After that window the user's explicit
    Show/Hide actions via the tray menu take over (this thread exits)."""
    if not (IS_WINDOWS and hwnd):
        return

    def _loop():
        deadline = time.monotonic() + duration_seconds
        while time.monotonic() < deadline:
            try:
                if ctypes.windll.user32.IsWindowVisible(hwnd):
                    ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
            except Exception:
                pass
            time.sleep(0.05)
    threading.Thread(
        target=_loop, name="gamerai-console-hide", daemon=True,
    ).start()


def _show_console(hwnd: int) -> None:
    if not (IS_WINDOWS and hwnd):
        return
    try:
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _set_console_title(title: str) -> None:
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def _tray_icon_path() -> Optional[Path]:
    """Locate tray.ico — bundled in MEIPASS for frozen builds, next to
    agent.py during dev. Returns None if missing; callers substitute a
    blank coloured square."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "tray.ico")
    candidates.append(Path(__file__).parent / "tray.ico")
    for p in candidates:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def _claim_single_instance() -> tuple[Optional["socket.socket"], bool]:
    """Bind 127.0.0.1:48591 as the first instance, or signal the running one.

    Returns (listening_socket, is_first_instance):
      (socket, True)   first instance; caller runs the IPC accept loop.
      (None,  False)   second instance, delegated to running agent;
                       caller should exit immediately.
      (None,  True)    port collision (not us); proceed without
                       single-instance enforcement rather than block start.
    """
    if not IS_WINDOWS:
        return (None, True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(4)
        _boot_log(f"single-instance: bind ok on :{SINGLE_INSTANCE_PORT}")
        return (sock, True)
    except OSError as exc:
        _boot_log(f"single-instance: bind FAILED ({exc!r}) — probing existing listener")
        try:
            sock.close()
        except OSError:
            pass
    # Port held by someone — verify it's actually another GamerAI agent
    # via the handshake before treating ourselves as the second instance.
    try:
        with socket.create_connection(
            ("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2.0
        ) as c:
            c.sendall(b"SHOW\n")
            c.settimeout(2.0)
            reply = c.recv(64).decode(errors="ignore").strip()
        if reply.startswith(IPC_HANDSHAKE):
            _boot_log(f"single-instance: handoff ok (reply={reply!r})")
            return (None, False)
        _boot_log(f"single-instance: handshake mismatch (reply={reply!r}) — port collision, proceeding without enforcement")
    except OSError as exc:
        _boot_log(f"single-instance: probe FAILED ({exc!r}) — port collision or stale state, proceeding without enforcement")
    # Some unrelated process owns 48591. Surrender the single-instance
    # check rather than refuse to start — a stray duplicate is a smaller
    # failure mode than an agent that never comes online.
    return (None, True)


def _ipc_serve(server_sock, on_show, log: logging.Logger) -> None:
    """Daemon-thread accept loop. Replies with the handshake so a
    second instance can verify it's talking to us, then dispatches."""
    while True:
        try:
            conn, _addr = server_sock.accept()
        except OSError:
            return  # listening socket closed → process exiting
        try:
            conn.settimeout(2.0)
            with conn:
                data = conn.recv(64).decode(errors="ignore").strip()
                reply = f"{IPC_HANDSHAKE} v{AGENT_VERSION}\n".encode()
                try:
                    conn.sendall(reply)
                except OSError:
                    pass
                if data == "SHOW":
                    try:
                        on_show()
                    except Exception as exc:
                        log.warning("ipc SHOW handler failed: %s", exc)
                # Add STATUS / VERSION / QUIT here when future CLI
                # subcommands need to query the running instance.
        except Exception as exc:
            log.debug("ipc accept loop error: %s", exc)


def _boot_log(message: str) -> None:
    """Append a timestamped line to %LOCALAPPDATA%\\GamerAI\\agent-boot.log.

    Used for the earliest possible diagnostic trail — BEFORE
    setup_logging runs, BEFORE the single-instance check returns. If
    a new agent process exits silently (bootstrap-stage crash, early
    return from the single-instance check, etc.) the rotating
    agent.log shows nothing because the logger never came up. This
    file is the breadcrumb that proves Python actually entered main().

    First-line entries are always written; subsequent lines per boot
    are append-only. Best-effort: silent no-op if the local-state dir
    isn't writable, because boot diagnostics must never block startup.
    """
    try:
        from datetime import datetime
        path = local_state_dir() / "agent-boot.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                f"pid={os.getpid()} {message}\n"
            )
    except Exception:
        pass


def _toast(title: str, msg: str, *, icon_path: Optional[Path] = None) -> None:
    """Fire-and-forget Windows toast. Silent no-op on non-Windows or if
    winotify is missing."""
    if not IS_WINDOWS:
        return
    try:
        from winotify import Notification, audio
    except Exception:
        return
    try:
        n = Notification(
            app_id="GamerAI Agent",
            title=title,
            msg=msg,
            icon=str(icon_path) if icon_path else "",
        )
        n.set_audio(audio.Default, loop=False)
        n.show()
    except Exception:
        pass


# CTRL_* constants for SetConsoleCtrlHandler. CTRL_C_EVENT (Ctrl+C) and
# CTRL_BREAK_EVENT are already covered by Python's KeyboardInterrupt
# path; we install our handler primarily for CTRL_CLOSE_EVENT (user
# clicked the console's X button or ran `taskkill /PID <pid>` without
# /F) plus CTRL_LOGOFF_EVENT and CTRL_SHUTDOWN_EVENT. Windows gives the
# handler up to ~5 s of runway before forced termination, so the
# shutdown_now callable must complete quickly.
_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1
_CTRL_CLOSE_EVENT = 2
_CTRL_LOGOFF_EVENT = 5
_CTRL_SHUTDOWN_EVENT = 6

# Module-level reference so the WINFUNCTYPE-wrapped callback isn't
# garbage-collected while Windows still holds the function pointer.
_console_ctrl_handler_ref = None  # type: ignore[var-annotated]


def _install_console_close_handler(shutdown_now) -> None:
    """Trap console-close / logoff / shutdown so the graceful-shutdown
    sequence (heartbeat offline + earnings) runs inside Windows' ~5 s
    grace window. Without this, the user's main_loop poll can still be
    asleep in time.sleep(polling_interval) when CTRL_CLOSE fires, and
    Windows force-terminates the process before main()'s finally block
    runs — leaving the worker registered as ``idle`` on the coordinator
    and the user's last earnings line never written to disk.

    ``shutdown_now`` is the same idempotent closure main()'s finally
    block calls. We invoke it synchronously from the OS handler thread
    so its work completes before Windows kills us.
    """
    global _console_ctrl_handler_ref
    if not IS_WINDOWS:
        return
    try:
        from ctypes import wintypes
        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _handler(ctrl_type):
            if ctrl_type in (
                _CTRL_C_EVENT, _CTRL_BREAK_EVENT, _CTRL_CLOSE_EVENT,
                _CTRL_LOGOFF_EVENT, _CTRL_SHUTDOWN_EVENT,
            ):
                try:
                    shutdown_now()
                except Exception:
                    pass
                return True
            return False

        cb = HANDLER(_handler)
        _console_ctrl_handler_ref = cb  # keep alive
        ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True)
    except Exception:
        pass


def _disable_console_quickedit() -> None:
    """Clear conhost's QuickEdit + Mouse Input flags so a contributor
    accidentally clicking inside the agent's console window doesn't
    park the entire process.

    Windows' default cmd.exe / conhost.exe enables QuickEdit, which
    means a single click anywhere in the buffer puts the console into
    "select mode" and BLOCKS every stdout/stderr write the process
    attempts until the user presses Enter or right-clicks. Our main
    loop logs on every tick (status lines, job-claim, job-complete),
    and Python's blocking stdout means the next log call freezes the
    main thread mid-claim. Symptom from the field: agent appears
    idle, /jobs/next stops, jobs sit on the queue. Pressing Enter in
    the console "resumes" the agent because it deselects and drains
    the buffered writes.

    Surveyed alternatives before reaching for ctypes:
      - pywin32 (win32console.SetConsoleMode) — works, but pulls in
        ~10 MB of native modules for a single GetMode/SetMode pair.
      - colorama — only touches output mode, not input mode flags.
      - windows-curses — adds an ncurses runtime; overkill.
      Pure ctypes is the established Win32 pattern in this file
      (see _install_console_close_handler above) and adds zero deps.

    Best-effort: no-op on non-Windows, when run headless without a
    console attached (Windows Service), or on any ctypes failure.
    The worst case is the original bug, not a regression.
    """
    if not IS_WINDOWS:
        return
    try:
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        # SetConsoleMode flags. Clearing QuickEdit only takes effect
        # when ENABLE_EXTENDED_FLAGS is also set — Microsoft's docs
        # state this explicitly.
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_MOUSE_INPUT = 0x0010
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        # GetStdHandle returns INVALID_HANDLE_VALUE (-1) on no console.
        if handle == 0 or handle == ctypes.c_void_p(-1).value:
            return
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        new_mode = (
            (mode.value & ~ENABLE_QUICK_EDIT_MODE & ~ENABLE_MOUSE_INPUT)
            | ENABLE_EXTENDED_FLAGS
        )
        if new_mode == mode.value:
            return
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


def _start_tray(
    *,
    stop_event: "threading.Event",
    console_hwnd: int,
    log_path: Path,
    icon_path: Optional[Path],
    log: logging.Logger,
):
    """Spawn the pystray icon on a daemon thread. Returns the Icon
    instance (or None if pystray / Pillow failed to import) so callers
    can stop it cleanly during shutdown."""
    try:
        import pystray
        from PIL import Image
    except Exception as exc:
        log.warning("tray init failed (pystray/Pillow missing?): %s", exc)
        return None

    img = None
    if icon_path and icon_path.exists():
        try:
            img = Image.open(icon_path)
        except Exception as exc:
            log.warning("tray icon load failed (%s): %s", icon_path, exc)
    if img is None:
        img = Image.new("RGBA", (32, 32), (15, 118, 110, 255))

    def _on_show(_icon, _item):
        _show_console(console_hwnd)

    def _on_hide(_icon, _item):
        _hide_console(console_hwnd)

    def _on_log(_icon, _item):
        try:
            os.startfile(str(log_path))
        except Exception as exc:
            log.warning("open log file failed: %s", exc)

    def _on_exit(icon, _item):
        log.info("tray Exit requested")
        stop_event.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Show console", _on_show, default=True),
        pystray.MenuItem("Hide console", _on_hide),
        pystray.MenuItem("Open log file", _on_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            f"GamerAI Agent v{AGENT_VERSION}", None, enabled=False
        ),
        pystray.MenuItem("Exit", _on_exit),
    )
    icon = pystray.Icon("gamerai", img, "GamerAI Agent", menu)
    threading.Thread(
        target=icon.run, name="gamerai-tray", daemon=True
    ).start()
    return icon


# ---------------------------------------------------------------------------
# Self-update
# ---------------------------------------------------------------------------
# version.txt is written by CI at build time (commit SHA + ISO timestamp)
# and bundled into the exe via PyInstaller --add-data. For frozen
# builds we read STRICTLY from sys._MEIPASS — the bootloader-set
# extraction dir for this exact process. Previous versions also
# fell back to <exe_parent>/version.txt and <__file__>.parent/
# version.txt; both could pick up stale content from leftover
# %TEMP%\_MEI<random>\ dirs when PyInstaller's atexit cleanup
# failed (AV holding files at process exit). That stale read drove
# an auto-update loop in v1.1.9: current_version() returned an old
# sha that didn't match /download/version.txt, agent updated, new
# process spawned, repeated every 60s. The narrower read here is
# the load-bearing fix; the cleanup function below removes the
# accumulated stale dirs as belt-and-suspenders. See v1.1.10
# devlog entry for the full diagnosis.
def current_version() -> str:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            try:
                p = Path(meipass) / "version.txt"
                if p.exists():
                    return p.read_text(encoding="utf-8").strip() or "unknown"
            except OSError:
                pass
        # Frozen build with no readable MEIPASS/version.txt is a
        # catastrophic bootloader failure. Return a sentinel that
        # the updater_loop guard treats as "do not auto-update" so
        # a broken read can't drive an infinite update cycle.
        return "unknown"
    # Source / non-frozen path (dev): read alongside agent.py.
    try:
        p = Path(__file__).parent / "version.txt"
        if p.exists():
            return p.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        pass
    return "dev"


def cleanup_stale_meipass_dirs(log: logging.Logger) -> int:
    """Best-effort removal of leftover ``%TEMP%\\_MEI<random>`` dirs
    that PyInstaller's atexit cleanup failed to delete. Skips the
    current process's MEIPASS and any dir touched in the last hour
    (might be another agent process still using it). Returns the
    count of dirs removed for the log line.

    Belt-and-suspenders behind the strict ``current_version()``
    fix in v1.1.10. With current_version reading strictly from
    sys._MEIPASS, stale dirs no longer drive the auto-update loop
    — but they still waste disk and can confuse manual diagnosis,
    so we sweep them on startup."""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return 0
    tmp = os.getenv("TEMP") or os.getenv("TMP")
    if not tmp:
        return 0
    current = getattr(sys, "_MEIPASS", None)
    try:
        current_resolved = (
            str(Path(current).resolve()) if current else None
        )
    except OSError:
        current_resolved = None
    cutoff = time.time() - 3600  # one hour
    removed = 0
    try:
        for entry in Path(tmp).iterdir():
            if not entry.is_dir() or not entry.name.startswith("_MEI"):
                continue
            try:
                if str(entry.resolve()) == current_resolved:
                    continue
                if entry.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            try:
                import shutil
                shutil.rmtree(entry, ignore_errors=True)
                if not entry.exists():
                    removed += 1
            except Exception:
                pass
    except OSError:
        pass
    if removed:
        # Escape the literal %TEMP% — logging treats the format string
        # as printf-style and tries to consume %T as a specifier
        # otherwise, raising TypeError on every startup that finds
        # stale dirs to sweep.
        log.info(
            "cleaned up %d stale _MEI dir(s) in %%TEMP%%", removed,
        )
    return removed


def _check_previous_update_failure(log: logging.Logger) -> None:
    """If update.bat's :move_failed branch ran on the previous cycle,
    it dropped a marker at %LOCALAPPDATA%\\GamerAI\\update-failed.txt.
    Log each line as WARN so the regression is visible (instead of
    the silent rollback the v1.0.x retry loop produced), then delete
    the marker so it's a one-time signal per failure event.

    Also checks the legacy %APPDATA%\\GamerAI\\update-failed.txt path
    used by v1.1.1 (before the path-convention audit moved markers to
    local-only state). A user upgrading from v1.1.1 with a pending
    marker still gets the warning."""
    candidates = [
        local_state_dir() / "update-failed.txt",
        state_dir() / "update-failed.txt",  # legacy v1.1.1 location
    ]
    for marker in candidates:
        if not marker.exists():
            continue
        try:
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as e:
            log.warning("could not read update-failed marker at %s: %s", marker, e)
            continue
        for line in text.splitlines() or [text]:
            if line.strip():
                log.warning(
                    "previous auto-update failed (probably AV interference). "
                    "Marker: %s", line.strip()[:400],
                )
        try:
            marker.unlink()
        except OSError:
            pass


def _check_stale_install_dir(log: logging.Logger) -> None:
    """One-line advisory when an agent.exe sits at the standard
    Inno-installer path but is not the binary we're running from.
    Catches the 'two copies on disk, only one updates' confusion the
    Avast incident exposed: auto-update only swaps the exe at
    sys.executable, so a stale install-dir copy can mask test
    results during diagnosis."""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return
    local_appdata = os.getenv("LOCALAPPDATA")
    if not local_appdata:
        return
    candidate = Path(local_appdata) / "Programs" / "GamerAI Agent" / "agent.exe"
    try:
        here = Path(sys.executable).resolve()
        if candidate.exists() and candidate.resolve() != here:
            log.warning(
                "stale agent.exe at %s does not match running copy %s — "
                "auto-update only swaps the running path. Consider "
                "deleting the stale copy.",
                candidate, here,
            )
    except OSError:
        pass


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


def _ps_relaunch_line(target: Path, args: list[str]) -> str:
    """Build the PowerShell line that a post-exit batch invokes to launch
    a frozen agent.exe. Shared by the self-update swap (_write_update_batch)
    and the in-place restart (_relaunch_in_place) so both spawn the new
    process the exact same proven way.

    v1.1.15+ uses WMI's Win32_Process.Create rather than Start-Process.
    Why: the v1.1.13 -> v1.1.14 test exposed a silent-relaunch failure
    where the new agent.exe was spawned (Start-Process returned
    errorlevel=0) but its PyInstaller bootloader exited before extracting
    (no _MEI orphan, no event log entry, no agent-boot.log line). Manual
    launches of the SAME exe immediately afterward worked. The strongest
    theory is that the chain `subprocess.Popen(cmd, DETACHED_PROCESS) ->
    cmd.exe -> powershell.exe -> Start-Process -> agent.exe` keeps the new
    agent.exe inside the parent PyInstaller worker's job object — and when
    the bat finishes and the job's last handle goes away, the new
    bootstrap gets terminated as a side-effect.

    Win32_Process.Create routes the spawn through WmiPrvSE (svchost), so
    the new agent.exe's parent is the WMI service. Complete escape from
    any inherited job, console state, or env-var chain (WMI gives the
    spawned process its own clean env, so the _MEIPASS2 inheritance fix
    from v1.1.13 is now redundant — left as belt-and-suspenders for anyone
    bypassing WMI in a custom build).

    Encoding via -EncodedCommand sidesteps the cmd-quote / PowerShell-quote
    escaping nightmare. The base64-encoded UTF-16LE string is opaque to
    cmd, so paths with spaces, single quotes, etc. all pass through
    cleanly.
    """
    target_str = str(target).replace("'", "''")
    dir_str = str(target.parent).replace("'", "''")
    args_str = " ".join(args)
    # Build the new process's command line in PowerShell rather than baking
    # it into a single literal — keeps the embedded double-quotes around
    # the exe path out of the surrounding encoding chain.
    ps_inner = (
        f"$exe = '{target_str}'; "
        f"$dir = '{dir_str}'; "
        f"$cmd = '\"' + $exe + '\" {args_str}'.Trim(); "
        f"$r = Invoke-WmiMethod -Class Win32_Process -Name Create "
        f"-ArgumentList $cmd, $dir; "
        f"Write-Host ('wmi-spawn ReturnValue=' + $r.ReturnValue + "
        f"' ProcessId=' + $r.ProcessId)"
    )
    encoded = base64.b64encode(ps_inner.encode("utf-16-le")).decode("ascii")
    return (
        f'powershell -NoProfile -WindowStyle Hidden '
        f'-EncodedCommand {encoded}\r\n'
    )


def _write_update_batch(
    exe: Path, new_exe: Path, relaunch_args: Optional[list[str]] = None,
) -> Path:
    """Generate the post-exit batch that swaps the binary and relaunches.
    Lives next to the exe so it inherits the right working directory.

    ``relaunch_args`` is the argv list the swapped binary is started
    with. ``None`` → restart in --tray (the autostart default since
    v1.1.12). ``[]`` → restart with no args (foreground console, so a
    user who typed ``update`` keeps their visible console after the
    swap).

    Retry policy (v1.1.1):
      The original 2-attempt, 9-second window wasn't long enough to
      survive Avast's behavioral shield holding the .new file's lock
      for ~30s during first-execute scan. New loop is 6 attempts
      across 60s with exponential backoff (2/4/8/16/30s gaps), and
      on total failure leaves a marker file + the .bat itself in
      place so the next agent run can log the regression visibly
      instead of the user silently rolling back to the prior version.
    """
    bat = exe.with_name("update.bat")
    previous_exe = exe.with_suffix(exe.suffix + ".previous")
    if relaunch_args is None:
        relaunch_args = ["--tray"]
    # Relaunch uses PowerShell's Start-Process, NOT cmd's `start ""`.
    # Reason: cmd's `start` calls ShellExecuteEx, which silently no-ops
    # when the calling process has no console handle. update.bat is
    # launched via subprocess.DETACHED_PROCESS from agent.py, so its
    # cmd has no console — exactly the case ShellExecuteEx breaks on.
    # PowerShell's Start-Process uses CreateProcess directly, which
    # works regardless of console state. -WindowStyle Hidden suppresses
    # the PowerShell host's own window; agent.exe gets its default
    # console (matches the user's foreground experience). Pre-v1.1.8
    # this was the silent bug behind every "agent never relaunched
    # after update" failure on contributor machines. See
    # _ps_relaunch_line for the full WMI-spawn rationale.
    relaunch_line = _ps_relaunch_line(exe, relaunch_args)
    # taskkill matches the running exe's actual filename — required for
    # users who renamed their binary (e.g. "GamerAI-Agent (1).exe" from
    # a re-download). The previous hard-coded "agent.exe" missed those.
    exe_basename = exe.name
    # Quote-and-escape paths defensively; the directory may have spaces
    # (e.g. C:\Program Files\GamerAI Agent).
    #
    # The `exit /b 0` after :move_done is REQUIRED as a fall-through
    # guard, not a return-code signal — without it, a successful swap
    # walks straight into the :move_failed body and writes a spurious
    # failure marker. The :move_failed branch ends at the end of file,
    # so no exit there.
    # In-bat forensics: dump self + per-step timestamps + capture
    # PowerShell stdout/stderr to %LOCALAPPDATA%\GamerAI\update-bat-trace.log.
    # v1.1.11 addition -- needed because every prior diagnosis of an
    # auto-update failure has been speculative (the bat self-deletes on
    # success and we have no record of what actually ran). With the
    # trace file, the next time something breaks we'll have the exact
    # bat content + per-step timing + any PowerShell error output.
    # The trace is overwritten each run (`>`, not `>>`, on the first
    # write) so it always reflects the most recent attempt.
    relaunch_with_trace = relaunch_line.replace(
        "\r\n",
        ' 1>>"%TRACE%" 2>&1\r\n',
        1,
    )
    bat.write_text(
        "@echo off\r\n"
        ":: Auto-generated by agent.exe self-update.\r\n"
        'title GamerAI Updating\r\n'
        ":: ---- forensic trace setup (v1.1.11+) ----\r\n"
        'if not exist "%LOCALAPPDATA%\\GamerAI" mkdir '
        '"%LOCALAPPDATA%\\GamerAI" >nul 2>nul\r\n'
        'set TRACE=%LOCALAPPDATA%\\GamerAI\\update-bat-trace.log\r\n'
        'echo === %DATE% %TIME% update.bat starting === > "%TRACE%"\r\n'
        'echo bat path: %~f0 >> "%TRACE%"\r\n'
        'echo === bat content follows === >> "%TRACE%"\r\n'
        'type "%~f0" >> "%TRACE%"\r\n'
        'echo === bat content end === >> "%TRACE%"\r\n'
        ":: ---- user-visible status banner ----\r\n"
        "echo.\r\n"
        "echo  ============================================================\r\n"
        "echo    GamerAI Agent - Update in progress\r\n"
        "echo  ============================================================\r\n"
        "echo.\r\n"
        ":: Step 1: give the parent agent time to exit cleanly.\r\n"
        'echo %TIME% step 1: waiting for parent exit >> "%TRACE%"\r\n'
        "echo  Waiting for old agent to exit...\r\n"
        "timeout /t 3 /nobreak >nul\r\n"
        ":: Step 2: defensive taskkill against the running exe by name.\r\n"
        'echo %TIME% step 2: taskkill /F /IM "' + exe_basename + '" >> "%TRACE%"\r\n'
        "echo  Stopping old agent...\r\n"
        f'taskkill /F /IM "{exe_basename}" >>"%TRACE%" 2>&1\r\n'
        ":: 8s (was 2s pre-v1.1.14): taskkill /F doesn't wait for the\r\n"
        ":: kernel to release mapped sections, file handles, or the\r\n"
        ":: parent's job object. A new PyInstaller --onefile bootstrap\r\n"
        ":: launched too eagerly into that residual state has been\r\n"
        ":: observed to exit silently (no event log, no _MEI orphan).\r\n"
        ":: 8s is empirically generous; the cost is acceptable on an\r\n"
        ":: update path that runs at most every few hours.\r\n"
        "echo  Releasing system resources (8 seconds)...\r\n"
        "timeout /t 8 /nobreak >nul\r\n"
        ":: Step 3: snapshot the current exe as a manual rollback path.\r\n"
        ":: `copy` (not move) so a subsequent swap-failure leaves the\r\n"
        ":: original target intact. After a successful swap, the user\r\n"
        ":: can roll back by running agent.exe.previous directly --\r\n"
        ":: useful when a release ships a regression worse than what\r\n"
        ":: it replaced.\r\n"
        'echo %TIME% step 3: snapshot to .previous >> "%TRACE%"\r\n'
        "echo  Saving rollback snapshot...\r\n"
        f'copy /Y "{exe}" "{previous_exe}" >>"%TRACE%" 2>&1\r\n'
        ":: Step 4: swap with 6-attempt exponential backoff. Total\r\n"
        ":: budget ~60s -- long enough to survive Avast / Defender\r\n"
        ":: scanning the .new file on first execute.\r\n"
        "echo  Installing new agent...\r\n"
        "set attempts=0\r\n"
        ":try_move\r\n"
        "set /a attempts+=1\r\n"
        'echo %TIME% step 4: move attempt %attempts% >> "%TRACE%"\r\n'
        f'move /Y "{new_exe}" "{exe}" >>"%TRACE%" 2>&1\r\n'
        "if not errorlevel 1 goto move_done\r\n"
        "if %attempts% geq 6 goto move_failed\r\n"
        "set wait=30\r\n"
        "if %attempts%==1 set wait=2\r\n"
        "if %attempts%==2 set wait=4\r\n"
        "if %attempts%==3 set wait=8\r\n"
        "if %attempts%==4 set wait=16\r\n"
        'echo %TIME% step 4: backoff %wait%s before retry >> "%TRACE%"\r\n'
        "echo  Install attempt %attempts% blocked (likely AV scan); "
        "retrying in %wait%s...\r\n"
        "timeout /t %wait% /nobreak >nul\r\n"
        "goto try_move\r\n"
        "\r\n"
        ":move_done\r\n"
        'echo %TIME% step 5: relaunch (success path) >> "%TRACE%"\r\n'
        "echo  Restarting new version...\r\n"
        + relaunch_with_trace
        + 'echo %TIME% step 5 done errorlevel=%errorlevel% >> "%TRACE%"\r\n'
        + "echo  Done. The new GamerAI agent will appear in the tray.\r\n"
        + "echo.\r\n"
        + ":: Brief pause so the user can read the final message before\r\n"
        + ":: the window closes on bat exit.\r\n"
        + "timeout /t 2 /nobreak >nul\r\n"
        + 'echo %TIME% step 6: del self >> "%TRACE%"\r\n'
        + 'del "%~f0"\r\n'
        "exit /b 0\r\n"
        "\r\n"
        ":move_failed\r\n"
        ":: All swap attempts failed (probably AV lock). Drop a marker\r\n"
        ":: the next agent boot picks up + logs as WARN, leave\r\n"
        ":: update.bat in place for forensics, then relaunch the OLD\r\n"
        ":: exe so the user is not left with no running agent.\r\n"
        ":: Marker goes in %LOCALAPPDATA% (local-only state), NOT\r\n"
        ":: %APPDATA% (Roaming) -- update failures are inherently\r\n"
        ":: per-machine and should never sync across an AD profile.\r\n"
        'echo %TIME% step 5: relaunch (FAILED path - all moves rejected) >> "%TRACE%"\r\n'
        "echo  Update install failed (file lock); restarting the existing version.\r\n"
        "echo  Check %%LOCALAPPDATA%%\\GamerAI\\update-failed.txt for details.\r\n"
        'echo update-failed %DATE% %TIME% attempts=%attempts% '
        f'target="{exe}" staged="{new_exe}" '
        '>> "%LOCALAPPDATA%\\GamerAI\\update-failed.txt"\r\n'
        ":: Best-effort cleanup of the orphan rollback snapshot --\r\n"
        ":: target was never swapped, so .previous is now a duplicate.\r\n"
        f'del "{previous_exe}" >nul 2>nul\r\n'
        + relaunch_with_trace
        + 'echo %TIME% step 5 done errorlevel=%errorlevel% (failed-path) >> "%TRACE%"\r\n'
        + "timeout /t 3 /nobreak >nul\r\n",
        encoding="ascii",
    )
    return bat


def _verify_ed25519(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    """Return True iff *signature_b64* is a valid Ed25519 signature over
    *message* under *public_key_b64*. Fail-closed: any malformed input,
    missing crypto library, or verification failure returns False rather
    than raising, so a caller can treat False as "do not trust this
    payload." Pure function (no I/O) so it's unit-testable without the
    network or the filesystem."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except Exception:
        # cryptography not bundled — caller decides whether that's fatal
        # (it is, when a key is configured: we can't verify, so refuse).
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        pub.verify(base64.b64decode(signature_b64), message)
        return True
    except (InvalidSignature, ValueError, TypeError, Exception):
        return False


def _verify_update_authenticity(
    staged: Path, base_url: str, log: logging.Logger
) -> bool:
    """Authenticity gate for a freshly-downloaded agent.exe.

    When ``UPDATE_PUBLIC_KEY`` is configured this is MANDATORY and
    fail-closed: fetch ``agent.exe.sig`` (base64 Ed25519 signature over
    the raw exe bytes) and verify it against the embedded public key.
    Any failure — sidecar unreachable, malformed, wrong/forged signature,
    or crypto lib absent — returns False and the caller must abort the
    swap. This is the control that makes owning the download server
    insufficient to push code to the fleet.

    When ``UPDATE_PUBLIC_KEY`` is empty (signing not yet provisioned)
    this returns True after logging a warning, leaving the SHA-256
    sidecar as the only (integrity-but-not-authenticity) check — the
    pre-signing behavior."""
    if not UPDATE_PUBLIC_KEY:
        log.warning(
            "self-update: update signing is NOT configured "
            "(UPDATE_PUBLIC_KEY empty) — proceeding on SHA-256 integrity "
            "only. A compromised download host could serve malicious "
            "code. See docs/OPERATOR.md 'Agent update signing'.",
        )
        return True
    sig_url = f"{base_url.rstrip('/')}/download/agent.exe.sig"
    try:
        with httpx.Client(timeout=10.0) as c:
            resp = c.get(sig_url)
    except Exception as e:
        log.warning(
            "self-update: signature required but sidecar fetch failed "
            "(%s); aborting", e,
        )
        return False
    if resp.status_code != 200 or not (resp.text or "").strip():
        log.warning(
            "self-update: signature required but %s returned HTTP %d / "
            "empty; aborting", sig_url, resp.status_code,
        )
        return False
    signature_b64 = resp.text.strip()
    try:
        with open(staged, "rb") as f:
            payload = f.read()
    except OSError as e:
        log.warning("self-update: could not read staged file to verify: %s", e)
        return False
    if not _verify_ed25519(UPDATE_PUBLIC_KEY, payload, signature_b64):
        log.warning(
            "self-update: signature verification FAILED — staged binary "
            "is not signed by the trusted key; aborting (possible tampered "
            "or spoofed download host)",
        )
        return False
    log.info("self-update: Ed25519 signature verified")
    return True


def _apply_update(
    base_url: str,
    exe: Path,
    log: logging.Logger,
    keep_awake_active: bool,
    relaunch_args: Optional[list[str]] = None,
) -> bool:
    """Download the published agent.exe, stage it next to the running
    binary, write update.bat, fire it as a detached process, and exit.

    Returns True if the update kicked off (caller should exit); False
    means we stayed put (download failed, etc.) and life continues."""
    new_url = f"{base_url.rstrip('/')}/download/agent.exe"
    # Stage under %LOCALAPPDATA%\GamerAI\updates\, NOT next to the
    # running exe and NOT in %TEMP%. Reasoning:
    #   1. Next to the exe (the v1.1.0 behavior) was %USERPROFILE%\
    #      Downloads\... for users who run from there — AV products
    #      scan that dir aggressively, breaking the swap.
    #   2. %TEMP% (the v1.1.1 behavior) gets nuked by Disk Cleanup,
    #      Storage Sense, and many third-party "PC cleanup" tools —
    #      potentially mid-update. It's also sometimes on a different
    #      volume than the install dir, making `move` a slow copy.
    #   3. %LOCALAPPDATA%\<App>\ is the Windows-native location for
    #      local app state. AV products typically exclude it from
    #      real-time scanning (it's where AV products themselves
    #      stage their own updates). Same volume as the per-user
    #      install dir, so `move` is atomic.
    # PID + timestamp keeps multiple-agent-update scenarios from
    # colliding on the staging filename.
    staged = update_staging_dir() / f"agent-{os.getpid()}-{int(time.time())}.exe"
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

    # SHA256 verification — fetch the sidecar and compare. Defends
    # against three cases the PE-magic check misses:
    #   1. Truncated download (network blip between bytes 2 and EOF)
    #   2. AV that quarantined the file, then "restored" a stub
    #   3. A man-in-the-middle who can serve a valid-looking PE but
    #      can't forge the hash (the sidecar is served from the same
    #      TLS-protected origin, so this is a partial defense — the
    #      real fix here is Authenticode signing).
    # Sidecar format: `<hex>  agent.exe\n` (matches sha256sum output).
    # If the sidecar is unreachable (older build, mirror eviction),
    # we log + skip rather than fail — preserves forward compat with
    # CI builds that didn't publish a hash yet.
    sha_url = f"{base_url.rstrip('/')}/download/agent.exe.sha256"
    expected_hash: Optional[str] = None
    try:
        with httpx.Client(timeout=10.0) as c:
            resp = c.get(sha_url)
        if resp.status_code == 200:
            text = (resp.text or "").strip()
            if text:
                # First whitespace-separated token is the hex digest.
                expected_hash = text.split()[0].lower()
        else:
            log.info(
                "self-update: sha256 sidecar at %s returned HTTP %d; "
                "skipping verification", sha_url, resp.status_code,
            )
    except Exception as e:
        log.info(
            "self-update: could not fetch sha256 sidecar (%s); "
            "skipping verification", e,
        )
    if expected_hash:
        import hashlib
        h = hashlib.sha256()
        try:
            with open(staged, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        except OSError as e:
            log.warning("self-update: could not hash staged file: %s", e)
            staged.unlink(missing_ok=True)
            return False
        actual_hash = h.hexdigest().lower()
        if actual_hash != expected_hash:
            log.warning(
                "self-update: sha256 mismatch (expected %s, got %s); "
                "aborting -- staged file likely truncated or tampered",
                expected_hash, actual_hash,
            )
            staged.unlink(missing_ok=True)
            return False
        log.info("self-update: sha256 verified (%s)", actual_hash[:16])

    # Authenticity gate (Ed25519). When a public key is embedded this is
    # mandatory + fail-closed; otherwise it warns and falls through to
    # SHA-only. Runs after the SHA check so a truncated download is
    # rejected cheaply before we bother fetching the signature.
    if not _verify_update_authenticity(staged, base_url, log):
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    try:
        bat = _write_update_batch(exe, staged, relaunch_args=relaunch_args)
    except OSError as e:
        log.warning("self-update: could not write update.bat (%s); aborting", e)
        staged.unlink(missing_ok=True)
        return False

    log.info("self-update: launching update.bat (%s) — exiting agent", bat)
    try:
        creationflags = 0
        if IS_WINDOWS:
            # CREATE_NEW_CONSOLE (was DETACHED_PROCESS pre-v1.1.20): give
            # update.bat its own visible cmd window so its user-facing
            # echo messages ("Stopping old agent...", "Restarting new
            # version...") show during the ~10 s gap when the tray icon
            # is gone and nothing else would be on screen. Without this
            # window, the gap reads as a crash. CREATE_NEW_PROCESS_GROUP
            # still keeps the child surviving our exit.
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
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
    force_event: Optional[threading.Event] = None,
    relaunch_args: Optional[list[str]] = None,
) -> None:
    """Background thread: poll the published version, trigger an update
    when ours is stale. ``keep_awake_holder`` is a mutable container so
    we can read the boolean from the main thread without races (a single
    flag is plenty here — we never write it from the updater).

    ``force_event`` lets another thread (the stdin command reader)
    request an immediate update: when set, the loop fetches the
    published binary and applies it even if the version string matches,
    so a contributor can type ``update`` in the console window to pull
    a hotfix without restarting from scratch. ``relaunch_args`` is the
    argv list to pass to the swapped binary."""
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
    interval_seconds = cfg.update_check_interval_hours * 3600.0
    last_scheduled_check = 0.0  # 0 ⇒ run a check on the first tick
    # Tick cadence: short enough that a typed ``update`` lands within
    # a couple of seconds, cheap enough that the steady-state cost is
    # nothing (no network on idle ticks).
    tick_seconds = 2.0
    while not stop_event.is_set():
        forced = force_event is not None and force_event.is_set()
        if forced:
            force_event.clear()
            log.info("self-update: manual 'update' command received")
        now = time.time()
        scheduled_due = (now - last_scheduled_check) >= interval_seconds
        if forced or scheduled_due:
            last_scheduled_check = now
            latest = fetch_latest_version(cfg.coordinator_url)
            # Guard against an infinite update cycle: if current_version()
            # returned the "unknown" or "dev" sentinel (catastrophic
            # MEIPASS read failure or non-frozen build), any non-empty
            # latest will compare unequal and trigger updates forever.
            # Force-update via stdin still works since `forced` bypasses
            # the version compare entirely. v1.1.10 added this after
            # v1.1.9 looped on every cycle when current_version() was
            # picking up stale content from leftover _MEI dirs.
            if not forced and here in ("unknown", "dev"):
                log.warning(
                    "self-update: skipping scheduled check — local "
                    "version reads as %r (PyInstaller MEIPASS issue?). "
                    "Use the `update` stdin command to force-update.",
                    here,
                )
                if stop_event.wait(tick_seconds):
                    return
                continue
            should_apply = forced or (
                latest and latest != here and latest != ""
            )
            if should_apply:
                if latest and latest != here:
                    log.info(
                        "self-update: published version %s differs from running %s",
                        latest, here,
                    )
                # Heads-up toast so the user understands the
                # incoming ~10-15 s tray-icon gap during update.bat's
                # taskkill -> 8s wait -> swap -> relaunch sequence.
                # Fires for both scheduled and forced ('update'
                # stdin) update paths. Best-effort: silent no-op on
                # non-Windows or if winotify is missing.
                _build_str = (
                    latest.split()[0] if latest else "new build"
                )
                _toast(
                    "GamerAI updating",
                    f"Downloading build {_build_str} — agent will "
                    f"restart in ~15 seconds.",
                    icon_path=_tray_icon_path(),
                )
                exe = _agent_exe_path()
                if exe is None:
                    log.info("self-update: not a frozen exe — skipping (dev mode)")
                else:
                    fired = _apply_update(
                        cfg.coordinator_url, exe, log,
                        keep_awake_active=keep_awake_holder.get("active", False),
                        relaunch_args=relaunch_args,
                    )
                    if fired:
                        # Tell main thread it's time to die.
                        keep_awake_holder["exit_requested"] = True
                        return
        if stop_event.wait(tick_seconds):
            return


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULTS = {
    "coordinator_url": "http://localhost:8000",
    "polling_interval_seconds": 5,
    "earnings_print_minutes": 10,
    "idle": {
        # 300s, not 60s: GetLastInputInfo only sees keyboard/mouse, so
        # VR controller activity reads as "idle" — a long window keeps
        # the agent from claiming a job mid-session on a gaming rig.
        # Note this gate is bypassed entirely when stayalive is on.
        "min_input_idle_seconds": 300,
        "max_cpu_percent": 30,
        # Skip claiming a GPU job when NVIDIA utilization is at/above
        # this percent — a running game pegs the GPU, so loading a model
        # onto it would OOM the game (the VR-session crash that motivated
        # this). Best-effort: no-op when nvidia-smi is absent (AMD /
        # integrated / dev). Set <= 0 to disable. Does not gate the
        # CPU-only TTS loop.
        "max_gpu_percent": 25,
        "cpu_sample_seconds": 2,
        # When true, if the user becomes active between job-claim and
        # inference-start the agent calls /jobs/abandon and forfeits
        # any pending earnings. Off by default: the existing behavior
        # is to drain (finish the in-flight job, *then* go offline).
        "override_drain": False,
        # Process names whose presence means "a game/VR session is
        # live" — checked even under stayalive, since VR controller
        # input is invisible to GetLastInputInfo so a standing-still
        # player reads as idle. Defaults to the SteamVR / Oculus / WMR
        # runtime processes (they run the whole session); operators can
        # add specific flatscreen game exes. Empty list disables.
        "game_processes": [
            "vrserver.exe",
            "vrcompositor.exe",
            "vrmonitor.exe",
            "vrdashboard.exe",
            "OVRServer_x64.exe",
            "WMRHost.exe",
        ],
    },
    "power": {
        # When true and running in --tray mode, ask Windows not to
        # sleep while the agent is online. This is the "I've opted my
        # machine into the network" contract — autostart shortcuts
        # pass --tray, so this only fires for explicit autostart
        # contributors. Foreground / --once never touches power state.
        "keep_awake_while_online": True,
    },
    "update": {
        # Background self-update. The agent polls
        # {coordinator_url}/download/version.txt every
        # check_interval_hours; on a version mismatch it pulls a fresh
        # agent.exe, writes update.bat, fires it detached, and exits.
        # Active in both --tray (autostart contributors) and
        # foreground (devs / manual launches) modes since v1.1.7.
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
        # KISS uniform-model policy: every contributor runs the same
        # canonical model so any chat job behaves identically no matter
        # which worker claims it. ``Config.load`` overwrites whatever
        # this field holds with ``_CURRENT_CHAT_MODEL`` below — leaving
        # it as documentation in DEFAULTS so an operator browsing
        # config.json understands what's installed by default. To roll
        # out a new canonical (e.g. 3B → 7B), bump _CURRENT_CHAT_MODEL,
        # update the mirror via setup-mirror.sh, ship the new agent,
        # contributors self-update within ~6 h.
        "model": "llama3.2:3b",
        "ollama_url": "http://localhost:11434",
        "mirror_base_url": None,  # null = use coordinator_url
        # Image-generation bootstrap. Best-effort: failure leaves the
        # agent running with chat only. Downloads sd.exe +
        # stable-diffusion.dll + the default GGUF from
        # {mirror_base}/download/{sd.exe,stable-diffusion.dll} and
        # {mirror_base}/download/sd-models/<slug>.gguf. The mirror is
        # populated by infra/setup-image-mirror.sh.
        "image_enabled": True,
        "image_model": "dreamshaperXL-lightning",
        # TTS bootstrap (Phase 1 voice feature). Best-effort: failure
        # leaves the agent serving chat/image only. Downloads the
        # piper runtime zip + the default voice .onnx/.onnx.json from
        # the mirror (setup-tts-mirror.sh). CPU-only — does not
        # contend with the GPU loop, so a worker can serve TTS while
        # mid-chat (see voice-phase1 design memory for the dual-loop
        # rationale).
        "tts_enabled": True,
        "tts_model": "piper:en_us-libritts-high",
    },
    # Smart mode (multi-machine pipeline). Two GamerAI machines on the
    # same LAN pool their VRAM to serve a 14B-class model via
    # llama.cpp's RPC backend — one machine is the "head" (runs
    # llama-server, holds the GGUF, claims chat:smart jobs), the
    # other is a "backend" (runs rpc-server, lends its GPU to the
    # head, claims nothing itself). Opt-in and OFF by default: a
    # smart-enabled agent stops advertising the standard chat/image
    # tools because the pipeline shard owns its VRAM. See
    # docs/smart-mode.md for the two-machine setup walkthrough.
    "smart": {
        "enabled": False,
        "role": "head",  # "head" | "backend"
        "model": "qwen2.5:14b",
        # head only: rpc-server peers to join, e.g. ["192.168.1.42:50052"].
        "rpc_peers": [],
        # backend only: where rpc-server listens. 0.0.0.0 exposes it on
        # the LAN — llama.cpp's RPC protocol is unauthenticated, so
        # NEVER port-forward this. To pair peers across the internet,
        # use an overlay (Tailscale/WireGuard) and bind to the overlay
        # IP instead — see docs/smart-mode.md "Pairing over the
        # internet".
        "rpc_listen_host": "0.0.0.0",
        "rpc_listen_port": 50052,
        # llama.cpp release pin. The RPC protocol is only guaranteed
        # compatible between identical builds, so head and backend MUST
        # run the same release — the bootstrap keys the install dir by
        # this tag so a bump re-downloads on both machines.
        "llama_release": "b9610",
        # Override URLs (null = derive from llama_release / model).
        "llama_zip_url": None,
        "cudart_zip_url": None,
        "gguf_url": None,
        # head only: llama-server knobs. context_length is shared
        # across the whole pipeline's KV cache; 8192 fits the 6+8 GB
        # reference pair with the Q4_K_M 14B. tensor_split (e.g.
        # "5,8") overrides the default free-VRAM-proportional layer
        # split — order is [rpc_peers..., local GPU].
        "context_length": 8192,
        "tensor_split": None,
        "llama_server_port": 8092,
        # Escape hatch: extra raw args appended to the llama-server /
        # rpc-server command line.
        "extra_args": [],
        # Set to an OpenAI-compatible base URL (e.g.
        # "http://127.0.0.1:8092") to skip the managed download/launch
        # entirely and serve smart jobs against a server you run
        # yourself. Also the dev path on non-Windows.
        "endpoint": None,
        # head only: how long to wait for llama-server's /health to go
        # green after launch. First load streams ~9 GB of weights to
        # the backend over the LAN, so this is minutes, not seconds.
        "startup_timeout_seconds": 1200,
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
    max_gpu_percent: float
    game_processes: list[str]
    keep_awake_while_online: bool
    update_enabled: bool
    update_check_interval_hours: float
    bootstrap_enabled: bool
    bootstrap_model: str
    bootstrap_ollama_url: str
    bootstrap_mirror_base_url: Optional[str]
    bootstrap_image_enabled: bool
    bootstrap_image_model: str
    bootstrap_tts_enabled: bool
    bootstrap_tts_model: str
    model: Optional[str]
    worker_id: Optional[str]
    api_token: Optional[str]
    # stayalive: when True, is_system_idle() ignores user-input
    # idleness and only gates on CPU. Useful for dev sessions over
    # Chrome Remote Desktop (which generates continuous mouse-move
    # events) or any scenario where the operator wants the agent to
    # keep accepting jobs without satisfying the 60 s no-input
    # requirement. Set via the --stayalive CLI flag, persisted in
    # state.json (so it survives restarts and auto-updates), cleared
    # with --no-stayalive.
    stayalive: bool = False
    # Smart-mode (multi-machine pipeline) knobs — see DEFAULTS["smart"]
    # for the field-by-field documentation. Defaulted (rather than
    # required like the bootstrap_* fields) because smart mode is the
    # opt-in exception, and tests construct Config directly.
    smart_enabled: bool = False
    smart_role: str = "head"
    smart_model: str = "qwen2.5:14b"
    smart_rpc_peers: list[str] = field(default_factory=list)
    smart_rpc_listen_host: str = "0.0.0.0"
    smart_rpc_listen_port: int = 50052
    smart_llama_release: str = "b9610"
    smart_llama_zip_url: Optional[str] = None
    smart_cudart_zip_url: Optional[str] = None
    smart_gguf_url: Optional[str] = None
    smart_context_length: int = 8192
    smart_tensor_split: Optional[str] = None
    smart_llama_server_port: int = 8092
    smart_extra_args: list[str] = field(default_factory=list)
    smart_endpoint: Optional[str] = None
    smart_startup_timeout_seconds: float = 1200.0

    @classmethod
    def load(cls, path: Optional[Path]) -> "Config":
        # Deep copy: _deep_merge mutates nested dicts in place, and a
        # shallow dict(DEFAULTS) would let one load's user values leak
        # into module-level DEFAULTS (visible to any later load in the
        # same process — tests, and any future reload-on-the-fly).
        import copy
        data = copy.deepcopy(DEFAULTS)
        if path and path.exists():
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            _deep_merge(data, user)
            # One-shot migrations of the on-disk config. Currently:
            # the v1.1.x legacy default llama3.2:1b → 3.2:3b. Writes
            # back to disk so the migration runs at most once.
            if _migrate_legacy_chat_model(data):
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    print(
                        f"config migration: bootstrap.model normalized "
                        f"to '{_CURRENT_CHAT_MODEL}' (KISS uniform-model "
                        f"policy — every contributor runs the same model "
                        f"so chat jobs behave identically regardless of "
                        f"which worker claims them). The file at {path} "
                        f"has been rewritten; this message won't repeat.",
                        flush=True,
                    )
                except OSError:
                    # Best effort: agent still runs against the
                    # canonical in-memory value (Config.load forces it
                    # below) — the on-disk migration just retries on
                    # next startup.
                    pass
        # Operator overrides, layered LAST so they win. The frozen exe's
        # bundled config.json (merged above) lives in an ephemeral
        # _MEIPASS dir, so runtime edits — the `smart` console command —
        # are persisted to %APPDATA%\GamerAI\config.json instead and
        # merged here. Holds only the changed keys; the rest still comes
        # from the bundle. Skipped when it IS the primary path (a launcher
        # that points --config straight at it) to avoid a double-merge.
        override = operator_config_path()
        try:
            if override.exists() and (
                path is None or override.resolve() != path.resolve()
            ):
                with open(override, "r", encoding="utf-8") as f:
                    ovr = json.load(f)
                if isinstance(ovr, dict):
                    _deep_merge(data, ovr)
        except (OSError, ValueError):
            # Malformed/locked override must never block startup — fall
            # back to the bundled config.
            pass
        idle = data["idle"]
        power = data.get("power", DEFAULTS["power"])
        update = data.get("update", DEFAULTS["update"])
        bootstrap = data.get("bootstrap", DEFAULTS["bootstrap"])
        smart = data.get("smart", DEFAULTS["smart"])
        if not isinstance(smart, dict):
            smart = DEFAULTS["smart"]
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
            max_gpu_percent=float(idle.get("max_gpu_percent", 25)),
            game_processes=list(
                idle.get("game_processes", DEFAULTS["idle"]["game_processes"])
            ),
            keep_awake_while_online=bool(
                power.get("keep_awake_while_online", True)
            ),
            update_enabled=bool(update.get("enabled", True)),
            update_check_interval_hours=float(
                update.get("check_interval_hours", 6)
            ),
            bootstrap_enabled=bool(bootstrap.get("enabled", True)),
            # KISS uniform-model: ignore whatever the on-disk config
            # carries and always advertise the canonical model. A
            # contributor who hand-edited bootstrap.model to something
            # exotic (mistral, qwen, custom GGUF) would otherwise show
            # up as a worker advertising llama3.2:3b but actually
            # serving a different model, which the coordinator can't
            # distinguish — that asymmetry is exactly what we're
            # avoiding. _migrate_legacy_chat_model rewrites the on-disk
            # value too so the next config.json read is consistent.
            bootstrap_model=_CURRENT_CHAT_MODEL,
            bootstrap_ollama_url=str(
                bootstrap.get("ollama_url", "http://localhost:11434")
            ).rstrip("/"),
            bootstrap_mirror_base_url=(
                str(bootstrap["mirror_base_url"]).rstrip("/")
                if bootstrap.get("mirror_base_url")
                else None
            ),
            bootstrap_image_enabled=bool(bootstrap.get("image_enabled", False)),
            bootstrap_image_model=str(bootstrap.get("image_model", "dreamshaperXL-lightning")),
            bootstrap_tts_enabled=bool(bootstrap.get("tts_enabled", False)),
            bootstrap_tts_model=str(
                bootstrap.get("tts_model", "piper:en_us-libritts-high")
            ),
            smart_enabled=bool(smart.get("enabled", False)),
            smart_role=str(smart.get("role", "head")).strip().lower(),
            smart_model=str(smart.get("model", "qwen2.5:14b")),
            smart_rpc_peers=[
                str(p).strip() for p in (smart.get("rpc_peers") or []) if str(p).strip()
            ],
            smart_rpc_listen_host=str(smart.get("rpc_listen_host", "0.0.0.0")),
            smart_rpc_listen_port=int(smart.get("rpc_listen_port", 50052)),
            smart_llama_release=str(smart.get("llama_release", "b9610")),
            smart_llama_zip_url=(
                str(smart["llama_zip_url"]) if smart.get("llama_zip_url") else None
            ),
            smart_cudart_zip_url=(
                str(smart["cudart_zip_url"]) if smart.get("cudart_zip_url") else None
            ),
            smart_gguf_url=(
                str(smart["gguf_url"]) if smart.get("gguf_url") else None
            ),
            smart_context_length=int(smart.get("context_length", 8192)),
            smart_tensor_split=(
                str(smart["tensor_split"]) if smart.get("tensor_split") else None
            ),
            smart_llama_server_port=int(smart.get("llama_server_port", 8092)),
            smart_extra_args=[
                str(a) for a in (smart.get("extra_args") or [])
            ],
            smart_endpoint=(
                str(smart["endpoint"]).rstrip("/")
                if smart.get("endpoint") else None
            ),
            smart_startup_timeout_seconds=float(
                smart.get("startup_timeout_seconds", 1200)
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


# The single canonical chat model every contributor runs. ``Config.load``
# enforces this — bootstrap_model on the dataclass is always equal to
# this constant regardless of what config.json carries. To roll out a
# new canonical, bump the constant, re-seed the mirror via
# setup-mirror.sh, ship a new agent build, contributors self-update
# within ~6 h. _LEGACY_CHAT_MODEL is kept for the migration log message
# below — it's the most common old value we expect to see in the wild.
_LEGACY_CHAT_MODEL = "llama3.2:1b"
_CURRENT_CHAT_MODEL = "llama3.2:3b"


def _migrate_legacy_chat_model(data: dict) -> bool:
    """One-shot migration: if the loaded config has a bootstrap.model
    that doesn't match the canonical _CURRENT_CHAT_MODEL, rewrite it
    in-place and return True. The caller persists ``data`` back to
    disk so the next load sees the canonical value with no churn.

    Broader than the original 1B → 3B migration since the KISS
    uniform-model policy landed: any non-canonical value (custom
    GGUFs, mistral, qwen, …) is overwritten because Config.load
    ignores it at runtime anyway. Persisting the migration keeps the
    on-disk file honest so a contributor inspecting config.json sees
    the model that's actually being advertised."""
    bs = data.get("bootstrap")
    if not isinstance(bs, dict):
        return False
    if bs.get("model") != _CURRENT_CHAT_MODEL:
        bs["model"] = _CURRENT_CHAT_MODEL
        return True
    return False


# ---------------------------------------------------------------------------
# Local state — worker_id + cumulative earnings, persisted next to config
# ---------------------------------------------------------------------------
def state_dir() -> Path:
    """Per-user agent state: worker_id, api_token, earnings totals,
    logs. Currently lives in %APPDATA% (Roaming) on Windows for
    backwards compatibility with installs created before the
    %APPDATA% vs %LOCALAPPDATA% audit — moving it would orphan
    existing workers' tokens + earnings. New local-only paths
    (update staging, failure markers) go through ``local_state_dir``
    instead. See devlog 2026-05-21 for the convention discussion."""
    if IS_WINDOWS:
        base = os.getenv("APPDATA") or os.path.expanduser("~")
        d = Path(base) / "GamerAI"
    else:
        d = Path.home() / ".gamerai"
    d.mkdir(parents=True, exist_ok=True)
    return d


def operator_config_path() -> Path:
    """Persistent, user-writable config OVERRIDES — layered on top of the
    bundled config.json at load time (see Config.load).

    Why this exists: the shipped agent.exe is PyInstaller --onefile, so
    its bundled config.json is extracted to a throwaway _MEIPASS temp dir
    that's recreated every launch and deleted on exit. Anything written
    there evaporates on the next start. This file lives next to state.json
    in %APPDATA%\\GamerAI (the same persistent, always-writable location
    that already survives restarts and self-updates), so the `smart`
    console command's edits actually stick. It only needs to hold the
    keys the operator changed; everything else still comes from the
    bundled config. Computed WITHOUT mkdir so it's a side-effect-free
    read used safely from Config.load and --diagnose."""
    if IS_WINDOWS:
        base = os.getenv("APPDATA") or os.path.expanduser("~")
        return Path(base) / "GamerAI" / "config.json"
    return Path.home() / ".gamerai" / "config.json"


def local_state_dir() -> Path:
    """Local-machine-only agent state — never roams across machines.
    Used for things that are inherently per-machine: the update
    staging area (the .new exe and update.bat), the post-failure
    marker file, anti-virus-friendly caches.

    On Windows, resolves to %LOCALAPPDATA%\\GamerAI\\ — the right
    convention for local app state per Microsoft's app-data
    guidelines. On non-Windows we fall back to the same dir as
    ``state_dir`` since there's no Roaming/Local split."""
    if IS_WINDOWS:
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or os.path.expanduser("~")
        d = Path(base) / "GamerAI"
    else:
        d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def update_staging_dir() -> Path:
    """Where ``_apply_update`` drops the freshly-downloaded agent.exe
    before update.bat moves it into place. Lives under local state so
    Disk Cleanup / Storage Sense don't nuke it (which is the real
    failure mode of using %TEMP%). Same volume as %LOCALAPPDATA%\\
    Programs\\<App>\\, so the eventual ``move`` is atomic rather than
    falling back to copy+delete across volumes."""
    d = local_state_dir() / "updates"
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
        # Last tool this agent served. Used by _ordered_queues to put
        # the warm queue first on the next tick. None on first run.
        "last_tool": None,
    }


_STATE_LOCK = threading.Lock()


def save_state(state: dict) -> None:
    # The stdin command thread can mutate state (e.g., the `stayalive`
    # toggle) while the main loop also writes after each job. json.dumps
    # iterates the dict, so a concurrent write would raise
    # RuntimeError: dictionary changed size during iteration. Lock keeps
    # the serialize+replace pair atomic across threads.
    with _STATE_LOCK:
        _save_state_locked(state)


def _save_state_locked(state: dict) -> None:
    """Inner save — caller must already hold _STATE_LOCK. Used by
    credit_completed_job so the increment + persist pair stays atomic
    under the same lock acquisition."""
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def credit_completed_job(
    state: dict,
    *,
    last_tool: Optional[str],
    earnings: float,
    tokens: int = 0,
) -> None:
    """Atomically credit a completed job to the shared state dict +
    persist. Necessary because the dual-loop architecture (voice-phase1)
    has the GPU and CPU loops racing on the same counters — without the
    lock here, a concurrent jobs+=1 from each loop would lose one
    increment.

    ``last_tool`` is the warm-model preference used by _ordered_queues.
    Pass None to leave the previous value (rare — the TTS path on the
    CPU loop deliberately doesn't promote tts to the front of the
    GPU loop's queue order)."""
    with _STATE_LOCK:
        state["jobs"] = int(state.get("jobs", 0)) + 1
        state["earnings_usd"] = round(
            float(state.get("earnings_usd", 0.0)) + earnings, 10,
        )
        if tokens:
            state["tokens"] = int(state.get("tokens", 0)) + int(tokens)
        if last_tool is not None:
            state["last_tool"] = last_tool
        _save_state_locked(state)


def resolve_worker_id(cfg_worker_id: Optional[str], state: dict) -> str:
    """Stable per-install worker_id, generated once and persisted in
    state.json (regenerated only by wiping state — e.g. a reinstall).

    Deliberately opaque random, not win-<hostname>-<rand>: the
    community ToS promises we don't collect hostnames, and embedding
    one here was quietly breaking that promise every time the agent
    registered. Human-friendly machine identification now comes from
    display_name (see ensure_machine_name / ``/register``'s
    display_name field) instead of being smuggled into the id."""
    if cfg_worker_id:
        return cfg_worker_id
    if state.get("worker_id"):
        return state["worker_id"]
    new_id = f"win-{uuid.uuid4().hex[:20]}"
    state["worker_id"] = new_id
    save_state(state)
    return new_id


# Tiny local word lists for the random machine-name default — no network
# call, no external dependency, nothing that could identify the machine
# or its owner. Same idea as the "quiet-badger"-style names other dev
# tools (Docker containers, GitHub Codespaces) generate for the same
# reason: something a human can tell apart at a glance.
_MACHINE_NAME_ADJECTIVES = [
    "amber", "brave", "bright", "calm", "cedar", "cobalt", "crimson",
    "dusty", "faded", "gentle", "golden", "hidden", "humble", "jolly",
    "lively", "misty", "quick", "quiet", "silent", "steady", "sunny",
    "swift", "tiny", "vivid",
]
_MACHINE_NAME_NOUNS = [
    "badger", "beetle", "cricket", "dolphin", "falcon", "gecko",
    "hare", "heron", "ibis", "jackal", "kite", "lemur", "lynx",
    "marten", "moth", "newt", "orca", "otter", "puffin", "raven",
    "sparrow", "weasel", "wombat", "wren",
]


def random_machine_name() -> str:
    return f"{random.choice(_MACHINE_NAME_ADJECTIVES)}_{random.choice(_MACHINE_NAME_NOUNS)}"


_MACHINE_NAME_MAX_LEN = 48


def ensure_machine_name(state: dict, *, _prompt=None) -> str:
    """One-time (per install) prompt for a human-friendly machine name —
    what identifies this PC on the dashboard/Machines page now that
    worker_id is opaque random (see resolve_worker_id). Optional:
    Enter alone, EOF, or Ctrl+C all fall through to a random
    adjective_noun default rather than blocking setup on a cosmetic
    choice. Persisted in state.json so this only runs once; every
    later /register call just resends the stored value.

    ``_prompt`` is injectable for tests (defaults to real ``input()``)."""
    existing = state.get("machine_name")
    if existing:
        return existing
    prompt = _prompt or (lambda p: input(p).strip())
    sys.stdout.write(
        "\nName this PC (optional — makes it easier to identify your "
        "machine from the web console). Leave blank for a random name: "
    )
    sys.stdout.flush()
    try:
        chosen = prompt("")
    except (EOFError, KeyboardInterrupt):
        chosen = ""
    chosen = (chosen or "").strip()[:_MACHINE_NAME_MAX_LEN]
    name = chosen or random_machine_name()
    state["machine_name"] = name
    save_state(state)
    return name


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Async-logging plumbing. The agent logger emits LogRecords onto a
# bounded in-memory queue (non-blocking), and a single background
# thread drains the queue and runs the real handlers (file + stdout).
# This decouples the main job loop from anything that could ever block
# on a log write:
#   - Windows conhost pausing the stdout pipe (QuickEdit select mode,
#     Ctrl+S/XOFF flow control, Pause/Break, scrollbar drag, resize).
#     _disable_console_quickedit() removes the most common trigger
#     (mouse click), but conhost has other pause modes we can't all
#     disable individually — the queue makes the agent immune to
#     every one of them by construction.
#   - File handle rotation latency on a slow disk.
#   - Any future custom handler (network log shipping, syslog, etc.)
#     that turns out to have a slow path.
# Bounded queue (DROP_ON_FULL) is the right policy here: better to
# lose log lines during a long conhost pause than to silently freeze
# the worker. Stdlib queue.Full raised in enqueue() is swallowed so
# the failure doesn't bubble back into the main thread via
# QueueHandler's default handleError -> sys.stderr write path (which
# would itself block on the same paused pipe).
_LOG_QUEUE_CAPACITY = 10_000
_LOG_LISTENER: Optional["logging.handlers.QueueListener"] = None


class _DropOnFullQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that drops records when the queue is full instead
    of propagating queue.Full back through handleError. handleError's
    default writes to sys.stderr, which on Windows shares the same
    paused conhost pipe — re-introducing the very block we're trying
    to escape. Silent drop is the only safe behavior here."""

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass


def setup_logging(headless: bool = False) -> logging.Logger:
    """Install the file + (optionally) stdout log handlers behind a
    queue, so the main thread never blocks on a slow or paused sink.

    ``headless=True`` skips the StreamHandler entirely — meant for any
    future truly-console-less mode. Tray mode passes ``headless=False``
    because the hidden console buffers stdout into its scrollback, which
    is exactly what the user sees when they click "Show console".

    Idempotent: calling twice tears down the prior listener cleanly.
    """
    log = logging.getLogger("gamerai.agent")
    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    # Real handlers — these are what actually do the WriteFile / write()
    # calls that can block. The listener thread owns them.
    real_handlers: list[logging.Handler] = []

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir() / "agent.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    real_handlers.append(file_handler)

    if not headless:
        try:
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(fmt)
            real_handlers.append(stream)
        except Exception:
            pass

    # Stop any prior listener (defensive — setup_logging is called once
    # in practice, but a re-invocation would otherwise leak a thread).
    global _LOG_LISTENER
    if _LOG_LISTENER is not None:
        try:
            _LOG_LISTENER.stop()
        except Exception:
            pass
        _LOG_LISTENER = None

    log_q: "queue.Queue[Optional[logging.LogRecord]]" = queue.Queue(
        maxsize=_LOG_QUEUE_CAPACITY,
    )
    log.addHandler(_DropOnFullQueueHandler(log_q))

    _LOG_LISTENER = logging.handlers.QueueListener(
        log_q, *real_handlers, respect_handler_level=False,
    )
    _LOG_LISTENER.start()

    return log


def stop_log_listener() -> None:
    """Flush and stop the background log thread. Idempotent. Called
    from the shutdown path so any records still in the queue land in
    the file before the process exits."""
    global _LOG_LISTENER
    listener = _LOG_LISTENER
    if listener is None:
        return
    _LOG_LISTENER = None
    try:
        listener.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Coordinator client
# ---------------------------------------------------------------------------
# Background heartbeat cadence. The coordinator marks a worker offline
# when no heartbeat lands within WORKER_TIMEOUT_SECONDS (15s in prod),
# so 5s leaves a 3x safety margin and matches the legacy main-loop
# cadence one-for-one.
HEARTBEAT_INTERVAL_SECONDS = 5.0

# Fallback heartbeat cadence while a machine is outside its uptime window
# (or paused), used until the coordinator's /heartbeat response supplies
# its own ``downtime_poll_seconds``. Slow because a sleeping machine has
# nothing to report except "still here, what's my schedule now?" — the
# beat doubles as the schedule-change poll, so a window edit in the web
# UI is picked up within this interval.
DOWNTIME_POLL_DEFAULT_SECONDS = 300.0

# How long the main loop blocks inside /jobs/next per tick. The
# background heartbeat thread keeps the worker visible to the
# coordinator throughout this window, so we're not constrained by
# WORKER_TIMEOUT_SECONDS. 25s is a balance between (a) holding the
# coordinator's request thread for a reasonable bound and (b)
# letting the user-activity check fire often enough that "stop
# taking new jobs" still happens within ~25s of the user coming
# back. Capped server-side at MAX_LONGPOLL_SECONDS = 30s.
LONG_POLL_WAIT_SECONDS = 25.0


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
        # ----- background heartbeat state -----
        # The main thread updates current_status / current_job_id via
        # set_idle / set_offline / set_busy / clear_busy. A dedicated
        # daemon thread reads these every HEARTBEAT_INTERVAL_SECONDS
        # and POSTs /heartbeat, so a long inference call on the main
        # thread no longer suppresses heartbeats — the symptom that
        # made worker_offline flap during DreamShaper image jobs and
        # opened a window where the reaper could (used to) requeue
        # healthy in-flight work.
        self._hb_lock = threading.Lock()
        self._current_status = "idle"
        self._current_job_id: Optional[str] = None
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        # ----- uptime-schedule state (refreshed from /heartbeat) -----
        # The coordinator is the authoritative gate (it won't dispatch a
        # job to a machine outside its window), but the agent self-gates
        # too so it stops polling and drops to a slow heartbeat during
        # downtime. Defaults to "allowed" so a coordinator that doesn't
        # return schedule info, or a transient heartbeat failure, never
        # strands the agent offline.
        self._allowed_now = True
        self._sleeping_until: Optional[str] = None
        self._downtime_poll_seconds = float(DOWNTIME_POLL_DEFAULT_SECONDS)

    # ---------- low-level HTTP ----------
    def _post(self, path: str, body: dict, timeout: float = 10.0) -> Optional[dict]:
        try:
            resp = self.http.post(f"{self.base}{path}", json=body, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            self.log.warning("coordinator %s failed: %s", path, e)
            return None

    def _post_raw(self, path: str, body: dict, timeout: float = 10.0) -> tuple[int, Optional[dict]]:
        """Like ``_post`` but returns ``(status_code, body)`` so callers
        can distinguish 410 Gone (claim was reassigned or cancelled —
        do NOT credit local state) from transport failures (unknown,
        treat as transient). ``status_code == 0`` signals a transport
        error (no HTTP response received)."""
        try:
            resp = self.http.post(f"{self.base}{path}", json=body, timeout=timeout)
        except httpx.HTTPError as e:
            self.log.warning("coordinator %s transport failed: %s", path, e)
            return 0, None
        try:
            data = resp.json()
        except ValueError:
            data = None
        return resp.status_code, data

    def _get(self, path: str) -> Optional[dict]:
        try:
            resp = self.http.get(f"{self.base}{path}", timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            self.log.warning("coordinator GET %s failed: %s", path, e)
            return None

    # ---------- registration ----------
    def register(
        self,
        capabilities: Optional[dict] = None,
        display_name: Optional[str] = None,
    ) -> bool:
        body: dict = {"worker_id": self.worker_id}
        if capabilities:
            body["capabilities"] = capabilities
        if display_name:
            body["display_name"] = display_name
        for attempt in range(20):
            if self._post("/register", body) is not None:
                self.log.info(
                    "registered with coordinator at %s (display_name=%r, capabilities=%s)",
                    self.base, display_name, capabilities or {},
                )
                return True
            time.sleep(min(2 * (attempt + 1), 15))
        self.log.error("could not register with coordinator")
        return False

    # ---------- heartbeat: state setters + background thread ----------
    def set_idle(self) -> None:
        with self._hb_lock:
            self._current_status = "idle"
            self._current_job_id = None

    def set_offline(self) -> None:
        with self._hb_lock:
            self._current_status = "offline"
            self._current_job_id = None

    def set_busy(self, job_id: str) -> None:
        with self._hb_lock:
            self._current_status = "busy"
            self._current_job_id = job_id

    def clear_busy(self) -> None:
        # Distinct name for the transition out of a job — the main
        # loop usually wants to follow it with set_idle, but a call
        # to clear_busy on its own (e.g. error path before deciding
        # next state) just blanks the job_id without lying about the
        # current status.
        with self._hb_lock:
            self._current_job_id = None

    def _apply_schedule_response(self, resp: Optional[dict]) -> None:
        """Update the cached uptime-schedule state from a /heartbeat
        response. A None/empty response (transient failure, or a
        coordinator that predates this field) leaves the last known
        state untouched — we never flip the gate on a missed beat."""
        if not resp:
            return
        with self._hb_lock:
            self._allowed_now = bool(resp.get("allowed_now", self._allowed_now))
            self._sleeping_until = resp.get("sleeping_until")
            downtime = resp.get("downtime_poll_seconds")
            if isinstance(downtime, (int, float)) and downtime > 0:
                self._downtime_poll_seconds = float(downtime)

    def _heartbeat_interval(self) -> float:
        """Seconds to wait before the next beat: the slow downtime
        cadence while gated off, the normal cadence while working."""
        with self._hb_lock:
            return (
                HEARTBEAT_INTERVAL_SECONDS if self._allowed_now
                else self._downtime_poll_seconds
            )

    def schedule_state(self) -> tuple[bool, Optional[str]]:
        """(allowed_now, sleeping_until) snapshot for the main loop."""
        with self._hb_lock:
            return self._allowed_now, self._sleeping_until

    def _heartbeat_loop(self) -> None:
        """POST /heartbeat with the current status + job_id snapshot, then
        wait — fast while working, slow while the machine is outside its
        uptime window (the response carries allowed_now + the downtime
        cadence). Never raises — the coordinator being temporarily
        unreachable is a logged warning inside ``_post`` and the next
        tick retries cleanly."""
        # First beat goes out immediately so a re-register followed by
        # a long inference doesn't leave the coordinator without a
        # post-register heartbeat.
        while not self._hb_stop.is_set():
            with self._hb_lock:
                status = self._current_status
                job_id = self._current_job_id
            resp = self._post(
                "/heartbeat",
                {
                    "worker_id": self.worker_id,
                    "status": status,
                    "job_id": job_id,
                },
                timeout=5,
            )
            self._apply_schedule_response(resp)
            self._hb_stop.wait(self._heartbeat_interval())

    def start_heartbeat(self) -> None:
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="gamerai-heartbeat",
            daemon=True,
        )
        self._hb_thread.start()

    def stop_heartbeat(self, send_offline: bool = True) -> None:
        """Stop the background thread and (by default) fire one final
        synchronous offline heartbeat so the coordinator's worker view
        flips immediately rather than waiting WORKER_TIMEOUT_SECONDS to
        time out the previous beat."""
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2.0)
        if send_offline:
            try:
                self._post(
                    "/heartbeat",
                    {"worker_id": self.worker_id, "status": "offline", "job_id": None},
                    timeout=3,
                )
            except Exception:
                pass

    # ---------- job lifecycle ----------
    def next_job(
        self,
        tool: str = "chat",
        tools: Optional[list[str]] = None,
        wait: float = 0.0,
    ) -> Optional[dict]:
        """Pull the next job.

        - ``tools=[...] + wait=N`` long-polls (BLPOP) over the listed
          per-tool queues for up to N seconds, in the order given so
          the warm-model preference (last_tool first) is preserved.
          This is the v1.1.25+ default and what cuts dispatch latency
          from "0-5s polling gap" to "one network round-trip."
        - ``tool=X + wait=0`` keeps the legacy single-queue zero-wait
          behavior for callers that want it.

        The returned dict (if any) carries the per-claim token under
        ``_claim_token`` — the caller must thread it back to
        ``complete`` / ``partial`` / ``abandon`` so a re-dispatched
        job's original worker can't clobber the new claimant's work."""
        body: dict = {"worker_id": self.worker_id}
        if tools:
            body["tools"] = tools
        else:
            body["tool"] = tool
        if wait > 0:
            body["wait"] = wait
        # Network timeout: cover the wait window plus a buffer for the
        # actual request round-trip. With wait=0 the legacy 10s budget
        # is plenty.
        network_timeout = wait + 5.0 if wait > 0 else 10.0
        out = self._post("/jobs/next", body, timeout=network_timeout)
        if not out:
            return None
        job = out.get("job")
        if not job:
            return None
        token = out.get("claim_token")
        if token:
            job["_claim_token"] = token
        return job

    def complete(
        self,
        payload: dict,
        claim_token: Optional[str] = None,
    ) -> tuple[bool, Optional[dict]]:
        """Submit a result. Returns ``(accepted, response)``:

        - ``accepted=True``: coordinator accepted the result; caller
          should credit local earnings/jobs state.
        - ``accepted=False`` with a logged 410: the claim was
          superseded (reaper requeued, member cancelled, etc.) — caller
          must NOT credit local state.
        - ``accepted=False`` with a transport failure: unknown state.
          The coordinator may or may not have recorded the result;
          local state is best left untouched to avoid double-counting
          if the reaper requeues and another worker also completes."""
        body = dict(payload)
        if claim_token is not None:
            body["claim_token"] = claim_token
        status, data = self._post_raw("/jobs/complete", body, timeout=30)
        if status == 200:
            return True, data
        if status == 410:
            reason = ((data or {}).get("detail") or {})
            reason_code = reason.get("reason") if isinstance(reason, dict) else None
            self.log.warning(
                "complete rejected (410 %s) for job %s — skipping local credit",
                reason_code or "gone", payload.get("job_id"),
            )
            return False, data
        self.log.warning(
            "complete failed (status=%s) for job %s",
            status, payload.get("job_id"),
        )
        return False, data

    def partial(
        self,
        job_id: str,
        text: str,
        claim_token: Optional[str] = None,
        audio_chunk_b64: Optional[str] = None,
        audio_chunk_seconds: Optional[float] = None,
        audio_chunk_seq: Optional[int] = None,
    ) -> None:
        """Push the accumulated streaming text so far. Fire-and-forget:
        the coordinator overwrites with the latest call (text is the
        FULL accumulated output, not a delta), and a dropped partial
        just means the next one carries the full state. The final
        ``/jobs/complete`` is the source of truth.

        ``audio_chunk_b64`` / ``audio_chunk_seconds`` / ``audio_chunk_seq``
        (voice-mode chat only): set when the agent has just finished
        synthesizing a sentence batch. Each chunk is keyed by its seq
        (0, 1, 2, ...) so the coordinator can store per-seq and the
        polling client receives an ordered list."""
        body = {"worker_id": self.worker_id, "job_id": job_id, "text": text}
        if claim_token is not None:
            body["claim_token"] = claim_token
        if audio_chunk_b64 is not None and audio_chunk_seq is not None:
            body["audio_chunk_b64"] = audio_chunk_b64
            body["audio_chunk_seconds"] = float(audio_chunk_seconds or 0.0)
            body["audio_chunk_seq"] = int(audio_chunk_seq)
        self._post("/jobs/partial", body, timeout=3)

    def abandon(
        self,
        job_id: str,
        claim_token: Optional[str] = None,
    ) -> bool:
        """Voluntarily return a claimed job to the queue. Used in
        override-drain mode when the user becomes active between
        claim and inference. Coordinator requeues; another worker
        picks it up. Earnings are forfeited."""
        body = {"worker_id": self.worker_id, "job_id": job_id}
        if claim_token is not None:
            body["claim_token"] = claim_token
        out = self._post("/jobs/abandon", body)
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


# Retry budget for the staged-file rename. Windows Defender's
# real-time scan can hold an open handle on a freshly-written 1.5 GB
# file for several seconds, which makes the very first
# `staged.replace(dest)` fail with WinError 32. The scan completes;
# subsequent attempts succeed. Delays in seconds; total wait ~38 s.
_RENAME_RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 20.0)


def _atomic_rename_with_retry(
    staged: Path, dest: Path, log: logging.Logger, label: str,
) -> bool:
    """Promote *staged* to *dest* with exponential backoff. Returns True
    on success. The first attempt is the normal path; the retries exist
    to ride out Windows Defender holding an open scan handle on the
    just-written file."""
    last_err: Optional[OSError] = None
    for attempt, delay in enumerate((0.0,) + _RENAME_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            staged.replace(dest)
            if attempt:
                log.info(
                    "bootstrap: %s rename succeeded on retry %d", label, attempt,
                )
            return True
        except OSError as e:
            last_err = e
            if attempt < len(_RENAME_RETRY_DELAYS):
                log.info(
                    "bootstrap: %s rename busy (%s); retrying in %.0fs",
                    label, e, _RENAME_RETRY_DELAYS[attempt],
                )
    log.warning(
        "bootstrap: could not finalize %s after %d retries: %s",
        label, len(_RENAME_RETRY_DELAYS), last_err,
    )
    return False


def _download_to(url: str, dest: Path, log: logging.Logger, label: str) -> bool:
    """Stream a URL to a temp file then rename atomically. Returns True
    only on a complete, non-empty download. Logs progress as percent +
    ETA when the server provides Content-Length (the usual case for a
    static file). Falls back to byte counts when Content-Length is
    absent (e.g. a chunked-encoded response).

    Recovery: if `dest` itself already exists (a prior run completed
    this download and nothing has cleaned it up since — e.g. a restart
    after an unrelated failure later in bootstrap), or a prior run left
    a *.part file, whose size matches the server's Content-Length,
    skip the redownload. Saves re-pulling a multi-GB file (the
    ollama/image/tts installers and model weights) on every retry, and
    a 1.5 GB redo when only the rename failed previously (the Windows
    Defender / WinError 32 case). Verified against a live HEAD each
    time rather than trusted blindly, so a mirror update to a new
    build doesn't silently get skipped as already-downloaded."""
    staged = dest.with_suffix(dest.suffix + ".part")
    existing = None
    if dest.exists() and dest.stat().st_size > 0:
        existing = dest
    elif staged.exists() and staged.stat().st_size > 0:
        existing = staged
    if existing is not None:
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as c:
                head = c.head(url)
            remote_total = int(head.headers.get("content-length") or 0)
        except Exception as e:
            log.info(
                "bootstrap: %s recovery HEAD failed (%s); falling back to redownload",
                label, e,
            )
            remote_total = 0
        if remote_total > 0 and existing.stat().st_size == remote_total:
            if existing is dest:
                log.info(
                    "bootstrap: %s already present (%.0f MB); skipping redownload",
                    label, remote_total / (1024 * 1024),
                )
                return True
            log.info(
                "bootstrap: %s found complete %s (%.0f MB); skipping redownload",
                label, staged.name, remote_total / (1024 * 1024),
            )
            return _atomic_rename_with_retry(staged, dest, log, label)
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
    except OSError as e:
        log.warning("bootstrap: could not stat %s: %s", label, e)
        return False
    return _atomic_rename_with_retry(staged, dest, log, label)


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
    log.info("")
    log.info(
        "bootstrap: please wait — this can take a minute or two before "
        "anything else appears. It is not frozen."
    )
    log.info(
        "bootstrap: if a 'User Account Control' / Windows Security prompt "
        "appears asking to install a Microsoft Visual C++ Redistributable, "
        "click Yes — that's Ollama's own installer pulling a dependency "
        "it needs. It's expected and safe; nothing will proceed until "
        "you answer it."
    )
    try:
        # Ollama's Windows installer is Inno Setup-based, not NSIS/Squirrel
        # (confirmed: docs.ollama.com documents /DIR="...", which is the
        # Inno Setup custom-install-path convention, not NSIS's /D=). NSIS's
        # /S is meaningless to it and gets silently ignored, so the
        # installer falls back to its full interactive UI — exactly what
        # surfaced and blocked an unattended (auto-start, no one watching)
        # install until the 600s subprocess timeout killed it. The correct
        # Inno Setup silent set: /VERYSILENT suppresses the wizard entirely,
        # /SUPPRESSMSGBOXES auto-answers any message box with its default,
        # /NORESTART stops it from rebooting the box on our behalf, /SP-
        # skips the "This will install... continue?" prompt. CREATE_NO_WINDOW
        # is the separate Windows-only piece that keeps the launcher's own
        # console window from surfacing — without it, even a fully silent
        # installer still hands Python's child process its own console.
        # Same pattern as _start_ollama_server below. getattr() falls back
        # to 0 on non-Windows so the agent's unit tests (which import this
        # module on Linux CI) stay green.
        rc = subprocess.run(
            [str(setup_dest), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
            timeout=600,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log.info("bootstrap: ollama installer exited rc=%s", rc.returncode)
        if rc.returncode != 0:
            log.warning(
                "bootstrap: ollama installer returned rc=%s (0 is the only "
                "clean-success code) — most likely the Windows Security "
                "prompt above was closed/declined rather than accepted. "
                "Restart the agent to retry.",
                rc.returncode,
            )
    except subprocess.TimeoutExpired:
        log.warning(
            "bootstrap: ollama installer did not finish within 600s — this "
            "almost always means the Windows Security prompt for the VC++ "
            "Redistributable is still sitting there waiting for a click "
            "and nobody's watching (e.g. an unattended auto-start). If "
            "you're at the console now, look for it; otherwise restart "
            "the agent while logged in so you can accept it."
        )
        return None
    except Exception as e:
        log.warning("bootstrap: ollama installer failed to run: %s", e)
        return None
    # Installer can take a moment to populate %LOCALAPPDATA%\Programs\Ollama.
    for _ in range(20):
        exe = _find_ollama_exe()
        if exe is not None:
            return exe
        time.sleep(1.0)
    log.warning(
        "bootstrap: ollama.exe not found after install — if a security "
        "prompt appeared and was closed/declined instead of accepted, "
        "that's the likely cause. Restart the agent to retry."
    )
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


# ---------------------------------------------------------------------------
# Image-generation bootstrap (Windows-only): install sd.exe + GGUF model
# ---------------------------------------------------------------------------
# Default chain on first-run:
#   1. Check %APPDATA%\GamerAI\sd\sd.exe — skip if present.
#   2. Else download {mirror}/download/sd.exe into that path.
#   3. Check %APPDATA%\GamerAI\sd\models\<slug>.gguf — skip if present.
#   4. Else download {mirror}/download/sd-models/<slug>.gguf.
#
# Best-effort: any failure leaves the agent running chat-only. Idempotent
# — every step short-circuits when its target file is already present.

def sd_install_dir() -> Path:
    """Where sd.exe + DLLs + model weights live on the contributor box.
    Sits under the same state_dir() as everything else the agent
    persists so a contributor cleaning up has one folder to remove."""
    d = state_dir() / "sd"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sd_models_dir() -> Path:
    d = sd_install_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sd_binary_path() -> Path:
    return sd_install_dir() / "sd.exe"


def sd_model_path(slug: str) -> Path:
    return sd_models_dir() / f"{slug}.gguf"


def sd_sidecar_path(slug: str) -> Path:
    return sd_models_dir() / f"{slug}.json"


# Per-model inference defaults the agent feeds to sd.exe when the
# job didn't override them. Loaded from sd-models/<slug>.json staged
# by infra/setup-image-mirror.sh; falls back to vanilla SD 1.5 numbers
# when the sidecar is missing or malformed (older deployments).
_SD_DEFAULTS_FALLBACK = {
    "default_width": 512,
    "default_height": 512,
    "default_steps": 20,
    "default_cfg_scale": 7.0,
    "default_sampler": "euler_a",
}


def load_sd_model_defaults(slug: str, log: logging.Logger) -> dict:
    """Read sd-models/<slug>.json and return the (steps, cfg, sampler,
    size) defaults for this model. Tolerates missing / malformed files
    by falling back to vanilla-SD1.5 numbers — callers don't have to
    branch on the absence."""
    path = sd_sidecar_path(slug)
    if not path.exists():
        return dict(_SD_DEFAULTS_FALLBACK)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("image: sidecar %s unreadable (%s) — using fallback", path, exc)
        return dict(_SD_DEFAULTS_FALLBACK)
    merged = dict(_SD_DEFAULTS_FALLBACK)
    for k in _SD_DEFAULTS_FALLBACK:
        if k in data:
            merged[k] = data[k]
    return merged


# Files the sd.cpp Windows build needs alongside sd.exe. sd.exe by
# itself won't load — `stable-diffusion.dll` carries the ggml + the
# GPU backend (vulkan/cuda/etc.). Add other names here when bundling
# alternative builds (e.g. the CUDA build also needs cudart-*.dll).
SD_RUNTIME_FILES = ("sd.exe", "stable-diffusion.dll")


def bootstrap_image_inference(cfg: "Config", log: logging.Logger) -> bool:
    """Make sure sd.exe + its runtime DLLs + the default GGUF are
    ready. Returns True on success, False (best-effort) on failure —
    caller falls back to chat-only capability advertisement."""
    if not cfg.bootstrap_image_enabled:
        log.info("image bootstrap: disabled in config — skipping")
        return False
    if not IS_WINDOWS:
        log.info("image bootstrap: not on Windows — skipping (dev mode)")
        return False
    mirror_base = (cfg.bootstrap_mirror_base_url or cfg.coordinator_url).rstrip("/")
    install_dir = sd_install_dir()
    missing = [f for f in SD_RUNTIME_FILES if not (install_dir / f).exists()]
    if missing:
        log.info(
            "image bootstrap: missing %s — pulling from mirror. "
            "First-run image setup pulls ~90 MB of runtime plus a "
            "~1.5 GB model — typically 5-15 minutes on home internet.",
            ", ".join(missing),
        )
        for fname in missing:
            url = f"{mirror_base}/download/{fname}"
            if not _download_to(url, install_dir / fname, log, fname):
                return False
    model_path = sd_model_path(cfg.bootstrap_image_model)
    if not model_path.exists():
        url = (
            f"{mirror_base}/download/sd-models/"
            f"{cfg.bootstrap_image_model}.gguf"
        )
        log.info(
            "image bootstrap: model %s not found; downloading from %s",
            cfg.bootstrap_image_model, url,
        )
        if not _download_to(url, model_path, log, f"{cfg.bootstrap_image_model}.gguf"):
            return False
    # Sidecar JSON carries per-model defaults (steps / cfg / sampler).
    # Treated as best-effort — if the mirror doesn't have it, the agent
    # falls back to hardcoded SD1.5 numbers. Refetched whenever the
    # file is missing locally so a mirror-side update to the defaults
    # propagates on the next bootstrap tick (cheap, ~200 B).
    sidecar_path = sd_sidecar_path(cfg.bootstrap_image_model)
    if not sidecar_path.exists():
        url = (
            f"{mirror_base}/download/sd-models/"
            f"{cfg.bootstrap_image_model}.json"
        )
        # Failure is non-fatal — fallback defaults kick in.
        _download_to(url, sidecar_path, log, f"{cfg.bootstrap_image_model}.json")
    # Sweep stale GGUF/JSON sidecars from previous model selections so
    # contributor disks don't accumulate a 1.5 GB blob per model swap.
    # Keeps only the currently configured slug.
    _sweep_stale_image_models(cfg.bootstrap_image_model, log)
    log.info(
        "image bootstrap: ready (install_dir=%s, model=%s)",
        install_dir, model_path,
    )
    return True


def _sweep_stale_image_models(active_slug: str, log: logging.Logger) -> None:
    """Delete *.gguf / *.json in sd_models_dir() that don't match the
    active slug. Best-effort: an OSError on one file shouldn't stop the
    others from being removed."""
    models_dir = sd_models_dir()
    if not models_dir.exists():
        return
    keep = {f"{active_slug}.gguf", f"{active_slug}.json"}
    for entry in models_dir.iterdir():
        if entry.name in keep:
            continue
        if entry.suffix not in (".gguf", ".json"):
            continue
        try:
            size_mb = entry.stat().st_size / (1024 * 1024)
            entry.unlink()
            log.info(
                "image bootstrap: removed stale %s (%.1f MB)",
                entry.name, size_mb,
            )
        except OSError as exc:
            log.warning("image bootstrap: could not remove %s (%s)", entry.name, exc)


# ---------------------------------------------------------------------------
# TTS bootstrap (Phase 1 voice feature, see voice-phase1 design memory)
#
# Same shape as the image bootstrap: pulls a runtime + a voice model
# from the coordinator's mirror, persists under state_dir()/tts so a
# user cleaning up has one folder to remove. Differences from image:
#   1. Runtime arrives as a zip (piper.exe + onnxruntime.dll +
#      espeak-ng-data/) — too many small files in espeak-ng-data to
#      flat-serve. We unzip once on first run.
#   2. Voice slugs use "piper:en_us-libritts-high" in the registry —
#      the colon is invalid on NTFS, so we substitute "__" when
#      mapping slug → filesystem path. The slug stays canonical in
#      the registry, capability list, and stdin commands.
#   3. CPU-only — does not contend with the image GPU loop.
# ---------------------------------------------------------------------------

def tts_install_dir() -> Path:
    d = state_dir() / "tts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tts_voices_dir() -> Path:
    d = tts_install_dir() / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tts_binary_path() -> Path:
    return tts_install_dir() / "piper.exe"


def _tts_slug_to_fs(slug: str) -> str:
    """Map a registry slug ("piper:en_us-libritts-high") to a
    filesystem-safe name ("piper__en_us-libritts-high"). Colon is
    legal on Linux but is the NTFS alt-stream separator on Windows,
    so the canonical slug round-trips through this on every filesystem
    touch. Mirrors the substitution in setup-tts-mirror.sh."""
    return slug.replace(":", "__")


def tts_voice_path(slug: str) -> Path:
    return tts_voices_dir() / f"{_tts_slug_to_fs(slug)}.onnx"


def tts_voice_config_path(slug: str) -> Path:
    """Upstream piper config — sample_rate, espeak voice, phoneme map.
    Required (not best-effort): the piper binary refuses to load a
    voice without its sibling .onnx.json."""
    return tts_voices_dir() / f"{_tts_slug_to_fs(slug)}.onnx.json"


def tts_sidecar_path(slug: str) -> Path:
    """GamerAI-owned per-voice defaults (length_scale, noise_scale)
    that are *separate* from the upstream config so we can tune pacing
    without mutating Piper's distributed file. Mirrors the image
    sidecar shape."""
    return tts_voices_dir() / f"{_tts_slug_to_fs(slug)}.json"


# Piper invocation defaults. length_scale=1.0 is natural pace;
# noise_scale + noise_w control phoneme variation (higher = more
# expressive but less predictable). Falls back to these when the
# sidecar JSON is missing or malformed.
_TTS_DEFAULTS_FALLBACK = {
    "sample_rate": 22050,
    "default_length_scale": 1.0,
    "default_noise_scale": 0.667,
    "default_noise_w": 0.8,
}


def load_tts_voice_defaults(slug: str, log: logging.Logger) -> dict:
    """Read tts-voices/<slug>.json (our sidecar) and return the
    length/noise/sample-rate defaults to feed Piper. Tolerates missing
    or malformed files — falls back to the Piper-recommended values so
    inference still runs."""
    path = tts_sidecar_path(slug)
    if not path.exists():
        return dict(_TTS_DEFAULTS_FALLBACK)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("tts: sidecar %s unreadable (%s) — using fallback", path, exc)
        return dict(_TTS_DEFAULTS_FALLBACK)
    merged = dict(_TTS_DEFAULTS_FALLBACK)
    for k in _TTS_DEFAULTS_FALLBACK:
        if k in data:
            merged[k] = data[k]
    return merged


# Marker file dropped after a successful unzip so subsequent runs can
# skip the (idempotent but slow) extract step. Touched with the URL
# the zip came from so a mirror_base_url change invalidates the cache.
_TTS_RUNTIME_MARKER = "piper-runtime.installed"


def bootstrap_tts_inference(cfg: "Config", log: logging.Logger) -> bool:
    """Make sure piper.exe + espeak-ng-data + the default voice ONNX
    are ready. Returns True on success, False (best-effort) on failure
    — caller drops "tts" from advertised capabilities and the agent
    keeps serving chat/image.

    Three phases:
      1. Download piper-runtime.zip (~30 MB) and unzip if marker is
         missing or stale.
      2. Download <voice>.onnx + <voice>.onnx.json (~75 MB combined
         for libritts-high).
      3. Download our sidecar JSON (best-effort; fallback defaults
         kick in if absent).
    """
    if not cfg.bootstrap_tts_enabled:
        log.info("tts bootstrap: disabled in config — skipping")
        return False
    if not IS_WINDOWS:
        log.info("tts bootstrap: not on Windows — skipping (dev mode)")
        return False
    mirror_base = (cfg.bootstrap_mirror_base_url or cfg.coordinator_url).rstrip("/")
    install_dir = tts_install_dir()
    marker = install_dir / _TTS_RUNTIME_MARKER
    runtime_url = f"{mirror_base}/download/piper-runtime.zip"
    marker_payload = runtime_url + "\n"
    needs_runtime = (
        not tts_binary_path().exists()
        or not marker.exists()
        or marker.read_text(encoding="utf-8", errors="replace") != marker_payload
    )
    if needs_runtime:
        zip_path = install_dir / "piper-runtime.zip"
        log.info(
            "tts bootstrap: pulling runtime from %s (~30 MB; first-run "
            "extract includes espeak-ng phoneme data)",
            runtime_url,
        )
        if not _download_to(runtime_url, zip_path, log, "piper-runtime.zip"):
            return False
        try:
            import zipfile
            with zipfile.ZipFile(zip_path) as zf:
                # Upstream layout is "piper/piper.exe" + siblings. We
                # flatten so piper.exe lands directly in install_dir
                # alongside espeak-ng-data/ — matches sd.exe shape and
                # keeps the binary callable by tts_binary_path().
                for member in zf.namelist():
                    # zipfile names use forward slashes regardless of
                    # platform; strip a leading "piper/" prefix.
                    rel = member.split("/", 1)[1] if "/" in member else member
                    if not rel:
                        continue  # the directory entry itself
                    dest = install_dir / rel
                    if member.endswith("/"):
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(dest, "wb") as out:
                        out.write(src.read())
            marker.write_text(marker_payload, encoding="utf-8")
        except (OSError, zipfile.BadZipFile) as exc:
            log.warning("tts bootstrap: extract failed (%s)", exc)
            return False
        finally:
            try:
                zip_path.unlink()
            except OSError:
                pass
    voice_path = tts_voice_path(cfg.bootstrap_tts_model)
    voice_config_path = tts_voice_config_path(cfg.bootstrap_tts_model)
    fs_slug = _tts_slug_to_fs(cfg.bootstrap_tts_model)
    if not voice_path.exists():
        url = f"{mirror_base}/download/tts-voices/{fs_slug}.onnx"
        log.info(
            "tts bootstrap: voice %s not found; downloading from %s",
            cfg.bootstrap_tts_model, url,
        )
        if not _download_to(url, voice_path, log, f"{fs_slug}.onnx"):
            return False
    if not voice_config_path.exists():
        url = f"{mirror_base}/download/tts-voices/{fs_slug}.onnx.json"
        if not _download_to(url, voice_config_path, log, f"{fs_slug}.onnx.json"):
            # Without the config Piper refuses to load — fail bootstrap
            # cleanly so the agent advertises chat only.
            return False
    sidecar_path = tts_sidecar_path(cfg.bootstrap_tts_model)
    if not sidecar_path.exists():
        url = f"{mirror_base}/download/tts-voices/{fs_slug}.json"
        # Best-effort — load_tts_voice_defaults falls back if absent.
        _download_to(url, sidecar_path, log, f"{fs_slug}.json")
    _sweep_stale_tts_voices(cfg.bootstrap_tts_model, log)
    log.info(
        "tts bootstrap: ready (install_dir=%s, voice=%s)",
        install_dir, voice_path,
    )
    return True


def _sweep_stale_tts_voices(active_slug: str, log: logging.Logger) -> None:
    """Delete voice ONNX / JSON files in tts_voices_dir() that don't
    match the active slug. Same rationale as
    _sweep_stale_image_models: contributor disks shouldn't accumulate
    a 75 MB voice per swap."""
    voices_dir = tts_voices_dir()
    if not voices_dir.exists():
        return
    fs_slug = _tts_slug_to_fs(active_slug)
    keep = {
        f"{fs_slug}.onnx",
        f"{fs_slug}.onnx.json",
        f"{fs_slug}.json",
    }
    for entry in voices_dir.iterdir():
        if entry.name in keep:
            continue
        if entry.suffix not in (".onnx", ".json"):
            continue
        try:
            size_mb = entry.stat().st_size / (1024 * 1024)
            entry.unlink()
            log.info(
                "tts bootstrap: removed stale %s (%.1f MB)",
                entry.name, size_mb,
            )
        except OSError as exc:
            log.warning("tts bootstrap: could not remove %s (%s)", entry.name, exc)


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
# Smart mode (multi-machine pipeline via llama.cpp RPC)
# ---------------------------------------------------------------------------
# Two LAN-linked contributor machines pool their VRAM to serve a
# 14B-class model neither card can hold alone:
#
#   backend role: runs llama.cpp's rpc-server, which exposes this
#       machine's GPU to the head over TCP. Holds a layer shard in
#       VRAM; claims no jobs itself.
#   head role: runs llama-server with --rpc <backend>, which splits
#       the model's layers across the local GPU + every backend in
#       proportion to free VRAM (override with tensor_split). Exposes
#       an OpenAI-compatible API on localhost; the agent advertises
#       the "chat:smart" tool and serves jobs against it.
#
# Both machines MUST run the same llama.cpp build — the RPC protocol
# is only compatible between identical versions — which is why the
# bootstrap pins a release tag and keys the install dir by it.
#
# Best-effort like the image/TTS bootstraps: any failure leaves the
# agent running without the smart capability. See docs/smart-mode.md.

# Single-file Q4_K_M GGUFs for the models the smart pipeline knows how
# to fetch. Overridable per-install via smart.gguf_url in config.json.
# The smaller entries exist for testing/refinement: pair them with the
# coordinator's SMART_MODEL env override so the whole smart path can be
# exercised with a 2-5 GB download and fast loads — see
# docs/smart-mode.md "Testing with a smaller model".
_SMART_GGUF_URLS = {
    "qwen2.5:14b": (
        "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/"
        "resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf"
    ),
    "qwen2.5:7b": (
        "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/"
        "resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf"
    ),
    "llama3.2:3b": (
        "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/"
        "resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf"
    ),
}

# How often the watchdog checks that the sidecar process is alive,
# and the backoff cap between relaunch attempts after a crash.
SMART_WATCHDOG_INTERVAL_SECONDS = 5.0
SMART_RESTART_BACKOFF_MAX_SECONDS = 300.0


def llama_install_dir() -> Path:
    """Root for everything smart mode puts on disk. Same state_dir()
    convention as the sd/ and tts/ trees: one folder to delete."""
    d = state_dir() / "llama"
    d.mkdir(parents=True, exist_ok=True)
    return d


def llama_bin_dir(release: str) -> Path:
    """Per-release binary dir. Keyed by the release tag so bumping
    smart.llama_release re-downloads cleanly on both machines and the
    pipeline can never silently run mismatched RPC protocol versions."""
    d = llama_install_dir() / release
    d.mkdir(parents=True, exist_ok=True)
    return d


def llama_models_dir() -> Path:
    d = llama_install_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def llama_rpc_cache_dir() -> Path:
    """Backend-side tensor cache (rpc-server -c). After the first load
    streams the layer shard from the head, every tensor is cached here
    by content hash — reloads hand the head a hash hit instead of
    re-transferring, which is what makes the shard transfer a one-time
    cost per model. Pinned via LLAMA_CACHE at launch so it lives under
    the agent's own tree (deterministic, survives agent updates, and
    deleting %APPDATA%\\GamerAI still removes everything)."""
    d = llama_install_dir() / "rpc-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def smart_gguf_path(model: str) -> Path:
    return llama_models_dir() / f"{_model_slug(model)}.gguf"


def _smart_default_llama_zip_url(release: str) -> str:
    # Official prebuilt Windows CUDA binaries. The 12.4 build has the
    # widest driver compatibility across the consumer cards we target.
    return (
        "https://github.com/ggml-org/llama.cpp/releases/download/"
        f"{release}/llama-{release}-bin-win-cuda-12.4-x64.zip"
    )


def _smart_default_cudart_zip_url(release: str) -> str:
    # CUDA runtime DLL sidecar published alongside each release —
    # needed when the machine doesn't have the CUDA toolkit installed
    # (i.e. virtually every contributor box).
    return (
        "https://github.com/ggml-org/llama.cpp/releases/download/"
        f"{release}/cudart-llama-bin-win-cuda-12.4-x64.zip"
    )


def _extract_zip_to(zip_path: Path, dest: Path, log: logging.Logger) -> bool:
    """Flatten-extract *zip_path* into *dest*. Some llama.cpp release
    zips nest everything under a build/bin/ prefix and some are flat;
    flattening means callers can always expect <dest>/<exe> regardless.
    Rejects unsafe member names (zip-slip) and skips directories."""
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                if not name or name.startswith(".."):
                    continue
                target = dest / name
                with zf.open(info) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
        return True
    except Exception as e:
        log.warning("smart: could not extract %s: %s", zip_path.name, e)
        return False


def _smart_ensure_binaries(cfg: "Config", log: logging.Logger) -> Optional[Path]:
    """Download + extract the pinned llama.cpp release (binaries zip +
    cudart sidecar) into the per-release dir. Idempotent: short-circuits
    when the role's exe is already present. Returns the bin dir, or
    None on failure."""
    bin_dir = llama_bin_dir(cfg.smart_llama_release)
    needed_exe = (
        "rpc-server.exe" if cfg.smart_role == "backend" else "llama-server.exe"
    )
    if (bin_dir / needed_exe).exists():
        return bin_dir
    log.info(
        "smart bootstrap: %s not present — downloading llama.cpp %s "
        "(~500 MB of binaries + CUDA runtime; one-time per release)",
        needed_exe, cfg.smart_llama_release,
    )
    downloads = [
        (
            cfg.smart_llama_zip_url
            or _smart_default_llama_zip_url(cfg.smart_llama_release),
            f"llama-{cfg.smart_llama_release}.zip",
        ),
        (
            cfg.smart_cudart_zip_url
            or _smart_default_cudart_zip_url(cfg.smart_llama_release),
            f"cudart-{cfg.smart_llama_release}.zip",
        ),
    ]
    for url, label in downloads:
        zip_dest = llama_install_dir() / label
        if not zip_dest.exists():
            if not _download_to(url, zip_dest, log, label):
                return None
        if not _extract_zip_to(zip_dest, bin_dir, log):
            return None
        # Zip already extracted into place — drop it so a contributor
        # box doesn't carry an extra ~500 MB per release bump.
        try:
            zip_dest.unlink()
        except OSError:
            pass
    if not (bin_dir / needed_exe).exists():
        log.warning(
            "smart bootstrap: %s missing after extraction — the release "
            "zip layout may have changed; check smart.llama_zip_url",
            needed_exe,
        )
        return None
    return bin_dir


def _smart_ensure_gguf(cfg: "Config", log: logging.Logger) -> Optional[Path]:
    """Head only: make sure the smart model's GGUF is on disk."""
    dest = smart_gguf_path(cfg.smart_model)
    if dest.exists():
        return dest
    url = cfg.smart_gguf_url or _SMART_GGUF_URLS.get(cfg.smart_model)
    if not url:
        log.warning(
            "smart bootstrap: no GGUF url known for %s — set "
            "smart.gguf_url in config.json", cfg.smart_model,
        )
        return None
    log.info(
        "smart bootstrap: downloading %s weights (~9 GB — this is a "
        "one-time download and can take a while on home internet)",
        cfg.smart_model,
    )
    if not _download_to(url, dest, log, dest.name):
        return None
    return dest


class SmartRuntime:
    """Owns the smart-mode sidecar process (llama-server on the head,
    rpc-server on a backend) plus the watchdog that relaunches it if
    it dies. ``ready()`` is what the job loop checks before polling
    the chat:smart queue, so a crashed/mid-restart pipeline stops
    claiming jobs it can't serve instead of erroring them.

    When config supplies smart.endpoint the runtime is unmanaged: no
    process, no watchdog — ready() just reflects the last health probe
    of the external server."""

    def __init__(self, cfg: "Config", log: logging.Logger, bin_dir: Optional[Path],
                 gguf: Optional[Path]):
        self.cfg = cfg
        self.log = log
        self.bin_dir = bin_dir
        self.gguf = gguf
        self.role = cfg.smart_role
        self.managed = cfg.smart_endpoint is None
        self.endpoint = (
            cfg.smart_endpoint
            or f"http://127.0.0.1:{cfg.smart_llama_server_port}"
        )
        self._proc: Optional[subprocess.Popen] = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

    # ---------- public ----------
    def ready(self) -> bool:
        return self._ready.is_set()

    def start(self) -> bool:
        """Launch (managed) or probe (unmanaged), then arm the watchdog.
        Returns True when the pipeline came up ready."""
        if not self.managed:
            ok = self._wait_healthy(timeout=15.0)
            if ok:
                self._ready.set()
                self.log.info(
                    "smart: using external server at %s", self.endpoint,
                )
            else:
                self.log.warning(
                    "smart: external endpoint %s is not answering /health",
                    self.endpoint,
                )
            return ok
        if not self._launch():
            return False
        ok = self._post_launch_ready()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="gamerai-smart-watchdog",
            daemon=True,
        )
        self._watchdog.start()
        return ok

    def stop(self) -> None:
        self._stop.set()
        self._ready.clear()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ---------- internals ----------
    def _command(self) -> list[str]:
        cfg = self.cfg
        assert self.bin_dir is not None
        if self.role == "backend":
            cmd = [
                str(self.bin_dir / "rpc-server.exe"),
                "-H", cfg.smart_rpc_listen_host,
                "-p", str(cfg.smart_rpc_listen_port),
                # Local tensor cache: after the first job, model shards
                # are cached on this machine's disk so a pipeline
                # restart doesn't re-stream ~5 GB over the LAN.
                "-c",
            ]
        else:
            assert self.gguf is not None
            cmd = [
                str(self.bin_dir / "llama-server.exe"),
                "-m", str(self.gguf),
                "--host", "127.0.0.1",
                "--port", str(cfg.smart_llama_server_port),
                # Offload everything; llama.cpp clamps to the real
                # layer count and spreads across local GPU + RPC
                # backends by free VRAM.
                "-ngl", "999",
                "-c", str(cfg.smart_context_length),
            ]
            if cfg.smart_rpc_peers:
                cmd += ["--rpc", ",".join(cfg.smart_rpc_peers)]
            if cfg.smart_tensor_split:
                cmd += ["-ts", cfg.smart_tensor_split]
        cmd += list(cfg.smart_extra_args)
        return cmd

    def _launch(self) -> bool:
        cmd = self._command()
        log_path = logs_dir() / f"llama-{self.role}.log"
        self.log.info("smart: launching %s", " ".join(cmd))
        try:
            out = open(log_path, "ab")
        except OSError:
            out = subprocess.DEVNULL  # type: ignore[assignment]
        # LLAMA_CACHE pins rpc-server's tensor cache (the thing that
        # makes the shard transfer one-time) to the agent's own tree
        # instead of whatever HOME/%LOCALAPPDATA% resolves to for a
        # tray-spawned process.
        env = {**os.environ, "LLAMA_CACHE": str(llama_rpc_cache_dir())}
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self.bin_dir),
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW_FLAGS,
                env=env,
            )
            return True
        except Exception as e:
            self.log.warning("smart: could not launch %s: %s", cmd[0], e)
            return False

    def _post_launch_ready(self) -> bool:
        """Role-specific readiness: the head must answer /health (model
        fully loaded — first load streams the backend's shard over the
        LAN, so this can take minutes); a backend just has to stay
        alive past its first few seconds."""
        if self.role == "backend":
            time.sleep(3.0)
            if self._proc is not None and self._proc.poll() is None:
                self._ready.set()
                self.log.info(
                    "smart: rpc-server up on %s:%d — this GPU is now "
                    "lendable to the pipeline head",
                    self.cfg.smart_rpc_listen_host,
                    self.cfg.smart_rpc_listen_port,
                )
                return True
            self.log.warning(
                "smart: rpc-server exited immediately — see %s",
                logs_dir() / "llama-backend.log",
            )
            return False
        ok = self._wait_healthy(self.cfg.smart_startup_timeout_seconds)
        if ok:
            self._ready.set()
            self.log.info(
                "smart: llama-server healthy at %s (model=%s, peers=%s)",
                self.endpoint, self.cfg.smart_model,
                ",".join(self.cfg.smart_rpc_peers) or "(local only)",
            )
        else:
            self.log.warning(
                "smart: llama-server did not become healthy within "
                "%.0fs — see %s. Common causes: backend rpc-server not "
                "running / wrong smart.rpc_peers address / firewall "
                "blocking port %d on the backend machine.",
                self.cfg.smart_startup_timeout_seconds,
                logs_dir() / "llama-head.log",
                self.cfg.smart_rpc_listen_port,
            )
        return ok

    def _wait_healthy(self, timeout: float) -> bool:
        """Poll GET /health until 200. llama-server returns 503 while
        the model is still loading, 200 once it can serve."""
        deadline = time.time() + timeout
        url = f"{self.endpoint}/health"
        while time.time() < deadline and not self._stop.is_set():
            if self.managed and self._proc is not None and self._proc.poll() is not None:
                return False  # process died — no point polling on
            try:
                with httpx.Client(timeout=5.0) as c:
                    if c.get(url).status_code == 200:
                        return True
            except httpx.HTTPError:
                pass
            time.sleep(5.0)
        return False

    def _watchdog_loop(self) -> None:
        backoff = SMART_WATCHDOG_INTERVAL_SECONDS
        while not self._stop.is_set():
            self._stop.wait(SMART_WATCHDOG_INTERVAL_SECONDS)
            if self._stop.is_set():
                return
            proc = self._proc
            if proc is None or proc.poll() is None:
                backoff = SMART_WATCHDOG_INTERVAL_SECONDS
                continue
            self._ready.clear()
            self.log.warning(
                "smart: %s exited rc=%s — relaunching in %.0fs",
                "rpc-server" if self.role == "backend" else "llama-server",
                proc.returncode, backoff,
            )
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, SMART_RESTART_BACKOFF_MAX_SECONDS)
            if self._launch():
                self._post_launch_ready()


def bootstrap_smart_runtime(
    cfg: "Config", log: logging.Logger,
) -> Optional["SmartRuntime"]:
    """Bring up smart mode per config. Returns a started SmartRuntime,
    or None when smart mode is disabled / failed to start (agent then
    runs exactly as before — best-effort, same contract as the image
    and TTS bootstraps)."""
    if not cfg.smart_enabled:
        return None
    if cfg.smart_role not in ("head", "backend"):
        log.warning(
            "smart: unknown role %r (want 'head' or 'backend') — "
            "smart mode disabled", cfg.smart_role,
        )
        return None
    if cfg.smart_endpoint is not None:
        # Unmanaged: operator runs their own llama-server. Works on
        # any OS — this is also the dev path on Linux/Mac.
        rt = SmartRuntime(cfg, log, bin_dir=None, gguf=None)
        return rt if rt.start() else None
    if not IS_WINDOWS:
        log.info(
            "smart: managed sidecar launch is Windows-only — set "
            "smart.endpoint to use an externally-run llama-server "
            "(dev mode)",
        )
        return None
    bin_dir = _smart_ensure_binaries(cfg, log)
    if bin_dir is None:
        return None
    gguf: Optional[Path] = None
    if cfg.smart_role == "head":
        gguf = _smart_ensure_gguf(cfg, log)
        if gguf is None:
            return None
        if not cfg.smart_rpc_peers:
            log.warning(
                "smart: head has no rpc_peers configured — running the "
                "%s pipeline on this machine alone (layers that don't "
                "fit in VRAM spill to CPU; expect it to be slow). Add "
                "the backend machine's ip:port to smart.rpc_peers.",
                cfg.smart_model,
            )
    rt = SmartRuntime(cfg, log, bin_dir=bin_dir, gguf=gguf)
    return rt if rt.start() else None


def _parse_sse_data(line: str) -> Optional[str]:
    """Extract the payload from one Server-Sent-Events line. Returns
    None for blanks / comments / non-data fields, the raw data string
    (possibly "[DONE]") otherwise. Split out of run_smart_inference so
    the protocol parsing is unit-testable without a server."""
    if not line:
        return None
    if line.startswith("data:"):
        return line[len("data:"):].strip()
    return None


def run_smart_inference(
    prompt: str,
    model: str,
    log: logging.Logger,
    endpoint: str,
    messages: Optional[list] = None,
    on_partial=None,
) -> dict:
    """Stream a chat completion from the smart pipeline's llama-server
    (OpenAI-compatible /v1/chat/completions SSE). Mirrors
    run_inference's contract — same return dict, same on_partial
    cadence — but raises on failure instead of falling back to mock:
    a silently-mocked 'smart' answer would be worse than an error
    bubble with a retry button."""
    msgs = messages or [{"role": "user", "content": prompt}]
    payload = {
        "model": model,
        "messages": msgs,
        "stream": True,
        # Ask for token usage on the final chunk. llama-server honors
        # this; if a build doesn't, the estimate fallback below kicks in.
        "stream_options": {"include_usage": True},
    }
    url = f"{endpoint.rstrip('/')}/v1/chat/completions"
    text = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    last_flush = 0.0
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as c:
        with c.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                data = _parse_sse_data(line)
                if data is None:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    token = (choices[0].get("delta") or {}).get("content") or ""
                    if token:
                        text += token
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage.get(
                        "completion_tokens", completion_tokens,
                    )
                now = time.time()
                if (
                    on_partial
                    and (now - last_flush) >= PARTIAL_FLUSH_INTERVAL_SECONDS
                ):
                    on_partial(text)
                    last_flush = now
    if on_partial:
        on_partial(text)
    return {
        "text": text,
        "prompt_tokens": int(prompt_tokens or estimate_tokens(prompt)),
        "completion_tokens": int(completion_tokens or estimate_tokens(text)),
        "model": model,
    }


# ---------------------------------------------------------------------------
# Inference (mock-only by default; real Ollama is opt-in)
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


# Min wall-clock between /jobs/partial pushes. Tight enough that the
# browser's typewriter looks live, loose enough that we don't hammer
# the coordinator with one POST per token on a fast model.
PARTIAL_FLUSH_INTERVAL_SECONDS = 0.25


def run_inference(
    prompt: str,
    model: Optional[str],
    log: logging.Logger,
    messages: Optional[list] = None,
    on_partial=None,
) -> dict:
    """Stream a response from local Ollama (or return a mock when
    OLLAMA_URL is unset). ``on_partial`` is invoked with the FULL
    accumulated text every ~250ms so the coordinator can serve
    progressive renders to the browser; a missed partial is harmless
    since the next one carries the full state and /jobs/complete is
    the source of truth.

    When ``messages`` is provided the call goes to ``/api/chat`` so
    the model's chat template is applied (proper multi-turn behavior).
    Single-shot prompts keep the legacy ``/api/generate`` path."""
    ollama_url = os.getenv("OLLAMA_URL")
    # Hardcoded fallback matches the v1.1.26 bootstrap default so a
    # caller that passes model=None still gets the model the agent
    # actually has loaded. In normal operation the caller threads
    # cfg.bootstrap_model in (see process_one), so this fallback
    # mainly catches misconfigured manual invocations.
    use_model = model or os.getenv("MODEL") or "llama3.2:3b"
    if not ollama_url:
        time.sleep(0.5)
        text = f"[mock] {prompt[:200]}"
        if on_partial:
            on_partial(text)
        return {
            "text": text,
            "prompt_tokens": estimate_tokens(prompt),
            "completion_tokens": estimate_tokens(text),
            "model": "mock",
        }
    if messages:
        endpoint = f"{ollama_url.rstrip('/')}/api/chat"
        payload = {"model": use_model, "messages": messages, "stream": True}
    else:
        endpoint = f"{ollama_url.rstrip('/')}/api/generate"
        payload = {"model": use_model, "prompt": prompt, "stream": True}
    text = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    try:
        last_flush = 0.0
        with httpx.Client(timeout=600) as c:
            with c.stream("POST", endpoint, json=payload) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # /api/chat streams {"message": {"content": "..."}};
                    # /api/generate streams {"response": "..."}. Read
                    # whichever shape the current chunk carries.
                    token = (
                        (chunk.get("message") or {}).get("content", "")
                        if messages
                        else chunk.get("response", "")
                    )
                    if token:
                        text += token
                    if chunk.get("done"):
                        prompt_tokens = chunk.get("prompt_eval_count")
                        completion_tokens = chunk.get("eval_count")
                        break
                    now = time.time()
                    if (
                        on_partial
                        and (now - last_flush) >= PARTIAL_FLUSH_INTERVAL_SECONDS
                    ):
                        on_partial(text)
                        last_flush = now
        if on_partial:
            on_partial(text)
        return {
            "text": text,
            "prompt_tokens": int(prompt_tokens or estimate_tokens(prompt)),
            "completion_tokens": int(completion_tokens or estimate_tokens(text)),
            "model": use_model,
        }
    except Exception as e:
        log.warning("ollama call failed (%s); falling back to mock", e)
        text = f"[mock-fallback] {prompt[:200]}"
        if on_partial:
            on_partial(text)
        return {
            "text": text,
            "prompt_tokens": estimate_tokens(prompt),
            "completion_tokens": estimate_tokens(text),
            "model": "mock",
        }


# ---------------------------------------------------------------------------
# Search inference (DuckDuckGo + optional fetch/extract + LLM summary)
# ---------------------------------------------------------------------------
# Number of DDG hits we ask for and then forward to the LLM as
# citations. Five is the sweet spot: enough breadth that the model can
# corroborate a fact across sources, few enough that the prompt fits
# inside a small chat model's context (~4k tokens including the
# extracted page bodies in comprehensive mode).
SEARCH_RESULT_COUNT = 5
# Hard ceiling on how much extracted text per page we forward to the
# LLM in comprehensive mode. A long-tail blog post can easily be
# 20k chars — passing all of it would blow the context window and
# slow inference dramatically. 2500 chars (~600 tokens) per page ×
# 5 pages = ~3k token system context, leaving room for history.
SEARCH_PAGE_CHAR_CAP = 2500
# Per-page fetch timeout. Trafilatura's fetch_url() default is much
# longer; a slow page shouldn't be allowed to wedge the whole search
# job. We accept whichever pages return in time and skip the rest —
# losing one source out of five is better than missing the deadline.
SEARCH_FETCH_TIMEOUT = 6.0


def _search_deps_available(log: logging.Logger) -> bool:
    """Probe import of the search-only dependencies (ddgs + trafilatura).
    Called once at startup to decide whether to advertise the 'search'
    tool. Logged so a contributor whose pyinstaller build dropped the
    wheels sees a clear "search not advertised" line in their log."""
    try:
        import ddgs  # noqa: F401
        import trafilatura  # noqa: F401
        return True
    except ImportError as e:
        log.info(
            "search tool not advertised — missing dep (%s); "
            "pip install ddgs trafilatura to enable",
            e,
        )
        return False


# Backend rotation order. ddgs 9.14.3 ships with eight free backends —
# each has its own rate-limit pool, so falling through on
# RatelimitException or an empty result multiplies our per-contributor
# capacity ~8×. Order matters: cheapest+most-reliable first, exotic
# last. DuckDuckGo first because that's what the user asked for; the
# others are de-facto failover.
_SEARCH_BACKENDS = (
    "duckduckgo", "mojeek", "brave", "bing", "yahoo", "startpage",
)
# In-process TTL cache for search results. A single worker handling
# back-to-back follow-ups on the same topic ("news on elon" → "what
# about boring company?" → "more on boring company") shouldn't burn
# fresh DDG calls when the queries overlap. cachetools is a pure-
# Python dep (~30KB) that bundles into pyinstaller without fuss.
_SEARCH_CACHE_TTL = 600  # 10 min
_SEARCH_CACHE_MAX = 512
try:
    from cachetools import TTLCache
    _search_cache: "TTLCache | None" = TTLCache(
        maxsize=_SEARCH_CACHE_MAX, ttl=_SEARCH_CACHE_TTL,
    )
except ImportError:
    _search_cache = None  # Cache silently disabled — search still works.


def _ddg_search(query: str, max_results: int = SEARCH_RESULT_COUNT) -> list[dict]:
    """Run a web search and return the raw hits, falling through a
    rotating list of free backends on rate-limit / empty results.

    Imported lazily so an agent build without the ddgs wheel installed
    can still serve chat/image jobs — only search jobs will fail at
    this point, with a clean error surface (see run_search_inference).

    Cache: same (query, max_results) hit within _SEARCH_CACHE_TTL
    seconds skips the network entirely. Misses populate the cache
    with whichever backend's hits we ended up using."""
    cache_key = (query, max_results)
    if _search_cache is not None and cache_key in _search_cache:
        return list(_search_cache[cache_key])

    from ddgs import DDGS
    try:
        from ddgs.exceptions import RatelimitException
    except ImportError:
        RatelimitException = Exception  # older ddgs builds

    last_err: Optional[Exception] = None
    for backend in _SEARCH_BACKENDS:
        try:
            with DDGS() as ddg:
                hits = ddg.text(
                    query, max_results=max_results, backend=backend,
                ) or []
            if hits:
                if _search_cache is not None:
                    _search_cache[cache_key] = list(hits)
                return hits
            # Empty results from one backend isn't fatal — DDG occasionally
            # returns 0 hits for a query Bing happily answers, and vice
            # versa. Try the next.
        except RatelimitException as e:
            last_err = e
            continue
        except Exception as e:
            # A backend-specific scrape break (HTML shape changed, 403,
            # network blip) shouldn't take the whole search down. Log
            # would be nice but we have no logger in scope; the caller
            # raises a clean RuntimeError when ALL backends fail.
            last_err = e
            continue
    if last_err is not None:
        raise last_err
    return []


def _fetch_and_extract(url: str, timeout: float = SEARCH_FETCH_TIMEOUT) -> str:
    """Best-effort fetch + clean-text extraction. Returns '' on any
    failure (network blip, JS-only page, 403, etc.) — the caller falls
    back to the DDG snippet for that source so the LLM still has
    something to cite."""
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={
                # Some sites 403 the default httpx UA; mimic a real
                # browser so we can extract their public content.
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
            },
        ) as c:
            resp = c.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception:
        return ""
    try:
        return (trafilatura.extract(html) or "").strip()
    except Exception:
        return ""


def _build_search_system_prompt(query: str, results: list[dict], mode: str) -> str:
    """Format the search results as a system message the LLM will use
    to ground its answer. Same ordering as ``results`` so the [1][2]
    citations the model emits line up with the sources list the client
    renders below the bubble. In comprehensive mode each entry also
    includes the extracted page body (capped); fast mode is snippets
    only."""
    lines = [
        "You are a search assistant. The user has asked a question and the",
        "system has retrieved fresh web results below. Use them — and only",
        "them — to answer concisely and accurately.",
        "",
        "Rules:",
        "- Cite sources inline as [1], [2], etc., matching the numbers below.",
        "- If the results don't contain enough information, say so plainly.",
        "- Prefer the most specific source for a given claim.",
        "- Do not invent URLs or facts that aren't in the results.",
        "",
        f"User query: {query}",
        "",
        "Search results:",
    ]
    for i, r in enumerate(results, start=1):
        title = (r.get("title") or "").strip()
        url = (r.get("href") or r.get("url") or "").strip()
        snippet = (r.get("body") or "").strip()
        lines.append(f"[{i}] {title}")
        lines.append(f"    URL: {url}")
        if snippet:
            lines.append(f"    Snippet: {snippet}")
        body = (r.get("_extracted") or "").strip() if mode == "comprehensive" else ""
        if body:
            lines.append(f"    Body: {body[:SEARCH_PAGE_CHAR_CAP]}")
        lines.append("")
    return "\n".join(lines)


def _domain_of(url: str) -> str:
    """Return ``example.com`` (no scheme, no www., no path) for the
    bubble-footer source labels. Falls back to the raw URL on any
    parse error so we still show *something* clickable."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or url
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return url


def run_search_inference(
    prompt: str,
    job: dict,
    model: Optional[str],
    log: logging.Logger,
    on_partial=None,
) -> dict:
    """Search + summarize. Runs a DuckDuckGo query, optionally fetches
    the top pages (comprehensive mode), assembles a citation-aware
    system prompt, and pipes everything to the local Ollama instance.

    Returns the same shape as ``run_inference`` plus a ``sources``
    list so the coordinator can stash it on JOB_RESULTS for the
    client's bubble-footer. Errors propagate up to process_one so the
    job records as status=error rather than silently masquerading as
    a complete-but-wrong chat answer."""
    search_env = job.get("search") or {}
    mode = (search_env.get("mode") or "fast").lower()
    if mode not in ("fast", "comprehensive"):
        mode = "fast"

    log.info("search job: mode=%s query=%r", mode, prompt[:120])
    try:
        results = _ddg_search(prompt, max_results=SEARCH_RESULT_COUNT)
    except Exception as e:
        # Bubble up — the coordinator records error and the user sees
        # a clear "search failed" message instead of a fabricated
        # answer with no grounding.
        raise RuntimeError(f"web search failed: {e}") from e

    if not results:
        # 0-results is treated as an error so the UI can prompt the
        # user to try a different query or uncheck search (matches the
        # design decision from the kickoff Q&A).
        raise RuntimeError(
            "no search results — try a different query or uncheck search"
        )

    # Comprehensive mode: fetch each source in parallel-ish (serial
    # for KISS; total budget = SEARCH_RESULT_COUNT * SEARCH_FETCH_TIMEOUT
    # = ~30s worst case, well inside JOB_TIMEOUT_SECONDS). A failed
    # fetch leaves the entry without _extracted so the prompt builder
    # gracefully falls back to the snippet for that source.
    if mode == "comprehensive":
        for r in results:
            url = r.get("href") or r.get("url") or ""
            if url:
                r["_extracted"] = _fetch_and_extract(url)

    system_text = _build_search_system_prompt(prompt, results, mode)

    # Reuse the conversation messages[] the coordinator built (if any)
    # so a follow-up search ("ok but what about in Europe?") keeps the
    # context. Inject the search-results system message at position 0;
    # any prior system messages from upstream stay after it so the
    # search context wins over generic instructions.
    base_messages = list(job.get("messages") or [])
    if not base_messages:
        base_messages = [{"role": "user", "content": prompt}]
    chat_messages = [{"role": "system", "content": system_text}] + base_messages

    summary = run_inference(
        prompt=prompt,
        model=model,
        log=log,
        messages=chat_messages,
        on_partial=on_partial,
    )

    sources = [
        {
            "n": i,
            "title": (r.get("title") or "").strip()
                     or _domain_of(r.get("href") or ""),
            "url": (r.get("href") or r.get("url") or "").strip(),
            "domain": _domain_of(r.get("href") or r.get("url") or ""),
        }
        for i, r in enumerate(results, start=1)
    ]

    return {
        "text": summary["text"],
        "prompt_tokens": summary["prompt_tokens"],
        "completion_tokens": summary["completion_tokens"],
        "model": summary["model"],
        "sources": sources,
    }


# ---------------------------------------------------------------------------
# Image inference (sd.cpp subprocess)
# ---------------------------------------------------------------------------
# How long sd.cpp is allowed to run before we give up. SD 1.5 at 512×512
# / 20 steps is ~5-30s on a modern GPU; SDXL / large batches push past
# a minute. The hard cap here is also coordinator JOB_TIMEOUT_SECONDS
# (default 120) — a worker still running past that has already lost
# the job to the reaper.
SD_TIMEOUT_SECONDS = 180


def run_image_inference(
    prompt: str,
    job: dict,
    cfg: "Config",
    log: logging.Logger,
) -> tuple[str, str]:
    """Generate one image with the bundled stable-diffusion.cpp binary
    and return (base64_png, model_used). Raises on subprocess error so
    process_one's outer try/except funnels it into a /jobs/complete
    error result the user sees in the bubble.

    Mock branch (non-Windows or sd.exe missing) returns a tiny solid-
    color PNG so dev/test on Linux can still exercise the end-to-end
    /jobs/complete + /images/{id} path. The mock PNG is deterministic
    so a test can byte-compare it.
    """
    if not IS_WINDOWS or not sd_binary_path().exists():
        log.info("image: sd.exe not available — returning mock PNG")
        return _mock_image_b64(), "mock-image"
    params = job.get("image") or {}
    model_slug = cfg.bootstrap_image_model
    # Pull per-model defaults from the sidecar JSON — LCM-distilled
    # models need step≈8 / cfg≈1.5 / sampler=lcm, vanilla SD1.5 wants
    # step≈20 / cfg≈7 / sampler=euler_a. Using the wrong numbers gives
    # mushy output (LCM at 20 steps) or wasted compute. Job-level
    # overrides still win.
    sd_defaults = load_sd_model_defaults(model_slug, log)
    width = int(params.get("width") or sd_defaults["default_width"])
    height = int(params.get("height") or sd_defaults["default_height"])
    steps = int(params.get("steps") or sd_defaults["default_steps"])
    cfg_scale = float(params.get("cfg_scale") or sd_defaults["default_cfg_scale"])
    sampler = str(params.get("sampler") or sd_defaults["default_sampler"])
    seed = params.get("seed")
    negative = params.get("negative_prompt") or ""
    init_image_b64 = params.get("init_image_b64")
    strength = params.get("strength")
    model_path = sd_model_path(model_slug)
    if not model_path.exists():
        raise RuntimeError(
            f"image model {model_slug}.gguf missing at {model_path}"
        )
    out_path = sd_install_dir() / f"out-{os.getpid()}-{int(time.time()*1000)}.png"
    # sd.cpp CLI: see https://github.com/leejet/stable-diffusion.cpp
    # `-M img_gen -m <gguf> -p <prompt> -W <w> -H <h> --steps N -o out.png`
    # NOTE: upstream consolidated txt2img / img2img / etc. into a
    # single `img_gen` mode in the master-637-ef92a00 build pinned by
    # infra/setup-image-mirror.sh. The old `txt2img` mode name was
    # rejected with "must be one of [img_gen, vid_gen, convert,
    # upscale, metadata]". If you bump the mirror's pinned sd.cpp
    # version, re-verify this flag still applies. img2img rides the
    # SAME mode — passing -i/--init-img is what switches it from
    # generate to edit; confirmed against the pinned build's own CLI
    # source (examples/common/common.cpp), since `sd-cli -h` isn't
    # available to check from a non-Windows dev box. -W/-H still
    # apply when -i is set: sd.cpp resizes the init image to them
    # rather than requiring an exact match.
    # The prompt is passed via a temp file (sd.cpp tolerates long
    # multi-line prompts that way) — using argv directly hits Windows'
    # 32K command-line cap on pathological inputs.
    prompt_file = out_path.with_suffix(".prompt.txt")
    prompt_file.write_text(prompt, encoding="utf-8")
    # Image alteration: the coordinator already validated + NSFW-
    # classified this (coordinator.main._validate_and_classify_init_image)
    # before the job ever reached a queue — decode failures here would
    # mean transit corruption or a non-standard caller, not a hostile
    # upload, so a clean RuntimeError (funneled to the same error-
    # bubble path as every other failure in this function) is the
    # right response, not a silent fallback to plain txt2img.
    init_image_path: Optional[Path] = None
    if init_image_b64:
        try:
            init_image_bytes = base64.b64decode(init_image_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise RuntimeError(f"init_image_b64 not valid base64: {e}") from e
        init_image_path = out_path.with_suffix(".init.png")
        # ``.init.png`` regardless of the source format (PNG or JPEG) —
        # sd.cpp's image loader sniffs actual file content, not the
        # extension, so this is cosmetic naming only.
        init_image_path.write_bytes(init_image_bytes)
    argv = [
        str(sd_binary_path()),
        "-M", "img_gen",
        "-m", str(model_path),
        "-p", prompt,
        "-W", str(width),
        "-H", str(height),
        "--steps", str(steps),
        "--cfg-scale", str(cfg_scale),
        "--sampling-method", sampler,
        # Tiled VAE decode. SDXL-class models at 1024² peak ~5.5–6 GB
        # of VRAM through the VAE step; without tiling, 6 GB cards
        # (1660 Ti / 3060 6 GB / 4050) OOM at decode time on an
        # otherwise-fine generation. Tiling lowers the peak by ~1 GB
        # at a small (~5 %) speed cost, which is the right trade for
        # the floor of our advertised contributor tier. Harmless on
        # SD 1.5 / dreamshaper8 (VAE fits well below the threshold).
        "--vae-tiling",
        "-o", str(out_path),
    ]
    if seed is not None:
        argv.extend(["--seed", str(int(seed))])
    if negative:
        argv.extend(["-n", negative])
    if init_image_path is not None:
        argv.extend(["-i", str(init_image_path)])
        # Omitted (not defaulted here) when the coordinator didn't
        # send one — sd.exe's own built-in default (0.75) applies.
        # Matches this codebase's standing rule against re-deciding
        # model defaults on the agent side (see the steps/cfg/sampler
        # sidecar-lookup comment above).
        if strength is not None:
            argv.extend(["--strength", str(float(strength))])
    log.info(
        "image: running sd.exe (model=%s %dx%d steps=%d cfg=%.1f sampler=%s%s)",
        model_slug, width, height, steps, cfg_scale, sampler,
        " edit" if init_image_path is not None else "",
    )
    try:
        # Hide stdout — sd.cpp is chatty with progress dots. We surface
        # stderr-on-error below.
        result = subprocess.run(
            argv,
            timeout=SD_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        try:
            prompt_file.unlink()
        except OSError:
            pass
        if init_image_path is not None:
            try:
                init_image_path.unlink()
            except OSError:
                pass
    if result.returncode != 0:
        snippet = (result.stderr or result.stdout or "").strip()[:500]
        # rc=3221226505 (0xC0000409 / STATUS_STACK_BUFFER_OVERRUN, seen
        # as -1073741623 on some Python/Windows builds) is a confirmed
        # crash in the bundled sd.exe's GGUF loader: the CRT's /GS
        # stack-cookie check kills the process while it's tallying
        # per-tensor weight dtypes at load time, on a model whose GGUF
        # mixes more distinct quant types (e.g. f16 + q4_K, as
        # dreamshaperXL-lightning does) than the loader's stack buffer
        # was sized for. Root-caused via Windows Event Viewer (WER
        # event 1000/1001, exception 0xC0000409 in ucrtbase.dll) on a
        # 2026-08-21 field test — it's a stable-diffusion.cpp bug in
        # the upstream binary, not a GPU driver/VRAM issue (crash
        # happens during model load, before any generation work or
        # TDR-sized GPU activity). Not fixable from here since we pull
        # a prebuilt release (see infra/setup-image-mirror.sh); this
        # just swaps the raw rc + truncated stdout for a message that
        # tells contributors what actually happened.
        if result.returncode in (3221226505, -1073741623):
            raise RuntimeError(
                f"sd.exe crashed loading {model_slug} (stack buffer "
                "overrun in the GGUF loader — a known stable-diffusion.cpp "
                "bug with this model's mixed-precision weights, not a "
                "driver or VRAM problem)"
            )
        raise RuntimeError(
            f"sd.exe failed (rc={result.returncode}): {snippet}"
        )
    if not out_path.exists():
        raise RuntimeError("sd.exe completed but produced no output PNG")
    try:
        data = out_path.read_bytes()
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass
    if not data:
        raise RuntimeError("sd.exe wrote an empty PNG")
    import base64 as _b64
    return _b64.b64encode(data).decode("ascii"), model_slug


# 1×1 transparent PNG — base64-encoded. Returned by the mock branch so
# the end-to-end coordinator+UI flow works on dev boxes without a GPU.
_MOCK_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _mock_image_b64() -> str:
    return _MOCK_PNG_B64


# ---------------------------------------------------------------------------
# TTS inference (Piper, CPU-only)
# ---------------------------------------------------------------------------

# Piper has a few-second startup hit per invocation (ONNX session
# warmup). For Phase 1 we eat it; Phase 2 will keep a long-lived piper
# subprocess and pipe text into it on demand to drop first-audio
# latency further.
TTS_TIMEOUT_SECONDS = 60


# Voice-mode chat sentence boundary. Ported verbatim from the client's
# VOICE_SENTENCE_RE at client/static/js/chat.js:48 — same shape on both
# sides keeps "sentence" defined identically wherever it's split. Used
# by the chat handler to detect the first complete sentence in the
# streaming LLM output so it can fire a CPU-side Piper synth in
# parallel with the rest of the response.
_VOICE_SENTENCE_RE = re.compile(r"""[.!?]+["')\]]*(?:\s+|$)|\n+""")


def find_first_sentence(text: str) -> tuple[Optional[str], int]:
    """Return ``(sentence, end_index)`` for the first complete sentence
    in ``text``, or ``(None, 0)`` if no terminator has arrived yet.

    ``end_index`` points just past the terminator's trailing whitespace,
    so ``text[end_index:]`` is the "rest of the response so far" that
    will be synthesized after the LLM completes."""
    if not text:
        return None, 0
    m = _VOICE_SENTENCE_RE.search(text)
    if m is None:
        return None, 0
    end = m.start() + len(m.group(0))
    sentence = text[:end].strip()
    if not sentence:
        return None, 0
    return sentence, end


# Minimum characters in the first voice-mode chunk. A 1-2 word
# response ("Yes.", "I agree.") produces a ~0.3s audio clip while
# Piper still pays its ~1.5s ONNX cold-start, so the cold-start
# becomes most of the perceived wait. Extending the first chunk past
# this threshold buys enough playback time to mask the cold-start
# AND give the second batch (typically 2 sentences) time to synthesize
# before the speaker runs out of audio. Later batches are sized
# exponentially and naturally exceed this, so the constraint only
# kicks in for seq=0.
VOICE_FIRST_CHUNK_MIN_CHARS = 40


def normalize_text_for_tts(text: str) -> str:
    """Strip markdown formatting before piping to Piper. Mirrors the
    client-side ``normalizeForTTS`` at client/static/js/readAloud.js:23
    so the manual speaker-icon path and the voice-mode chat path produce
    the same spoken output for the same model text. Without this Piper
    reads ``**bold**`` as "asterisk asterisk bold asterisk asterisk".
    """
    if not text:
        return ""
    s = text
    s = re.sub(r"```[\w-]*\s*\n?", "", s)
    s = s.replace("```", "")
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = re.sub(r"(\*|_)(?=\S)(.+?)(?<=\S)\1", r"\2", s)

    def _strip_atx(m: "re.Match[str]") -> str:
        body = m.group(1).strip()
        return body if re.search(r"[.!?:]$", body) else body + "."

    s = re.sub(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", _strip_atx, s, flags=re.MULTILINE)
    s = re.sub(r"^[ \t]*[-*+][ \t]+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^[ \t]*\d+\.[ \t]+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^[ \t]*>[ \t]?", "", s, flags=re.MULTILINE)
    s = re.sub(r"\n[ \t]*\n[\s]*", "\n\n", s)
    return s.strip()


def run_tts_inference(
    prompt: str,
    job: dict,
    cfg: "Config",
    log: logging.Logger,
) -> tuple[str, float, str]:
    """Synthesize ``prompt`` to WAV audio with piper.exe and return
    ``(base64_wav, audio_seconds, model_used)``. audio_seconds is the
    *playback* duration (what bills against the submitter's
    voice_minutes daily cap), measured from the WAV's data-chunk size
    and the voice's sample rate — never from wall-clock synthesis
    time, which would let a slow worker over-charge the user.

    Mock branch (non-Windows or piper.exe missing) returns a 1-second
    silent WAV so dev/test on Linux can still exercise the end-to-end
    coordinator → /result flow."""
    if not IS_WINDOWS or not tts_binary_path().exists():
        log.info("tts: piper.exe not available — returning mock silent WAV")
        return _mock_audio_b64(), 1.0, "mock-tts"
    voice_slug = cfg.bootstrap_tts_model
    defaults = load_tts_voice_defaults(voice_slug, log)
    sample_rate = int(defaults["sample_rate"])
    length_scale = float(defaults["default_length_scale"])
    noise_scale = float(defaults["default_noise_scale"])
    noise_w = float(defaults["default_noise_w"])
    voice_path = tts_voice_path(voice_slug)
    if not voice_path.exists():
        raise RuntimeError(
            f"tts voice {voice_slug}.onnx missing at {voice_path}"
        )
    out_path = (
        tts_install_dir() / f"out-{os.getpid()}-{int(time.time()*1000)}.wav"
    )
    # Piper CLI:
    #   piper --model voice.onnx --output_file out.wav \
    #         --length_scale L --noise_scale N --noise_w W
    # Reads text from stdin (one line = one utterance for our use).
    # See https://github.com/rhasspy/piper for the full flag set.
    argv = [
        str(tts_binary_path()),
        "--model", str(voice_path),
        "--output_file", str(out_path),
        "--length_scale", f"{length_scale:.3f}",
        "--noise_scale", f"{noise_scale:.3f}",
        "--noise_w", f"{noise_w:.3f}",
    ]
    log.info(
        "tts: running piper.exe (voice=%s len=%.2f noise=%.2f)",
        voice_slug, length_scale, noise_scale,
    )
    try:
        result = subprocess.run(
            argv,
            input=prompt,
            timeout=TTS_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        try:
            out_path.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"piper.exe timed out after {TTS_TIMEOUT_SECONDS}s"
        )
    if result.returncode != 0:
        snippet = (result.stderr or result.stdout or "").strip()[:500]
        raise RuntimeError(
            f"piper.exe failed (rc={result.returncode}): {snippet}"
        )
    if not out_path.exists():
        raise RuntimeError("piper.exe completed but produced no WAV output")
    try:
        data = out_path.read_bytes()
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass
    if not data:
        raise RuntimeError("piper.exe wrote an empty WAV")
    audio_seconds = _wav_duration_seconds(data, sample_rate)
    import base64 as _b64
    return _b64.b64encode(data).decode("ascii"), audio_seconds, voice_slug


def _wav_duration_seconds(wav_bytes: bytes, fallback_sample_rate: int) -> float:
    """Compute WAV playback duration from the file's data-chunk size
    and bytes-per-frame, using the stdlib wave module. Falls back to
    a sample-rate-only estimate if the header is malformed, since a
    bogus duration is better than crashing the job. The voice cap
    enforcement reads this number, so accuracy matters for billing —
    a worker that wrote a 5-second clip can't claim 50 seconds."""
    try:
        import io
        import wave
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or fallback_sample_rate
            if rate <= 0:
                return 0.0
            return frames / float(rate)
    except (wave.Error, EOFError, OSError):
        # Rough fallback: assume 16-bit mono at fallback rate. Off by
        # a constant factor if the voice is actually stereo or
        # different bit depth, but never wildly wrong.
        if fallback_sample_rate <= 0:
            return 0.0
        body_bytes = max(len(wav_bytes) - 44, 0)  # 44 = standard WAV header
        return body_bytes / float(fallback_sample_rate * 2)


# Minimal valid WAV: 44-byte header + 1s of silence at 22050 Hz mono
# 16-bit. Computed once at module load so the mock path doesn't
# rebuild it on every job. Used to keep the end-to-end /result flow
# testable on Linux dev boxes where piper.exe isn't present.
def _build_mock_wav() -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(b"\x00\x00" * 22050)
    return buf.getvalue()


_MOCK_WAV_BYTES = _build_mock_wav()
_MOCK_WAV_B64 = __import__("base64").b64encode(_MOCK_WAV_BYTES).decode("ascii")


def _mock_audio_b64() -> str:
    return _MOCK_WAV_B64


# ---------------------------------------------------------------------------
# Idle gate
# ---------------------------------------------------------------------------
def is_system_idle(cfg: Config, *, check_gpu: bool = True) -> tuple[bool, str]:
    """Decide whether the machine is free to claim a job.

    ``check_gpu`` gates the NVIDIA utilization check. The GPU loop
    passes True (don't load a model onto a GPU a game is using); the
    CPU-only TTS loop passes False, since the dual-loop design runs TTS
    on the CPU *in parallel* with a GPU chat job and must not be blocked
    by GPU activity.

    The game-process gate runs unconditionally — even under stayalive —
    because a running game is exactly the case stayalive must not be
    allowed to override (that footgun OOM'd a live VR session)."""
    idle_for = input_idle_seconds()
    if not cfg.stayalive and idle_for < cfg.min_input_idle_seconds:
        return False, f"user active ({idle_for:.0f}s since last input)"
    game = active_game_process(cfg.game_processes)
    if game:
        return False, f"game running ({game})"
    cpu = cpu_percent(cfg.cpu_sample_seconds)
    if cpu >= cfg.max_cpu_percent:
        return False, f"cpu busy ({cpu:.1f}%)"
    if check_gpu and cfg.max_gpu_percent > 0:
        gpu = gpu_busy_percent()
        if gpu is not None and gpu >= cfg.max_gpu_percent:
            return False, f"gpu busy ({gpu:.0f}%)"
    suffix = " stayalive" if cfg.stayalive else ""
    return True, f"idle ({idle_for:.0f}s, cpu {cpu:.1f}%{suffix})"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def print_earnings(state: dict, log: logging.Logger) -> None:
    # No $ figure here — we're not paying contributors yet, so the
    # terminal only shows jobs done, not an implied balance.
    log.info("Jobs completed: %d", int(state.get("jobs", 0)))


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


def _relaunch_in_place(
    log: logging.Logger, relaunch_args: Optional[list[str]] = None,
) -> bool:
    """Restart the running agent.exe in place — same binary, no download
    or swap — so a just-written config change is picked up by a fresh
    Config.load(). Used by the `smart` console command after it edits
    config.json.

    Writes a tiny detached batch that waits for THIS process's PID to
    disappear (so the new agent doesn't race the old one for the rpc /
    llama-server ports or a duplicate coordinator registration), then
    fires the same WMI relaunch line the updater uses. Returns True if the
    relaunch was scheduled — the caller is then responsible for signalling
    a clean shutdown. Returns False on non-Windows or a non-frozen (dev)
    build, where there's nothing to relaunch; the caller falls back to
    'restart to apply'.
    """
    exe = _agent_exe_path()
    if exe is None or not IS_WINDOWS:
        return False
    pid = os.getpid()
    relaunch_line = _ps_relaunch_line(exe, relaunch_args or [])
    bat = exe.with_name("smart-relaunch.bat")
    try:
        bat.write_text(
            "@echo off\r\n"
            ":: Auto-generated by the `smart` console command -- restarts\r\n"
            ":: agent.exe in place (no binary swap) to apply config.json.\r\n"
            "title GamerAI - applying smart-mode config\r\n"
            "echo.\r\n"
            "echo  Restarting agent to apply smart-mode config...\r\n"
            ":: Wait for the old agent (this PID) to fully exit so the new\r\n"
            ":: one can bind the rpc / llama-server ports cleanly.\r\n"
            ":waitloop\r\n"
            f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto waitloop\r\n"
            ")\r\n"
            + relaunch_line
            + "del \"%~f0\"\r\n",
            encoding="ascii",
        )
    # ValueError catches UnicodeEncodeError too: a .bat is interpreted in
    # the console's OEM code page, so its content must stay ASCII-clean.
    # A stray non-ASCII byte must degrade to the "restart to apply"
    # fallback, never kill the stdin thread (the v1.3.0 -> v1.3.1 fix).
    except (OSError, ValueError) as e:
        log.warning("smart: could not write smart-relaunch.bat (%s)", e)
        return False
    try:
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        )
        subprocess.Popen(
            ["cmd.exe", "/c", str(bat)],
            close_fds=True,
            creationflags=creationflags,
        )
    except Exception as e:
        log.warning("smart: could not launch smart-relaunch.bat (%s)", e)
        return False
    log.info("smart: in-place relaunch scheduled (pid=%s) — exiting agent", pid)
    return True


def _apply_smart_config(
    config_path: Optional[Path], updates: dict, log: logging.Logger,
) -> bool:
    """Merge ``updates`` into the ``smart`` block of the persistent
    operator-override file (operator_config_path) and write it back
    atomically. This file holds ONLY the keys the operator changed — it's
    layered on top of the bundled config at load time — so we read/merge
    the existing override, never the bundled config. That keeps the
    override a small delta and lets bundled updates (coordinator_url, new
    smart defaults) keep flowing through. Returns True on success."""
    if config_path is None:
        log.warning("smart: no config path known — cannot persist")
        return False
    try:
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
    except (OSError, ValueError) as e:
        log.warning("smart: could not read %s (%s)", config_path, e)
        return False
    smart = data.get("smart")
    if not isinstance(smart, dict):
        smart = {}
    smart.update(updates)
    data["smart"] = smart
    try:
        # The override dir (%APPDATA%\GamerAI) normally already exists
        # (state.json lives there), but create it defensively for a
        # first-ever write before any state has been saved.
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(config_path)
    except OSError as e:
        log.warning("smart: could not write %s (%s)", config_path, e)
        return False
    return True


def _parse_host_port(token: str, default_port: int) -> Optional[tuple[str, int]]:
    """Validate a ``host[:port]`` token (an rpc peer or a listen address).
    Returns (host, port) or None when it doesn't look like one — the
    caller prints usage rather than relaunching into a broken config.
    Splits on the LAST colon so IPv6 literals in brackets survive."""
    token = token.strip()
    if not token:
        return None
    if ":" in token:
        host, _, port_str = token.rpartition(":")
        host = host.strip()
        port_str = port_str.strip()
        if not host or not port_str.isdigit():
            return None
        port = int(port_str)
    else:
        host = token
        port = default_port
    if not (1 <= port <= 65535):
        return None
    return host, port


_STDIN_HELP = (
    "commands:\n"
    "  update          download the latest agent.exe and relaunch\n"
    "  version         print the running version + build\n"
    "  status          print worker_id and current status\n"
    "  stayalive       toggle the user-input idle gate (or use 'on'/'off')\n"
    "  stayalive on    ignore user-input idleness (CPU-only gate)\n"
    "  stayalive off   restore normal user-input idle gate\n"
    "  smart status    print the current smart-mode config\n"
    "  smart backend   make this the backend (rpc-server); writes config + restarts\n"
    "  smart head IP:PORT [MODEL]\n"
    "                  make this the head, with the backend at IP:PORT\n"
    "  smart off       disable smart mode; writes config + restarts\n"
    "  help            show this list\n"
    "  quit            exit the agent\n"
    "(smart * is a temporary convenience for bringing the pipeline up\n"
    " from the console - it edits config.json and restarts the agent.)\n"
)


def stdin_command_loop(
    log: logging.Logger,
    worker_id: str,
    force_update_event: threading.Event,
    stop_event: threading.Event,
    cfg: "Config",
    state: dict,
    config_path: Optional[Path] = None,
    relaunch_args: Optional[list[str]] = None,
) -> None:
    """Read line-oriented commands from the console window. Runs in
    both foreground and tray modes — in tray mode the console starts
    hidden but stdin remains attached, so reads block silently until
    the user clicks "Show console" and types. The thread is a daemon
    so a stuck read doesn't keep the process alive on shutdown.

    Recognized commands are listed in ``_STDIN_HELP``. Unknown input
    is echoed back with a hint instead of crashing the loop, because a
    contributor poking at the window should never lose the agent."""
    try:
        sys.stdout.write(
            "Type 'help' for commands; 'update' pulls the latest "
            "agent.exe and relaunches.\n"
        )
        sys.stdout.flush()
    except Exception:
        pass
    while not stop_event.is_set():
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if not line:
            # EOF (console closed) — leave it alone; the main loop's
            # KeyboardInterrupt / parent-exit handling will clean up.
            return
        cmd = line.strip().lower()
        if not cmd:
            continue
        if cmd in ("update", "u"):
            force_update_event.set()
            try:
                sys.stdout.write(
                    "update requested — downloading latest agent.exe; "
                    "the window will close and relaunch when ready.\n"
                )
                sys.stdout.flush()
            except Exception:
                pass
        elif cmd in ("version", "v"):
            try:
                sys.stdout.write(
                    f"GamerAI agent v{AGENT_VERSION} (build {current_version()})\n"
                )
                sys.stdout.flush()
            except Exception:
                pass
        elif cmd == "status":
            try:
                sys.stdout.write(
                    f"worker_id={worker_id} v{AGENT_VERSION} "
                    f"(build {current_version()})\n"
                )
                sys.stdout.flush()
            except Exception:
                pass
        elif cmd.startswith("stayalive"):
            # Accept: "stayalive" (toggle), "stayalive on", "stayalive off".
            parts = cmd.split()
            arg = parts[1] if len(parts) > 1 else ""
            if arg in ("on", "true", "1", "yes"):
                new_value = True
            elif arg in ("off", "false", "0", "no"):
                new_value = False
            elif arg == "":
                new_value = not cfg.stayalive
            else:
                try:
                    sys.stdout.write(
                        f"stayalive: unknown argument {arg!r} — "
                        f"use 'on' / 'off' / nothing-to-toggle\n"
                    )
                    sys.stdout.flush()
                except Exception:
                    pass
                continue
            cfg.stayalive = new_value
            state["stayalive"] = new_value
            try:
                save_state(state)
            except Exception as exc:
                log.warning("stayalive: state save failed (%s)", exc)
            log.info("stayalive %s (via stdin command)", "ON" if new_value else "OFF")
            try:
                sys.stdout.write(
                    f"stayalive: {'ON' if new_value else 'OFF'} "
                    f"(persisted to state.json)\n"
                )
                sys.stdout.flush()
            except Exception:
                pass
        elif cmd.startswith("smart"):
            # Temporary convenience for the rollout: configure + enable
            # smart mode (or turn it off) from the console, then restart
            # in place so a fresh Config.load() brings the pipeline up.
            # role/peers differ per machine, so this writes the whole
            # block rather than flipping a single flag. Args are parsed
            # from the original-case line — IPs and model names must not
            # be lowercased.
            raw_parts = line.strip().split()
            sub = raw_parts[1].lower() if len(raw_parts) > 1 else "status"

            def _emit(msg: str) -> None:
                try:
                    sys.stdout.write(msg)
                    sys.stdout.flush()
                except Exception:
                    pass

            if sub == "status":
                peers = ", ".join(cfg.smart_rpc_peers) or "(none)"
                _emit(
                    f"smart mode: {'ENABLED' if cfg.smart_enabled else 'disabled'}\n"
                    f"  role   = {cfg.smart_role}\n"
                    f"  model  = {cfg.smart_model}\n"
                    f"  peers  = {peers}\n"
                    f"  listen = {cfg.smart_rpc_listen_host}:"
                    f"{cfg.smart_rpc_listen_port}\n"
                )
                continue

            updates: Optional[dict] = None
            summary = ""
            if sub == "backend":
                # Optional HOST:PORT override for what the rpc-server binds;
                # default to the configured/standard listen address.
                if len(raw_parts) > 2:
                    hp = _parse_host_port(raw_parts[2], cfg.smart_rpc_listen_port)
                    if hp is None:
                        _emit(
                            f"smart: bad listen address {raw_parts[2]!r} - "
                            f"want HOST:PORT\n"
                        )
                        continue
                    host, port = hp
                else:
                    host, port = cfg.smart_rpc_listen_host, cfg.smart_rpc_listen_port
                updates = {
                    "enabled": True,
                    "role": "backend",
                    "rpc_listen_host": host,
                    "rpc_listen_port": port,
                }
                summary = f"role=backend, listen {host}:{port}"
            elif sub == "head":
                if len(raw_parts) < 3:
                    _emit(
                        "smart: 'smart head' needs the backend address - "
                        "e.g. 'smart head 192.168.1.50:50052 [model]'\n"
                    )
                    continue
                # One or more comma-separated IP:PORT backends.
                peers: Optional[list[str]] = []
                for tok in raw_parts[2].split(","):
                    hp = _parse_host_port(tok, cfg.smart_rpc_listen_port)
                    if hp is None:
                        _emit(f"smart: bad peer {tok!r} - want IP:PORT\n")
                        peers = None
                        break
                    peers.append(f"{hp[0]}:{hp[1]}")
                if peers is None:
                    continue
                # Optional model override (positional, after the peer
                # token); else SMART_MODEL env (the testing knob from the
                # docs), else whatever config already resolves to.
                model = (
                    raw_parts[3] if len(raw_parts) > 3
                    else os.getenv("SMART_MODEL") or cfg.smart_model
                )
                updates = {
                    "enabled": True,
                    "role": "head",
                    "rpc_peers": peers,
                    "model": model,
                }
                summary = f"role=head, model={model}, peers {', '.join(peers)}"
            elif sub in ("off", "disable", "false"):
                updates = {"enabled": False}
                summary = "disabled"
            else:
                _emit(
                    f"smart: unknown subcommand {sub!r} - use "
                    f"'status' / 'backend' / 'head IP:PORT' / 'off'\n"
                )
                continue

            if not _apply_smart_config(config_path, updates, log):
                _emit("smart: failed to write config.json (see log) - no change\n")
                continue
            log.info("smart: wrote %s to config (via stdin command)", summary)
            _emit(f"smart: saved {summary} (persisted in %APPDATA%\\GamerAI)\n")
            if _relaunch_in_place(log, relaunch_args):
                _emit("smart: restarting to apply - the window will reopen.\n")
                stop_event.set()
                return
            _emit(
                "smart: config written - restart the agent (close & reopen, "
                "or type 'update') to apply.\n"
            )
        elif cmd in ("help", "?", "h"):
            try:
                sys.stdout.write(_STDIN_HELP)
                sys.stdout.flush()
            except Exception:
                pass
        elif cmd in ("quit", "exit", "q"):
            log.info("quit requested from console")
            stop_event.set()
            return
        else:
            try:
                sys.stdout.write(
                    f"unknown command: {cmd!r} (type 'help')\n"
                )
                sys.stdout.flush()
            except Exception:
                pass


def main_loop(
    cfg: Config,
    coord: Coordinator,
    state: dict,
    log: logging.Logger,
    once: bool,
    should_exit=lambda: False,
    tools: Optional[list[str]] = None,
    smart_rt: Optional["SmartRuntime"] = None,
) -> None:
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

        if now - last_earnings_print > cfg.earnings_print_seconds:
            print_earnings(state, log)
            last_earnings_print = now

        # Uptime-schedule gate. The coordinator is authoritative (it
        # won't dispatch outside the window), but self-gating here means
        # we don't poll /jobs/next or even sample CPU/GPU while sleeping.
        # The heartbeat thread keeps beating (slowly) and refreshes this
        # flag, so a window edit in the web UI flips us back on within
        # the downtime poll interval.
        allowed, sleeping_until = coord.schedule_state()
        if not allowed:
            just_drained_job_id = None
            coord.set_offline()
            until = f" until {sleeping_until}" if sleeping_until else ""
            emit_status("offline", f"scheduled off{until}")
            time.sleep(cfg.polling_interval)
            continue

        idle, reason = is_system_idle(cfg)
        if not idle:
            if just_drained_job_id is not None:
                log.info(
                    "user activity detected (%s) — last job %s complete, agent offline",
                    reason, just_drained_job_id,
                )
                just_drained_job_id = None
            coord.set_offline()
            emit_status("offline", reason)
            time.sleep(cfg.polling_interval)
            continue

        # Idle. If we just came off a job we'd previously logged the
        # drain message, clear the breadcrumb so the next user-active
        # transition doesn't reference a stale job_id.
        just_drained_job_id = None
        coord.set_idle()
        emit_status("idle", reason)

        did_work, processed_job_id = process_one(
            cfg, coord, state, log, tools=tools, smart_rt=smart_rt,
        )
        if did_work:
            just_drained_job_id = processed_job_id
        if once and did_work:
            return
        # No post-process_one sleep: process_one's long-poll inside
        # /jobs/next already blocked for LONG_POLL_WAIT_SECONDS when
        # there was no work, so the next loop tick can dive straight
        # back in. The user-active branch above still uses
        # cfg.polling_interval — that's a check cadence, not a wait
        # for jobs.


def _ordered_queues(
    tools: Optional[list[str]], last_tool: Optional[str],
) -> list[str]:
    """Per-tool poll order for one main-loop tick.

    Defaults to ["chat"]; appends "image" / "search" when the agent
    advertises them. Promotes the agent's *last-served* tool to the
    front so back-to-back jobs of the same kind keep the model warm
    in VRAM — a chat streak keeps Ollama hot; an image streak keeps
    sd.cpp's weights loaded; a search streak skips the cold-cache hit
    on trafilatura's lazy imports. The flip only happens when the
    preferred queue is empty AND another queue has work, so demand-
    shaped routing still falls out naturally without any coordinator-
    side state.

    "tts" is intentionally NOT included here even when the agent
    advertises it: the dedicated CPU loop (tts_loop) is the sole
    consumer of job_queue:tts, so a worker mid-chat can serve TTS in
    parallel on its idle CPU rather than queueing TTS behind a 10 s
    chat. See voice-phase1 design memory for the latency walkthrough
    that motivates the dual-loop architecture.

    A smart-pipeline head advertises ["chat:smart"] INSTEAD of "chat"
    (its VRAM belongs to the 14B shard, so the 3B canonical isn't
    loaded), and a smart backend advertises no GPU tools at all — in
    that case this returns [] and the main loop just idles between
    heartbeats while rpc-server lends the GPU to the head."""
    gpu_tools = ("chat", "chat:smart", "image", "search")
    available = [t for t in (tools if tools is not None else ["chat"])
                 if t in gpu_tools]
    if last_tool in available and last_tool != available[0]:
        return [last_tool] + [t for t in available if t != last_tool]
    return available


def process_one(
    cfg: Config,
    coord: Coordinator,
    state: dict,
    log: logging.Logger,
    tools: Optional[list[str]] = None,
    smart_rt: Optional["SmartRuntime"] = None,
) -> tuple[bool, Optional[str]]:
    """Pop, claim, run, complete one job. Returns (did_work, job_id).
    The job_id is captured so the main loop can reference it in the
    next iteration's drain-visibility log line.

    Uses long-poll: the coordinator BLPOPs across the agent's tool
    queues for LONG_POLL_WAIT_SECONDS, so when /generate enqueues a
    job we wake up within one network round-trip rather than waiting
    for the next polling interval. Returns ``(False, None)`` when the
    window expires with no work — main loop falls through without an
    additional sleep (the wait already happened)."""
    queue_order = _ordered_queues(tools, state.get("last_tool"))
    # Don't claim smart jobs while the pipeline is down (llama-server
    # crashed / mid-restart) — the watchdog flips ready() back on once
    # it's healthy again. Better that jobs wait on the queue than get
    # claimed and errored.
    if "chat:smart" in queue_order and (smart_rt is None or not smart_rt.ready()):
        queue_order = [t for t in queue_order if t != "chat:smart"]
    if not queue_order:
        # Smart backend (no GPU tools) or smart head mid-restart:
        # nothing to poll this tick. Sleep one polling interval so the
        # main loop doesn't spin.
        time.sleep(cfg.polling_interval)
        return False, None
    job = coord.next_job(tools=queue_order, wait=LONG_POLL_WAIT_SECONDS)
    if not job:
        return False, None
    job_id = job.get("job_id")
    tool = (job.get("tool") or "chat").lower()
    prompt = job.get("prompt", "")
    # /jobs/next now atomically claims the job and returns a
    # per-claim secret. Stash it locally so subsequent partial/
    # complete/abandon calls can prove they're still the rightful
    # claimant — a 410 on any of these means the reaper requeued
    # or the user cancelled, in which case we skip local credit.
    claim_token = job.pop("_claim_token", None)
    log.info("job %s started (tool=%s)", job_id, tool)
    coord.set_busy(job_id)

    if cfg.override_drain:
        idle_now, reason = is_system_idle(cfg)
        if not idle_now:
            log.info(
                "override-drain: %s — abandoning job %s (earnings forfeited)",
                reason, job_id,
            )
            coord.abandon(job_id, claim_token=claim_token)
            coord.set_offline()
            return False, None

    started = time.time()
    try:
        if tool == "image":
            image_b64, model_used = run_image_inference(
                prompt, job, cfg, log,
            )
            duration = round(time.time() - started, 3)
            accepted, out = coord.complete(
                {
                    "worker_id": coord.worker_id,
                    "job_id": job_id,
                    "text": prompt[:200],
                    "model": model_used,
                    "prompt_tokens": estimate_tokens(prompt),
                    "completion_tokens": 0,
                    "duration_seconds": duration,
                    "status": "complete",
                    "image_b64": image_b64,
                },
                claim_token=claim_token,
            )
            if accepted:
                earnings = float((out or {}).get("earnings", 0.0))
                credit_completed_job(
                    state, last_tool="image", earnings=earnings,
                )
                log.info(
                    "job %s finished (image): %.2fs",
                    job_id, duration,
                )
            else:
                log.info(
                    "job %s (image): %.2fs of work discarded — "
                    "claim was superseded or cancelled",
                    job_id, duration,
                )
        else:
            # Model resolution precedence:
            #  1. cfg.model — operator's explicit pin in config.json
            #     (usually None)
            #  2. job.get("model") — coordinator's per-job pin
            #     (None for the rewrite job; set for retried-with-
            #     model-pinned conversations)
            #  3. cfg.bootstrap_model — what the agent actually has
            #     loaded in Ollama. The right default; without this
            #     the rewrite job would fall through to run_inference's
            #     hardcoded backstop on misconfigured installs. On a
            #     smart-pipeline head the loaded model is the smart one,
            #     not the Ollama canonical.
            smart_head = smart_rt is not None and smart_rt.role == "head"
            use_model = cfg.model or job.get("model") or (
                cfg.smart_model if smart_head else cfg.bootstrap_model
            )
            # Voice-mode chat pipelining (Phase A). When the job carries
            # voice_mode=true, watch the streaming LLM output for the
            # first complete sentence and fire a parallel Piper synth
            # on the CPU side as soon as one is detected. The audio
            # gets pushed to the coordinator on a partial so the client
            # can start playback before the LLM finishes. The "rest"
            # of the response is synthesized synchronously below after
            # run_inference returns. Search jobs ignore voice_mode —
            # the streaming portion of a search response is just the
            # final-summary LLM call, and the search-mode UX isn't a
            # candidate for voice in Phase A.
            voice_mode = bool(job.get("voice_mode")) and tool == "chat"
            # Voice-mode chat exponential batching (Phase B). State is
            # shared between the GPU thread (on_partial detects sentence
            # boundaries and enqueues batches) and a single CPU-side
            # synth worker that drains the queue serially through Piper.
            # Batch sizes double: 1, 2, 4, 8, ... — first audio plays
            # fast and later batches have more playback time to absorb
            # their longer Piper synth. Every transition logs a wall-
            # clock delta from t0 so the gap source is observable from
            # the agent log alone (see Phase A field report where 40s
            # of "rest" synth made the feature feel broken).
            voice_t0 = time.time()
            voice_state: dict = {
                "cursor": 0,            # text-index past last enqueued sentence
                "next_batch_size": 1,
                "next_seq": 0,
                "queue": queue.Queue(),
                "current_text": "",
                "audio_chunks": [],     # collected for /complete payload
                "chunks_lock": threading.Lock(),
                "drained": threading.Event(),
                "first_sentence_t": None,
                "thread": None,
            }

            def _voice_synth_worker() -> None:
                # Single CPU-side consumer that pulls batches off the
                # queue in order and runs Piper sequentially. Sequential
                # because concurrent piper.exe spawns just thrash CPU
                # and each subprocess pays the ONNX cold-start anyway —
                # warm-Piper is the Phase C fix for that. Each completed
                # synth posts a partial with the chunk attached so the
                # client picks it up on its next /result poll.
                while True:
                    item = voice_state["queue"].get()
                    if item is None:
                        voice_state["drained"].set()
                        return
                    seq, batch_text = item
                    t_start = time.time()
                    try:
                        spoken = normalize_text_for_tts(batch_text)
                        if not spoken:
                            continue
                        log.info(
                            "voice-mode chunk synth start | seq=%d | chars=%d | t+%.2fs",
                            seq, len(spoken), t_start - voice_t0,
                        )
                        b64, secs, _model = run_tts_inference(
                            spoken, {"job_id": job_id}, cfg, log,
                        )
                        t_done = time.time()
                        log.info(
                            "voice-mode chunk synth done  | seq=%d | %.2fs synth | %.2fs audio | t+%.2fs",
                            seq, t_done - t_start, secs, t_done - voice_t0,
                        )
                        with voice_state["chunks_lock"]:
                            voice_state["audio_chunks"].append({
                                "seq": seq,
                                "audio_b64": b64,
                                "audio_seconds": secs,
                            })
                        try:
                            coord.partial(
                                job_id, voice_state["current_text"],
                                claim_token=claim_token,
                                audio_chunk_b64=b64,
                                audio_chunk_seconds=secs,
                                audio_chunk_seq=seq,
                            )
                            log.info(
                                "voice-mode chunk posted     | seq=%d | t+%.2fs",
                                seq, time.time() - voice_t0,
                            )
                        except Exception as post_exc:
                            log.warning(
                                "voice-mode chunk %d post failed: %s",
                                seq, post_exc,
                            )
                    except Exception as e:
                        log.warning("voice-mode chunk %d synth failed: %s", seq, e)
                    finally:
                        voice_state["queue"].task_done()

            def _enqueue_batch_if_ready(text: str) -> None:
                # Try to slice out the next exponential batch from text.
                # The Nth batch needs next_batch_size more completed
                # sentences past cursor. For the very first batch we
                # ALSO require at least VOICE_FIRST_CHUNK_MIN_CHARS of
                # text so a one-word lead-in ("Sure!") doesn't get
                # synthesized alone behind Piper's cold-start. Later
                # batches grow exponentially and exceed the threshold
                # naturally, so this only adds work on seq=0.
                unsynth = text[voice_state["cursor"]:]
                n = voice_state["next_batch_size"]
                min_chars = (
                    VOICE_FIRST_CHUNK_MIN_CHARS
                    if voice_state["next_seq"] == 0
                    else 0
                )
                pos = 0
                found = 0
                end_in_unsynth: Optional[int] = None
                while pos < len(unsynth):
                    m = _VOICE_SENTENCE_RE.search(unsynth[pos:])
                    if not m:
                        break
                    end = pos + m.start() + len(m.group(0))
                    found += 1
                    if found >= n and end >= min_chars:
                        end_in_unsynth = end
                        break
                    pos = end
                if end_in_unsynth is None:
                    return
                batch_text = unsynth[:end_in_unsynth].strip()
                if not batch_text:
                    return
                voice_state["cursor"] += end_in_unsynth
                seq = voice_state["next_seq"]
                voice_state["next_seq"] += 1
                voice_state["queue"].put((seq, batch_text))
                now = time.time()
                if voice_state["first_sentence_t"] is None:
                    voice_state["first_sentence_t"] = now
                    log.info(
                        "voice-mode first sentence found | seq=%d | chars=%d | t+%.2fs",
                        seq, len(batch_text), now - voice_t0,
                    )
                else:
                    log.info(
                        "voice-mode batch ready          | seq=%d | sentences=%d | chars=%d | t+%.2fs",
                        seq, n, len(batch_text), now - voice_t0,
                    )
                voice_state["next_batch_size"] *= 2

            def on_partial(text: str) -> None:
                voice_state["current_text"] = text
                coord.partial(job_id, text, claim_token=claim_token)
                if voice_mode:
                    # A single partial may carry text past multiple batch
                    # boundaries (e.g., the LLM emitted 4 sentences while
                    # on_partial slept 250ms and we're already at batch
                    # size 2). Loop so each batch goes out as the text
                    # crosses its sentence count.
                    while True:
                        prev_seq = voice_state["next_seq"]
                        _enqueue_batch_if_ready(text)
                        if voice_state["next_seq"] == prev_seq:
                            break

            if voice_mode:
                log.info(
                    "voice-mode chat job claimed   | job=%s | t+0.00s",
                    job_id,
                )
                voice_state["thread"] = threading.Thread(
                    target=_voice_synth_worker, daemon=True,
                )
                voice_state["thread"].start()

            if tool == "search":
                # Search jobs do the DDG fetch + extract first, then
                # pipe the assembled context through the same Ollama
                # streaming path so the user sees the summary appear
                # progressively (the search step itself takes 1-5s and
                # is logged but not streamed — there's nothing to type
                # out yet).
                result = run_search_inference(
                    prompt, job, use_model, log, on_partial=on_partial,
                )
            elif smart_head:
                # Pipeline head: every chat-tool job this worker claims
                # (it only polls chat:smart) runs against the local
                # llama-server, which splits the model across this GPU
                # + the rpc backends. Raises on failure → the generic
                # error path below reports it instead of mocking.
                result = run_smart_inference(
                    prompt,
                    use_model,
                    log,
                    endpoint=smart_rt.endpoint,
                    messages=job.get("messages"),
                    on_partial=on_partial,
                )
            else:
                result = run_inference(
                    prompt,
                    use_model,
                    log,
                    messages=job.get("messages"),
                    on_partial=on_partial,
                )
            duration = round(time.time() - started, 3)
            if not (result.get("text") or "").strip():
                # Ollama's streaming endpoint can return an HTTP 200
                # with a `done: true` chunk and zero content tokens
                # when generation dies mid-request server-side (GPU
                # driver timeout/reset, OOM, model crash) rather than
                # raising — run_inference has nothing to except on, so
                # it hands back a clean-looking empty string. Reporting
                # that as status="complete" produces a silent blank
                # reply the user can't tell apart from a real failure
                # and can't retry. Raise instead so the outer except
                # below reports status="error" — retryable, and logged.
                raise RuntimeError(
                    f"{tool} inference returned no text (likely a "
                    "local Ollama/GPU hiccup mid-generation, not a "
                    "real empty answer)"
                )
            complete_payload = {
                "worker_id": coord.worker_id,
                "job_id": job_id,
                "text": result["text"],
                "model": result["model"],
                "prompt_tokens": result["prompt_tokens"],
                "completion_tokens": result["completion_tokens"],
                "duration_seconds": duration,
                "status": "complete",
            }
            if result.get("sources"):
                complete_payload["sources"] = result["sources"]
            # Voice-mode finishing: after the LLM is done, run on_partial
            # one last time on the FINAL text so any trailing batch the
            # 250ms-cadence callback missed gets enqueued. Then drain
            # whatever text sits past the cursor as a final undersized
            # chunk (the last batch might want 16 sentences but we only
            # produced 11 — synth those 11 immediately, don't wait).
            # Finally poison-pill the worker and wait for it to drain.
            if voice_mode:
                full_text = result.get("text") or ""
                voice_state["current_text"] = full_text
                # Drain any remaining sentences. _enqueue_batch_if_ready
                # advances cursor, so loop until it can no longer find
                # next_batch_size more sentences.
                while True:
                    prev = voice_state["next_seq"]
                    _enqueue_batch_if_ready(full_text)
                    if voice_state["next_seq"] == prev:
                        break
                # Leftover past cursor: enqueue regardless of count, so
                # the last few sentences (or trailing fragment) still
                # get synthesized. Or, if no boundary ever fired, this
                # is the whole short response collapsed into chunk 0.
                leftover = full_text[voice_state["cursor"]:].strip()
                if leftover:
                    seq = voice_state["next_seq"]
                    voice_state["next_seq"] += 1
                    voice_state["queue"].put((seq, leftover))
                    log.info(
                        "voice-mode final leftover queued | seq=%d | chars=%d | t+%.2fs",
                        seq, len(leftover), time.time() - voice_t0,
                    )
                # Poison pill + wait for drain. Generous timeout: a
                # long final chunk on a slow CPU can take ~30s. We
                # bound at 8× TTS_TIMEOUT_SECONDS to avoid hanging the
                # job loop on a wedged Piper.
                voice_state["queue"].put(None)
                voice_state["drained"].wait(timeout=TTS_TIMEOUT_SECONDS * 8)
                with voice_state["chunks_lock"]:
                    chunks = list(voice_state["audio_chunks"])
                chunks.sort(key=lambda c: c["seq"])
                if chunks:
                    complete_payload["audio_chunks"] = chunks
                total_audio = sum(c.get("audio_seconds", 0.0) for c in chunks)
                log.info(
                    "voice-mode all chunks done    | count=%d | total_audio=%.2fs | t+%.2fs",
                    len(chunks), total_audio, time.time() - voice_t0,
                )
            accepted, out = coord.complete(
                complete_payload,
                claim_token=claim_token,
            )
            if accepted:
                earnings = float((out or {}).get("earnings", 0.0))
                credit_completed_job(
                    state,
                    last_tool=tool,
                    earnings=earnings,
                    tokens=int(result["completion_tokens"]),
                )
                log.info(
                    "job %s finished (%s): %d tokens, %.2fs",
                    job_id, tool, result["completion_tokens"], duration,
                )
            else:
                log.info(
                    "job %s (%s): %d tokens of work discarded — "
                    "claim was superseded or cancelled",
                    job_id, tool, result["completion_tokens"],
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
            },
            claim_token=claim_token,
        )
    finally:
        coord.set_idle()
    return True, job_id


# ---------------------------------------------------------------------------
# CPU TTS loop (voice-phase1 dual-loop architecture)
#
# Runs concurrently with the GPU loop (main_loop above) so a worker
# mid-chat can serve TTS jobs from its idle CPU instead of queueing
# them behind the multi-second chat job. See voice-phase1 design
# memory for the worked latency comparison that motivated this.
#
# This loop intentionally does NOT touch coord.set_busy / set_idle —
# those reflect the GPU loop's state for the heartbeat-extension
# protocol the reaper relies on for long chat/image jobs. TTS jobs
# are short enough (~1-2 s of Piper synthesis) that JOB_TIMEOUT_SECONDS
# (120s) is never even close, so they don't need reaper extension.
# ---------------------------------------------------------------------------

def tts_loop(
    cfg: "Config",
    coord: "Coordinator",
    state: dict,
    log: logging.Logger,
    stop_event: threading.Event,
) -> None:
    """Drive the CPU TTS loop until stop_event is set. Same idle-gate
    semantics as the GPU loop: when the user is active, defer
    polling so the contributor's box isn't audibly cranking through
    TTS jobs while they're trying to game."""
    log.info("tts loop started (cpu-only, voice-phase1 dual-loop)")
    while not stop_event.is_set():
        # check_gpu=False: TTS is CPU-only and runs in parallel with a
        # GPU chat job, so a busy GPU must not gate it.
        idle, _reason = is_system_idle(cfg, check_gpu=False)
        if not idle:
            # GPU loop's main_loop handles the user-visible offline
            # message + WORKER_STATUS update — we just back off here.
            stop_event.wait(cfg.polling_interval)
            continue
        try:
            process_tts_one(cfg, coord, state, log)
        except Exception:
            log.exception("tts loop tick failed")
            stop_event.wait(cfg.polling_interval)
    log.info("tts loop stopped")


def process_tts_one(
    cfg: "Config",
    coord: "Coordinator",
    state: dict,
    log: logging.Logger,
) -> None:
    """One iteration of the CPU TTS loop. Long-polls only on
    job_queue:tts (via tools=["tts"]) so it never claims a chat or
    image job — those belong to the GPU loop."""
    job = coord.next_job(tools=["tts"], wait=LONG_POLL_WAIT_SECONDS)
    if not job:
        return
    job_id = job.get("job_id")
    prompt = job.get("prompt", "")
    claim_token = job.pop("_claim_token", None)
    log.info("tts job %s started (cpu loop)", job_id)

    if cfg.override_drain:
        idle_now, reason = is_system_idle(cfg, check_gpu=False)
        if not idle_now:
            log.info(
                "override-drain: %s — abandoning tts job %s "
                "(earnings forfeited)",
                reason, job_id,
            )
            coord.abandon(job_id, claim_token=claim_token)
            return

    started = time.time()
    try:
        audio_b64, audio_seconds, model_used = run_tts_inference(
            prompt, job, cfg, log,
        )
        duration = round(time.time() - started, 3)
        accepted, out = coord.complete(
            {
                "worker_id": coord.worker_id,
                "job_id": job_id,
                # Echo the spoken text back so the admin view has a
                # human-readable handle. Bounded so a long-paragraph
                # TTS job doesn't bloat the JOB_RESULTS payload.
                "text": prompt[:200],
                "model": model_used,
                "prompt_tokens": estimate_tokens(prompt),
                "completion_tokens": 0,
                "duration_seconds": duration,
                "status": "complete",
                "audio_b64": audio_b64,
                "audio_seconds": audio_seconds,
            },
            claim_token=claim_token,
        )
        if accepted:
            earnings = float((out or {}).get("earnings", 0.0))
            # last_tool=None: don't promote tts to the GPU loop's
            # warm-queue front (that's the chat/image/search preference
            # heuristic — tts has its own dedicated loop here).
            credit_completed_job(
                state, last_tool=None, earnings=earnings,
            )
            log.info(
                "tts job %s finished: %.2fs audio, %.2fs synth",
                job_id, audio_seconds, duration,
            )
        else:
            log.info(
                "tts job %s discarded — claim was superseded or cancelled",
                job_id,
            )
    except Exception as e:
        log.exception("tts job %s failed: %s", job_id, e)
        coord.complete(
            {
                "worker_id": coord.worker_id,
                "job_id": job_id,
                "text": "",
                "model": cfg.bootstrap_tts_model,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "duration_seconds": round(time.time() - started, 3),
                "status": "error",
                "error": str(e),
            },
            claim_token=claim_token,
        )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# --diagnose
# ---------------------------------------------------------------------------
# Process names of AV products commonly seen on contributor machines.
# Used by the diagnose command to call out known-problematic combos
# (Avast in particular routinely blocks auto-updates). Heuristic only —
# absence here doesn't mean "no AV running."
_AV_PROCESS_HINTS: dict[str, str] = {
    # Avast / AVG (shared engine)
    "avastsvc.exe": "Avast Service",
    "avastui.exe": "Avast UI",
    "aswidsagent.exe": "Avast Web Shield",
    "avgsvc.exe": "AVG Service",
    "avgui.exe": "AVG UI",
    # Microsoft Defender
    "msmpeng.exe": "Windows Defender",
    "nissrv.exe": "Defender Network Inspection",
    # Norton
    "nortonsecurity.exe": "Norton Security",
    "ns.exe": "Norton",
    # McAfee
    "mcshield.exe": "McAfee Shield",
    "mcuicnt.exe": "McAfee UI",
    # Bitdefender
    "bdservicehost.exe": "Bitdefender Service",
    "vsserv.exe": "Bitdefender",
    # Kaspersky
    "avp.exe": "Kaspersky",
    # ESET
    "ekrn.exe": "ESET Service",
}


def _resolve_effective_token(
    cfg_token: Optional[str], state: dict,
) -> tuple[Optional[str], Optional[str]]:
    """Mirror resolve_api_token's chain (env > config.json > state.json)
    without the interactive-prompt branch and without writing anywhere.

    Returns (token, source_label) where source_label is
    ``"env API_TOKEN"``, ``"config.json"``, ``"state.json"``, or
    ``None`` when no token is found.

    Config.load already merges env + config.json into ``cfg_token``
    (env wins on collision). We add the state.json branch here the
    same way the agent's main() does at startup via
    resolve_api_token. Read-only — never modifies state.

    Used by ``cmd_diagnose`` so the auth check reports the same
    answer the running agent's token resolver would give, instead
    of only knowing about env + config.json sources."""
    if os.getenv("API_TOKEN"):
        return cfg_token, "env API_TOKEN"
    if cfg_token:
        return cfg_token, "config.json"
    state_token = (state.get("api_token") or "").strip() or None
    if state_token:
        return state_token, "state.json"
    return None, None


def _detect_av() -> list[str]:
    """Enumerate known AV processes by exact name match. Returns the
    set of human-readable labels found. Empty list on non-Windows."""
    if not IS_WINDOWS:
        return []
    found: set[str] = set()
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            label = _AV_PROCESS_HINTS.get(name)
            if label:
                found.add(label)
    except (psutil.AccessDenied, psutil.NoSuchProcess, RuntimeError):
        pass
    return sorted(found)


def cmd_diagnose(cfg: Config) -> int:
    """Sectioned self-check report. Designed to compress the multi-step
    "where's the agent installed, is it talking to the coordinator,
    is the image stack present, what version is on disk, is AV doing
    something" diagnostic dance into one command.

    Side-effect-free: does NOT register with the coordinator, does NOT
    trigger any bootstrap downloads, does NOT mutate state.json.
    Returns 0 if every check is OK or WARN, 1 if any check is FAIL.
    """
    fails = 0
    warns = 0

    def section(name: str) -> None:
        print(name)

    def ok(line: str) -> None:
        print(f"  OK   {line}")

    def warn(line: str) -> None:
        nonlocal warns
        print(f"  WARN {line}")
        warns += 1

    def fail(line: str) -> None:
        nonlocal fails
        print(f"  FAIL {line}")
        fails += 1

    def info(label: str, value: str) -> None:
        print(f"  {label:<14} {value}")

    print("GamerAI Agent Diagnostic Report")
    print("=" * 40)
    print()

    # ---- Binary ---------------------------------------------------------
    section("Binary")
    info("version", f"v{AGENT_VERSION} (build {current_version()})")
    if getattr(sys, "frozen", False):
        path = Path(sys.executable).resolve()
        info("install path", str(path))
        try:
            local_appdata = (
                Path(os.getenv("LOCALAPPDATA")) if os.getenv("LOCALAPPDATA") else None
            )
            downloads = Path.home() / "Downloads"
            if downloads in path.parents:
                warn(
                    f"running from %USERPROFILE%\\Downloads — AV scans "
                    f"this dir aggressively. Consider moving the agent "
                    f"to {local_appdata / 'Programs' / 'GamerAI Agent'} "
                    f"if available."
                )
            elif local_appdata is not None and local_appdata in path.parents:
                ok("running from %LOCALAPPDATA% (AV-friendly)")
            else:
                info("location", "non-standard install path")
        except OSError:
            pass
    else:
        info("running from", "python source (dev mode)")
    print()

    # ---- Configuration --------------------------------------------------
    # Load state.json early so the auth check can consult it. ``load_state``
    # is read-only -- distinct from ``resolve_worker_id`` which would
    # WRITE a new id on first run. Diagnose must never mutate state.
    state = load_state()
    effective_token, token_source = _resolve_effective_token(cfg.api_token, state)

    section("Configuration")
    info("coordinator", cfg.coordinator_url)
    if effective_token:
        info("auth", f"configured (source: {token_source})")
    else:
        info("auth", "MISSING")
        fail(
            "no api_token resolved from env API_TOKEN, config.json, or "
            "state.json -- agent cannot register or claim jobs. Edit "
            "%APPDATA%\\GamerAI\\state.json's api_token field, or set "
            "$API_TOKEN, or paste the token at the foreground prompt."
        )
    info(
        "worker_id",
        state.get("worker_id") or cfg.worker_id or "(none — first run will allocate)",
    )
    print()

    # ---- Coordinator connectivity --------------------------------------
    section("Coordinator connectivity")
    auth_headers = (
        {"Authorization": f"Bearer {effective_token}"} if effective_token else {}
    )
    try:
        t0 = time.time()
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{cfg.coordinator_url}/health")
        dt_ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            ok(f"/health returned 200 in {dt_ms}ms")
        else:
            fail(f"/health returned {r.status_code} in {dt_ms}ms")
    except Exception as e:
        fail(f"/health unreachable: {e}")
    if effective_token:
        try:
            with httpx.Client(timeout=10.0, headers=auth_headers) as c:
                r = c.get(f"{cfg.coordinator_url}/workers")
            if r.status_code == 200:
                workers = (r.json() or {}).get("workers") or []
                wid = state.get("worker_id") or cfg.worker_id
                mine = next((w for w in workers if w.get("worker_id") == wid), None)
                if mine is not None:
                    secs = mine.get("seconds_since_heartbeat")
                    caps = mine.get("capabilities") or {}
                    tools = caps.get("tools") or ["chat"]
                    if secs is not None and secs < 30:
                        ok(f"worker registered (last heartbeat: {secs}s ago)")
                    else:
                        warn(
                            f"worker registered but stale "
                            f"(last heartbeat: {secs}s ago)"
                        )
                    info("advertised tools", ", ".join(tools))
                else:
                    warn(
                        f"worker_id {wid!r} not in /workers — will "
                        f"register on next agent run"
                    )
            elif r.status_code == 401:
                fail("/workers returned 401 — api_token rejected by coordinator")
            else:
                warn(f"/workers returned {r.status_code}")
        except Exception as e:
            warn(f"could not query /workers: {e}")
    print()

    # ---- Ollama / chat tool --------------------------------------------
    section("Ollama (chat tool)")
    ollama_url = (os.getenv("OLLAMA_URL") or cfg.bootstrap_ollama_url).rstrip("/")
    info("url", ollama_url)
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(f"{ollama_url}/api/tags")
        if r.status_code == 200:
            tags = (r.json() or {}).get("models") or []
            ok(f"API responding ({len(tags)} model(s) loaded)")
            target = cfg.bootstrap_model
            if any(t.get("name") == target for t in tags):
                ok(f"{target} present")
            else:
                fail(
                    f"{target} not installed — chat jobs will fail. "
                    f"Run `ollama pull {target}` or let the next agent "
                    f"start re-bootstrap."
                )
        else:
            fail(f"/api/tags returned {r.status_code}")
    except Exception as e:
        fail(f"/api/tags unreachable: {e}")
    print()

    # ---- Image generation ----------------------------------------------
    section("Image generation (sd.cpp)")
    info("install dir", str(sd_install_dir()))
    if not cfg.bootstrap_image_enabled:
        warn(
            "image bootstrap disabled in config — agent will advertise "
            "chat only"
        )
    else:
        for fname in SD_RUNTIME_FILES:
            p = sd_install_dir() / fname
            if p.exists():
                ok(f"{fname} present ({p.stat().st_size / (1024 * 1024):.1f} MB)")
            else:
                fail(f"{fname} missing — image jobs will fail")
        model = sd_model_path(cfg.bootstrap_image_model)
        if model.exists():
            ok(f"{model.name} present ({model.stat().st_size / (1024 * 1024):.1f} MB)")
        else:
            fail(f"{model.name} missing — image jobs will fail")
    print()

    # ---- State ----------------------------------------------------------
    section("State")
    info("jobs done", str(int(state.get("jobs", 0))))
    info("tokens", str(int(state.get("tokens", 0))))
    info("last tool", str(state.get("last_tool") or "(none yet)"))
    print()

    # ---- Recent auto-update --------------------------------------------
    section("Recent auto-update")
    marker_found = False
    for marker in (
        local_state_dir() / "update-failed.txt",
        state_dir() / "update-failed.txt",
    ):
        if not marker.exists():
            continue
        marker_found = True
        try:
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            warn(f"failure marker present but unreadable: {marker}")
            continue
        warn(f"failure marker at {marker}:")
        for line in text.splitlines()[-5:]:
            print(f"           {line.strip()[:120]}")
    if not marker_found:
        ok("no pending failure marker")
    print()

    # ---- System / AV ----------------------------------------------------
    section("System")
    info("OS", platform.platform())
    av = _detect_av()
    if av:
        info("AV detected", ", ".join(av))
        avast_running = any("avast" in a.lower() or "avg" in a.lower() for a in av)
        if avast_running:
            warn(
                "Avast/AVG detected — known to interfere with auto-update. "
                "Exclude %LOCALAPPDATA%\\Programs\\GamerAI Agent\\ and "
                "%LOCALAPPDATA%\\GamerAI\\updates\\ from real-time scanning "
                "if updates keep failing."
            )
    else:
        info("AV detected", "none recognized")
    print()

    # ---- Log file -------------------------------------------------------
    section("Log file")
    log_path = logs_dir() / "agent.log"
    if log_path.exists():
        info("path", str(log_path))
        info("size", f"{log_path.stat().st_size / (1024 * 1024):.2f} MB")
    else:
        warn(f"log file missing at {log_path}")
    print()

    # ---- Summary --------------------------------------------------------
    print("=" * 40)
    print(f"Diagnostic complete. {fails} error(s), {warns} warning(s).")
    return 1 if fails > 0 else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GamerAI Windows agent")
    here = Path(__file__).resolve().parent
    p.add_argument("--config", type=Path, default=here / "config.json")
    p.add_argument("--tray", action="store_true",
                   help="run in tray mode: hide the console window, surface a "
                        "system tray icon (Show/Hide/Open-log/Exit). Used by "
                        "the autostart shortcut.")
    p.add_argument("--background", action="store_true",
                   help="deprecated alias for --tray; kept so existing "
                        "startup shortcuts keep working through one release.")
    p.add_argument("--once", action="store_true",
                   help="process at most one job and exit (useful for tests)")
    p.add_argument("--status", action="store_true",
                   help="print local job stats and exit")
    p.add_argument("--version", action="store_true",
                   help="print agent version + build id and exit")
    p.add_argument("--diagnose", action="store_true",
                   help="run a self-check (config, coordinator, ollama, "
                        "image stack, AV) and print a report")
    p.add_argument("--stayalive", action="store_true",
                   help="ignore user-input idleness; idle gate is CPU "
                        "only. Persists in state.json so the setting "
                        "survives restarts and auto-updates.")
    p.add_argument("--no-stayalive", dest="no_stayalive",
                   action="store_true",
                   help="clear the persisted stayalive setting.")
    p.add_argument("--pair", action="store_true",
                   help="run the browser-handoff pairing flow: open the "
                        "default browser to the coordinator's /agent/pair "
                        "page, wait for the signed-in user to click "
                        "Pair this PC, then save the issued token to "
                        "state.json and exit.")
    p.add_argument("--signup", action="store_true",
                   help="create a brand-new GamerAI account for this PC "
                        "(no existing account or invite needed) via the "
                        "coordinator's public POST /signup, save the "
                        "issued token to state.json, and exit. Prefer "
                        "--pair instead if you already have an account.")
    p.add_argument("--unpair", action="store_true",
                   help="retire this agent's bearer with the coordinator "
                        "(POST /agents/pair/unpair) and wipe the local "
                        "api_token from state.json. Invoked automatically "
                        "by the Windows uninstaller; safe to run "
                        "manually any time. Best-effort: a network "
                        "failure logs and exits 0 so it never blocks "
                        "uninstall.")
    args = p.parse_args(argv)
    # Deprecation alias: --background → --tray. v1.1.12 replaces the
    # silent --background mode with tray + hidden console; existing
    # autostart shortcuts (created by pre-1.1.12 installers) pass
    # --background and must keep working until the user reinstalls.
    args.background_was_used = bool(args.background)
    if args.background and not args.tray:
        args.tray = True
    return args


def run_pair_flow(
    coordinator_url: str,
    state: dict,
    *,
    poll_interval_seconds: float = 2.0,
    timeout_seconds: float = 300.0,
    open_browser: bool = True,
) -> Optional[str]:
    """Browser-handoff pairing.

    1. POST /agents/pair/start → ``{pair_code, user_code, verification_url}``
    2. Show ``user_code`` on screen and open the user's default browser
       to ``verification_url`` (which contains NO secret). The user
       types the code we display to approve — that out-of-band step is
       what stops someone from phishing the pairing with a link.
    3. Poll ``POST /agents/pair/poll`` with the secret ``pair_code``
       every ``poll_interval_seconds`` until the user approves, then
       persist the returned bearer in ``state.json``.

    Returns the new token on success, None on failure / timeout /
    user-abort. Prints status to stdout so a contributor running this
    interactively gets a visible progress trail.
    """
    import webbrowser  # local import — only used in this code path

    base = coordinator_url.rstrip("/")
    try:
        r = httpx.post(f"{base}/agents/pair/start", timeout=10)
        r.raise_for_status()
    except httpx.HTTPError as e:
        sys.stderr.write(f"pair: couldn't reach coordinator at {base}: {e}\n")
        return None
    info = r.json()
    code = info.get("pair_code")
    url = info.get("verification_url") or info.get("pair_url")
    # user_code is the human-typeable code the browser asks for. Older
    # coordinators didn't emit it; fall back to the secret code only so
    # the flow still completes against a stale server (the secret is
    # never put in the URL either way).
    user_code = info.get("user_code") or code
    if not code or not url:
        sys.stderr.write(
            f"pair: coordinator returned unexpected payload: {info!r}\n"
        )
        return None

    sys.stdout.write(
        "\n"
        "Pairing this PC with your GamerAI account\n"
        "-----------------------------------------\n"
        "1. Sign in (or stay signed in) on your browser at:\n"
        f"     {url}\n"
        "2. Enter this code to approve (valid 5 minutes):\n"
        "\n"
        f"     {user_code}\n"
        "\n"
        "Only type this code into the GamerAI page above. We will never\n"
        "ask for it anywhere else.\n"
        "Waiting for your approval"
    )
    sys.stdout.flush()
    if open_browser:
        try:
            webbrowser.open(url, new=2)
        except Exception:
            # webbrowser is best-effort — on Server Core or a stripped
            # Linux container there may be no browser. The URL is
            # already printed above; user can paste it manually.
            pass

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            r = httpx.post(
                f"{base}/agents/pair/poll",
                json={"pair_code": code},
                timeout=10,
            )
        except httpx.HTTPError as e:
            sys.stdout.write(f"\npair: poll failed: {e}\n")
            return None
        if r.status_code == 404:
            sys.stdout.write(
                "\npair: code expired before the browser approved it. "
                "Run --pair again to get a new code.\n"
            )
            return None
        if r.status_code != 200:
            sys.stdout.write(
                f"\npair: unexpected poll status {r.status_code}: "
                f"{r.text[:200]}\n"
            )
            return None
        body = r.json()
        if body.get("state") == "approved":
            token = body.get("token")
            if not token:
                sys.stderr.write(
                    "\npair: approval payload missing token; aborting\n"
                )
                return None
            state["api_token"] = token
            save_state(state)
            sys.stdout.write(
                f"\nPaired. Token saved to {STATE_PATH}.\n"
                "You can close the browser tab; the agent will pick the "
                "token up on its next run.\n"
            )
            return token
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(poll_interval_seconds)

    sys.stdout.write(
        "\npair: timed out waiting for browser approval. "
        "Run --pair again when ready.\n"
    )
    return None


def run_signup_flow(
    coordinator_url: str,
    state: dict,
    *,
    _confirm=None,
    _email_prompt=None,
) -> Optional[str]:
    """Public, invite-free account creation — the literal version of
    business.md's "contribute-to-use" pitch: running the agent for the
    first time, with no existing GamerAI account and nobody to invite
    you, is how you join. Calls the coordinator's public ``POST
    /signup`` directly (no browser handoff needed, unlike
    ``run_pair_flow`` — there's no existing session to approve this
    against) with a generated username + a strong random password + a
    real, prompted-for email (required — chat/image/voice gate on
    verifying it, see coordinator's POST /signup), then persists the
    returned bearer to ``state.json`` exactly like pairing does.

    Requires an explicit y/n ToS confirmation before calling the
    endpoint — same bar as the web redemption page's click-through
    checkbox; a background/non-interactive run should not silently
    accept terms on the operator's behalf. ``_confirm`` and
    ``_email_prompt`` are both injectable for tests (default to real
    ``input()`` prompts).

    Returns the new token on success, None on failure/decline. Prints
    the generated credentials once, clearly, since this is the only
    time the password is ever shown — the coordinator only stores its
    hash."""
    confirm = _confirm or (lambda prompt: input(prompt).strip().lower())
    email_prompt = _email_prompt or (lambda prompt: input(prompt).strip())

    sys.stdout.write(
        "\n"
        "No GamerAI account found for this PC.\n"
        "-----------------------------------------\n"
        "Running the agent is how you join the network — no invite\n"
        "required. This will create a new account for you.\n"
        "\n"
        f"Community terms: {coordinator_url.rstrip('/')}/tos\n"
        "Accept and create an account? [y/N] "
    )
    sys.stdout.flush()
    try:
        answer = confirm("")
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\nsignup: aborted.\n")
        return None
    if answer not in ("y", "yes"):
        sys.stdout.write(
            "signup: declined. Run --pair instead if you already have "
            "an account.\n"
        )
        return None

    username = f"gamer-{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex + uuid.uuid4().hex[:8]

    # Chat/image/voice now gate on a confirmed real email (coordinator
    # email-verification slice) — ask for one instead of synthesizing
    # an unmailable placeholder like earlier versions did, since nobody
    # could ever click a link sent to that. Same retry bar as the y/n
    # decline above: a few tries, then give up rather than loop forever.
    email = None
    for _attempt in range(3):
        try:
            candidate = email_prompt(
                "\nEmail address (used to verify your account and "
                "recover access — never shown publicly): "
            )
        except (EOFError, KeyboardInterrupt):
            sys.stderr.write("\nsignup: aborted.\n")
            return None
        candidate = (candidate or "").strip()
        if candidate and "@" in candidate and " " not in candidate:
            email = candidate
            break
        sys.stderr.write(
            "That doesn't look like a valid email — try again.\n"
        )
    if email is None:
        sys.stderr.write("signup: no valid email provided, aborting.\n")
        return None

    base = coordinator_url.rstrip("/")
    try:
        r = httpx.post(
            f"{base}/signup",
            json={
                "username": username,
                "password": password,
                "email": email,
                "tos_accepted": True,
            },
            timeout=10,
        )
    except httpx.HTTPError as e:
        sys.stderr.write(f"signup: couldn't reach coordinator at {base}: {e}\n")
        return None
    if r.status_code != 200:
        sys.stderr.write(
            f"signup: coordinator rejected the request "
            f"({r.status_code}): {r.text[:200]}\n"
        )
        return None
    body = r.json()
    token = body.get("token")
    if not token:
        sys.stderr.write(f"signup: unexpected response: {body!r}\n")
        return None

    state["api_token"] = token
    save_state(state)
    verified = bool(body.get("email_verified", True))
    verify_note = (
        "\n"
        "Check your inbox to verify your email — chat, image\n"
        "generation, and voice stay locked until you do. This does\n"
        "NOT affect contributing compute; the agent runs either way.\n"
        if not verified else ""
    )
    sys.stdout.write(
        "\n"
        "Account created.\n"
        "-----------------------------------------\n"
        f"  username: {username}\n"
        f"  password: {password}\n"
        f"  email:    {email}\n"
        "\n"
        "Save this password now — it is shown only this once (the\n"
        "coordinator stores only its hash). Sign in with it in the\n"
        "chat UI to invite friends from your account. This agent is\n"
        f"already paired — the token is saved to {STATE_PATH}.\n"
        f"{verify_note}"
    )
    return token


def run_unpair_flow(coordinator_url: str, state: dict) -> int:
    """Retire this agent's bearer with the coordinator and wipe the
    local copy. Best-effort: a network failure logs and returns 0 so
    the Windows uninstaller never blocks on a coordinator outage.

    Order matters: we hit the network first (so the token gets
    revoked even if the file wipe somehow fails) and clear the local
    api_token second (so a recovered state.json holds nothing useful
    even if the network call succeeded).
    """
    token = state.get("api_token")
    if not token:
        sys.stdout.write("unpair: no api_token in state.json; nothing to do\n")
        return 0
    base = coordinator_url.rstrip("/")
    try:
        r = httpx.post(
            f"{base}/agents/pair/unpair",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if r.status_code == 200:
            body = r.json() if r.content else {}
            sys.stdout.write(
                f"unpair: coordinator revocation ok "
                f"(deleted={body.get('deleted', 'unknown')})\n"
            )
        elif r.status_code == 401:
            # Token already invalid — nothing to revoke. Treat as
            # success since the outcome is what we wanted anyway.
            sys.stdout.write(
                "unpair: coordinator returned 401 (token already invalid)\n"
            )
        else:
            sys.stdout.write(
                f"unpair: coordinator returned {r.status_code}; "
                f"wiping local copy anyway\n"
            )
    except httpx.HTTPError as e:
        # Network down, DNS broken, coordinator unreachable. Don't
        # block — uninstall should still succeed locally.
        sys.stdout.write(
            f"unpair: couldn't reach coordinator ({e}); "
            f"wiping local copy anyway\n"
        )
    # Local wipe always runs, even if the network call failed. A
    # recovered state.json without an api_token is harmless.
    state["api_token"] = None
    try:
        save_state(state)
        sys.stdout.write("unpair: local api_token cleared\n")
    except Exception as e:
        sys.stderr.write(f"unpair: failed to write state.json: {e}\n")
        # Still return 0 — best-effort, never block uninstall.
    return 0


def resolve_api_token(
    cfg_token: Optional[str],
    state: dict,
    background: bool,
) -> Optional[str]:
    """Token-resolution chain for first-run onboarding:

      env API_TOKEN   ← already merged into cfg.api_token by Config.load
        →  config.json["api_token"]
        →  state.json["api_token"]      (persisted from a prior first-run prompt)
        →  interactive prompt (skipped when ``background`` is True)

    Returns the resolved token, or None when ``background`` is True and
    no token is available (caller should error out — we can't prompt
    when there's no console). Since v1.1.12, tray mode pre-unhides the
    console before calling this with ``background=False``, so the prompt
    is visible to the user.
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
        "This agent isn't paired with an account yet. Easiest path:\n"
        "  agent --pair\n"
        "That opens your browser to confirm pairing under your account.\n"
        "\n"
        "If you have a bearer token instead, paste it now (looks like\n"
        "gai_<64 hex chars>), or press Enter to abort and run --pair.\n"
        "\n"
    )
    sys.stdout.flush()
    try:
        entered = input("token (or Enter to abort): ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\naborted — no token entered. Run --pair when ready.\n")
        return None
    if not entered:
        sys.stderr.write("aborted — run --pair to pair this PC.\n")
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
    Suppressed for --once and --status (focused subcommands); always
    runs in --tray mode (output goes to the hidden console's
    scrollback, surfaced later by "Show console" in the tray menu).

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
    # Literal first line — proves the process reached Python at all.
    # Surfaces silent bootstrap-stage exits and the early-return paths
    # below (single-instance handoff, --version, --diagnose, --status)
    # that otherwise leave nothing in agent.log. Always best-effort;
    # _boot_log itself never raises.
    _raw_argv = argv if argv is not None else sys.argv[1:]
    _boot_log(f"main entry v{AGENT_VERSION} argv={_raw_argv!r}")
    _boot_log(f"main: sys.frozen={getattr(sys, 'frozen', False)} _MEIPASS={getattr(sys, '_MEIPASS', None)!r}")
    args = parse_args(_raw_argv)
    _boot_log(f"main: parse_args ok tray={args.tray} background_was_used={args.background_was_used}")
    if args.version:
        print(f"GamerAI agent v{AGENT_VERSION} (build {current_version()})")
        return 0
    if args.pair:
        cfg = Config.load(args.config if args.config.exists() else None)
        state = load_state()
        token = run_pair_flow(cfg.coordinator_url, state)
        if token:
            ensure_machine_name(state)
        return 0 if token else 2
    if args.signup:
        cfg = Config.load(args.config if args.config.exists() else None)
        state = load_state()
        token = run_signup_flow(cfg.coordinator_url, state)
        if token:
            ensure_machine_name(state)
        return 0 if token else 2
    if args.unpair:
        cfg = Config.load(args.config if args.config.exists() else None)
        state = load_state()
        return run_unpair_flow(cfg.coordinator_url, state)
    if args.diagnose:
        # Diagnose loads config but skips logging setup + worker_id
        # allocation. Both write to disk; diagnose must be read-only.
        cfg = Config.load(args.config if args.config.exists() else None)
        return cmd_diagnose(cfg)

    # Early deprecation log for --background. Has to land in agent.log
    # BEFORE the single-instance check, because a second-instance
    # launch with --background returns 0 below before setup_logging
    # runs — and a deprecation notice the user never sees is no
    # notice at all. setup_logging's RotatingFileHandler opens in
    # append mode, so a one-line direct write here doesn't conflict
    # with the proper logger taking over a few lines later.
    if args.background_was_used:
        try:
            from datetime import datetime
            log_path = logs_dir() / "agent.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"[WARNING] --background is deprecated; use --tray "
                    f"instead (silently aliased for one release).\n"
                )
        except Exception:
            pass

    # State load moved up here (was after _print_greeting) so the
    # tray-mode init can read state["last_seen_version"] to decide
    # whether to show the post-update welcome banner. The captured
    # _was_last_seen_version is also used by the update-applied toast
    # below so it sees the pre-mutation value too.
    state = load_state()
    _was_last_seen_version = state.get("last_seen_version")
    _boot_log(f"main: state loaded, last_seen_version={_was_last_seen_version!r}")

    # Tray-mode init: hide the console window. Done BEFORE the greeting
    # and logger setup so a second launch exits immediately without
    # touching state. --once / --status / --version skip tray setup;
    # they're foreground subcommands a user invoked directly.
    console_hwnd = 0
    ipc_sock: Optional[socket.socket] = None
    tray_active = args.tray and not args.once and not args.status
    # Single-instance protection is claimed for ANY long-running launch,
    # not just --tray. The installer's postinstall "Launch now" checkbox
    # and the plain Start Menu / Desktop icons all launch with no --tray
    # flag (only the Startup-folder autorun shortcut passes --tray) — a
    # foreground console launch used to skip _claim_single_instance()
    # entirely, so a manual launch racing an already-running tray
    # instance (or another manual launch) got two fully independent
    # processes with no coordination between them, each free to
    # clobber the other's state.json write (including a just-issued
    # signup/pair token) with its own stale in-memory copy. --once /
    # --status still skip it — those are meant to run standalone
    # alongside an existing instance, not collide with it.
    wants_singleton = not args.once and not args.status
    show_welcome = False
    if tray_active:
        _set_aumid()
        _set_console_title(f"GamerAI Agent — v{AGENT_VERSION}")
        console_hwnd = _get_console_hwnd()
        # First launch on a new version (or first install) shows a
        # 10-second welcome banner instead of hiding immediately.
        # Otherwise the post-update tray flash + invisible startup
        # reads as "the agent broke" to a user expecting visible
        # feedback. state["last_seen_version"] is updated by the
        # toast block below, so the welcome runs exactly once per
        # transition.
        show_welcome = _was_last_seen_version != AGENT_VERSION
        if show_welcome:
            _print_welcome_banner()
            _schedule_delayed_hide(console_hwnd, 10.0)
            _boot_log(
                f"tray: welcome banner shown (was={_was_last_seen_version!r})"
            )
        else:
            _hide_console(console_hwnd)
            # PyInstaller bootstrap creates the conhost window
            # asynchronously, so the single hide above sometimes
            # races the window's realization and gets lost. A short
            # polling thread keeps re-hiding for 2 seconds; after
            # that any visibility change comes from the user's tray
            # menu.
            _persist_console_hidden(console_hwnd)
            _boot_log("tray: console hidden (welcome already shown for this version)")
    if wants_singleton:
        ipc_sock, is_first = _claim_single_instance()
        if not is_first:
            # Another agent is already running; we already told it to
            # surface its console (tray mode) or IPC handoff alone
            # covers it (console mode — print an explicit line since
            # there's no tray to surface here).
            if not tray_active:
                try:
                    sys.stdout.write(
                        "GamerAI agent is already running (check your "
                        "system tray or Task Manager) — exiting.\n"
                    )
                    sys.stdout.flush()
                except Exception:
                    pass
            _boot_log("exit: deferred to existing instance via single-instance handoff")
            return 0
        _boot_log(f"single-instance: proceeding as first instance (ipc_sock={'bound' if ipc_sock else 'none/collision'})")

    _print_greeting(args.once, args.status)
    _boot_log("main: greeting printed, loading config")
    cfg = Config.load(args.config if args.config.exists() else None)
    # state was already loaded above (before tray init) so the welcome
    # banner could check last_seen_version. Don't re-load here — that
    # would clobber any state mutations the tray init path made.
    worker_id = resolve_worker_id(cfg.worker_id, state)
    _boot_log(f"main: config+state loaded, worker_id={worker_id}")
    # Tray mode keeps the StreamHandler on — the hidden console's
    # scrollback is what "Show console" surfaces. Only a hypothetical
    # truly-headless mode would pass headless=True.
    log = setup_logging(headless=False)
    _boot_log("main: setup_logging ok — further trace goes to agent.log")

    # Apply --stayalive / --no-stayalive (CLI overrides persisted
    # value; the new value is itself persisted so the next launch +
    # any post-update relaunch inherit it without re-passing the
    # flag). Useful for dev sessions over Chrome Remote Desktop
    # which generates continuous mouse-move events that defeat the
    # 60 s no-input idle gate.
    if args.no_stayalive:
        if state.get("stayalive"):
            log.info("stayalive: cleared via --no-stayalive (was on)")
        state["stayalive"] = False
        save_state(state)
    elif args.stayalive:
        if not state.get("stayalive"):
            log.info("stayalive: enabled via --stayalive (persisted)")
        state["stayalive"] = True
        save_state(state)
    cfg.stayalive = bool(state.get("stayalive", False))
    if cfg.stayalive:
        log.warning(
            "stayalive: ON — user-input idleness is ignored, CPU is "
            "the only idle gate. Clear with --no-stayalive."
        )

    # Rapid-restart detection. An update loop (e.g. v1.1.9's
    # stale-MEIPASS bug) used to manifest as the agent restarting
    # every ~60 s with no human-visible signal. The gap from the
    # previous start is logged at INFO/WARN/ERROR depending on how
    # tight it is, so a grep for "previous start" surfaces a runaway
    # restart pattern in seconds instead of hours.
    now_ts = time.time()
    prev_start = state.get("last_start_at")
    if isinstance(prev_start, (int, float)):
        delta = now_ts - prev_start
        if delta < 60:
            log.error(
                "rapid-restart: %.0fs since previous start "
                "(<60s — likely a restart loop, check update path)",
                delta,
            )
        elif delta < 300:
            log.warning(
                "rapid-restart: %.0fs since previous start "
                "(<5min — investigate if this repeats)",
                delta,
            )
        elif delta < 3600:
            log.info("previous start was %.0fs ago", delta)
    state["last_start_at"] = now_ts
    save_state(state)

    if args.status:
        print(f"version:   v{AGENT_VERSION} (build {current_version()})")
        print(f"worker_id: {worker_id}")
        print(f"jobs:      {state.get('jobs', 0)}")
        print(f"tokens:    {state.get('tokens', 0)}")
        return 0

    # Update-applied toast + welcome-banner marker. Uses the
    # _was_last_seen_version captured before tray init so welcome and
    # toast see the same pre-mutation value. Toast skips first install
    # (no prior version to compare against). last_seen_version is
    # updated here so the welcome banner runs exactly once per
    # version transition.
    if tray_active:
        if _was_last_seen_version and _was_last_seen_version != AGENT_VERSION:
            _toast(
                "GamerAI updated",
                f"Now running v{AGENT_VERSION} (was v{_was_last_seen_version}).",
                icon_path=_tray_icon_path(),
            )
        if _was_last_seen_version != AGENT_VERSION:
            state["last_seen_version"] = AGENT_VERSION
            save_state(state)

    # Start the IPC accept loop now that we have a logger. The lambda
    # closes over console_hwnd so future SHOW commands surface the
    # right window.
    if ipc_sock is not None:
        threading.Thread(
            target=_ipc_serve,
            args=(ipc_sock, lambda: _show_console(console_hwnd), log),
            name="gamerai-ipc",
            daemon=True,
        ).start()

    # First-run pairing UX. The legacy path here was "show a 'paste
    # your token' prompt" — but the user already has a sign-in on the
    # web side, so the friendly path is to open their browser to the
    # pairing confirm page and let them click "Pair this PC". Token
    # gets minted on the server, polled by the agent, persisted to
    # state.json. Zero copy-paste, zero "where do I find this URL."
    #
    # Tray mode pre-unhides the console so the contributor sees the
    # pairing URL + progress dots even if the default browser open
    # fails silently. The console stays open until pairing succeeds
    # or times out.
    have_token = bool(cfg.api_token or state.get("api_token"))
    auto_unhidden_for_token = False
    if not have_token:
        if tray_active:
            _toast(
                "GamerAI Agent needs an account",
                "Opening the console to pair or create your account.",
                icon_path=_tray_icon_path(),
            )
            _show_console(console_hwnd)
            auto_unhidden_for_token = True
        # Two doors in, matching business.md's actor model: someone who
        # already has a GamerAI account (invited, or signed up on the
        # web) pairs this PC to it via browser handoff; someone with
        # neither creates a brand-new account right here — running the
        # agent for the first time IS how they join, no invite needed.
        # Default to the signup path on a bare Enter — most first
        # installs are exactly that case, not a returning contributor
        # re-pairing a second machine.
        sys.stdout.write(
            "\nDo you already have a GamerAI account to pair this PC "
            "with? [y/N] "
        )
        sys.stdout.flush()
        try:
            has_account = input("").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            has_account = False
        if has_account:
            token = run_pair_flow(cfg.coordinator_url, state)
        else:
            token = run_signup_flow(cfg.coordinator_url, state)
        if not token:
            msg = (
                "account setup didn't complete. Open the agent console "
                "and run `agent --pair` (existing account) or "
                "`agent --signup` (new account) to try again, or paste "
                "a bearer token into %APPDATA%\\GamerAI\\state.json "
                "manually."
                if IS_WINDOWS else
                "account setup didn't complete. Run `agent --pair` or "
                "`agent --signup` to try again."
            )
            log.error(msg)
            sys.stderr.write(msg + "\n")
            return 2
        cfg.api_token = token
        # Console is already visible here (tray_active unhid it above,
        # or this is a foreground run) — safe to prompt.
        ensure_machine_name(state)
    else:
        cfg.api_token = cfg.api_token or state.get("api_token")
        if not state.get("machine_name"):
            # Pre-existing install updating to this version: backfill
            # silently rather than prompting. This branch runs on every
            # ordinary restart, including unattended tray autostart with
            # no console shown — blocking on input() here would hang
            # forever. Naming is cosmetic; a random default is fine.
            state["machine_name"] = random_machine_name()
            save_state(state)

    log.info(
        "GamerAI agent v%s (build %s) starting on %s — worker_id=%s",
        AGENT_VERSION, current_version(), platform.platform(), worker_id,
    )
    log.info("coordinator=%s polling=%ss idle threshold=%ss cpu<%s%% auth=%s",
             cfg.coordinator_url, cfg.polling_interval,
             cfg.min_input_idle_seconds, cfg.max_cpu_percent,
             "on" if cfg.api_token else "off")

    # Surface any self-update failure from the previous cycle, and warn
    # if the user has a second agent.exe at the standard install dir
    # that isn't the one running. Both are diagnostic-only — neither
    # blocks startup.
    _check_previous_update_failure(log)
    _check_stale_install_dir(log)
    # Sweep stale %TEMP%\_MEI<random> dirs left over from prior agent
    # runs where PyInstaller's atexit cleanup failed (e.g. AV holding
    # files at exit). Defunct since current_version() now reads
    # strictly from sys._MEIPASS, but keeps temp space sane.
    cleanup_stale_meipass_dirs(log)

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
    # pointing at a remote/test Ollama keep that override. Also skipped
    # entirely in smart mode: the pipeline shard owns this GPU's VRAM,
    # so loading the Ollama canonical alongside it would OOM — a
    # smart-enabled agent serves chat:smart (head) or nothing (backend)
    # on the GPU, never standard chat.
    if not cfg.smart_enabled and not os.getenv("OLLAMA_URL"):
        if not tray_active and cfg.bootstrap_enabled and IS_WINDOWS:
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
    # enabled when the contributor opts into tray mode (autostart
    # installer toggle) and the config knob is on. Foreground/--once
    # runs never touch power state.
    keep_awake_active = False
    if tray_active and cfg.keep_awake_while_online:
        keep_awake_active = keep_awake_begin(log)
    elif tray_active and not cfg.keep_awake_while_online:
        log.info("keep-awake off (power.keep_awake_while_online=false)")

    # Image-tool bootstrap (best-effort; never blocks chat). When it
    # succeeds the worker advertises tools=["chat","image"] and pulls
    # from the per-tool image queue; on failure we stay chat-only and
    # the coordinator flags this worker as a partial contributor.
    # Skipped in smart mode for the same VRAM-ownership reason as the
    # chat bootstrap above.
    if cfg.smart_enabled:
        image_ready = False
        log.info(
            "smart mode on — skipping image bootstrap (the pipeline "
            "shard owns this GPU's VRAM)"
        )
    else:
        image_ready = bootstrap_image_inference(cfg, log)
    if not image_ready and not cfg.smart_enabled:
        # Loud warning block — partial contributors are a documented
        # second-class state and the user should see it on first run
        # rather than discover it later via the account page badge.
        # Print to both the console (for foreground / first-run flows)
        # and the rotating log file (for tray-mode operators who never
        # see stdout). The console block is no-op when output is
        # redirected.
        msg_lines = [
            "",
            "================================================================",
            "  WARNING: image generation is NOT available on this machine.",
            "",
            "  Your agent will still serve chat jobs, but the coordinator",
            "  will register this worker as a PARTIAL CONTRIBUTOR — image",
            "  jobs won't be routed here, and future tier promotions may",
            "  weight your contribution accordingly.",
            "",
            "  To fix: ensure the image model (.gguf) is present and the",
            "  sd.cpp sidecar reachable, then restart the agent. See the",
            "  install guide / contribute page for details.",
            "================================================================",
            "",
        ]
        try:
            for line in msg_lines:
                sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        log.warning(
            "image bootstrap failed — running as partial contributor "
            "(chat only)"
        )

    # TTS bootstrap (Phase 1 voice feature; see voice-phase1 design).
    # Best-effort like image: failure drops "tts" from advertised
    # capabilities, agent keeps serving chat/image. Quiet failure —
    # voice is additive, so it doesn't warrant the loud partial-
    # contributor banner above.
    tts_ready = bootstrap_tts_inference(cfg, log)
    if not tts_ready and cfg.bootstrap_tts_enabled:
        log.warning(
            "tts bootstrap failed — voice jobs will not route to "
            "this worker (chat / image still served)"
        )

    # Smart-mode bootstrap (multi-machine pipeline). Downloads the
    # pinned llama.cpp build (+ the GGUF on the head), launches the
    # role's sidecar, and on the head waits for /health — the first
    # load streams the backend's layer shard over the LAN, so this can
    # take minutes. Best-effort: on failure the agent keeps running
    # with whatever else bootstrapped (which, with the chat/image
    # skips above, is typically just TTS — clearly logged).
    smart_rt = bootstrap_smart_runtime(cfg, log)
    if cfg.smart_enabled and smart_rt is None:
        log.warning(
            "smart bootstrap failed — this machine is contributing "
            "no smart capability this run; fix the issue above and "
            "restart the agent"
        )

    coord = Coordinator(cfg.coordinator_url, worker_id, log, cfg.api_token)
    # Advertise the model and tools we can actually serve. The chat
    # bootstrap above either confirmed the model is loaded into Ollama
    # or fell back to mock; image bootstrap is independent (sd.exe +
    # GGUF on disk). Coordinator-side routing uses capabilities.tools
    # to pick the right Redis queue when /jobs/next is called.
    #
    # Search piggybacks on the chat capability: it needs the same
    # local Ollama to synthesize the summary, plus the ddgs +
    # trafilatura packages from requirements.txt. We probe the import
    # at startup so an agent built without those wheels (e.g. a
    # custom pyinstaller spec that excluded them) advertises chat
    # without search and the coordinator never hands it a search job.
    # TTS is its own capability (CPU-only Piper) — see voice-phase1.
    capabilities: Optional[dict] = None
    if cfg.smart_enabled:
        # Smart mode replaces the standard GPU toolset. The head
        # advertises ONLY chat:smart (its VRAM holds the 14B shard, so
        # serving 3B chat or SDXL alongside would OOM); a backend
        # advertises no GPU tools at all — its contribution flows
        # through the head. Explicit empty tools matters: it's what
        # stops the coordinator treating this worker as legacy
        # chat-capable. TTS stays — it's CPU-only.
        tools = []
        if smart_rt is not None and smart_rt.role == "head" and smart_rt.ready():
            tools.append("chat:smart")
        if tts_ready:
            tools.append("tts")
        capabilities = {
            "models": [cfg.smart_model] if "chat:smart" in tools else [],
            "tools": tools,
            "notes": f"smart-pipeline {cfg.smart_role}",
        }
    else:
        tools = ["chat"]
        if image_ready:
            tools.append("image")
        if _search_deps_available(log):
            tools.append("search")
        if tts_ready:
            tools.append("tts")
        if cfg.bootstrap_enabled and os.getenv("OLLAMA_URL"):
            capabilities = {"models": [cfg.bootstrap_model], "tools": tools}
        elif image_ready:
            capabilities = {"models": [cfg.bootstrap_image_model], "tools": ["image"]}
    # Hardware label, merged in regardless of which branch above ran —
    # it's informational (dashboard display), not a routing input, so
    # it doesn't belong in the tools/models decision tree above.
    if capabilities is not None:
        gpu_name, gpu_vram_gb = gpu_hardware_info()
        if gpu_name:
            capabilities["gpu_model"] = gpu_name
        if gpu_vram_gb is not None:
            capabilities["vram_gb"] = gpu_vram_gb
    if not coord.register(capabilities=capabilities, display_name=state.get("machine_name")):
        if keep_awake_active:
            keep_awake_end(log)
        return 1
    # Background heartbeat thread takes over /heartbeat POSTs from the
    # main loop. While inference is running on the main thread (chat
    # streams, image gen — sometimes 30–60s on DreamShaper) this thread
    # keeps the worker visibly alive to the coordinator, so the reaper
    # never falsely requeues a healthy in-flight job.
    coord.start_heartbeat()

    # Registration succeeded. If we forcibly unhid the console for the
    # first-run pairing flow, fire a toast and queue the
    # press-any-key-to-hide prompt so the user can verify the agent
    # is alive before sending it to the tray.
    #
    # auto_unhidden_for_token is True only when we just paired (the
    # first-run path), so subsequent agent launches skip this whole
    # block and go straight to silent tray mode — the existing
    # default behavior.
    if auto_unhidden_for_token:
        _toast(
            "GamerAI Agent online",
            f"Token saved. Worker {worker_id[:8]}… connected.",
            icon_path=_tray_icon_path(),
        )
        _schedule_post_pair_hide(console_hwnd, log)

    # Self-update background thread. Runs in BOTH background and
    # foreground modes now — a contributor running agent.exe by
    # double-click should stay current without manual download trips.
    # Communicates with the main thread via stop_event + a tiny shared
    # holder dict: keep_awake.active is read-only for the updater; the
    # updater sets exit_requested when it kicks off the swap-and-restart.
    # ``force_update_event`` lets the stdin command thread request an
    # immediate update on the next tick. ``relaunch_args`` preserves
    # launch mode across the swap (foreground stays foreground).
    updater_thread: Optional[threading.Thread] = None
    stop_event = threading.Event()
    force_update_event = threading.Event()
    keep_awake_holder = {"active": keep_awake_active, "exit_requested": False}
    relaunch_args = ["--tray"] if args.tray else []
    if cfg.update_enabled:
        updater_thread = threading.Thread(
            target=updater_loop,
            args=(
                cfg, log, keep_awake_holder, stop_event,
                force_update_event, relaunch_args,
            ),
            name="gamerai-updater",
            daemon=True,
        )
        updater_thread.start()

    # Stdin reader for the typed ``update`` / ``help`` / ``status`` /
    # ``quit`` commands. In tray mode the console starts hidden, but
    # the stdin handle is still attached — power users can type into
    # it after clicking "Show console". A blocking read on a hidden
    # console just waits silently, so the thread is harmless.
    stdin_thread = threading.Thread(
        target=stdin_command_loop,
        args=(
            log, worker_id, force_update_event, stop_event, cfg, state,
            # The `smart` command persists to the operator-override file
            # (%APPDATA%), NOT the ephemeral bundled config the agent was
            # launched with — see operator_config_path / Config.load.
            operator_config_path(), relaunch_args,
        ),
        name="gamerai-stdin",
        daemon=True,
    )
    stdin_thread.start()

    # CPU TTS loop (voice-phase1 dual-loop). Only spawned when Piper
    # bootstrap succeeded — without it, advertising "tts" capability
    # would route audio jobs to a worker that can't fulfil them, so
    # the capability advertisement above is also gated on tts_ready.
    # daemon=True so a clean shutdown_done.set() lets us not bother
    # joining; the loop's stop_event check exits within one
    # LONG_POLL_WAIT_SECONDS BLPOP window.
    tts_thread: Optional[threading.Thread] = None
    if tts_ready:
        tts_thread = threading.Thread(
            target=tts_loop,
            args=(cfg, coord, state, log, stop_event),
            name="gamerai-tts",
            daemon=True,
        )
        tts_thread.start()

    # Tray icon: spawn after stop_event exists so Exit can signal a
    # clean shutdown. Returns None if pystray/Pillow failed to import
    # (e.g. dev environment); the agent still runs, just without UI.
    tray_icon = None
    if tray_active:
        tray_icon = _start_tray(
            stop_event=stop_event,
            console_hwnd=console_hwnd,
            log_path=logs_dir() / "agent.log",
            icon_path=_tray_icon_path(),
            log=log,
        )

    # Graceful-shutdown closure. Same body as the finally block below,
    # but ALSO invoked from the SetConsoleCtrlHandler trampoline so
    # console-close / logoff / shutdown events flush the offline
    # heartbeat + final earnings line before Windows force-terminates
    # us. Python's KeyboardInterrupt path does NOT fire for
    # CTRL_CLOSE_EVENT (console X button, `taskkill /PID` without /F),
    # so without this the user's last earnings + the worker's offline
    # status are silently lost. Idempotent via shutdown_done.
    shutdown_done = threading.Event()

    def _shutdown_now():
        if shutdown_done.is_set():
            return
        shutdown_done.set()
        stop_event.set()
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass
        try:
            log.info("graceful shutdown")
        except Exception:
            pass
        try:
            # Stops the background heartbeat thread and pushes one
            # final offline beat so the coordinator's /workers view
            # flips immediately rather than waiting for the timeout.
            coord.stop_heartbeat(send_offline=True)
        except Exception:
            pass
        if smart_rt is not None:
            # Terminate the managed llama-server / rpc-server so the
            # pipeline's VRAM is released with the agent — a gamer
            # closing the agent expects their GPU back immediately.
            try:
                smart_rt.stop()
            except Exception:
                pass
        if (keep_awake_holder.get("active")
                and not keep_awake_holder.get("exit_requested")):
            try:
                keep_awake_end(log)
            except Exception:
                pass
        try:
            print_earnings(state, log)
        except Exception:
            pass
        # Drain any log records still in the queue + stop the listener
        # thread cleanly. Runs last so all of the messages above (the
        # graceful-shutdown line, final earnings) actually make it to
        # the file before the process exits.
        try:
            stop_log_listener()
        except Exception:
            pass

    _install_console_close_handler(_shutdown_now)
    # Disable QuickEdit so a user click inside the console never parks
    # the main loop by blocking stdout writes. See the function's
    # docstring for the failure mode this prevents.
    _disable_console_quickedit()

    try:
        main_loop(
            cfg, coord, state, log,
            once=args.once,
            should_exit=lambda: (
                keep_awake_holder.get("exit_requested", False)
                or stop_event.is_set()
            ),
            tools=tools,
            smart_rt=smart_rt,
        )
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        _shutdown_now()
    return 0


if __name__ == "__main__":
    sys.exit(main())
