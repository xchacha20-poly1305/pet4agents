#!/usr/bin/env python3
"""
Long-running pet daemon. Renders a frameless transparent always-on-top window,
plays per-frame animations from a Codex-format spritesheet, and listens for
event JSON over a Unix socket sent by pet_event.py.

State machine has three layers, in priority order:
  drag_state    — set while the mouse is dragging the pet
  oneshot_state — single-pass states (waving/jumping/failed/review)
  base_state    — looping ambient state (idle/running/waiting)

When a oneshot finishes, we fall back to base_state. drag_state is cleared on
mouse release.
"""
from __future__ import annotations

import json
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
    QEvent,
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


def _log(line: str) -> None:
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with config.LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] daemon: {line}\n")
    except Exception:
        pass


# ----------------------- Pet discovery -----------------------

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
    sheet_rel = meta.get("spritesheetPath") or "spritesheet.webp"
    return (d / sheet_rel).exists()


def discover_pet() -> tuple[Path, dict] | None:
    """Return (pet_dir, metadata) or None."""
    cfg_pet = ""
    if config.CONFIG_PATH.exists():
        try:
            cfg_pet = json.loads(config.CONFIG_PATH.read_text("utf-8")).get("pet_id", "")
        except Exception:
            cfg_pet = ""

    candidates: list[Path] = []
    env_pet = os.environ.get("CLAUDE_PET_ID", "").strip()
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
DRAG_IDLE_TIMEOUT_MS = 220  # fallback drag: no moveEvent for this long -> ended
# A press is treated as a click (→ jumping oneshot on release) until either
# the cursor crosses DRAG_VEL_THRESHOLD or the press is held this long. Once
# either condition fires the press promotes to a drag (drag_state set).
LONG_PRESS_MS = 220


class PetWindow(QWidget):
    quit_requested = Signal()

    def __init__(self, pet_dir: Path, meta: dict) -> None:
        super().__init__()
        self.pet_dir = pet_dir
        self.meta = meta
        self.atlas: QPixmap = QPixmap()
        self._frame_buf = QPixmap(config.CELL_W, config.CELL_H)

        self.base_state = "idle"
        self.oneshot_state: str | None = None
        self.drag_state: str | None = None

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
        self._last_move_t_ms = 0
        self._drag_offset = QPoint()
        self._system_drag_started_t_ms = 0
        # Accumulates horizontal movement until it crosses DRAG_VEL_THRESHOLD,
        # at which point we commit a running direction. Resets on sign reversal
        # so direction changes propagate even during slow drags.
        self._drag_dx_accum = 0
        # Cursor position from the previous poll tick (used to derive dx during
        # a system drag, since some compositors don't deliver moveEvents while
        # the compositor owns the gesture).
        self._last_cursor_pos = QPoint()

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

        # Watchdog for client-side fallback drags. Native system drags may not
        # deliver move/release events while the compositor owns the gesture.
        self._drag_idle_timer = QTimer(self)
        self._drag_idle_timer.setSingleShot(True)
        self._drag_idle_timer.setInterval(DRAG_IDLE_TIMEOUT_MS)
        self._drag_idle_timer.timeout.connect(self._on_drag_idle)

        # Cursor poll for system drags. Some compositors (notably Wayland with
        # xdg_toplevel.move) don't deliver moveEvents to the application while
        # they own the drag gesture, so we sample QCursor.pos() periodically
        # to derive horizontal motion and update the running direction.
        self._drag_poll_timer = QTimer(self)
        self._drag_poll_timer.setInterval(33)  # ~30 Hz
        self._drag_poll_timer.timeout.connect(self._poll_drag)

        # Long-press timer: distinguishes click from drag. Starts on press and
        # promotes the armed press into a drag if it fires before release.
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(LONG_PRESS_MS)
        self._long_press_timer.timeout.connect(self._on_long_press_fire)

    def _init_window(self) -> None:
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFixedSize(QSize(config.CELL_W, config.CELL_H))
        self.setWindowTitle(self.meta.get("displayName") or "Pet")

    def _load_pet(self, pet_dir: Path, meta: dict) -> None:
        sheet_rel = meta.get("spritesheetPath") or "spritesheet.webp"
        sheet_path = pet_dir / sheet_rel
        pix = QPixmap(str(sheet_path))
        if pix.isNull():
            _log(f"failed to load atlas: {sheet_path}")
            return
        if pix.width() != config.ATLAS_W or pix.height() != config.ATLAS_H:
            _log(
                f"atlas size mismatch: got {pix.width()}x{pix.height()}, "
                f"expected {config.ATLAS_W}x{config.ATLAS_H}"
            )
        self.atlas = pix
        self.pet_dir = pet_dir
        self.meta = meta

    def reload_pet(self) -> None:
        found = discover_pet()
        if not found:
            _log("reload_pet: no pet found")
            return
        pet_dir, meta = found
        self._load_pet(pet_dir, meta)
        self._render_current_frame()

    # ----- state machine -----

    def _active_animation(self) -> tuple[str, bool]:
        if self.drag_state:
            return self.drag_state, False  # drag states loop while held
        if self.oneshot_state:
            return self.oneshot_state, True
        return self.base_state, False

    def _sync_anim(self) -> None:
        name, oneshot = self._active_animation()
        if name != self.anim.name or oneshot != self.anim.is_oneshot:
            self.anim.set(name, oneshot)

    def apply_event(self, event_name: str) -> None:
        spec = config.EVENT_MAP.get(event_name)
        if not spec:
            return
        anim_name, is_oneshot = spec
        if event_name in config.RESET_BASE_EVENTS:
            self.base_state = "idle"
        if is_oneshot:
            self.oneshot_state = anim_name
        else:
            self.base_state = anim_name
            self.oneshot_state = None
        self._sync_anim()
        self._restart_timer()

    def _restart_timer(self) -> None:
        self.timer.stop()
        self._render_current_frame()
        self.timer.start(self.anim.current_duration_ms())

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
        if os.environ.get("CCPET_DEBUG"):
            _log(f"tick anim={self.anim.name} frame={self.anim.frame_index} drag={self.drag_state}")
        self._render_current_frame()
        self.timer.start(self.anim.current_duration_ms())

    def _render_current_frame(self) -> None:
        if self.atlas.isNull():
            return
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
        self._last_move_t_ms = int(time.monotonic() * 1000)
        self._system_drag_started_t_ms = 0
        self._drag_dx_accum = 0
        self._last_cursor_pos = self._press_pos
        self._long_press_timer.start()
        if os.environ.get("CCPET_DEBUG"):
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
        if os.environ.get("CCPET_DEBUG"):
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
        if self._system_drag_started_t_ms:
            ev.accept()
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
            moved_by_system = False
            wh = self.windowHandle()
            if wh is not None:
                try:
                    moved_by_system = bool(wh.startSystemMove())
                except Exception as e:
                    _log(f"startSystemMove failed: {e}")
            if moved_by_system:
                self._system_drag_started_t_ms = int(time.monotonic() * 1000)
                self._last_move_pos = self.pos()
                self._last_move_t_ms = self._system_drag_started_t_ms
                self._last_cursor_pos = pointer_pos
                self._drag_poll_timer.start()
                ev.accept()
                return
            self._manual_dragging = True

        dx = new_pos.x() - self._last_move_pos.x()
        self._update_drag_position(new_pos, dx)
        self.move(new_pos)
        ev.accept()

    def moveEvent(self, ev) -> None:
        super().moveEvent(ev)
        if self._dragging and not self._manual_dragging and self._system_drag_started_t_ms:
            dx = ev.pos().x() - self._last_move_pos.x()
            self._last_move_pos = ev.pos()
            self._last_move_t_ms = int(time.monotonic() * 1000)
            self._update_drag_state(dx)

    def event(self, ev) -> bool:
        if self._dragging and not self._manual_dragging and self._system_drag_started_t_ms:
            event_type = ev.type()
            if os.environ.get("CCPET_DEBUG") and event_type in {
                QEvent.Type.WindowActivate,
                QEvent.Type.NonClientAreaMouseButtonRelease,
                QEvent.Type.MouseButtonRelease,
            }:
                _log(f"drag event type={event_type}")
            if event_type in {
                QEvent.Type.WindowActivate,
                QEvent.Type.NonClientAreaMouseButtonRelease,
                QEvent.Type.MouseButtonRelease,
            }:
                now_ms = int(time.monotonic() * 1000)
                if now_ms - self._system_drag_started_t_ms >= DRAG_IDLE_TIMEOUT_MS:
                    self._on_drag_idle()
        return super().event(ev)

    def _update_drag_position(self, new_pos: QPoint, dx: int) -> None:
        self._last_move_pos = new_pos
        self._last_move_t_ms = int(time.monotonic() * 1000)
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
            if os.environ.get("CCPET_DEBUG"):
                _log(f"_update_drag_state dx={dx} -> {new_state}")

    def _poll_drag(self) -> None:
        """Sample QCursor.pos() during a system drag to derive horizontal motion.

        Only used on platforms where startSystemMove() actually delivers move
        events to the compositor *and* keeps QCursor.pos() live (some X11
        WMs). On Wayland we never enter this path because we force manual
        dragging.
        """
        if not (self._dragging and self._system_drag_started_t_ms and not self._manual_dragging):
            return
        cursor_pos = QCursor.pos()
        if cursor_pos == self._last_cursor_pos:
            return
        dx = cursor_pos.x() - self._last_cursor_pos.x()
        self._last_cursor_pos = cursor_pos
        self._last_move_t_ms = int(time.monotonic() * 1000)
        self._update_drag_state(dx)

    def _on_drag_idle(self) -> None:
        # Defensive: also disarm any pending long-press so a stale timer can't
        # promote a press into a drag after the gesture has already ended.
        self._long_press_timer.stop()
        self._armed = False
        if not self._dragging:
            return
        self._dragging = False
        self._manual_dragging = False
        self._system_drag_started_t_ms = 0
        self._drag_dx_accum = 0
        self._drag_idle_timer.stop()
        self._drag_poll_timer.stop()
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
            if os.environ.get("CCPET_DEBUG"):
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
            self.win.apply_event(msg.get("event", ""))
        elif kind == "quit":
            self.win.quit_requested.emit()
        elif kind == "reload":
            self.win.reload_pet()
        else:
            _log(f"unknown kind: {kind}")


# ----------------------- main -----------------------

def acquire_singleton() -> bool:
    """Best-effort daemon singleton via pidfile + socket probe."""
    config.ensure_dirs()
    if config.PIDFILE_PATH.exists():
        try:
            pid = int(config.PIDFILE_PATH.read_text("utf-8").strip())
        except Exception:
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 0)
                # Process exists; check the socket too. If both look alive, abort.
                if config.SOCKET_PATH.exists():
                    _log(f"another daemon already running (pid={pid})")
                    return False
            except OSError:
                pass  # stale pidfile
    config.PIDFILE_PATH.write_text(str(os.getpid()) + "\n", encoding="utf-8")
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
        try:
            QLocalServer.removeServer(str(config.SOCKET_PATH))
            if config.PIDFILE_PATH.exists():
                config.PIDFILE_PATH.unlink()
        except Exception:
            pass
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
