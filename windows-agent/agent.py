"""GamerAI Windows agent.

Runs in the background on a gamer's machine, picks up inference jobs from
the coordinator only when the system is idle, and tracks per-job earnings.

Usage:
    python agent.py                      # foreground, prints to console
    python agent.py --tray               # tray mode: hidden console + system tray icon
    python agent.py --background         # deprecated alias for --tray
    python agent.py --config config.json
    python agent.py --once               # process at most one job, then exit
    python agent.py --status             # print local earnings totals and exit

This file is single-file by design so it can be packaged with:
    pyinstaller --onefile --name agent agent.py
"""
from __future__ import annotations

import argparse
import base64
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
AGENT_VERSION = "1.1.19"

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
        log.info(
            "cleaned up %d stale _MEI dir(s) in %TEMP%", removed,
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
    # after update" failure on contributor machines.
    def _ps_relaunch(target: Path, args: list[str]) -> str:
        """Build the PowerShell line that update.bat invokes to launch
        the swapped agent.exe.

        v1.1.15+ uses WMI's Win32_Process.Create rather than
        Start-Process. Why: the v1.1.13 -> v1.1.14 test exposed a
        silent-relaunch failure where the new agent.exe was spawned
        (Start-Process returned errorlevel=0) but its PyInstaller
        bootloader exited before extracting (no _MEI orphan, no event
        log entry, no agent-boot.log line). Manual launches of the
        SAME exe immediately afterward worked. The strongest theory
        is that the chain `subprocess.Popen(cmd, DETACHED_PROCESS) ->
        cmd.exe -> powershell.exe -> Start-Process -> agent.exe`
        keeps the new agent.exe inside the parent PyInstaller worker's
        job object — and when the bat finishes and the job's last
        handle goes away, the new bootstrap gets terminated as a
        side-effect.

        Win32_Process.Create routes the spawn through WmiPrvSE
        (svchost), so the new agent.exe's parent is the WMI service.
        Complete escape from any inherited job, console state, or
        env-var chain (WMI gives the spawned process its own clean
        env, so the _MEIPASS2 inheritance fix from v1.1.13 is now
        redundant — left as belt-and-suspenders for anyone bypassing
        WMI in a custom build).

        Encoding via -EncodedCommand sidesteps the cmd-quote /
        PowerShell-quote escaping nightmare. The base64-encoded
        UTF-16LE string is opaque to cmd, so paths with spaces,
        single quotes, etc. all pass through cleanly.
        """
        target_str = str(target).replace("'", "''")
        dir_str = str(target.parent).replace("'", "''")
        args_str = " ".join(args)
        # Build the new process's command line in PowerShell rather
        # than baking it into a single literal — keeps the embedded
        # double-quotes around the exe path out of the surrounding
        # encoding chain.
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
    relaunch_line = _ps_relaunch(exe, relaunch_args)
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
        ":: ---- forensic trace setup (v1.1.11+) ----\r\n"
        'if not exist "%LOCALAPPDATA%\\GamerAI" mkdir '
        '"%LOCALAPPDATA%\\GamerAI" >nul 2>nul\r\n'
        'set TRACE=%LOCALAPPDATA%\\GamerAI\\update-bat-trace.log\r\n'
        'echo === %DATE% %TIME% update.bat starting === > "%TRACE%"\r\n'
        'echo bat path: %~f0 >> "%TRACE%"\r\n'
        'echo === bat content follows === >> "%TRACE%"\r\n'
        'type "%~f0" >> "%TRACE%"\r\n'
        'echo === bat content end === >> "%TRACE%"\r\n'
        ":: Step 1: give the parent agent time to exit cleanly.\r\n"
        'echo %TIME% step 1: waiting for parent exit >> "%TRACE%"\r\n'
        "timeout /t 3 /nobreak >nul\r\n"
        ":: Step 2: defensive taskkill against the running exe by name.\r\n"
        'echo %TIME% step 2: taskkill /F /IM "' + exe_basename + '" >> "%TRACE%"\r\n'
        f'taskkill /F /IM "{exe_basename}" >>"%TRACE%" 2>&1\r\n'
        ":: 8s (was 2s pre-v1.1.14): taskkill /F doesn't wait for the\r\n"
        ":: kernel to release mapped sections, file handles, or the\r\n"
        ":: parent's job object. A new PyInstaller --onefile bootstrap\r\n"
        ":: launched too eagerly into that residual state has been\r\n"
        ":: observed to exit silently (no event log, no _MEI orphan).\r\n"
        ":: 8s is empirically generous; the cost is acceptable on an\r\n"
        ":: update path that runs at most every few hours.\r\n"
        "timeout /t 8 /nobreak >nul\r\n"
        ":: Step 3: snapshot the current exe as a manual rollback path.\r\n"
        ":: `copy` (not move) so a subsequent swap-failure leaves the\r\n"
        ":: original target intact. After a successful swap, the user\r\n"
        ":: can roll back by running agent.exe.previous directly --\r\n"
        ":: useful when a release ships a regression worse than what\r\n"
        ":: it replaced.\r\n"
        'echo %TIME% step 3: snapshot to .previous >> "%TRACE%"\r\n'
        f'copy /Y "{exe}" "{previous_exe}" >>"%TRACE%" 2>&1\r\n'
        ":: Step 4: swap with 6-attempt exponential backoff. Total\r\n"
        ":: budget ~60s -- long enough to survive Avast / Defender\r\n"
        ":: scanning the .new file on first execute.\r\n"
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
        "timeout /t %wait% /nobreak >nul\r\n"
        "goto try_move\r\n"
        "\r\n"
        ":move_done\r\n"
        'echo %TIME% step 5: relaunch (success path) >> "%TRACE%"\r\n'
        + relaunch_with_trace
        + 'echo %TIME% step 5 done errorlevel=%errorlevel% >> "%TRACE%"\r\n'
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
        'echo update-failed %DATE% %TIME% attempts=%attempts% '
        f'target="{exe}" staged="{new_exe}" '
        '>> "%LOCALAPPDATA%\\GamerAI\\update-failed.txt"\r\n'
        ":: Best-effort cleanup of the orphan rollback snapshot --\r\n"
        ":: target was never swapped, so .previous is now a duplicate.\r\n"
        f'del "{previous_exe}" >nul 2>nul\r\n'
        + relaunch_with_trace
        + 'echo %TIME% step 5 done errorlevel=%errorlevel% (failed-path) >> "%TRACE%"\r\n',
        encoding="ascii",
    )
    return bat


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
        "model": "llama3.2:1b",
        "ollama_url": "http://localhost:11434",
        "mirror_base_url": None,  # null = use coordinator_url
        # Image-generation bootstrap. Best-effort: failure leaves the
        # agent running with chat only. Downloads sd.exe +
        # stable-diffusion.dll + the default GGUF from
        # {mirror_base}/download/{sd.exe,stable-diffusion.dll} and
        # {mirror_base}/download/sd-models/<slug>.gguf. The mirror is
        # populated by infra/setup-image-mirror.sh.
        "image_enabled": True,
        "image_model": "sd1.5",
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
    bootstrap_image_enabled: bool
    bootstrap_image_model: str
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
            bootstrap_image_enabled=bool(bootstrap.get("image_enabled", False)),
            bootstrap_image_model=str(bootstrap.get("image_model", "sd1.5")),
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
def setup_logging(headless: bool = False) -> logging.Logger:
    """Install the file + (optionally) stdout log handlers.

    ``headless=True`` skips the StreamHandler entirely — meant for any
    future truly-console-less mode. Tray mode passes ``headless=False``
    because the hidden console buffers stdout into its scrollback, which
    is exactly what the user sees when they click "Show console".
    """
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

    if not headless:
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

    def next_job(self, tool: str = "chat") -> Optional[dict]:
        """Pull the next job for *tool*. Defaults to chat for the legacy
        single-queue path; an image-capable worker calls this twice
        (chat, then image) per main-loop tick so it serves both queues
        without starving either."""
        out = self._post(
            "/jobs/next",
            {"worker_id": self.worker_id, "tool": tool},
            timeout=10,
        )
        return (out or {}).get("job")

    def claim(self, job_id: str) -> bool:
        return self._post("/jobs/claim", {"worker_id": self.worker_id, "job_id": job_id}) is not None

    def complete(self, payload: dict) -> Optional[dict]:
        return self._post("/jobs/complete", payload, timeout=30)

    def partial(self, job_id: str, text: str) -> None:
        """Push the accumulated streaming text so far. Fire-and-forget:
        the coordinator overwrites with the latest call (text is the
        FULL accumulated output, not a delta), and a dropped partial
        just means the next one carries the full state. The final
        ``/jobs/complete`` is the source of truth."""
        self._post(
            "/jobs/partial",
            {"worker_id": self.worker_id, "job_id": job_id, "text": text},
            timeout=3,
        )

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
    log.info(
        "image bootstrap: ready (install_dir=%s, model=%s)",
        install_dir, model_path,
    )
    return True


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
    use_model = model or os.getenv("MODEL") or "llama3.2:1b"
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
    width = int(params.get("width") or 512)
    height = int(params.get("height") or 512)
    steps = int(params.get("steps") or 20)
    seed = params.get("seed")
    negative = params.get("negative_prompt") or ""
    model_slug = cfg.bootstrap_image_model
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
    # version, re-verify this flag still applies.
    # The prompt is passed via a temp file (sd.cpp tolerates long
    # multi-line prompts that way) — using argv directly hits Windows'
    # 32K command-line cap on pathological inputs.
    prompt_file = out_path.with_suffix(".prompt.txt")
    prompt_file.write_text(prompt, encoding="utf-8")
    argv = [
        str(sd_binary_path()),
        "-M", "img_gen",
        "-m", str(model_path),
        "-p", prompt,
        "-W", str(width),
        "-H", str(height),
        "--steps", str(steps),
        "-o", str(out_path),
    ]
    if seed is not None:
        argv.extend(["--seed", str(int(seed))])
    if negative:
        argv.extend(["-n", negative])
    log.info(
        "image: running sd.exe (model=%s %dx%d steps=%d)",
        model_slug, width, height, steps,
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
    if result.returncode != 0:
        snippet = (result.stderr or result.stdout or "").strip()[:500]
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
# Idle gate
# ---------------------------------------------------------------------------
def is_system_idle(cfg: Config) -> tuple[bool, str]:
    idle_for = input_idle_seconds()
    if not cfg.stayalive and idle_for < cfg.min_input_idle_seconds:
        return False, f"user active ({idle_for:.0f}s since last input)"
    cpu = cpu_percent(cfg.cpu_sample_seconds)
    if cpu >= cfg.max_cpu_percent:
        return False, f"cpu busy ({cpu:.1f}%)"
    suffix = " stayalive" if cfg.stayalive else ""
    return True, f"idle ({idle_for:.0f}s, cpu {cpu:.1f}%{suffix})"


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


_STDIN_HELP = (
    "commands:\n"
    "  update          download the latest agent.exe and relaunch\n"
    "  version         print the running version + build\n"
    "  status          print worker_id and current status\n"
    "  stayalive       toggle the user-input idle gate (or use 'on'/'off')\n"
    "  stayalive on    ignore user-input idleness (CPU-only gate)\n"
    "  stayalive off   restore normal user-input idle gate\n"
    "  help            show this list\n"
    "  quit            exit the agent\n"
)


def stdin_command_loop(
    log: logging.Logger,
    worker_id: str,
    force_update_event: threading.Event,
    stop_event: threading.Event,
    cfg: "Config",
    state: dict,
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

        did_work, processed_job_id = process_one(
            cfg, coord, state, log, tools=tools,
        )
        if did_work:
            just_drained_job_id = processed_job_id
        if once and did_work:
            return
        if not did_work:
            time.sleep(cfg.polling_interval)


def _ordered_queues(
    tools: Optional[list[str]], last_tool: Optional[str],
) -> list[str]:
    """Per-tool poll order for one main-loop tick.

    Defaults to ["chat"]; appends "image" when the agent advertises it.
    Promotes the agent's *last-served* tool to the front so back-to-
    back jobs of the same kind keep the model warm in VRAM — a chat
    streak keeps Ollama hot; an image streak keeps sd.cpp's weights
    loaded. The flip only happens when the preferred queue is empty
    AND the other queue has work, so demand-shaped routing still
    falls out naturally without any coordinator-side state."""
    available = ["chat"]
    if tools and "image" in tools:
        available.append("image")
    if last_tool in available and last_tool != available[0]:
        return [last_tool] + [t for t in available if t != last_tool]
    return available


def process_one(
    cfg: Config,
    coord: Coordinator,
    state: dict,
    log: logging.Logger,
    tools: Optional[list[str]] = None,
) -> tuple[bool, Optional[str]]:
    """Pop, claim, run, complete one job. Returns (did_work, job_id).
    The job_id is captured so the main loop can reference it in the
    next iteration's drain-visibility log line."""
    queue_order = _ordered_queues(tools, state.get("last_tool"))
    job = None
    for q in queue_order:
        job = coord.next_job(tool=q)
        if job:
            break
    if not job:
        return False, None
    job_id = job.get("job_id")
    tool = (job.get("tool") or "chat").lower()
    prompt = job.get("prompt", "")
    log.info("job %s started (tool=%s)", job_id, tool)
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
        if tool == "image":
            image_b64, model_used = run_image_inference(
                prompt, job, cfg, log,
            )
            duration = round(time.time() - started, 3)
            out = coord.complete(
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
                }
            )
            earnings = float((out or {}).get("earnings", 0.0))
            state["jobs"] = int(state.get("jobs", 0)) + 1
            state["earnings_usd"] = round(
                float(state.get("earnings_usd", 0.0)) + earnings, 10,
            )
            state["last_tool"] = "image"
            save_state(state)
            log.info(
                "job %s finished (image): $%.8f, %.2fs",
                job_id, earnings, duration,
            )
        else:
            result = run_inference(
                prompt,
                cfg.model or job.get("model"),
                log,
                messages=job.get("messages"),
                on_partial=lambda text: coord.partial(job_id, text),
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
            state["last_tool"] = "chat"
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
    info("earnings", f"${float(state.get('earnings_usd', 0.0)):.6f}")
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
                   help="print local earnings totals and exit")
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
    args = p.parse_args(argv)
    # Deprecation alias: --background → --tray. v1.1.12 replaces the
    # silent --background mode with tray + hidden console; existing
    # autostart shortcuts (created by pre-1.1.12 installers) pass
    # --background and must keep working until the user reinstalls.
    args.background_was_used = bool(args.background)
    if args.background and not args.tray:
        args.tray = True
    return args


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

    # Tray-mode init: hide the console window and claim 127.0.0.1:48591
    # as the single-instance + IPC port. Done BEFORE the greeting and
    # logger setup so a second launch exits immediately without
    # touching state. --once / --status / --version skip tray setup;
    # they're foreground subcommands a user invoked directly.
    console_hwnd = 0
    ipc_sock: Optional[socket.socket] = None
    tray_active = args.tray and not args.once and not args.status
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
        ipc_sock, is_first = _claim_single_instance()
        if not is_first:
            # Another agent is already running; we already told it to
            # surface its console. Exit silently.
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
        print(f"earnings:  ${float(state.get('earnings_usd', 0.0)):.6f}")
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

    # First-run token UX: tray mode hides the console at startup, so
    # the existing input("token: ") prompt would block invisibly. If
    # we know we'll need to prompt (no token in env/config/state),
    # fire a toast and unhide the console first. We re-hide after a
    # successful coordinator registration below.
    have_token = bool(cfg.api_token or state.get("api_token"))
    auto_unhidden_for_token = False
    if tray_active and not have_token:
        _toast(
            "GamerAI Agent needs your token",
            "The console will open — paste the token from your invite page.",
            icon_path=_tray_icon_path(),
        )
        _show_console(console_hwnd)
        auto_unhidden_for_token = True
    # background=False: we always have a console available (hidden or
    # visible). The deprecated truly-headless path no longer exists.
    token = resolve_api_token(cfg.api_token, state, background=False)
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
    # pointing at a remote/test Ollama keep that override.
    if not os.getenv("OLLAMA_URL"):
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
    # from the per-tool image queue; on failure we stay chat-only.
    image_ready = bootstrap_image_inference(cfg, log)

    coord = Coordinator(cfg.coordinator_url, worker_id, log, cfg.api_token)
    # Advertise the model and tools we can actually serve. The chat
    # bootstrap above either confirmed the model is loaded into Ollama
    # or fell back to mock; image bootstrap is independent (sd.exe +
    # GGUF on disk). Coordinator-side routing uses capabilities.tools
    # to pick the right Redis queue when /jobs/next is called.
    tools = ["chat"]
    if image_ready:
        tools.append("image")
    capabilities: Optional[dict] = None
    if cfg.bootstrap_enabled and os.getenv("OLLAMA_URL"):
        capabilities = {"models": [cfg.bootstrap_model], "tools": tools}
    elif image_ready:
        capabilities = {"models": [cfg.bootstrap_image_model], "tools": ["image"]}
    if not coord.register(capabilities=capabilities):
        if keep_awake_active:
            keep_awake_end(log)
        return 1

    # Registration succeeded. If we forcibly unhid the console for the
    # first-run token prompt, hide it again now and fire a confirmation
    # toast so the user knows the agent is online.
    if auto_unhidden_for_token:
        _toast(
            "GamerAI Agent online",
            f"Token saved. Worker {worker_id[:8]}… connected.",
            icon_path=_tray_icon_path(),
        )
        _hide_console(console_hwnd)

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
        args=(log, worker_id, force_update_event, stop_event, cfg, state),
        name="gamerai-stdin",
        daemon=True,
    )
    stdin_thread.start()

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
            coord.heartbeat("offline")
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

    _install_console_close_handler(_shutdown_now)

    try:
        main_loop(
            cfg, coord, state, log,
            once=args.once,
            should_exit=lambda: (
                keep_awake_holder.get("exit_requested", False)
                or stop_event.is_set()
            ),
            tools=tools,
        )
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        _shutdown_now()
    return 0


if __name__ == "__main__":
    sys.exit(main())
