"""Shared constants for pet4agents."""
from __future__ import annotations

import json
import os
from pathlib import Path

# --- Atlas geometry (matches Codex pet contract) ---
#
# Two sprite generations are supported, distinguished by `spriteVersionNumber`
# in pet.json (absent or 1 = v1, 2 = v2):
#
#   v1: 1536x1872, 8 cols x  9 rows — the nine animation rows below.
#   v2: 1536x2288, 8 cols x 11 rows — same nine animation rows, plus a neutral
#       look cell at row 0 / col 6 and 16 look directions filling rows 9-10.
#
# Cell size is identical in both, so every v1 row index stays valid for v2.
CELL_W = 192
CELL_H = 208
ATLAS_COLS = 8
SPRITE_V1 = 1
SPRITE_V2 = 2
ATLAS_ROWS_BY_VERSION = {SPRITE_V1: 9, SPRITE_V2: 11}
ATLAS_W = CELL_W * ATLAS_COLS  # 1536
ATLAS_ROWS = ATLAS_ROWS_BY_VERSION[SPRITE_V1]
ATLAS_H = CELL_H * ATLAS_ROWS  # 1872


def atlas_height(sprite_version: int) -> int:
    """Expected atlas height in pixels for a sprite generation."""
    return CELL_H * ATLAS_ROWS_BY_VERSION.get(sprite_version, ATLAS_ROWS)


def sprite_version_for_height(height: int) -> int | None:
    """Reverse lookup: which sprite generation an atlas of this pixel height
    is, or None if it matches neither contract."""
    for version, rows in ATLAS_ROWS_BY_VERSION.items():
        if height == CELL_H * rows:
            return version
    return None


# --- v2 look-direction layout ---
#
# 16 cells, clockwise, index 0 = looking straight up, index 4 = right,
# 8 = down, 12 = left (22.5 degrees per step). They fill rows 9 and 10 left to
# right: index 0-7 in row 9, index 8-15 in row 10. `LOOK_NEUTRAL_CELL` is the
# front-facing pose used inside the pointer deadzone.
LOOK_DIRECTIONS = 16
LOOK_FIRST_ROW = 9
LOOK_NEUTRAL_CELL = (0, 6)  # (row, col)
LOOK_DEGREES_PER_STEP = 360.0 / LOOK_DIRECTIONS


def look_cell(direction_index: int) -> tuple[int, int]:
    """(row, col) of the look cell for a clockwise-from-up direction index."""
    idx = direction_index % LOOK_DIRECTIONS
    return LOOK_FIRST_ROW + idx // ATLAS_COLS, idx % ATLAS_COLS

# --- Animation table (row index, used columns, per-frame durations in ms) ---
# Source: ~/.claude/skills/hatch-pet/references/animation-rows.md. Durations
# match the Codex TUI's built-in pet (codex-rs/tui/src/pets/model.rs), whose
# idle loop is deliberately slow and calm.
ANIMATIONS: dict[str, dict] = {
    "idle":          {"row": 0, "durations": [1680, 660, 660, 840, 840, 1920]},
    "running-right": {"row": 1, "durations": [120, 120, 120, 120, 120, 120, 120, 220]},
    "running-left":  {"row": 2, "durations": [120, 120, 120, 120, 120, 120, 120, 220]},
    "waving":        {"row": 3, "durations": [140, 140, 140, 280]},
    "jumping":       {"row": 4, "durations": [140, 140, 140, 140, 280]},
    "failed":        {"row": 5, "durations": [140, 140, 140, 140, 140, 140, 140, 240]},
    "waiting":       {"row": 6, "durations": [150, 150, 150, 150, 150, 260]},
    "running":       {"row": 7, "durations": [120, 120, 120, 120, 120, 220]},
    "review":        {"row": 8, "durations": [150, 150, 150, 150, 150, 280]},
}

# How many cycles a state's animation plays before the pet settles into the
# idle loop. The state itself is kept (it decides what RESOLVES may do next);
# only the rendered animation settles. This is the Codex TUI's
# `app_state_animation` shape — a state plays three times and then hands off
# to idle — and it is what keeps the pet from animating forever when a turn
# ends without any hook: neither agent fires `Stop` for an Esc interrupt, and
# Claude Code fires nothing at all when the user denies a permission prompt.
# Oneshots always play once; drag animations loop while the button is held.
STATE_REPEATS = 3

# --- Event → animation mapping ---
#
# The pet holds one state at a time, like the Codex TUI pet, with the same
# meaning:
#   running — the agent is working on a turn
#   waiting — the agent needs the user (approval, elicitation, idle prompt)
#   review  — the turn finished and the result is ready
#   failed  — the turn ended with an error
#   idle    — nothing going on
# A new state replaces the old one and replays from its first frame.
#
# Why not nest states? Measured hook order does not nest: `PreToolUse` fires
# *before* `PermissionRequest` (in both agents), approving a prompt fires no
# event until the tool's `PostToolUse`, and background subagents send their
# tool events after the main agent's `Stop`, under the same session id.

# event -> state it switches to
STATES: dict[str, str] = {
    "UserPromptSubmit":  "running",
    "PermissionRequest": "waiting",
    # MCP server asking for input behaves like PermissionRequest.
    "Elicitation":       "waiting",
    # Only the idle prompt reaches here; see `is_noise_event`.
    "Notification":      "waiting",
    # Compaction is busy work the agent does on its own.
    "PreCompact":        "running",
    "Stop":              "review",
    "StopFailure":       "failed",
    # Codex only: the user interrupted the turn.
    "Interrupt":         "idle",
    "SessionEnd":        "idle",
}

# event -> (state, new state): switch only when the pet is in that state.
# These are the signals that a needs-input state was answered and the agent
# carries on. They fire for every tool call, so they must not touch any other
# state (a subagent's PostToolUse arriving after the main agent's Stop must
# leave `review` alone).
RESOLVES: dict[str, tuple[str, str]] = {
    "PostToolUse":        ("waiting", "running"),
    "PostToolUseFailure": ("waiting", "running"),
    "PermissionDenied":   ("waiting", "running"),
    "ElicitationResult":  ("waiting", "running"),
}

# event -> animation played once as a flash overlay on the current state
ONESHOTS: dict[str, str] = {
    "SessionStart":       "waving",
    "SessionEnd":         "waving",
    "SubagentStop":       "jumping",
    "SubagentStart":      "review",
    "TaskCreated":        "review",
    "TaskCompleted":      "jumping",
    "PostToolUseFailure": "failed",
    "PermissionDenied":   "failed",
}

# Claude Code sends this Notification ~6s after the PermissionRequest it
# duplicates; replaying `waiting` for it would only restart the animation.
NOISE_NOTIFICATION_TYPES = {"permission_prompt"}


def is_noise_event(event: str, hook_data: dict) -> bool:
    """Hook firings that don't reflect anything the user asked for.

    Claude Code runs internal subagents (prompt suggestions after each turn,
    compaction) whose `SubagentStop` carries an empty `agent_type` and has no
    matching `SubagentStart`. Real subagents always name their type, in both
    Claude Code and Codex."""
    if event == "SubagentStop":
        return not hook_data.get("agent_type")
    if event == "Notification":
        return hook_data.get("notification_type") in NOISE_NOTIFICATION_TYPES
    return False

# --- Filesystem paths ---
def _xdg(env: str, default: Path) -> Path:
    val = os.environ.get(env)
    return Path(val) if val else default

HOME = Path.home()
XDG_CONFIG = _xdg("XDG_CONFIG_HOME", HOME / ".config")
XDG_DATA = _xdg("XDG_DATA_HOME", HOME / ".local/share")
XDG_STATE = _xdg("XDG_STATE_HOME", HOME / ".local/state")
XDG_RUNTIME = _xdg("XDG_RUNTIME_DIR", Path("/tmp"))

CONFIG_DIR = XDG_CONFIG / "pet4agents"
DATA_DIR = XDG_DATA / "pet4agents"
STATE_DIR = XDG_STATE / "pet4agents"
VENV_DIR = DATA_DIR / "venv"
VENV_PY = VENV_DIR / "bin" / "python"
SOCKET_PATH = XDG_RUNTIME / "pet4agents.sock"
LEGACY_CONFIG_DIR = XDG_CONFIG / "claude-code-pet"
LEGACY_DATA_DIR = XDG_DATA / "claude-code-pet"
LEGACY_STATE_DIR = XDG_STATE / "claude-code-pet"
LEGACY_SOCKET_PATH = XDG_RUNTIME / "claude-code-pet.sock"
PIDFILE_PATH = STATE_DIR / "daemon.pid"
LOG_PATH = STATE_DIR / "event.log"
INSTALL_LOG_PATH = STATE_DIR / "install.log"
CONFIG_PATH = CONFIG_DIR / "config.json"
WINDOW_STATE_PATH = CONFIG_DIR / "state.json"
VENV_STATE_PATH = VENV_DIR / ".pet-runtime.json"


def migrate_legacy_paths() -> None:
    """Move old-brand state once, without overwriting newer state."""
    for old, new in (
        (LEGACY_CONFIG_DIR, CONFIG_DIR),
        (LEGACY_DATA_DIR, DATA_DIR),
        (LEGACY_STATE_DIR, STATE_DIR),
    ):
        if old.exists() and not new.exists():
            try:
                old.rename(new)
            except OSError:
                pass


migrate_legacy_paths()

# --- Pet discovery roots ---
CODEX_PETS_DIR = HOME / ".codex/pets"
PLUGIN_ROOT = Path(
    os.environ.get("PLUGIN_ROOT")
    or os.environ.get("CLAUDE_PLUGIN_ROOT")
    or Path(__file__).resolve().parent.parent
)
PLUGIN_PETS_DIR = PLUGIN_ROOT / "pets"

# --- Managed Python runtime ---
#
# The daemon always runs inside a private venv that the hook relay provisions on
# demand. Keep the pinned dependency set here so install/update policy is shared
# between the hook process and any future tooling.
PINNED_PYTHON_DEPENDENCIES: tuple[str, ...] = (
    "PySide6==6.11.1",
)
UV_LOCKFILE_PATH = PLUGIN_ROOT / "requirements-uv.lock.txt"


def _load_plugin_manifest_version() -> str:
    for rel in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        path = PLUGIN_ROOT / rel
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            continue
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return "0"


PLUGIN_VERSION = _load_plugin_manifest_version()


def ensure_dirs() -> None:
    """Create the writable dirs we use. Safe to call repeatedly."""
    for d in (CONFIG_DIR, DATA_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def truncate_log_if_needed(log_path: Path, max_bytes: int) -> None:
    """If *log_path* exceeds *max_bytes*, keep roughly the newest half."""
    if max_bytes <= 0:
        return
    try:
        size = log_path.stat().st_size
        if size <= max_bytes:
            return
        keep = max_bytes // 2
        with log_path.open("rb") as f:
            f.seek(size - keep)
            tail = f.read()
        # Advance past the first partial line so we start on a clean boundary.
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1 :]
        log_path.write_bytes(tail)
    except Exception:
        pass


# --- User config (`~/.config/pet4agents/config.json`) ---
#
# The on-disk shape is a flat JSON object. Unknown keys are preserved on write
# (see `pet_event.py:cmd_set_pet`). Defaults below are merged at read-time so
# missing keys never crash the daemon.
DEFAULT_USER_CONFIG: dict = {
    # Empty string = use auto-discovery order (env -> codex pets -> plugin pets).
    "pet_id": "",
    # Per-tool pet IDs. When set, the daemon switches to this pet on the first
    # SessionStart from that tool and reverts when only the other tool's
    # sessions remain. Empty string = fall back to shared `pet_id` discovery.
    "claude_pet_id": "",
    "codex_pet_id": "",
    # v2 pets only: turn the pet's head toward the mouse pointer while it is
    # idle. Ignored for v1 pets (their atlas has no look rows).
    "look_at_cursor": True,
    # Pointer distance (px, from the pet's center) beyond which the pet stops
    # tracking and plays its normal idle loop.
    "look_radius": 600,
    # Pointer distance (px) below which the pet shows the neutral look cell
    # instead of a direction — avoids jittery spinning when the pointer sits
    # on top of the pet.
    "look_deadzone": 48,
    # When False (default) the daemon exits after the last agent session ends.
    # When True the daemon keeps running until `/pet-stop` (legacy behavior).
    "stay_even_no_session": False,
    # Maximum size (bytes) each log file is allowed to reach before
    # truncation. When a log exceeds this, roughly the newest half is kept.
    # 0 disables truncation (unbounded growth). Default: 5 MiB.
    "max_log_size": 5 * 1024 * 1024,
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
