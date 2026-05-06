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
# (animation, is_oneshot)
# is_oneshot=True: play once then return to base; base is unchanged
# is_oneshot=False: change base state (looping)
EVENT_MAP: dict[str, tuple[str, bool]] = {
    "SessionStart":       ("waving",  True),
    "SessionEnd":         ("waving",  True),
    "UserPromptSubmit":   ("running", False),
    "PreToolUse":         ("running", False),
    "Stop":               ("jumping", True),    # also resets base to idle (handled in daemon)
    "SubagentStop":       ("jumping", True),
    "Notification":       ("review",  True),
    "PermissionRequest":  ("waving",  True),
    "PostToolUseFailure": ("failed",  True),
}

# Events that should also reset base state to idle
RESET_BASE_EVENTS = {"Stop", "SubagentStop", "SessionEnd"}

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
