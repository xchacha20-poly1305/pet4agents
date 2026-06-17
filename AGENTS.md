# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`claude-code-pet` is a Claude Code and Codex CLI **plugin** (not a standalone app) that ships a Linux desktop pet. It reuses the Codex pet asset format (`pet.json` + 1536×1872 spritesheet, 8×9 grid, 192×208 cells). There is no build system, no test suite, no linter — the whole plugin is three Python files driven by agent hooks.

Install during development by pointing Claude `/plugin add` at this directory, copying/cloning into `~/.claude/plugins/`, or referencing this directory from a Codex marketplace.

## Three-tier runtime architecture

The plugin is intentionally split so the hook process never blocks on Qt/PySide6:

1. **`hooks/hooks.json` / `hooks/codex-hooks.json`** — Claude Code uses `hooks/hooks.json`, which maps the Claude events this plugin currently animates (`SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `Notification`, `PermissionRequest`, `PostToolUseFailure`, `PermissionDenied`, `SubagentStart`, `TaskCreated`, `TaskCompleted`, `StopFailure`, `PreCompact`, `PostCompact`, `Elicitation`, `ElicitationResult`) to `scripts/pet_event.py <EventName>` via `${CLAUDE_PLUGIN_ROOT}` and sets `PET4CLAUDE_AGENT=claude` in the hook command. Claude Code supports additional hook events that this plugin intentionally ignores unless they are added to `config.py` and `hooks/hooks.json`. Those Claude hooks are `async: true`; `SessionStart` has a longer timeout for first install, and the rest are short-timeout. Claude's hook-file schema allows a top-level `description` field in `hooks/hooks.json`. Codex uses `.codex-plugin/plugin.json` → `hooks/codex-hooks.json`, which maps the narrower Codex-supported subset (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`, `SubagentStop`, `Stop`, `PermissionRequest`) via `${PLUGIN_ROOT}` and sets `PET4CLAUDE_AGENT=codex`. Codex command hooks currently run synchronously, so the Codex hook file does not set `async`. Codex's hooks-file schema is stricter: `hooks/codex-hooks.json` must have only the top-level `hooks` key, so do not copy Claude's top-level `description` into the Codex file. Do not infer the source from `CLAUDE_PLUGIN_ROOT` alone: Codex also injects that variable for Claude-plugin compatibility.

2. **`scripts/pet_event.py`** (hook relay) — Runs in the hook process. It first tries to send one JSON line over a Unix socket to the daemon without importing Qt or requiring the managed venv. If the daemon is missing, only `SessionStart` starts it: the foreground hook spawns a detached `daemon-spawn-event` worker, and that worker may take its time running `ensure_venv_and_reexec`, launching the daemon, and replaying the original `SessionStart` event. This keeps Codex's synchronous hook path from blocking on PySide6 install. `daemon-spawn` still ensures the venv before directly starting the daemon; `daemon-stop` and `set-pet` do not need the venv. Every error path is swallowed and logged; hooks must never block the agent.

3. **`scripts/pet_daemon.py`** (long-running Qt process) — Frameless transparent always-on-top `QWidget`, renders frames from the atlas, listens on `QLocalServer`. Singleton-enforced via pidfile + socket probe. Tracks live sessions as `session_id -> (parent_pid, agent_type)` and, by default, schedules its own quit shortly after the last session drains (whether via a clean `SessionEnd` hook or, when `pet_event.py` found a reliable Claude/Codex PID, via the 5s liveness reaper noticing that PID is gone — covers crashes / `SIGKILL` / closed terminal only when a reliable PID was found). `pet_event.py` accepts a PID for liveness only from an ancestor whose `comm` or `argv[0]` basename is exactly `claude` or `codex`; if no reliable agent process is found, it sends `parent_pid=0` and the daemon skips liveness reaping for that session rather than mistaking a short-lived shell wrapper for the agent. On each `SessionStart` the daemon switches to the per-tool pet configured via `claude_pet_id` / `codex_pet_id`; on clean `SessionEnd` it reverts to the remaining tool's pet if all surviving sessions belong to one tool. The liveness reaper drains dead sessions and runs the quit check, but does not currently perform that pet reversion step. Set `stay_even_no_session: true` in `~/.config/claude-code-pet/config.json` for the legacy "always-on" behavior; `/pet-stop` and the Codex pet skill still work either way.

`scripts/config.py` is shared by both processes — paths, atlas geometry, the animation table, and the event-to-animation map all live there. **Change behavior there, not in the daemon.**

## State machine (daemon)

Three priority layers, highest first — see `PetWindow._active_animation`:

- `drag_state` — set on mouse press, cleared on release/idle. Values: `running-right` / `running-left` / `jumping`. Loops while held.
- `oneshot_state` — single-pass overlay (`waving`, `jumping`, `failed`, `review`). Cleared by `_tick` when the cycle finishes; falls back to the top of `base_stack`.
- `base_stack` — stack of `(opener_event, animation)` entries. Top is the active loop. Bottom sentinel `(None, "idle")` is never popped, so the pet always has an animation to play.

Each event is a combination of three actions, processed in order:

1. **Close** intervals — `config.INTERVAL_CLOSE[event]` is a set of opener event names; the daemon pops entries off the top of the stack while the topmost opener is in that set, stopping at the first non-match (so nested intervals stay correct).
2. **Open** an interval — `config.INTERVAL_OPEN[event]` declares the looping animation to push.
3. **Flash** a oneshot — `config.ONESHOTS[event]` overlays a single-pass animation on top of whatever's currently on the stack.

A single event can do all three (e.g. `PreToolUse` closes a `PermissionRequest` interval, opens a `waiting` interval, and plays no oneshot; `Stop` closes everything down to idle and flashes a `jumping` oneshot). Events that appear in none of the three tables are ignored.

The closed-loop model means terminal-feeling events (`Notification`, `PermissionRequest`) loop until the next user action implicitly closes them, instead of flashing once and being missed.

## Drag handling — platform pitfalls

The drag code is more complex than it looks because compositors disagree:

- The pet must respond to left/right clicks and dragging without taking focus from the terminal. `PetWindow._init_window` uses `WindowDoesNotAcceptFocus`, `WA_ShowWithoutActivating`, `WA_X11DoNotAcceptFocus`, `NoFocus`, and `BypassWindowManagerHint`; keep any popup/menu widgets on the same non-activating path.
- Dragging is deliberately client-side (`move()` from mouse events), not `windowHandle().startSystemMove()`. WM-managed moves can activate/focus the window on click and break the no-focus guarantee.
- The `_drag_idle_timer` watchdog checks button state after 220ms without movement: if left is still down, the drag session stays alive and can resume without changing animation; if not, drag cleanup runs.
- `_update_drag_state` accumulates dx until it crosses `DRAG_VEL_THRESHOLD` (4px) and resets the accumulator on sign reversal — **don't** call `_restart_timer()` from there; it would reset `frame_index` and starve the animation while the cursor moves.

When changing drag logic, set `CCPET_DEBUG=1` in the daemon's environment to get verbose drag logs in `event.log`.

## Pet discovery order

`pet_daemon.discover_pet` checks, in order: `$CLAUDE_PET_ID` / `$CODEX_PET_ID` (searched in `~/.codex/pets/`, then this plugin's `pets/`) → `config.json`'s `pet_id` (same two roots) → first valid dir under `~/.codex/pets/` (sorted) → first valid dir under this plugin's `pets/`. A "valid" dir has `pet.json` and a spritesheet — either the file referenced by `spritesheetPath`, or (when that field is omitted) `spritesheet.webp` / `spritesheet.png` discovered via probe order. The `/pet-set <id>` slash command and Codex pet skill write `pet_id` to config and ask the daemon to `reload`.

Per-tool overrides (`claude_pet_id` / `codex_pet_id` in config) are handled separately by `PetWindow._switch_pet_for_agent` at runtime — they fire on every `SessionStart` and do not affect `discover_pet`. Both roots (`~/.codex/pets/` and the plugin's `pets/`) are searched.

## Filesystem layout (XDG-respecting)

| Purpose | Path |
| --- | --- |
| Managed venv (PySide6 only) | `~/.local/share/claude-code-pet/venv/` |
| User config | `~/.config/claude-code-pet/config.json` |
| Window position | `~/.config/claude-code-pet/state.json` |
| Unix socket | `${XDG_RUNTIME_DIR:-/tmp}/claude-code-pet.sock` |
| Daemon pidfile | `~/.local/state/claude-code-pet/daemon.pid` |
| Event log | `~/.local/state/claude-code-pet/event.log` |
| Install log | `~/.local/state/claude-code-pet/install.log` |

`event.log` is the primary debugging surface — both relay and daemon append to it with timestamps. Hooks otherwise produce no visible output.

## Common tasks

- **Manually run the daemon for debugging** (after first install populates the venv):
  ```bash
  ~/.local/share/claude-code-pet/venv/bin/python scripts/pet_daemon.py
  ```
- **Send a synthetic event** without going through Claude/Codex:
  ```bash
  echo '{}' | scripts/pet_event.py Stop
  ```
- **Stop the daemon**: `/pet-stop` (or `scripts/pet_event.py daemon-stop`).
- **Switch pets**: `/pet-set <pet-id>`, ask Codex to switch the pet, or edit `~/.config/claude-code-pet/config.json`.
- **Force a clean reinstall**: see the Uninstall block in `README.md`.

## Adding a new animation or event

1. Add the row + per-frame durations to `config.ANIMATIONS` and put it in `LOOPING_STATES` or `ONESHOT_STATES`.
2. If it's triggered by a Claude/Codex event, register it in one or more of `config.INTERVAL_OPEN` (looping interval), `config.INTERVAL_CLOSE` (which openers it pops), and `config.ONESHOTS` (single-pass flash). A single event can appear in all three.
3. If it's a new Claude Code hook, add it to `hooks/hooks.json`. If Codex supports the same event and should drive the pet from it, also add it to `hooks/codex-hooks.json`.
4. The daemon's state machine picks it up automatically — no daemon code changes needed unless you're introducing a new priority layer.

## Tuning animation durations

Per-frame durations on every animation in `config.ANIMATIONS` can be overridden from `~/.config/claude-code-pet/config.json` via the `animation_durations` key (see README). `config._apply_animation_duration_overrides()` runs at import time and mutates `ANIMATIONS` in-place, so any consumer that reads `config.ANIMATIONS` (the daemon does) automatically sees the user values — **don't add a second merge path**. JSON is the only supported entry point; do not add CLI flags or slash commands for this. Validation is intentionally strict-but-silent: list length must equal the default frame count, all entries must be positive numbers, unknown animation names are ignored.
