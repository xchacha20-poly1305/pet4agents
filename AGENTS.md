# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pet4agents` is a Claude Code and Codex CLI **plugin** (not a standalone app) that ships a Linux desktop pet. It reuses the Codex pet asset format (`pet.json` + spritesheet, 192×208 cells): v1 (1536×1872, 8×9 grid) and v2 (1536×2288, 8×11 grid). There is no build system, no test suite, no linter — the whole plugin is three Python files driven by agent hooks.

Install during development by pointing Claude `/plugin add` at this directory, copying/cloning into `~/.claude/plugins/`, or referencing this directory from a Codex marketplace.

## Three-tier runtime architecture

The plugin is intentionally split so the hook process never blocks on Qt/PySide6:

1. **`hooks/hooks.json` / `hooks/codex-hooks.json`** — Claude Code uses `hooks/hooks.json`, which maps the Claude events this plugin currently animates (`SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `SubagentStop`, `Notification`, `PermissionRequest`, `PostToolUseFailure`, `PermissionDenied`, `SubagentStart`, `TaskCreated`, `TaskCompleted`, `StopFailure`, `PreCompact`, `Elicitation`, `ElicitationResult`) to `scripts/pet_event.py <EventName>` via `${CLAUDE_PLUGIN_ROOT}` and sets `PET4AGENTS_AGENT=claude` in the hook command. Claude Code supports additional hook events that this plugin intentionally ignores unless they are added to `config.py` and `hooks/hooks.json`. Those Claude hooks are `async: true`; `SessionStart` has a longer timeout for first install, and the rest are short-timeout. Claude's hook-file schema allows a top-level `description` field in `hooks/hooks.json`. Codex uses `.codex-plugin/plugin.json` → `hooks/codex-hooks.json`, which maps the narrower Codex-supported subset (`SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PostToolUse`, `PreCompact`, `SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`, `PermissionRequest`; `Interrupt` is Codex-only) via `${PLUGIN_ROOT}` and sets `PET4AGENTS_AGENT=codex`. Codex command hooks currently run synchronously, so the Codex hook file does not set `async`. Codex's hooks-file schema is stricter: `hooks/codex-hooks.json` must have only the top-level `hooks` key, so do not copy Claude's top-level `description` into the Codex file. Do not infer the source from `CLAUDE_PLUGIN_ROOT` alone: Codex also injects that variable for Claude-plugin compatibility.

2. **`scripts/pet_event.py`** (hook relay) — Runs in the hook process. It first tries to send one JSON line over a Unix socket to the daemon without importing Qt or requiring the managed venv. If the daemon is missing, only `SessionStart` starts it: the foreground hook spawns a detached `daemon-spawn-event` worker, and that worker may take its time running `ensure_venv_and_reexec`, launching the daemon, and replaying the original `SessionStart` event. This keeps Codex's synchronous hook path from blocking on PySide6 install. `daemon-spawn` still ensures the venv before directly starting the daemon; `daemon-stop` and `set-pet` do not need the venv. The managed venv installs the pinned dependency set from `config.PINNED_PYTHON_DEPENDENCIES`, and both the `uv` path and the stdlib `venv` + `pip` fallback must install through the repo's hash-pinned `requirements-uv.lock.txt`. The relay stores a small runtime-state file under the venv; if a later plugin update changes the plugin version, pinned deps, or lockfile content, the next `SessionStart` tears down the old venv and rebuilds it before spawning the daemon. Every error path is swallowed and logged; hooks must never block the agent.

3. **`scripts/pet_daemon.py`** (long-running Qt process) — Frameless transparent always-on-top `QWidget`, renders frames from the atlas, listens on `QLocalServer`. Singleton-enforced via pidfile + socket probe. Tracks live sessions as `session_id -> (parent_pid, agent_type)` and, by default, schedules its own quit shortly after the last session drains (whether via a clean `SessionEnd` hook or, when `pet_event.py` found a reliable Claude/Codex PID, via the 5s liveness reaper noticing that PID is gone — covers crashes / `SIGKILL` / closed terminal only when a reliable PID was found). `pet_event.py` accepts a PID for liveness only from an ancestor whose `comm` or `argv[0]` basename is exactly `claude` or `codex`; if no reliable agent process is found, it sends `parent_pid=0` and the daemon skips liveness reaping for that session rather than mistaking a short-lived shell wrapper for the agent. It also sends `parent_pid=0` when the nearest `codex` ancestor is an app-server listening on a socket (`argv[1] == "app-server"` with a non-stdio `--listen`). Codex auto-starts such a daemon (`codex app-server --listen unix://`, detached via `setsid`) and the TUI attaches to it as a client, so hooks run under the daemon, the TUI is not in their ancestry, and the daemon outlives every TUI. For those sessions the pet depends on `SessionEnd`, which Codex fires only when the daemon unloads the thread: no subscribers and no active turn for `thread_unload_delay_secs` (default 60). A stdio `codex app-server` (IDE / desktop app) belongs to one client, so its PID is still used. On each `SessionStart` the daemon switches to the per-tool pet configured via `claude_pet_id` / `codex_pet_id`; on clean `SessionEnd` it reverts to the remaining tool's pet if all surviving sessions belong to one tool. The liveness reaper drains dead sessions and runs the quit check, but does not currently perform that pet reversion step. Set `stay_even_no_session: true` in `~/.config/pet4agents/config.json` for the legacy "always-on" behavior; `/pet-stop` and the Codex pet skill still work either way.

`scripts/config.py` is shared by both processes — paths, atlas geometry, the animation table, and the event-to-animation map all live there. **Change behavior there, not in the daemon.**

## State machine (daemon)

Three priority layers, highest first — see `PetWindow._active_animation`:

- `drag_state` — set on mouse press, cleared on release/idle. Values: `running-right` / `running-left` / `jumping`. Loops while held.
- `oneshot_state` — single-pass overlay (e.g. `waving`, `jumping`, `failed`). Cleared when the cycle finishes; falls back to `state`.
- `state` — the agent's current state, one at a time like the Codex TUI pet: `running` (working on a turn), `waiting` (needs the user), `review` (turn finished), `failed` (turn errored), `idle`.

Each event does up to two things (`apply_event`):

1. **Switch the state** — `config.STATES[event]` replaces the state unconditionally. `config.RESOLVES[event] = (from, to)` switches only when the current state is `from`; these are the per-tool-call events (`PostToolUse`, …) that end a `waiting` state, and they must never override anything else.
2. **Flash** a oneshot — `config.ONESHOTS[event]` overlays a single-pass animation on the state.

Anything that changes replays from its first frame. Events in none of the tables are ignored, and every hook registered in either hooks file must appear in a table (tests enforce this). `config.is_noise_event` drops hook firings in the relay before they reach the daemon: Claude Code's internal subagents (prompt suggestions, compaction) send a `SubagentStop` with an empty `agent_type`, and `Notification(permission_prompt)` duplicates `PermissionRequest` ~6s later.

Like the TUI's `app_state_animation`, a state's animation plays `config.STATE_REPEATS` (3) cycles and then settles into the idle loop while the state is kept. `AnimationController` owns this: `name` is the requested animation and `shown` is what is on screen (`"idle"` once settled). `PetWindow.state_settled` remembers it so a oneshot or drag over a settled state returns to idle instead of replaying it. `_tick` renders; `_advance_frame` holds the Qt-free frame logic.

Why one state and not nested intervals: the measured hook order does not nest. In both agents `PreToolUse` fires *before* `PermissionRequest`, approving fires nothing until the tool's `PostToolUse`, and background subagents send their tool events after the main agent's `Stop` under the same session id. Many turn endings fire no hook at all — Esc (Claude Code; Codex sends `Interrupt`), a permission denied in the UI, a tool blocked by validation or another hook — and settling is what keeps the pet from animating forever in those cases.

## Sprite versions and the look layer

`pet.json`'s `spriteVersionNumber` selects the atlas contract: absent/`1` = v1 (1536×1872, 9 rows), `2` = v2 (1536×2288, 11 rows). Geometry lives in `config.ATLAS_ROWS_BY_VERSION` / `atlas_height()` / `sprite_version_for_height()`. `PetWindow._detect_sprite_version` treats the declared field as a hint only — the measured atlas height wins when they disagree (a pet claiming v2 with a 9-row sheet would otherwise make the daemon sample look cells that don't exist), and anything unrecognizable falls back to v1. Rows 0–8 are identical across versions, so nothing in `config.ANIMATIONS` is version-dependent.

v2 adds a neutral look cell at row 0 / col 6 (`config.LOOK_NEUTRAL_CELL`) and 16 clockwise look directions filling rows 9–10 (`config.look_cell(index)`; index 0 = up, 4 = right, 8 = down, 12 = left, 22.5° per step). The daemon polls `QCursor.pos()` every `LOOK_POLL_MS` and picks a cell from the angle to the pet's center: inside `look_deadzone` → neutral cell, beyond `look_radius` → no look at all.

The look layer is deliberately **render-only** — it is not a fourth state-machine layer. `_update_look` writes `self.look_cell`, and `_render_current_frame` draws that cell instead of the animation frame while `_look_ready()` holds (v2 atlas, no drag, no oneshot, the controller is showing `idle` — the idle state or a settled one). Because the readiness check runs at paint time too, an event or drag that lands between polls drops the look pose immediately instead of showing a stale one. Look settings are cached by `_reload_look_settings()` (called at startup and on `reload_pet`) so the 10 Hz poll never touches the filesystem.

## Drag handling — platform pitfalls

The drag code is more complex than it looks because compositors disagree:

- The pet must respond to left/right clicks and dragging without taking focus from the terminal. `PetWindow._init_window` uses `WindowDoesNotAcceptFocus`, `WA_ShowWithoutActivating`, `WA_X11DoNotAcceptFocus`, `NoFocus`, and `BypassWindowManagerHint`; keep any popup/menu widgets on the same non-activating path.
- Dragging is deliberately client-side (`move()` from mouse events), not `windowHandle().startSystemMove()`. WM-managed moves can activate/focus the window on click and break the no-focus guarantee.
- The `_drag_idle_timer` watchdog checks button state after 220ms without movement: if left is still down, the drag session stays alive and can resume without changing animation; if not, drag cleanup runs.
- `_update_drag_state` accumulates dx until it crosses `DRAG_VEL_THRESHOLD` (4px) and resets the accumulator on sign reversal — **don't** call `_restart_timer()` from there; it would reset `frame_index` and starve the animation while the cursor moves.

When changing drag logic, set `PET4AGENTS_DEBUG=1` in the daemon's environment to get verbose drag logs in `event.log`.

## Pet discovery order

`pet_daemon.discover_pet` checks, in order: `$CLAUDE_PET_ID` / `$CODEX_PET_ID` (searched in `~/.codex/pets/`, then this plugin's `pets/`) → `config.json`'s `pet_id` (same two roots) → first valid dir under `~/.codex/pets/` (sorted) → first valid dir under this plugin's `pets/`. A "valid" dir has `pet.json` and a spritesheet — either the file referenced by `spritesheetPath`, or (when that field is omitted) `spritesheet.webp` / `spritesheet.png` discovered via probe order. The `/pet-set <id>` slash command and Codex pet skill write `pet_id` to config and ask the daemon to `reload`.

Per-tool overrides (`claude_pet_id` / `codex_pet_id` in config) are handled separately by `PetWindow._switch_pet_for_agent` at runtime — they fire on every `SessionStart` and do not affect `discover_pet`. Both roots (`~/.codex/pets/` and the plugin's `pets/`) are searched.

## Filesystem layout (XDG-respecting)

| Purpose | Path |
| --- | --- |
| Managed venv (PySide6 only) | `~/.local/share/pet4agents/venv/` |
| Venv runtime state | `~/.local/share/pet4agents/venv/.pet-runtime.json` |
| User config | `~/.config/pet4agents/config.json` |
| Window position | `~/.config/pet4agents/state.json` |
| Unix socket | `${XDG_RUNTIME_DIR:-/tmp}/pet4agents.sock` |
| Daemon pidfile | `~/.local/state/pet4agents/daemon.pid` |
| Event log | `~/.local/state/pet4agents/event.log` |
| Install log | `~/.local/state/pet4agents/install.log` |

`event.log` is the primary debugging surface — both relay and daemon append to it with timestamps. Hooks otherwise produce no visible output. Both `event.log` and `install.log` are auto-truncated when they exceed `max_log_size` (default 5 MiB, configurable in `config.json`; 0 disables).

## Common tasks

- **Manually run the daemon for debugging** (after first install populates the venv):
  ```bash
  ~/.local/share/pet4agents/venv/bin/python scripts/pet_daemon.py
  ```
- **Send a synthetic event** without going through Claude/Codex:
  ```bash
  echo '{}' | scripts/pet_event.py Stop
  ```
- **Stop the daemon**: `/pet-stop` (or `scripts/pet_event.py daemon-stop`).
- **Switch pets**: `/pet-set <pet-id>`, ask Codex to switch the pet, or edit `~/.config/pet4agents/config.json`.
- **Run tests**: `uv run --with-requirements requirements-test.txt pytest tests/` (or `pip install -r requirements-test.txt && python -m pytest tests/`).
- **Force a clean reinstall**: see the Uninstall block in `README.md`.

## Adding a new animation or event

1. Add the row + per-frame durations to `config.ANIMATIONS`. How it plays depends on where it is used: as a state it plays `STATE_REPEATS` cycles and settles into idle, as a oneshot it plays once, as a drag state it loops while held.
2. If it's triggered by a Claude/Codex event, register it in `config.STATES` (switch the state), `config.RESOLVES` (switch only from a given state), and/or `config.ONESHOTS` (single-pass flash). Before relying on an event's ordering, record a live session: hook order is often not what the event names suggest.
3. If it's a new Claude Code hook, add it to `hooks/hooks.json`. If Codex supports the same event and should drive the pet from it, also add it to `hooks/codex-hooks.json`.
4. The daemon's state machine picks it up automatically — no daemon code changes needed unless you're introducing a new priority layer.

## Tuning animation durations

Per-frame durations on every animation in `config.ANIMATIONS` can be overridden from `~/.config/pet4agents/config.json` via the `animation_durations` key (see README). `config._apply_animation_duration_overrides()` runs at import time and mutates `ANIMATIONS` in-place, so any consumer that reads `config.ANIMATIONS` (the daemon does) automatically sees the user values — **don't add a second merge path**. JSON is the only supported entry point; do not add CLI flags or slash commands for this. Validation is intentionally strict-but-silent: list length must equal the default frame count, all entries must be positive numbers, unknown animation names are ignored.
