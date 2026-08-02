"""Shared fixtures/helpers for the pet4agents test suite.

Both `scripts/config.py` and `scripts/pet_daemon.py` compute their paths at
import time, so tests redirect them by rebinding the module attributes rather
than by setting XDG env vars.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402


_QT_STUB_NAMES = {
    "PySide6.QtCore": ["QByteArray", "QPoint", "QRect", "QSize", "Qt", "QTimer", "Signal"],
    "PySide6.QtGui": ["QCursor", "QGuiApplication", "QPainter", "QPixmap"],
    "PySide6.QtNetwork": ["QLocalServer", "QLocalSocket"],
    "PySide6.QtWidgets": ["QApplication", "QLabel", "QMenu", "QWidget"],
}


def _install_qt_stubs() -> None:
    """Register import-time stand-ins for the Qt names `pet_daemon` imports.

    `QWidget` has to be a real class (PetWindow subclasses it) and `Signal` a
    real callable (it is evaluated in the class body), so a bare MagicMock
    module is not enough. Everything else is only touched at runtime by code
    these tests do not exercise.
    """
    import types

    class _StubQObject:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            return mock.MagicMock()

    pyside6 = types.ModuleType("PySide6")
    sys.modules["PySide6"] = pyside6
    for mod_name, names in _QT_STUB_NAMES.items():
        mod = types.ModuleType(mod_name)
        for name in names:
            if name == "Signal":
                setattr(mod, name, lambda *a, **kw: mock.MagicMock())
            else:
                setattr(mod, name, type(name, (_StubQObject,), {}))
        sys.modules[mod_name] = mod
        setattr(pyside6, mod_name.rsplit(".", 1)[1], mod)


def import_pet_daemon():
    """Import `pet_daemon`, stubbing PySide6 out when it isn't installed.

    The daemon imports Qt at module level; CI runners are headless and have no
    PySide6, so the stub keeps the pure-logic parts importable.
    """
    if "pet_daemon" in sys.modules:
        return sys.modules["pet_daemon"]
    try:
        import PySide6  # noqa: F401
    except ModuleNotFoundError:
        _install_qt_stubs()
    import pet_daemon  # noqa: E402

    return pet_daemon


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    """Redirect every config/state/pet path into a temp tree.

    Yields the tmp root; `paths / "config" / "config.json"` is the user config,
    `paths / "codex-pets"` and `paths / "plugin-pets"` are the two pet roots.
    """
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    codex_pets = tmp_path / "codex-pets"
    plugin_pets = tmp_path / "plugin-pets"
    for d in (state_dir, config_dir, data_dir, codex_pets, plugin_pets):
        d.mkdir()

    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "LOG_PATH", state_dir / "event.log")
    monkeypatch.setattr(config, "INSTALL_LOG_PATH", state_dir / "install.log")
    monkeypatch.setattr(config, "CONFIG_PATH", config_dir / "config.json")
    monkeypatch.setattr(config, "WINDOW_STATE_PATH", config_dir / "state.json")
    monkeypatch.setattr(config, "PIDFILE_PATH", state_dir / "daemon.pid")
    monkeypatch.setattr(config, "SOCKET_PATH", tmp_path / "pet4agents.sock")
    monkeypatch.setattr(config, "LEGACY_SOCKET_PATH", tmp_path / "legacy.sock")
    monkeypatch.setattr(config, "VENV_DIR", data_dir / "venv")
    monkeypatch.setattr(config, "VENV_PY", data_dir / "venv" / "bin" / "python")
    monkeypatch.setattr(config, "VENV_STATE_PATH", data_dir / "venv" / ".pet-runtime.json")
    monkeypatch.setattr(config, "CODEX_PETS_DIR", codex_pets)
    monkeypatch.setattr(config, "PLUGIN_PETS_DIR", plugin_pets)
    return tmp_path


def write_user_config(paths_root: Path, cfg: dict) -> None:
    import json

    (paths_root / "config" / "config.json").write_text(json.dumps(cfg))


def make_pet(root: Path, pet_id: str, *, meta: dict | None = None,
             sheet_name: str | None = "spritesheet.png") -> Path:
    """Create a minimal pet dir. `sheet_name=None` omits the spritesheet."""
    import json

    d = root / pet_id
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("pet.json").write_text(json.dumps(meta if meta is not None else {"name": pet_id}))
    if sheet_name:
        d.joinpath(sheet_name).write_bytes(b"\x89PNG\r\n")
    return d
