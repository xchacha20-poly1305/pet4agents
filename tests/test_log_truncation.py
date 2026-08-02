"""Tests for the max_log_size truncation feature."""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import config


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_env(tmp_path):
    """Redirect all config/state paths into a temp directory and restore after."""
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()

    orig = {
        "STATE_DIR": config.STATE_DIR,
        "LOG_PATH": config.LOG_PATH,
        "INSTALL_LOG_PATH": config.INSTALL_LOG_PATH,
        "CONFIG_DIR": config.CONFIG_DIR,
        "CONFIG_PATH": config.CONFIG_PATH,
    }
    config.STATE_DIR = state_dir
    config.LOG_PATH = state_dir / "event.log"
    config.INSTALL_LOG_PATH = state_dir / "install.log"
    config.CONFIG_DIR = config_dir
    config.CONFIG_PATH = config_dir / "config.json"
    yield tmp_path
    for k, v in orig.items():
        setattr(config, k, v)


def _write_config(value: int | str | None, tmp_path: Path) -> None:
    cfg: dict = {}
    if value is not None:
        cfg["max_log_size"] = value
    (tmp_path / "config" / "config.json").write_text(json.dumps(cfg))


def _fill_log(path: Path, n_lines: int = 200, line_len: int = 60) -> int:
    with path.open("w") as f:
        for i in range(n_lines):
            f.write(f"[2026-08-02 12:00:00] event line {i:04d}" + "x" * line_len + "\n")
    return path.stat().st_size


# ── truncate_log_if_needed unit tests ─────────────────────────────────────


class TestTruncateLogIfNeeded:
    def test_no_truncation_under_limit(self, tmp_path):
        p = tmp_path / "small.log"
        p.write_text("line1\nline2\nline3\n")
        size_before = p.stat().st_size
        config.truncate_log_if_needed(p, 10000)
        assert p.stat().st_size == size_before

    def test_truncation_over_limit(self, tmp_path):
        p = tmp_path / "big.log"
        original_size = _fill_log(p, n_lines=200)
        config.truncate_log_if_needed(p, 2000)
        new_size = p.stat().st_size
        assert new_size < original_size
        assert new_size <= 1100  # roughly half of 2000

    def test_truncated_content_starts_on_clean_line(self, tmp_path):
        p = tmp_path / "clean.log"
        _fill_log(p, n_lines=200)
        config.truncate_log_if_needed(p, 2000)
        content = p.read_text()
        assert content.startswith("["), f"Should start with '[', got: {content[:30]!r}"
        assert "\n" not in content.split("\n")[0][:5]

    def test_truncated_content_preserves_newest_lines(self, tmp_path):
        p = tmp_path / "newest.log"
        with p.open("w") as f:
            for i in range(100):
                f.write(f"[ts] line {i:04d}\n")
        config.truncate_log_if_needed(p, 200)
        lines = p.read_text().strip().split("\n")
        last_num = int(lines[-1].split()[-1])
        assert last_num == 99, f"Last line should be 0099, got {last_num}"

    def test_zero_limit_disables_truncation(self, tmp_path):
        p = tmp_path / "nolimit.log"
        original_size = _fill_log(p, n_lines=200)
        config.truncate_log_if_needed(p, 0)
        assert p.stat().st_size == original_size

    def test_negative_limit_disables_truncation(self, tmp_path):
        p = tmp_path / "neg.log"
        original_size = _fill_log(p, n_lines=200)
        config.truncate_log_if_needed(p, -100)
        assert p.stat().st_size == original_size

    def test_nonexistent_file_no_crash(self, tmp_path):
        config.truncate_log_if_needed(tmp_path / "ghost.log", 100)

    def test_exact_limit_no_truncation(self, tmp_path):
        p = tmp_path / "exact.log"
        p.write_text("abcde\n")
        size = p.stat().st_size
        config.truncate_log_if_needed(p, size)
        assert p.stat().st_size == size

    def test_one_byte_over_limit(self, tmp_path):
        p = tmp_path / "oneover.log"
        p.write_text("line-a\nline-b\nline-c\n")
        size = p.stat().st_size
        config.truncate_log_if_needed(p, size - 1)
        assert p.stat().st_size < size

    def test_single_line_file(self, tmp_path):
        p = tmp_path / "single.log"
        p.write_text("only one line with no newline at end")
        config.truncate_log_if_needed(p, 10)
        content = p.read_text()
        assert len(content) < 35

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.log"
        p.write_text("")
        config.truncate_log_if_needed(p, 10)
        assert p.stat().st_size == 0

    def test_repeated_truncation_converges(self, tmp_path):
        """Calling truncation many times should not shrink to nothing."""
        p = tmp_path / "converge.log"
        _fill_log(p, n_lines=100)
        limit = 2000
        for _ in range(20):
            config.truncate_log_if_needed(p, limit)
        size = p.stat().st_size
        assert size > 0, "Repeated truncation should not empty the file"
        content = p.read_text()
        assert len(content.strip().split("\n")) >= 1


# ── DEFAULT_USER_CONFIG tests ─────────────────────────────────────────────


class TestDefaultConfig:
    def test_default_has_max_log_size(self):
        assert "max_log_size" in config.DEFAULT_USER_CONFIG

    def test_default_value_is_5mib(self):
        assert config.DEFAULT_USER_CONFIG["max_log_size"] == 5 * 1024 * 1024

    def test_load_user_config_returns_default(self, tmp_env):
        cfg = config.load_user_config()
        assert cfg["max_log_size"] == 5 * 1024 * 1024

    def test_load_user_config_respects_override(self, tmp_env):
        _write_config(1024, tmp_env)
        cfg = config.load_user_config()
        assert cfg["max_log_size"] == 1024

    def test_load_user_config_respects_zero(self, tmp_env):
        _write_config(0, tmp_env)
        cfg = config.load_user_config()
        assert cfg["max_log_size"] == 0

    def test_load_user_config_missing_key_uses_default(self, tmp_env):
        _write_config(None, tmp_env)
        cfg = config.load_user_config()
        assert cfg["max_log_size"] == 5 * 1024 * 1024


# ── integrated _log tests (pet_event.py) ──────────────────────────────────


class TestPetEventLogIntegration:
    def test_log_respects_max_log_size(self, tmp_env):
        import pet_event

        _write_config(500, tmp_env)
        for i in range(100):
            pet_event._log(f"event {i:04d} padding padding padding")
        size = config.LOG_PATH.stat().st_size
        assert size <= 550, f"Log should be bounded to ~250, got {size}"
        content = config.LOG_PATH.read_text()
        lines = content.strip().split("\n")
        assert all(line.startswith("[") for line in lines)

    def test_log_install_respects_max_log_size(self, tmp_env):
        import pet_event

        _write_config(500, tmp_env)
        for i in range(100):
            pet_event._log_install(f"install {i:04d} padding padding")
        size = config.INSTALL_LOG_PATH.stat().st_size
        assert size <= 550, f"Install log should be bounded, got {size}"

    def test_log_unbounded_when_zero(self, tmp_env):
        import pet_event

        _write_config(0, tmp_env)
        for i in range(100):
            pet_event._log(f"event {i:04d}")
        size = config.LOG_PATH.stat().st_size
        assert size > 2000, f"Log should grow unbounded, got {size}"


# ── daemon _log cache tests ──────────────────────────────────────────────

# pet_daemon imports PySide6 at module level. In headless CI environments
# PySide6 is not installed, so we stub it out before importing the module.

def _import_pet_daemon():
    """Import pet_daemon with PySide6 stubbed out."""
    if "pet_daemon" in sys.modules:
        return sys.modules["pet_daemon"]
    try:
        import PySide6  # noqa: F401
    except ModuleNotFoundError:
        pyside_mods = [
            "PySide6", "PySide6.QtCore", "PySide6.QtGui",
            "PySide6.QtNetwork", "PySide6.QtWidgets",
        ]
        for mod_name in pyside_mods:
            sys.modules[mod_name] = mock.MagicMock()
    import pet_daemon  # noqa: E402
    return pet_daemon


class TestDaemonLogCache:
    def test_cached_value_used(self, tmp_env):
        pet_daemon = _import_pet_daemon()
        _write_config(500, tmp_env)
        pet_daemon._cached_max_log_size = None

        for i in range(50):
            pet_daemon._log(f"daemon event {i:04d} padding padding")
        size = config.LOG_PATH.stat().st_size
        assert size <= 550

        assert pet_daemon._cached_max_log_size == 500

    def test_cache_not_reloaded_on_every_call(self, tmp_env):
        pet_daemon = _import_pet_daemon()
        _write_config(500, tmp_env)
        pet_daemon._cached_max_log_size = None

        pet_daemon._log("first call loads cache")
        assert pet_daemon._cached_max_log_size == 500

        _write_config(9999, tmp_env)
        pet_daemon._log("second call uses cached value")
        assert pet_daemon._cached_max_log_size == 500, "Should still be 500, not re-read"

    def test_cache_reset_invalidates(self, tmp_env):
        """Setting _cached_max_log_size to None forces re-read on next _log."""
        pet_daemon = _import_pet_daemon()
        _write_config(500, tmp_env)
        pet_daemon._cached_max_log_size = None

        pet_daemon._log("triggers cache load")
        assert pet_daemon._cached_max_log_size == 500

        _write_config(2000, tmp_env)
        pet_daemon._cached_max_log_size = None  # simulate reload_pet

        pet_daemon._log("triggers cache reload")
        assert pet_daemon._cached_max_log_size == 2000
