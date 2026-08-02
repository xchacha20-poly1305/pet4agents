#!/usr/bin/env python3
"""
Long-running pet daemon. Renders a frameless transparent always-on-top window,
plays per-frame animations from a Codex-format spritesheet, and listens for
event JSON over a Unix socket sent by pet_event.py.

State machine has three layers, in priority order:
  drag_state    — set while the mouse is dragging the pet
  oneshot_state — single-pass overlay (waving/jumping/failed/review)
  base_stack    — stack of looping intervals, top is the active loop;
                  bottom is always (None, "idle") and never popped

Events open intervals (push onto base_stack), close intervals (pop matching
entries off the top), and/or play a oneshot. See config.INTERVAL_OPEN /
INTERVAL_CLOSE / ONESHOTS for the per-event tables. When a oneshot finishes,
we fall back to base_stack[-1]. drag_state is cleared on mouse release.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import json
import math
import os
import signal
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

from PySide6.QtCore import (  # noqa: E402
    QByteArray,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (  # noqa: E402
    QCursor,
    QGuiApplication,
    QPainter,
    QPixmap,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QWidget  # noqa: E402


class _X11PointerProbe:
    """Detect whether XWayland still owns the surface under the pointer."""

    def __init__(self) -> None:
        self._lib = None
        self._display = None
        if QGuiApplication.platformName() != "xcb":
            return
        try:
            name = ctypes.util.find_library("X11")
            if not name:
                return
            lib = ctypes.CDLL(name)
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            lib.XDefaultRootWindow.restype = ctypes.c_ulong
            lib.XQueryPointer.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_uint),
            ]
            lib.XQueryPointer.restype = ctypes.c_int
            display = lib.XOpenDisplay(None)
            if display:
                self._lib = lib
                self._display = display
                self._root = lib.XDefaultRootWindow(display)
        except Exception:
            self._lib = None
            self._display = None

    def is_tracked(self) -> bool:
        if self._lib is None or self._display is None:
            return True
        root_return = ctypes.c_ulong()
        child_return = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        win_x = ctypes.c_int()
        win_y = ctypes.c_int()
        mask = ctypes.c_uint()
        try:
            ok = self._lib.XQueryPointer(
                self._display,
                self._root,
                ctypes.byref(root_return),
                ctypes.byref(child_return),
                ctypes.byref(root_x),
                ctypes.byref(root_y),
                ctypes.byref(win_x),
                ctypes.byref(win_y),
                ctypes.byref(mask),
            )
        except Exception:
            return True
        return not ok or child_return.value != 0


def _log(line: str) -> None:
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with config.LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] daemon: {line}\n")
    except Exception:
        pass


# ----------------------- Pet discovery -----------------------

_SHEET_FALLBACKS = ("spritesheet.webp", "spritesheet.png")


def _resolve_sheet_path(pet_dir: Path, meta: dict) -> Path | None:
    """Return the spritesheet path for a pet dir, or None if not found.

    If `spritesheetPath` is set in pet.json, that exact file must exist.
    Otherwise probe the conventional names (webp first for back-compat, then png)."""
    explicit = meta.get("spritesheetPath")
    if explicit:
        p = pet_dir / explicit
        return p if p.exists() else None
    for name in _SHEET_FALLBACKS:
        p = pet_dir / name
        if p.exists():
            return p
    return None


def _positive_number(value, fallback: float) -> float:
    """Config values are user-authored, so treat anything that isn't a
    positive number as "not set" instead of letting it reach the renderer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(fallback)
    return float(value) if value > 0 else float(fallback)


def _is_valid_pet_dir(d: Path) -> bool:
    if not d.is_dir():
        return False
    pet_json = d / "pet.json"
    if not pet_json.exists():
        return False
    try:
        meta = json.loads(pet_json.read_text("utf-8"))
    except Exception:
        return False
    return _resolve_sheet_path(d, meta) is not None


def discover_pet() -> tuple[Path, dict] | None:
    """Return (pet_dir, metadata) or None."""
    cfg_pet = config.load_user_config().get("pet_id", "") or ""

    candidates: list[Path] = []
    env_pet = (
        os.environ.get("CLAUDE_PET_ID", "").strip()
        or os.environ.get("CODEX_PET_ID", "").strip()
    )
    if env_pet:
        candidates.append(config.CODEX_PETS_DIR / env_pet)
        candidates.append(config.PLUGIN_PETS_DIR / env_pet)
    if cfg_pet:
        candidates.append(config.CODEX_PETS_DIR / cfg_pet)
        candidates.append(config.PLUGIN_PETS_DIR / cfg_pet)

    for root in (config.CODEX_PETS_DIR, config.PLUGIN_PETS_DIR):
        if root.exists():
            for sub in sorted(root.iterdir()):
                candidates.append(sub)

    seen: set[Path] = set()
    for c in candidates:
        try:
            r = c.resolve()
        except Exception:
            continue
        if r in seen:
            continue
        seen.add(r)
        if _is_valid_pet_dir(c):
            try:
                meta = json.loads((c / "pet.json").read_text("utf-8"))
            except Exception:
                continue
            return c, meta
    return None


# ----------------------- Animation -----------------------

class AnimationController:
    """Tracks frame index + remaining frame time. Owns the active animation name."""

    def __init__(self) -> None:
        self.name: str = "idle"
        self.frame_index: int = 0
        self.is_oneshot: bool = False

    def set(self, name: str, oneshot: bool) -> None:
        if name == self.name and oneshot == self.is_oneshot:
            return
        self.name = name
        self.is_oneshot = oneshot
        self.frame_index = 0

    def _spec(self) -> dict:
        return config.ANIMATIONS.get(self.name, config.ANIMATIONS["idle"])

    def current_row(self) -> int:
        return self._spec()["row"]

    def current_duration_ms(self) -> int:
        durs = self._spec()["durations"]
        idx = min(self.frame_index, len(durs) - 1)
        return int(durs[idx])

    def advance(self) -> bool:
        """Advance to the next frame. Returns True if the animation finished
        a full cycle (caller should resolve fallback for oneshots)."""
        durs = self._spec()["durations"]
        self.frame_index += 1
        if self.frame_index >= len(durs):
            self.frame_index = 0
            return True
        return False


# ----------------------- Window -----------------------

DRAG_VEL_THRESHOLD = 4   # px per moveEvent to count as "running"
DRAG_IDLE_TIMEOUT_MS = 220  # fallback drag: no moveEvent for this long -> check button state
# A press is treated as a click (→ jumping oneshot on release) until either
# the cursor crosses DRAG_VEL_THRESHOLD or the press is held this long. Once
# either condition fires the press promotes to a drag (drag_state set).
LONG_PRESS_MS = 220
# How often the pointer is sampled for v2 look tracking. 100ms reads as
# instant while costing one cursor query per tick.
LOOK_POLL_MS = 100


class PetWindow(QWidget):
    quit_requested = Signal()

    def __init__(self, pet_dir: Path, meta: dict) -> None:
        super().__init__()
        self.pet_dir = pet_dir
        self.meta = meta
        self.atlas: QPixmap = QPixmap()
        self._frame_buf = QPixmap(config.CELL_W, config.CELL_H)

        self.base_stack: list[tuple[str | None, str]] = [(None, "idle")]
        self.oneshot_state: str | None = None
        self.drag_state: str | None = None
        # v2 look layer: (row, col) of the look cell to draw instead of the
        # current animation frame, or None when the pet isn't tracking the
        # pointer. Sits above every other layer at *render* time only — it
        # never touches the state machine, so a look pose is dropped the
        # instant an event/drag makes the pet do something else.
        self.sprite_version: int = config.SPRITE_V1
        self.look_cell: tuple[int, int] | None = None
        self._x11_pointer = _X11PointerProbe()
        self._reload_look_settings()

        # Tracks live sessions: session_id -> (parent_pid, agent_type).
        # parent_pid: 0 means unknown — session is exempt from liveness check.
        # agent_type: "claude"|"codex"|"" — used for per-tool pet switching.
        # Populated on event arrival, drained on Claude's SessionEnd or when
        # the parent PID is observed dead (`_reap_dead_sessions`). Codex does
        # not currently provide SessionEnd, so its clean terminal-exit path is
        # the PID reaper. When this dict drains and `stay_even_no_session` is
        # False (the default), the daemon schedules its own quit.
        self.active_sessions: dict[str, tuple[int, str]] = {}

        self.anim = AnimationController()

        self._dragging = False
        self._manual_dragging = False
        # `_armed` is True between mousePress and the moment we either commit
        # to a drag (long-press timer fired or cursor moved past threshold) or
        # release the button (→ click → jumping oneshot). While armed we do
        # NOT touch the animation, so a quick click leaves the base/oneshot
        # animation untouched until release.
        self._armed = False
        self._last_move_pos = QPoint()
        self._press_pos = QPoint()
        self._drag_offset = QPoint()
        # Accumulates horizontal movement until it crosses DRAG_VEL_THRESHOLD,
        # at which point we commit a running direction. Resets on sign reversal
        # so direction changes propagate even during slow drags.
        self._drag_dx_accum = 0

        self._init_window()
        self._load_pet(pet_dir, meta)
        self._restore_position()

        self.label = QLabel(self)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.label.setGeometry(0, 0, config.CELL_W, config.CELL_H)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._tick)
        self._render_current_frame()
        self.timer.start(self.anim.current_duration_ms())

        # Watchdog for client-side drags. If movement stops while the button is
        # still down, keep the drag session alive so movement can resume.
        self._drag_idle_timer = QTimer(self)
        self._drag_idle_timer.setSingleShot(True)
        self._drag_idle_timer.setInterval(DRAG_IDLE_TIMEOUT_MS)
        self._drag_idle_timer.timeout.connect(self._on_drag_idle)

        # Long-press timer: distinguishes click from drag. Starts on press and
        # promotes the armed press into a drag if it fires before release.
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(LONG_PRESS_MS)
        self._long_press_timer.timeout.connect(self._on_long_press_fire)

        # Periodic liveness reaper for sessions whose agent parent disappeared
        # without firing SessionEnd (Codex exit, crash, SIGKILL, terminal
        # killed). 5s feels responsive without being expensive (one os.kill
        # syscall per tracked session).
        self._session_liveness_timer = QTimer(self)
        self._session_liveness_timer.setInterval(self._SESSION_LIVENESS_INTERVAL_MS)
        self._session_liveness_timer.timeout.connect(self._reap_dead_sessions)
        self._session_liveness_timer.start()

        # Pointer tracking for v2 look directions. Polled rather than driven by
        # mouse events: the window is click-through-ish and never grabs focus,
        # so we only see events that land on the pet itself.
        self._look_timer = QTimer(self)
        self._look_timer.setInterval(LOOK_POLL_MS)
        self._look_timer.timeout.connect(self._update_look)
        self._look_timer.start()

    def _init_window(self) -> None:
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint
            | Qt.WindowDoesNotAcceptFocus
            | Qt.BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_X11DoNotAcceptFocus, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(QSize(config.CELL_W, config.CELL_H))
        self.setWindowTitle(self.meta.get("displayName") or "Pet")

    def _make_non_activating(self, widget: QWidget) -> None:
        widget.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
        widget.setWindowFlag(Qt.BypassWindowManagerHint, True)
        widget.setAttribute(Qt.WA_ShowWithoutActivating, True)
        widget.setAttribute(Qt.WA_X11DoNotAcceptFocus, True)
        widget.setFocusPolicy(Qt.NoFocus)

    def _load_pet(self, pet_dir: Path, meta: dict) -> None:
        sheet_path = _resolve_sheet_path(pet_dir, meta)
        if sheet_path is None:
            _log(f"no spritesheet (.webp/.png) found in {pet_dir}")
            return
        pix = QPixmap(str(sheet_path))
        if pix.isNull():
            _log(f"failed to load atlas: {sheet_path}")
            return
        self.sprite_version = self._detect_sprite_version(pix, meta)
        if (pix.width() != config.ATLAS_W
                or pix.height() != config.atlas_height(self.sprite_version)):
            _log(
                f"atlas size mismatch: got {pix.width()}x{pix.height()}, "
                f"expected {config.ATLAS_W}x"
                f"{config.atlas_height(self.sprite_version)} "
                f"for sprite v{self.sprite_version}"
            )
        self.atlas = pix
        self.pet_dir = pet_dir
        self.meta = meta
        # A new pet may not have look rows; drop any stale look pose.
        self.look_cell = None

    @staticmethod
    def _detect_sprite_version(pix: QPixmap, meta: dict) -> int:
        """Decide which sprite generation an atlas is.

        `spriteVersionNumber` in pet.json is the declaration, but the pixels
        are the truth: a pet that claims v2 while shipping a 9-row sheet would
        otherwise have us sampling look cells that don't exist. When the two
        disagree (or the field is missing/garbage) the measured height wins,
        and we fall back to v1 for anything unrecognizable."""
        declared = meta.get("spriteVersionNumber")
        if isinstance(declared, bool) or not isinstance(declared, int):
            declared = None
        measured = config.sprite_version_for_height(pix.height())
        if measured is None:
            if declared in config.ATLAS_ROWS_BY_VERSION:
                return declared
            return config.SPRITE_V1
        if declared is not None and declared != measured:
            _log(
                f"pet declares spriteVersionNumber={declared} but the atlas is "
                f"{pix.height()}px tall (v{measured}); trusting the atlas"
            )
        return measured

    def reload_pet(self) -> None:
        found = discover_pet()
        if not found:
            _log("reload_pet: no pet found")
            return
        pet_dir, meta = found
        self._load_pet(pet_dir, meta)
        self._reload_look_settings()
        self._render_current_frame()

    # ----- state machine -----

    def _active_animation(self) -> tuple[str, bool]:
        if self.drag_state:
            return self.drag_state, False  # drag states loop while held
        if self.oneshot_state:
            return self.oneshot_state, True
        return self.base_stack[-1][1], False

    def _sync_anim(self) -> None:
        name, oneshot = self._active_animation()
        if name != self.anim.name or oneshot != self.anim.is_oneshot:
            self.anim.set(name, oneshot)

    def _close_intervals(self, close_set: set[str]) -> None:
        """Pop entries off the top of base_stack while their opener is in
        close_set. Stops at the first non-matching entry to preserve nesting,
        and never pops the sentinel (None, "idle") at the bottom."""
        while len(self.base_stack) > 1:
            opener, _ = self.base_stack[-1]
            if opener in close_set:
                self.base_stack.pop()
            else:
                break

    def apply_event(self, event_name: str, session_id: str = "", parent_pid: int = 0, agent_type: str = "") -> None:
        # Session lifecycle bookkeeping runs first so the
        # `stay_even_no_session` check can see an up-to-date set even for
        # events that don't appear in any of the animation tables.
        self._track_session(event_name, session_id, parent_pid, agent_type)

        # Ignore events that don't appear in any of the three tables. This
        # also covers internal/unknown messages.
        if (event_name not in config.INTERVAL_CLOSE
                and event_name not in config.INTERVAL_OPEN
                and event_name not in config.ONESHOTS):
            return

        # Order matters: close first (so an event can close an outer interval
        # before opening its own), then open, then trigger any oneshot flash.
        close_set = config.INTERVAL_CLOSE.get(event_name)
        if close_set:
            self._close_intervals(close_set)

        open_anim = config.INTERVAL_OPEN.get(event_name)
        if open_anim:
            self.base_stack.append((event_name, open_anim))

        oneshot = config.ONESHOTS.get(event_name)
        if oneshot:
            self.oneshot_state = oneshot

        self._sync_anim()
        self._restart_timer()

    def _restart_timer(self) -> None:
        self.timer.stop()
        self._render_current_frame()
        self.timer.start(self.anim.current_duration_ms())

    # ----- session lifecycle (drives `stay_even_no_session`) -----

    # Delay before checking whether to quit, in milliseconds. Long enough for
    # the SessionEnd `waving` oneshot (~700ms) to play through, plus a small
    # cushion so a fresh SessionStart racing the SessionEnd has time to land.
    _NO_SESSION_QUIT_DELAY_MS = 900

    # How often to poll Claude Code parent PIDs for liveness. Cheap (one
    # signal-zero syscall per session) so we keep it fairly snappy.
    _SESSION_LIVENESS_INTERVAL_MS = 5000

    def _switch_pet_for_agent(self, agent_type: str) -> None:
        """Load the per-tool pet from config if one is configured for agent_type."""
        if not agent_type:
            return
        cfg = config.load_user_config()
        pet_id = cfg.get(f"{agent_type}_pet_id", "").strip()
        if not pet_id:
            return
        for root in (config.CODEX_PETS_DIR, config.PLUGIN_PETS_DIR):
            candidate = root / pet_id
            if _is_valid_pet_dir(candidate):
                try:
                    meta = json.loads((candidate / "pet.json").read_text("utf-8"))
                    self._load_pet(candidate, meta)
                    self._render_current_frame()
                    _log(f"switched to {agent_type} pet: {pet_id}")
                except Exception as e:
                    _log(f"_switch_pet_for_agent({agent_type!r}): load failed: {e}")
                return
        _log(f"_switch_pet_for_agent({agent_type!r}): pet {pet_id!r} not found")

    def _maybe_revert_pet(self) -> None:
        """After a session ends, switch to the pet for the remaining agent type
        if all remaining sessions belong to a single known tool."""
        if not self.active_sessions:
            return
        remaining = {atype for _pid, atype in self.active_sessions.values() if atype}
        if len(remaining) == 1:
            self._switch_pet_for_agent(next(iter(remaining)))

    def _track_session(
        self, event_name: str, session_id: str, parent_pid: int, agent_type: str = ""
    ) -> None:
        """Maintain `self.active_sessions` from incoming events.

        - SessionEnd drains the entry (and triggers the no-session quit check).
          Codex does not currently emit it, so Codex sessions drain via the
          liveness reaper when the Codex process exits.
        - Any other event with a session_id late-binds the session into the
          dict if we haven't seen it yet (covers the case where the daemon
          was restarted mid-session and SessionStart was missed). PID is
          updated whenever we learn a better value than what we had stored.

        When all sessions drain and `stay_even_no_session` is False, schedule
        a deferred quit so the goodbye wave has time to play."""
        if event_name == "SessionEnd":
            if session_id and self.active_sessions.pop(session_id, None) is not None:
                _log(
                    f"session ended: {session_id} "
                    f"(active={len(self.active_sessions)})"
                )
            self._maybe_revert_pet()
            self._maybe_schedule_quit()
            return

        if not session_id:
            return
        prev = self.active_sessions.get(session_id)
        if prev is None:
            self.active_sessions[session_id] = (parent_pid, agent_type)
            label = "registered" if event_name == "SessionStart" else "learned mid-stream"
            _log(
                f"session {label}: {session_id} pid={parent_pid or '?'} "
                f"agent={agent_type or '?'} (active={len(self.active_sessions)})"
            )
            if event_name == "SessionStart":
                self._switch_pet_for_agent(agent_type)
        else:
            prev_pid, prev_type = prev
            if parent_pid > 0 and prev_pid <= 0:
                # Late binding: we knew the session but not its PID; learn now.
                self.active_sessions[session_id] = (parent_pid, agent_type or prev_type)
                _log(f"session {session_id} pid learned: {parent_pid}")

    def _should_quit_now(self) -> bool:
        """Common predicate for both the deferred-quit scheduler and the
        deferred-quit fire path. Centralizes the 'no sessions + no opt-in'
        check so the two callers can't drift."""
        if self.active_sessions:
            return False
        if config.load_user_config().get("stay_even_no_session"):
            return False
        return True

    def _maybe_schedule_quit(self) -> None:
        """Called whenever sessions transition toward empty. Schedules a
        deferred quit if appropriate; the deferred check re-validates so
        a session arriving during the delay window cancels the quit."""
        if not self._should_quit_now():
            return
        _log(
            "no active sessions remaining; scheduling daemon quit "
            f"in {self._NO_SESSION_QUIT_DELAY_MS}ms"
        )
        QTimer.singleShot(self._NO_SESSION_QUIT_DELAY_MS, self._quit_if_no_sessions)

    def _quit_if_no_sessions(self) -> None:
        """Deferred quit fire: a SessionStart that arrived during the delay
        window cancels the quit; a config flip to `stay_even_no_session=true`
        also cancels it."""
        if not self._should_quit_now():
            return
        _log("no active sessions; quitting daemon")
        self.quit_requested.emit()

    def _reap_dead_sessions(self) -> None:
        """Drop sessions whose Claude Code parent PID is no longer alive.

        This is the safety net for sessions that ended uncleanly (crash,
        SIGKILL, terminal closed) and never delivered a SessionEnd hook.
        Sessions registered without a usable PID (value <= 0) are skipped —
        they can only be drained by an explicit SessionEnd."""
        if not self.active_sessions:
            return
        dead: list[tuple[str, int]] = []
        for sid, (pid, _atype) in self.active_sessions.items():
            if pid <= 0:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                dead.append((sid, pid))
            except PermissionError:
                # PID exists but isn't ours to signal. Treat as alive — we'd
                # rather leave the daemon running than kill it on a /proc
                # permissions glitch.
                pass
            except OSError:
                pass
        if not dead:
            return
        for sid, pid in dead:
            self.active_sessions.pop(sid, None)
            _log(
                f"session {sid} parent (pid={pid}) gone; reaped "
                f"(active={len(self.active_sessions)})"
            )
        self._maybe_schedule_quit()

    # ----- ticking -----

    def _tick(self) -> None:
        # If active animation changed underneath us (e.g. drag state flipped
        # during a previous tick interval), switch first; otherwise advance.
        name, oneshot = self._active_animation()
        if self.anim.name != name or self.anim.is_oneshot != oneshot:
            self.anim.set(name, oneshot)
        else:
            finished = self.anim.advance()
            if finished and self.anim.is_oneshot and self.anim.name == self.oneshot_state:
                self.oneshot_state = None
                # Re-resolve after clearing oneshot.
                name2, oneshot2 = self._active_animation()
                self.anim.set(name2, oneshot2)
        if os.environ.get("PET4AGENTS_DEBUG"):
            _log(f"tick anim={self.anim.name} frame={self.anim.frame_index} drag={self.drag_state}")
        self._render_current_frame()
        self.timer.start(self.anim.current_duration_ms())

    # ----- v2 look directions -----

    def _reload_look_settings(self) -> None:
        """Cache the look-related user config. Read once here rather than on
        every poll so pointer tracking costs no filesystem IO."""
        cfg = config.load_user_config()
        defaults = config.DEFAULT_USER_CONFIG
        self._look_enabled = bool(cfg.get("look_at_cursor", defaults["look_at_cursor"]))
        self._look_radius = _positive_number(
            cfg.get("look_radius"), defaults["look_radius"]
        )
        self._look_deadzone = _positive_number(
            cfg.get("look_deadzone"), defaults["look_deadzone"]
        )

    def _look_ready(self) -> bool:
        """Look poses only stand in for the plain idle loop — never during a
        drag, a oneshot flash, or while any interval is open, and never for v1
        pets (their atlas has no look rows)."""
        if self.sprite_version != config.SPRITE_V2 or self.atlas.isNull():
            return False
        if self.drag_state or self.oneshot_state:
            return False
        return self.base_stack[-1][1] == "idle"

    def _update_look(self) -> None:
        """Pick the look cell for the current pointer position, if any."""
        cell: tuple[int, int] | None = None
        if (self._look_enabled and self._look_ready()
                and self._x11_pointer.is_tracked()):
            center = self.frameGeometry().center()
            pos = QCursor.pos()
            dx = pos.x() - center.x()
            dy = pos.y() - center.y()
            dist = math.hypot(dx, dy)
            if dist <= self._look_radius:
                if dist < self._look_deadzone:
                    cell = config.LOOK_NEUTRAL_CELL
                else:
                    # Clockwise from screen-up, matching the v2 cell order.
                    angle = math.degrees(math.atan2(dx, -dy)) % 360.0
                    cell = config.look_cell(
                        round(angle / config.LOOK_DEGREES_PER_STEP)
                    )
        if cell != self.look_cell:
            self.look_cell = cell
            self._render_current_frame()

    def _render_current_frame(self) -> None:
        if self.atlas.isNull():
            return
        if self.look_cell and self._look_ready():
            row, col = self.look_cell
        else:
            col = self.anim.frame_index
            row = self.anim.current_row()
        rect = QRect(col * config.CELL_W, row * config.CELL_H, config.CELL_W, config.CELL_H)
        self._frame_buf.fill(Qt.transparent)
        p = QPainter(self._frame_buf)
        p.drawPixmap(0, 0, self.atlas, rect.x(), rect.y(), rect.width(), rect.height())
        p.end()
        self.label.setPixmap(self._frame_buf)

    # ----- drag handling -----

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return
        if self._dragging:
            self._on_drag_idle()
        # Arm the press but don't change the animation yet. We don't know if
        # this is a click or a drag — that decision is made by either the
        # long-press timer firing or the cursor moving past DRAG_VEL_THRESHOLD.
        self._long_press_timer.stop()
        self._armed = True
        self._dragging = False
        self._manual_dragging = False
        self._drag_offset = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
        self._last_move_pos = self.pos()
        self._press_pos = ev.globalPosition().toPoint()
        self._drag_dx_accum = 0
        self._long_press_timer.start()
        if os.environ.get("PET4AGENTS_DEBUG"):
            _log("mousePress armed (waiting for long-press or drag-threshold)")
        ev.accept()

    def _on_long_press_fire(self) -> None:
        """Long-press timer expired while still armed → promote to drag."""
        if not self._armed:
            return
        self._begin_drag()

    def _begin_drag(self) -> None:
        """Promote an armed press into an actual drag. Cursor may not have
        moved yet — drag_state starts as 'jumping' (loops while held) and is
        replaced by 'running-{dir}' once horizontal motion is detected."""
        if self._dragging:
            return
        self._armed = False
        self._long_press_timer.stop()
        self._dragging = True
        self.drag_state = "jumping"
        self._sync_anim()
        self._restart_timer()
        self._drag_idle_timer.start()
        if os.environ.get("PET4AGENTS_DEBUG"):
            _log("_begin_drag (long-press or drag-threshold reached)")

    def mouseMoveEvent(self, ev) -> None:
        if self._armed and not self._dragging:
            # Still deciding click-vs-drag. If the cursor moves past the
            # threshold before the long-press timer fires, promote to drag
            # immediately (otherwise fast drags would feel laggy).
            pointer_pos = ev.globalPosition().toPoint()
            total_dx = pointer_pos.x() - self._press_pos.x()
            total_dy = pointer_pos.y() - self._press_pos.y()
            if max(abs(total_dx), abs(total_dy)) < DRAG_VEL_THRESHOLD:
                ev.accept()
                return
            self._begin_drag()
            # Fall through to the drag-handling block below.
        if not self._dragging:
            super().mouseMoveEvent(ev)
            return

        pointer_pos = ev.globalPosition().toPoint()
        total_dx = pointer_pos.x() - self._press_pos.x()
        total_dy = pointer_pos.y() - self._press_pos.y()
        new_pos = pointer_pos - self._drag_offset

        if not self._manual_dragging:
            if max(abs(total_dx), abs(total_dy)) < DRAG_VEL_THRESHOLD:
                ev.accept()
                return
            self._update_drag_state(total_dx)
            self._manual_dragging = True

        dx = new_pos.x() - self._last_move_pos.x()
        self._update_drag_position(new_pos, dx)
        self.move(new_pos)
        ev.accept()

    def _update_drag_position(self, new_pos: QPoint, dx: int) -> None:
        self._last_move_pos = new_pos
        self._update_drag_state(dx)
        if self._manual_dragging:
            self._drag_idle_timer.start()  # restart watchdog

    def _update_drag_state(self, dx: int) -> None:
        # Accumulate horizontal motion until it crosses the threshold, then
        # commit a running direction. Reset the accumulator the moment dx
        # reverses sign so direction changes are detected even during slow
        # drags (where individual moveEvent deltas stay below the threshold).
        if dx * self._drag_dx_accum < 0:
            self._drag_dx_accum = 0
        self._drag_dx_accum += dx
        if abs(self._drag_dx_accum) < DRAG_VEL_THRESHOLD:
            return
        new_state = "running-right" if self._drag_dx_accum > 0 else "running-left"
        self._drag_dx_accum = 0
        if new_state != self.drag_state:
            self.drag_state = new_state
            # Do NOT restart the timer here: that would reset the frame index
            # and starve the animation while the cursor moves. The next _tick
            # will pick up the new state automatically.
            if os.environ.get("PET4AGENTS_DEBUG"):
                _log(f"_update_drag_state dx={dx} -> {new_state}")

    def _on_drag_idle(self) -> None:
        # Defensive: also disarm any pending long-press so a stale timer can't
        # promote a press into a drag after the gesture has already ended.
        self._long_press_timer.stop()
        self._armed = False
        if not self._dragging:
            return
        if QApplication.mouseButtons() & Qt.LeftButton:
            self._drag_dx_accum = 0
            self._manual_dragging = True
            self._drag_idle_timer.start()
            return
        self._dragging = False
        self._manual_dragging = False
        self._drag_dx_accum = 0
        self._drag_idle_timer.stop()
        self.drag_state = None
        self._sync_anim()
        self._restart_timer()
        self._save_position()

    def mouseReleaseEvent(self, ev) -> None:
        # On X11 we may still get this; on Wayland we won't. Either way it's safe.
        if ev.button() != Qt.LeftButton:
            return
        if self._armed and not self._dragging:
            # Released before long-press fired and without crossing the move
            # threshold → treat as a click and fire a jumping oneshot.
            self._long_press_timer.stop()
            self._armed = False
            self.oneshot_state = "jumping"
            self._sync_anim()
            self._restart_timer()
            if os.environ.get("PET4AGENTS_DEBUG"):
                _log("mouseRelease: click → jumping oneshot")
            ev.accept()
            return
        self._on_drag_idle()
        ev.accept()

    def mouseDoubleClickEvent(self, ev) -> None:
        # Keep double-click as a no-op for the right button; the context menu
        # handles quitting via an explicit user choice.
        pass

    def contextMenuEvent(self, ev) -> None:
        # Right-click → show a small menu so quitting is an explicit choice
        # rather than an accidental side-effect of clicking the pet.
        menu = QMenu(self)
        self._make_non_activating(menu)
        quit_action = menu.addAction("Exit")
        chosen = menu.exec(ev.globalPos())
        if chosen is quit_action:
            self.quit_requested.emit()
        ev.accept()

    # ----- position persistence -----

    def _save_position(self) -> None:
        try:
            config.ensure_dirs()
            pos = self.pos()
            config.WINDOW_STATE_PATH.write_text(
                json.dumps({"x": pos.x(), "y": pos.y()}) + "\n", encoding="utf-8"
            )
        except Exception as e:
            _log(f"save_position failed: {e}")

    def _restore_position(self) -> None:
        screen = QGuiApplication.primaryScreen()
        sr = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        x = sr.right() - config.CELL_W - 50
        y = sr.bottom() - config.CELL_H - 50

        if config.WINDOW_STATE_PATH.exists():
            try:
                data = json.loads(config.WINDOW_STATE_PATH.read_text("utf-8"))
                x, y = int(data.get("x", x)), int(data.get("y", y))
                # Clamp into screen so the pet is never off-screen.
                x = max(sr.left(), min(x, sr.right() - config.CELL_W))
                y = max(sr.top(), min(y, sr.bottom() - config.CELL_H))
            except Exception:
                pass
        self.move(x, y)


# ----------------------- IPC server -----------------------

class IpcServer:
    def __init__(self, win: PetWindow) -> None:
        self.win = win
        self.server = QLocalServer()
        # Make sure stale socket doesn't block us.
        QLocalServer.removeServer(str(config.SOCKET_PATH))
        ok = self.server.listen(str(config.SOCKET_PATH))
        if not ok:
            _log(f"QLocalServer.listen failed: {self.server.errorString()}")
            raise RuntimeError("could not bind socket")
        self.server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self) -> None:
        while True:
            sock = self.server.nextPendingConnection()
            if sock is None:
                break
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(sock.deleteLater)

    def _on_ready(self, sock: QLocalSocket) -> None:
        data: QByteArray = sock.readAll()
        text = bytes(data).decode("utf-8", "replace").strip()
        for line in text.splitlines():
            self._handle(line)

    def _handle(self, line: str) -> None:
        if not line:
            return
        try:
            msg = json.loads(line)
        except Exception:
            _log(f"bad message: {line[:200]}")
            return
        kind = msg.get("kind")
        if kind == "event":
            try:
                parent_pid = int(msg.get("parent_pid") or 0)
            except (TypeError, ValueError):
                parent_pid = 0
            self.win.apply_event(
                msg.get("event", ""),
                msg.get("session_id", ""),
                parent_pid,
                msg.get("agent_type", ""),
            )
        elif kind == "quit":
            self.win.quit_requested.emit()
        elif kind == "reload":
            self.win.reload_pet()
        else:
            _log(f"unknown kind: {kind}")


# ----------------------- main -----------------------

# Kept open for the process lifetime: closing it (or letting it be
# garbage-collected) releases the flock below. Never reassign/close this
# once acquire_singleton() succeeds.
_pidfile_lock_fd: int | None = None


def acquire_singleton() -> bool:
    """Daemon singleton via an flock'd pidfile.

    A plain "check pid, then write pidfile" dance is racy: two daemons
    launched back-to-back (e.g. Codex firing several SessionStart events in
    quick succession while compacting context, or a user rapidly
    opening/closing the Claude Code UI) can both pass the check before
    either writes, and both end up believing they're the only instance.
    flock(2) on the pidfile is atomic at the kernel level, so only one
    process can ever hold it regardless of timing.
    """
    global _pidfile_lock_fd
    config.ensure_dirs()
    fd = os.open(str(config.PIDFILE_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            existing = os.read(fd, 64).decode("utf-8", "replace").strip()
        except OSError:
            existing = "?"
        os.close(fd)
        _log(f"another daemon already running (pid={existing or '?'})")
        return False
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    os.fsync(fd)
    _pidfile_lock_fd = fd  # keep the lock held for the life of this process
    return True


def main() -> int:
    if not acquire_singleton():
        return 0

    found = discover_pet()
    if not found:
        _log(
            "no pet found; place one at ~/.codex/pets/<id>/ "
            "or set CLAUDE_PET_ID"
        )
        return 1
    pet_dir, meta = found

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    win = PetWindow(pet_dir, meta)
    try:
        ipc = IpcServer(win)  # noqa: F841 (kept alive for the duration)
    except Exception as e:
        _log(f"IPC server setup failed: {e}\n{traceback.format_exc()}")
        return 1

    def _quit():
        global _pidfile_lock_fd
        try:
            QLocalServer.removeServer(str(config.SOCKET_PATH))
            if config.PIDFILE_PATH.exists():
                config.PIDFILE_PATH.unlink()
        except Exception:
            pass
        if _pidfile_lock_fd is not None:
            try:
                os.close(_pidfile_lock_fd)
            except OSError:
                pass
            _pidfile_lock_fd = None
        app.quit()

    win.quit_requested.connect(_quit)
    signal.signal(signal.SIGTERM, lambda *_: _quit())
    signal.signal(signal.SIGINT, lambda *_: _quit())

    win.show()
    _log(f"daemon started, pet={pet_dir.name}, platform={QGuiApplication.platformName()}")
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        _log(f"top-level exception: {e}\n{traceback.format_exc()}")
        sys.exit(1)
