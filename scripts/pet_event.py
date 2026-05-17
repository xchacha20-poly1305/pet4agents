#!/usr/bin/env python3
"""
Hook entry for claude-code-pet.

Usage:
    pet_event.py <EventName>          # called by Claude Code / Codex hooks
    pet_event.py daemon-stop          # quit the daemon
    pet_event.py set-pet <pet-id>     # switch the active pet
    pet_event.py daemon-spawn         # internal: start daemon (called via fork)

Reads hook JSON from stdin, sends a one-line JSON message to the daemon over a
Unix socket. If the daemon socket doesn't exist and the event is SessionStart,
spawns the daemon detached. All errors are swallowed (logged to event.log) so
hooks never block the coding agent.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

# Make `import config` work regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def _log(line: str) -> None:
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with config.LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
    except Exception:
        pass


def _log_install(line: str) -> None:
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with config.INSTALL_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
    except Exception:
        pass


def in_pet_venv() -> bool:
    """True if we're already running under our managed venv."""
    try:
        return Path(sys.executable).resolve() == config.VENV_PY.resolve()
    except Exception:
        return False


def venv_has_pyside6() -> bool:
    """Check whether PySide6 is importable in the managed venv."""
    if not config.VENV_PY.exists():
        return False
    try:
        r = subprocess.run(
            [str(config.VENV_PY), "-c", "import PySide6"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _create_venv_with_uv(uv: str) -> bool:
    """Create the managed venv using uv. Returns True on success.

    `--seed` ensures pip/setuptools/wheel are present, so the stdlib pip
    fallback still works if the uv install step later fails.
    """
    _log_install(f"creating venv at {config.VENV_DIR} (via uv)")
    try:
        r = subprocess.run(
            [uv, "venv", "--seed", str(config.VENV_DIR)],
            capture_output=True,
            timeout=120,
        )
        if r.returncode == 0:
            return True
        _log_install("uv venv failed: " + r.stderr.decode("utf-8", "replace"))
    except Exception as e:
        _log_install(f"uv venv exception: {e}")
    return False


def _create_venv_with_stdlib() -> bool:
    """Create the managed venv using the stdlib venv module."""
    _log_install(f"creating venv at {config.VENV_DIR} (via stdlib venv)")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "venv", str(config.VENV_DIR)],
            capture_output=True,
            timeout=120,
        )
        if r.returncode != 0:
            _log_install("venv create failed: " + r.stderr.decode("utf-8", "replace"))
            return False
        return True
    except Exception as e:
        _log_install(f"venv create exception: {e}")
        return False


def _install_pyside6_with_uv(uv: str) -> bool:
    """Install PySide6 into the managed venv via uv. Returns True on success."""
    _log_install("installing PySide6 via uv (this may take a while)")
    try:
        r = subprocess.run(
            [
                uv, "pip", "install",
                "--python", str(config.VENV_PY),
                "--quiet",
                "PySide6",
            ],
            capture_output=True,
            timeout=600,
        )
        if r.returncode == 0:
            _log_install("PySide6 installed successfully (via uv)")
            return True
        _log_install("uv pip install failed: " + r.stderr.decode("utf-8", "replace"))
    except Exception as e:
        _log_install(f"uv pip install exception: {e}")
    return False


def _install_pyside6_with_pip() -> bool:
    """Install PySide6 into the managed venv via the venv's own pip."""
    _log_install("installing PySide6 via pip (this may take a while)")
    try:
        r = subprocess.run(
            [
                str(config.VENV_PY),
                "-m", "pip", "install",
                "--quiet", "--disable-pip-version-check",
                "PySide6",
            ],
            capture_output=True,
            timeout=600,
        )
        if r.returncode != 0:
            _log_install("pip install failed: " + r.stderr.decode("utf-8", "replace"))
            return False
        _log_install("PySide6 installed successfully")
        return True
    except Exception as e:
        _log_install(f"pip install exception: {e}")
        return False


def install_venv() -> bool:
    """Create the venv and install PySide6. Returns True on success.

    Prefers `uv` when available (it's much faster) and falls back to the
    stdlib `venv` module + `pip` otherwise. The fallback is independent
    per step: if `uv venv` fails we retry creation with stdlib; if the uv
    install step fails we retry the install with the venv's own pip.
    """
    config.ensure_dirs()
    uv = shutil.which("uv")

    if not config.VENV_PY.exists():
        created = bool(uv) and _create_venv_with_uv(uv)
        if not created and not _create_venv_with_stdlib():
            return False

    if uv and _install_pyside6_with_uv(uv):
        return True
    return _install_pyside6_with_pip()


def ensure_venv_and_reexec() -> None:
    """If not already in the managed venv, ensure it exists and re-exec under it."""
    if in_pet_venv():
        return
    if not venv_has_pyside6():
        if not install_venv():
            _log("venv install failed; bailing out")
            sys.exit(0)
    # Re-exec under the venv python so PySide6 is available.
    os.execv(str(config.VENV_PY), [str(config.VENV_PY), os.path.abspath(__file__), *sys.argv[1:]])


def send_to_daemon(payload: dict, timeout: float = 1.0) -> bool:
    """Send one JSON line to the daemon. Returns True on success."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(config.SOCKET_PATH))
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        s.close()
        return True
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
        return False


# ------------------------- Agent PID discovery -------------------------
#
# When a session ends "cleanly" the agent fires the SessionEnd hook, which
# tells the daemon to drain the session. But if the agent crashes / is
# SIGKILL'd / the terminal is closed, no SessionEnd ever lands. To recover,
# we ship the agent PID along with each event so the daemon can poll
# liveness and treat a dead PID as an implicit SessionEnd.
#
# `os.getppid()` alone isn't always the agent — hooks may run via a
# shell wrapper that exits as soon as the hook returns, which would make the
# daemon think the session died immediately. So we walk up the /proc tree
# until we find an ancestor whose comm/argv[0] looks like Claude or Codex, and use
# that PID. Falls back to getppid() when /proc isn't available or no
# matching ancestor is found.

AGENT_PROCESS_NAMES = {"claude", "codex"}


def _agent_name(pid: int) -> str:
    """Return the agent name ("claude"|"codex") if pid is a supported agent binary, else "".

    Matches on `comm` (kernel-truncated executable basename) and on argv[0]'s
    basename. This keeps us from matching shell wrappers whose cmdline merely
    references paths under `~/.claude/` or `~/.codex/`.
    """
    try:
        comm = Path(f"/proc/{pid}/comm").read_text("utf-8").strip().lower()
        if comm in AGENT_PROCESS_NAMES:
            return comm
    except OSError:
        pass
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        argv0 = cmdline.split(b"\x00", 1)[0]
        if argv0:
            name = Path(argv0.decode("utf-8", "replace")).name.lower()
            # Strip a trailing .exe defensively for unusual wrapper builds.
            if name.endswith(".exe"):
                name = name[:-4]
            if name in AGENT_PROCESS_NAMES:
                return name
    except OSError:
        pass
    return ""


def _is_agent_pid(pid: int) -> bool:
    return bool(_agent_name(pid))


def _read_ppid(pid: int) -> int:
    """Read PPid from /proc/<pid>/status. Returns 0 on error."""
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def find_agent_info() -> tuple[int, str]:
    """Walk up the process tree to find the Claude Code or Codex process.

    Returns (pid, agent_type) where agent_type is "claude"|"codex"|"".
    Falls back to (os.getppid(), "") if no agent ancestor is found or if /proc
    isn't available (non-Linux / containers without procfs)."""
    fallback = os.getppid()
    try:
        if not Path("/proc").exists():
            return fallback, ""
        pid = fallback
        seen: set[int] = set()
        # Cap iterations to defend against hostile /proc edits — process trees
        # in practice are well under 64 deep.
        for _ in range(64):
            if pid <= 1 or pid in seen:
                break
            seen.add(pid)
            name = _agent_name(pid)
            if name:
                return pid, name
            ppid = _read_ppid(pid)
            if ppid <= 0:
                break
            pid = ppid
    except Exception:
        pass
    return fallback, ""


def find_agent_pid() -> int:
    return find_agent_info()[0]


def spawn_daemon() -> None:
    """Detached spawn of pet_daemon.py under the managed venv."""
    daemon_script = Path(__file__).resolve().parent / "pet_daemon.py"
    if not daemon_script.exists():
        _log(f"daemon script not found: {daemon_script}")
        return
    try:
        config.ensure_dirs()
        log_f = config.LOG_PATH.open("a", encoding="utf-8")
        env = os.environ.copy()
        if env.get("CLAUDE_PET_QPA_PLATFORM"):
            env["QT_QPA_PLATFORM"] = env["CLAUDE_PET_QPA_PLATFORM"]
        elif env.get("DISPLAY"):
            # Prefer XWayland (xcb) when an X server is reachable. Native
            # Wayland's xdg_toplevel.move grabs the pointer for the duration
            # of a drag — the app receives no events and can't update the
            # facing direction mid-drag. Under xcb the WM-driven move keeps
            # delivering moveEvent, which our drag-state tracker depends on.
            # Users who explicitly want native Wayland can set
            # CLAUDE_PET_QPA_PLATFORM=wayland.
            env["QT_QPA_PLATFORM"] = "xcb"
        subprocess.Popen(
            [str(config.VENV_PY), str(daemon_script)],
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        _log("spawned daemon")
    except Exception as e:
        _log(f"spawn_daemon failed: {e}\n{traceback.format_exc()}")


def cmd_event(event_name: str) -> None:
    """Forward a hook event to the daemon."""
    raw = ""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
    except Exception:
        raw = ""

    hook_data = {}
    if raw.strip():
        try:
            hook_data = json.loads(raw)
        except Exception:
            hook_data = {"raw": raw[:200]}

    agent_pid, agent_type = find_agent_info()
    payload = {
        "kind": "event",
        "event": event_name,
        "session_id": hook_data.get("session_id", ""),
        "cwd": hook_data.get("cwd", os.getcwd()),
        # Daemon polls this PID for liveness so it can drain the session even
        # if SessionEnd never fires (agent crash / SIGKILL / terminal closed).
        "parent_pid": agent_pid,
        # Which tool fired this event — used for per-tool pet selection.
        "agent_type": agent_type,
    }

    ok = send_to_daemon(payload)
    if not ok:
        # Daemon not running. Spawn on SessionStart; otherwise stay silent.
        if event_name == "SessionStart":
            spawn_daemon()
            # Give the daemon a moment to bind the socket, then deliver the event.
            for _ in range(20):
                time.sleep(0.1)
                if send_to_daemon(payload):
                    break
            else:
                _log("daemon did not come up in time after spawn")
        else:
            _log(f"daemon not running, skipping event={event_name}")


def cmd_daemon_stop() -> None:
    if not send_to_daemon({"kind": "quit"}):
        print("pet daemon is not running")
        return
    print("pet daemon stop requested")


def cmd_set_pet(pet_id: str) -> None:
    pet_id = pet_id.strip()
    if not pet_id or "/" in pet_id or pet_id.startswith("."):
        print(f"invalid pet id: {pet_id!r}")
        return
    config.ensure_dirs()
    cfg: dict = {}
    if config.CONFIG_PATH.exists():
        try:
            cfg = json.loads(config.CONFIG_PATH.read_text("utf-8"))
        except Exception:
            cfg = {}
    cfg["pet_id"] = pet_id
    config.CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    sent = send_to_daemon({"kind": "reload"})
    print(f"pet set to {pet_id}" + ("" if sent else " (daemon not running)"))


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0

    sub = args[0]

    # Subcommands that don't require the venv (no PySide6 needed)
    if sub == "daemon-stop":
        cmd_daemon_stop()
        return 0
    if sub == "set-pet":
        if len(args) < 2:
            print("usage: pet_event.py set-pet <pet-id>")
            return 1
        cmd_set_pet(args[1])
        return 0

    # All other subcommands need PySide6 (the daemon does the rendering, but
    # SessionStart needs to spawn it, so we ensure venv exists first).
    ensure_venv_and_reexec()

    if sub == "daemon-spawn":
        spawn_daemon()
        return 0

    # Default: treat the first arg as a hook event name.
    cmd_event(sub)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _log(f"top-level exception: {e}\n{traceback.format_exc()}")
        sys.exit(0)
