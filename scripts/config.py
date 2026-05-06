"""Shared constants for claude-code-pet."""
from __future__ import annotations

import json
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
PLUGIN_ROOT = Path(
    os.environ.get("CLAUDE_PLUGIN_ROOT")
    or os.environ.get("CODEX_PLUGIN_ROOT")
    or Path(__file__).resolve().parent.parent
)
PLUGIN_PETS_DIR = PLUGIN_ROOT / "pets"


def ensure_dirs() -> None:
    """Create the writable dirs we use. Safe to call repeatedly."""
    for d in (CONFIG_DIR, DATA_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --- User config (`~/.config/claude-code-pet/config.json`) ---
#
# The on-disk shape is a flat JSON object. Unknown keys are preserved on write
# (see `pet_event.py:cmd_set_pet`). Defaults below are merged at read-time so
# missing keys never crash the daemon.
DEFAULT_USER_CONFIG: dict = {
    # Empty string = use auto-discovery order (env -> codex pets -> plugin pets).
    "pet_id": "",
    # When False (default) the daemon exits after the last Claude session ends.
    # When True the daemon keeps running until `/pet-stop` (legacy behavior).
    "stay_even_no_session": False,
    # Per-animation per-frame duration overrides (ms). Shape:
    #   { "<animation-name>": [d0, d1, ...], ... }
    # Each list, if provided, MUST have the same length as the default
    # durations for that animation (one entry per frame in the row). Unknown
    # animation names and malformed entries are silently ignored so a typo in
    # config.json never bricks the daemon. JSON is the only supported entry
    # point — there is no CLI/slash-command for tweaking these.
    "animation_durations": {},
}


def load_user_config() -> dict:
    """Read the user config, returning a dict that always contains the keys in
    `DEFAULT_USER_CONFIG`. Never raises — corrupt/missing files fall back to
    defaults so the daemon stays robust."""
    cfg = dict(DEFAULT_USER_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text("utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def _apply_animation_duration_overrides() -> None:
    """Mutate `ANIMATIONS` in-place with any per-animation duration overrides
    found in the user config. Called once at import time so every consumer of
    `config.ANIMATIONS` (notably `pet_daemon.py`) sees the user's values
    without needing its own merge step.

    Validation rules (all failures are silent):
      - override map must be a dict
      - animation name must already exist in `ANIMATIONS`
      - value must be a list/tuple of positive numbers
      - length must match the default durations length (frame count is fixed
        by the spritesheet row, so changing it would desync rendering)
    """
    try:
        cfg = load_user_config()
    except Exception:
        return
    overrides = cfg.get("animation_durations")
    if not isinstance(overrides, dict):
        return
    for name, durs in overrides.items():
        spec = ANIMATIONS.get(name)
        if not spec:
            continue
        if not isinstance(durs, (list, tuple)):
            continue
        default = spec["durations"]
        if len(durs) != len(default):
            continue
        cleaned: list[int] = []
        ok = True
        for d in durs:
            if isinstance(d, bool) or not isinstance(d, (int, float)) or d <= 0:
                ok = False
                break
            cleaned.append(int(d))
        if not ok:
            continue
        spec["durations"] = cleaned


_apply_animation_duration_overrides()
