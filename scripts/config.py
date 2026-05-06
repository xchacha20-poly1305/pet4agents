"""Shared constants for claude-code-pet."""
from __future__ import annotations

import os
from pathlib import Path

# --- Atlas geometry (matches Codex pet contract) ---
CELL_W = 192
CELL_H = 208
ATLAS_COLS = 8
ATLAS_ROWS = 9
ATLAS_W = CELL_W * ATLAS_COLS  # 1536
ATLAS_H = CELL_H * ATLAS_ROWS  # 1872

# --- Animation table (row index, used columns, per-frame durations in ms) ---
# Source: ~/.claude/skills/hatch-pet/references/animation-rows.md
ANIMATIONS: dict[str, dict] = {
    "idle":          {"row": 0, "durations": [280, 110, 110, 140, 140, 320]},
    "running-right": {"row": 1, "durations": [120, 120, 120, 120, 120, 120, 120, 220]},
    "running-left":  {"row": 2, "durations": [120, 120, 120, 120, 120, 120, 120, 220]},
    "waving":        {"row": 3, "durations": [140, 140, 140, 280]},
    "jumping":       {"row": 4, "durations": [140, 140, 140, 140, 280]},
    "failed":        {"row": 5, "durations": [140, 140, 140, 140, 140, 140, 140, 240]},
    "waiting":       {"row": 6, "durations": [150, 150, 150, 150, 150, 260]},
    "running":       {"row": 7, "durations": [120, 120, 120, 120, 120, 220]},
    "review":        {"row": 8, "durations": [150, 150, 150, 150, 150, 280]},
}

# Which states loop forever vs play once
LOOPING_STATES = {"idle", "running", "waiting", "running-right", "running-left"}
ONESHOT_STATES = {"waving", "jumping", "failed", "review"}

# --- Event → animation mapping ---
#
# Three orthogonal tables. Any event may appear in zero, one, or several:
#   INTERVAL_OPEN  : pushes a looping animation onto the base stack
#   INTERVAL_CLOSE : pops matching opens off the top of the stack
#   ONESHOTS       : plays a single animation overlaid on whatever's on top
#
# An event is processed in order: close -> open -> oneshot. This lets a single
# event (e.g. PreToolUse) close one interval (PermissionRequest) and open
# another (a tool-execution interval) atomically.
#
# Why a stack? Tool calls nest inside the prompt turn, so PostToolUse needs to
# fall back to the *outer* "running" loop, not all the way to idle. The stack
# also lets terminal events like Notification / PermissionRequest loop forever
# until the next user action implicitly closes them — instead of flashing once
# and being missed.

# event -> animation pushed when the event opens an interval
INTERVAL_OPEN: dict[str, str] = {
    "UserPromptSubmit":  "running",
    "PreToolUse":        "waiting",
    "Notification":      "review",
    "PermissionRequest": "waving",
}

# event -> set of opener event names this event closes (popped from stack top
# while the topmost entry is in the set; stops at the first non-match so
# nested intervals stay correct)
INTERVAL_CLOSE: dict[str, set[str]] = {
    "PostToolUse":        {"PreToolUse"},
    "PostToolUseFailure": {"PreToolUse"},
    # Tool execution implies any pending permission request was resolved.
    "PreToolUse":         {"PermissionRequest"},
    # User responding closes any pending notification/permission alert.
    "UserPromptSubmit":   {"Notification", "PermissionRequest"},
    # Terminal events tear everything down to base.
    "Stop":               {"UserPromptSubmit", "PreToolUse",
                           "Notification", "PermissionRequest"},
    "SubagentStop":       {"UserPromptSubmit", "PreToolUse",
                           "Notification", "PermissionRequest"},
    "SessionEnd":         {"UserPromptSubmit", "PreToolUse",
                           "Notification", "PermissionRequest"},
}

# event -> animation played once as a flash overlay on the current base
ONESHOTS: dict[str, str] = {
    "SessionStart":       "waving",
    "SessionEnd":         "waving",
    "Stop":               "jumping",
    "SubagentStop":       "jumping",
    "PostToolUseFailure": "failed",
}

# --- Filesystem paths ---
def _xdg(env: str, default: Path) -> Path:
    val = os.environ.get(env)
    return Path(val) if val else default

HOME = Path.home()
XDG_CONFIG = _xdg("XDG_CONFIG_HOME", HOME / ".config")
XDG_DATA = _xdg("XDG_DATA_HOME", HOME / ".local/share")
XDG_STATE = _xdg("XDG_STATE_HOME", HOME / ".local/state")
XDG_RUNTIME = _xdg("XDG_RUNTIME_DIR", Path("/tmp"))

CONFIG_DIR = XDG_CONFIG / "claude-code-pet"
DATA_DIR = XDG_DATA / "claude-code-pet"
STATE_DIR = XDG_STATE / "claude-code-pet"
VENV_DIR = DATA_DIR / "venv"
VENV_PY = VENV_DIR / "bin" / "python"
SOCKET_PATH = XDG_RUNTIME / "claude-code-pet.sock"
PIDFILE_PATH = STATE_DIR / "daemon.pid"
LOG_PATH = STATE_DIR / "event.log"
INSTALL_LOG_PATH = STATE_DIR / "install.log"
CONFIG_PATH = CONFIG_DIR / "config.json"
WINDOW_STATE_PATH = CONFIG_DIR / "state.json"

# --- Pet discovery roots ---
CODEX_PETS_DIR = HOME / ".codex/pets"
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))
PLUGIN_PETS_DIR = PLUGIN_ROOT / "pets"


def ensure_dirs() -> None:
    """Create the writable dirs we use. Safe to call repeatedly."""
    for d in (CONFIG_DIR, DATA_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)
