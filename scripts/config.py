"""Shared constants for pet4agents."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
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
    # Compaction is busy work the agent does on its own. PostCompact is the
    # same state, so finishing compaction replays running.
    "PreCompact":        "running",
    "PostCompact":       "running",
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

# SessionStart sources that must not flash the greeting wave. Compaction
# already played running via PreCompact/PostCompact, then both agents fire
# SessionStart with this source on the same turn.
SESSION_START_SOURCES_WITHOUT_WAVE = frozenset({"compact"})


def oneshot_for(event: str, source: str = "") -> str | None:
    """Oneshot animation for `event`, or None when it should not flash."""
    if event == "SessionStart" and source in SESSION_START_SOURCES_WITHOUT_WAVE:
        return None
    return ONESHOTS.get(event)


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
    # DEPRECATED — superseded by `process_rules` (the daemon warns when either
    # is set). Per-tool pet IDs: the daemon switches to this pet on the first
    # SessionStart from that tool and reverts when only the other tool's
    # sessions remain. Empty string = fall back to shared `pet_id` discovery.
    "claude_pet_id": "",
    "codex_pet_id": "",
    # Custom agent-process rules, tried in order before the built-in
    # claude/codex detection. Each entry:
    #   { "process": "<regex>", "id": "<pet id>",
    #     "animation_durations": { ... } }   # optional, overrides the global map
    # See `ProcessRule`.
    "process_rules": [],
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


def valid_duration_overrides(overrides) -> dict[str, list[int]]:
    """Filter a user-authored `animation_durations` map down to the entries
    that can safely replace `ANIMATIONS` durations.

    Validation rules (all failures are silent):
      - the map must be a dict
      - animation name must already exist in `ANIMATIONS`
      - value must be a list/tuple of positive numbers
      - length must match the default durations length (frame count is fixed
        by the spritesheet row, so changing it would desync rendering)
    """
    if not isinstance(overrides, dict):
        return {}
    valid: dict[str, list[int]] = {}
    for name, durs in overrides.items():
        spec = ANIMATIONS.get(name)
        if not spec:
            continue
        if not isinstance(durs, (list, tuple)):
            continue
        if len(durs) != len(spec["durations"]):
            continue
        if any(isinstance(d, bool) or not isinstance(d, (int, float)) or d <= 0
               for d in durs):
            continue
        valid[name] = [int(d) for d in durs]
    return valid


def _apply_animation_duration_overrides() -> None:
    """Mutate `ANIMATIONS` in-place with the global per-animation duration
    overrides found in the user config. Called once at import time so every
    consumer of `config.ANIMATIONS` (notably `pet_daemon.py`) sees the user's
    values without needing its own merge step. Per-rule overrides
    (`ProcessRule.animation_durations`) layer on top of this at runtime."""
    try:
        cfg = load_user_config()
    except Exception:
        return
    for name, durs in valid_duration_overrides(cfg.get("animation_durations")).items():
        ANIMATIONS[name]["durations"] = durs


# --- Per-process profiles ---
#
# A profile says which pet to show (and how fast it animates) for sessions of
# one agent. `process_rules` entries match the agent's process by name; the
# deprecated `claude_pet_id` / `codex_pet_id` keys are the fallback for the
# built-in claude/codex detection.

LEGACY_PET_ID_KEYS = ("claude_pet_id", "codex_pet_id")


@dataclass(frozen=True)
class ProcessRule:
    """One `process_rules` entry. `process` is a regex searched (not fully
    matched — anchor it with ^/$ if needed) against the process's `comm` and
    its argv[0] basename."""

    process: str
    pattern: re.Pattern
    pet_id: str = ""
    animation_durations: dict = field(default_factory=dict)

    def matches(self, names) -> bool:
        return any(self.pattern.search(n) for n in names)


@dataclass(frozen=True)
class Profile:
    pet_id: str = ""
    animation_durations: dict = field(default_factory=dict)


def load_process_rules(cfg: dict | None = None, on_error=None) -> list[ProcessRule]:
    """Parse `process_rules` from the user config. Malformed entries are
    skipped, and each is reported through `on_error(message)` when given, so
    one typo never disables the other rules."""
    report = on_error or (lambda msg: None)
    raw = (cfg if cfg is not None else load_user_config()).get("process_rules")
    if not raw:
        return []
    if not isinstance(raw, list):
        report("process_rules must be a list")
        return []
    rules: list[ProcessRule] = []
    for i, entry in enumerate(raw):
        process = entry.get("process") if isinstance(entry, dict) else None
        if not isinstance(process, str) or not process:
            report(f"process_rules[{i}]: missing 'process' regex")
            continue
        try:
            pattern = re.compile(process)
        except re.error as e:
            report(f"process_rules[{i}]: bad regex {process!r}: {e}")
            continue
        pet_id = entry.get("id")
        rules.append(ProcessRule(
            process=process,
            pattern=pattern,
            pet_id=pet_id.strip() if isinstance(pet_id, str) else "",
            animation_durations=valid_duration_overrides(
                entry.get("animation_durations")
            ),
        ))
    return rules


def resolve_profile(agent_type: str, rule: str, cfg: dict | None = None) -> Profile:
    """Profile for a session. `rule` is the `process` of the custom rule that
    matched its agent process ("" for built-in detection); it takes priority,
    and `agent_type` ("claude"|"codex") then selects the legacy per-tool id."""
    cfg = cfg if cfg is not None else load_user_config()
    if rule:
        for r in load_process_rules(cfg):
            if r.process == rule:
                return Profile(r.pet_id, r.animation_durations)
        return Profile()
    if agent_type in ("claude", "codex"):
        legacy = cfg.get(f"{agent_type}_pet_id")
        if isinstance(legacy, str):
            return Profile(legacy.strip())
    return Profile()


def legacy_pet_id_keys_in_use(cfg: dict | None = None) -> list[str]:
    """Deprecated `*_pet_id` keys that the user config fills in."""
    cfg = cfg if cfg is not None else load_user_config()
    return [
        key for key in LEGACY_PET_ID_KEYS
        if isinstance(cfg.get(key), str) and cfg[key].strip()
    ]


# --- Config verification (`pet_event.py verify`) ---
#
# The loaders above are deliberately forgiving. `verify_config` is the strict
# counterpart: it inspects the raw file content and reports everything the
# loaders would silently ignore or only warn about.

PROCESS_RULE_KEYS = ("process", "id", "animation_durations")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _verify_duration_map(overrides, where: str) -> list[str]:
    if not isinstance(overrides, dict):
        return [f"{where}: must be an object"]
    problems: list[str] = []
    for name, durs in overrides.items():
        spec = ANIMATIONS.get(name)
        if spec is None:
            problems.append(f"{where}.{name}: unknown animation")
        elif not isinstance(durs, (list, tuple)) or len(durs) != len(spec["durations"]):
            problems.append(
                f"{where}.{name}: must be a list of {len(spec['durations'])} numbers"
            )
        elif any(not _is_number(d) or d <= 0 for d in durs):
            problems.append(f"{where}.{name}: entries must be positive numbers")
    return problems


def _verify_process_rules(rules) -> list[str]:
    if not isinstance(rules, list):
        return ["process_rules: must be a list"]
    problems: list[str] = []
    for i, entry in enumerate(rules):
        where = f"process_rules[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be an object")
            continue
        for key in entry:
            if key not in PROCESS_RULE_KEYS:
                problems.append(f"{where}.{key}: unknown field")
        process = entry.get("process")
        if not isinstance(process, str) or not process:
            problems.append(f"{where}.process: missing regex")
        else:
            try:
                re.compile(process)
            except re.error as e:
                problems.append(f"{where}.process: bad regex {process!r}: {e}")
        if "id" in entry and not isinstance(entry["id"], str):
            problems.append(f"{where}.id: must be a string")
        if "animation_durations" in entry:
            problems += _verify_duration_map(
                entry["animation_durations"], f"{where}.animation_durations"
            )
    return problems


def verify_config(data) -> list[str]:
    """Problems found in the parsed content of `config.json` (empty = valid).

    Reports unknown and deprecated keys, wrongly typed values, and malformed
    `process_rules` / `animation_durations` entries."""
    if not isinstance(data, dict):
        return ["config must be a JSON object"]
    problems: list[str] = []
    for key, value in data.items():
        if key in LEGACY_PET_ID_KEYS:
            problems.append(f"{key}: deprecated, use process_rules")
        elif key not in DEFAULT_USER_CONFIG:
            problems.append(f"{key}: unknown field")
        elif key == "process_rules":
            problems += _verify_process_rules(value)
        elif key == "animation_durations":
            problems += _verify_duration_map(value, key)
        elif key == "pet_id":
            if not isinstance(value, str):
                problems.append(f"{key}: must be a string")
        elif key in ("look_at_cursor", "stay_even_no_session"):
            if not isinstance(value, bool):
                problems.append(f"{key}: must be true or false")
        elif key in ("look_radius", "look_deadzone"):
            if not _is_number(value) or value <= 0:
                problems.append(f"{key}: must be a positive number")
        elif key == "max_log_size":
            if not _is_number(value) or value < 0:
                problems.append(f"{key}: must be a non-negative number")
    return problems


_apply_animation_duration_overrides()
