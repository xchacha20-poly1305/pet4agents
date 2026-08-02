"""Tests for `scripts/pet_event.py`: the hook relay.

Covers agent-PID discovery over a synthetic /proc tree, the IPC send path over
a real Unix socket, runtime-state bookkeeping, and the `set-pet` subcommand.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

import config
import pet_event


# ── synthetic /proc ───────────────────────────────────────────────────────


@pytest.fixture()
def fake_proc(tmp_path, monkeypatch):
    """Redirect `/proc/...` reads in pet_event to a temp tree.

    Returns a helper that registers a pid with a comm, argv and ppid.
    `_agent_name` uses `pet_event.Path`; `_read_ppid` uses the bare `open`
    builtin, which module-global shadowing intercepts.
    """
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    real_path = Path
    real_open = open

    def _redirect(p) -> str:
        s = str(p)
        if s == "/proc" or s.startswith("/proc/"):
            return str(proc_root) + s[len("/proc"):]
        return s

    monkeypatch.setattr(pet_event, "Path", lambda p: real_path(_redirect(p)))
    monkeypatch.setattr(
        pet_event, "open",
        lambda f, *a, **kw: real_open(_redirect(f), *a, **kw),
        raising=False,
    )

    def add(pid: int, *, comm: str = "", argv: list[str] | None = None, ppid: int = 0):
        d = proc_root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        if comm:
            (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in (argv or [])) + b"\x00")
        (d / "status").write_text(f"Name:\t{comm or 'x'}\nPid:\t{pid}\nPPid:\t{ppid}\n")
        return d

    add.root = proc_root
    return add


class TestAgentName:
    def test_matches_comm(self, fake_proc):
        fake_proc(100, comm="claude")
        assert pet_event._agent_name(100) == "claude"

    def test_matches_comm_case_insensitively(self, fake_proc):
        fake_proc(100, comm="Codex")
        assert pet_event._agent_name(100) == "codex"

    def test_matches_argv0_basename(self, fake_proc):
        fake_proc(100, comm="node", argv=["/usr/local/bin/codex", "exec"])
        assert pet_event._agent_name(100) == "codex"

    def test_strips_exe_suffix(self, fake_proc):
        fake_proc(100, comm="wrapper", argv=["/opt/claude.exe"])
        assert pet_event._agent_name(100) == "claude"

    def test_shell_wrapper_referencing_claude_path_does_not_match(self, fake_proc):
        fake_proc(100, comm="bash", argv=["/bin/bash", "-c", "~/.claude/plugins/pet/hook.sh"])
        assert pet_event._agent_name(100) == ""

    def test_missing_pid_returns_empty(self, fake_proc):
        assert pet_event._agent_name(4242) == ""

    def test_is_agent_pid(self, fake_proc):
        fake_proc(100, comm="claude")
        fake_proc(101, comm="bash")
        assert pet_event._is_agent_pid(100) is True
        assert pet_event._is_agent_pid(101) is False


class TestReadPpid:
    def test_reads_ppid(self, fake_proc):
        fake_proc(200, comm="bash", ppid=42)
        assert pet_event._read_ppid(200) == 42

    def test_missing_pid_returns_zero(self, fake_proc):
        assert pet_event._read_ppid(9999) == 0

    def test_malformed_status_returns_zero(self, fake_proc):
        d = fake_proc(201, comm="bash")
        (d / "status").write_text("PPid:\tnot-a-number\n")
        assert pet_event._read_ppid(201) == 0


class TestInferAgentTypeFromEnv:
    def test_explicit_marker_wins(self, monkeypatch):
        monkeypatch.setenv("PET4AGENTS_AGENT", "codex")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/x")
        assert pet_event._infer_agent_type_from_env() == "codex"

    def test_explicit_marker_is_normalized(self, monkeypatch):
        monkeypatch.setenv("PET4AGENTS_AGENT", "  Claude ")
        assert pet_event._infer_agent_type_from_env() == "claude"

    def test_unknown_marker_ignored(self, monkeypatch):
        monkeypatch.setenv("PET4AGENTS_AGENT", "gemini")
        monkeypatch.delenv("PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert pet_event._infer_agent_type_from_env() == ""

    def test_plugin_root_wins_over_claude_plugin_root(self, monkeypatch):
        """Codex injects CLAUDE_PLUGIN_ROOT for compatibility, so it must lose."""
        monkeypatch.delenv("PET4AGENTS_AGENT", raising=False)
        monkeypatch.setenv("PLUGIN_ROOT", "/x")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/x")
        assert pet_event._infer_agent_type_from_env() == "codex"

    def test_claude_plugin_root_alone(self, monkeypatch):
        monkeypatch.delenv("PET4AGENTS_AGENT", raising=False)
        monkeypatch.delenv("PLUGIN_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/x")
        assert pet_event._infer_agent_type_from_env() == "claude"

    def test_nothing_set(self, monkeypatch):
        for var in ("PET4AGENTS_AGENT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
            monkeypatch.delenv(var, raising=False)
        assert pet_event._infer_agent_type_from_env() == ""


class TestFindAgentInfo:
    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for var in ("PET4AGENTS_AGENT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
            monkeypatch.delenv(var, raising=False)

    def test_walks_up_to_the_agent(self, fake_proc, monkeypatch):
        fake_proc(10, comm="claude", ppid=1)
        fake_proc(11, comm="bash", ppid=10)
        fake_proc(12, comm="sh", ppid=11)
        monkeypatch.setattr(pet_event.os, "getppid", lambda: 12)
        assert pet_event.find_agent_info() == (10, "claude")

    def test_direct_parent_is_the_agent(self, fake_proc, monkeypatch):
        fake_proc(20, comm="codex", ppid=1)
        monkeypatch.setattr(pet_event.os, "getppid", lambda: 20)
        assert pet_event.find_agent_info() == (20, "codex")

    def test_no_agent_found_returns_zero_pid(self, fake_proc, monkeypatch):
        fake_proc(30, comm="bash", ppid=1)
        monkeypatch.setattr(pet_event.os, "getppid", lambda: 30)
        assert pet_event.find_agent_info() == (0, "")

    def test_no_agent_found_still_infers_type_from_env(self, fake_proc, monkeypatch):
        fake_proc(30, comm="bash", ppid=1)
        monkeypatch.setattr(pet_event.os, "getppid", lambda: 30)
        monkeypatch.setenv("PET4AGENTS_AGENT", "codex")
        assert pet_event.find_agent_info() == (0, "codex")

    def test_ppid_cycle_terminates(self, fake_proc, monkeypatch):
        fake_proc(40, comm="bash", ppid=41)
        fake_proc(41, comm="bash", ppid=40)
        monkeypatch.setattr(pet_event.os, "getppid", lambda: 40)
        assert pet_event.find_agent_info() == (0, "")

    def test_depth_is_capped(self, fake_proc, monkeypatch):
        """A 200-deep chain with the agent past the cap must not be found."""
        for pid in range(50, 250):
            fake_proc(pid, comm="bash", ppid=pid + 1)
        fake_proc(250, comm="claude", ppid=1)
        monkeypatch.setattr(pet_event.os, "getppid", lambda: 50)
        assert pet_event.find_agent_info() == (0, "")

    def test_find_agent_pid_is_the_first_element(self, fake_proc, monkeypatch):
        fake_proc(60, comm="claude", ppid=1)
        monkeypatch.setattr(pet_event.os, "getppid", lambda: 60)
        assert pet_event.find_agent_pid() == 60


# ── IPC ───────────────────────────────────────────────────────────────────


class _FakeDaemon:
    """Minimal Unix socket server that records one JSON line per connection."""

    def __init__(self, path: Path):
        self.received: list[dict] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(4)
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(65536).decode("utf-8").strip()
            if data:
                try:
                    self.received.append(json.loads(data))
                except ValueError:
                    pass

    def wait_for(self, count: int, timeout: float = 2.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while len(self.received) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.received) >= count, f"daemon received {self.received}"
        return self.received

    def close(self):
        self._stop = True
        self.sock.close()


@pytest.fixture()
def fake_daemon(paths):
    d = _FakeDaemon(config.SOCKET_PATH)
    yield d
    d.close()


class TestSendToDaemon:
    def test_returns_false_when_no_socket(self, paths):
        assert pet_event.send_to_daemon({"kind": "quit"}) is False

    def test_sends_payload(self, fake_daemon):
        assert pet_event.send_to_daemon({"kind": "event", "event": "Stop"}) is True
        assert fake_daemon.wait_for(1)[0] == {"kind": "event", "event": "Stop"}

    def test_falls_back_to_legacy_socket(self, paths):
        legacy = _FakeDaemon(config.LEGACY_SOCKET_PATH)
        try:
            assert pet_event.send_to_daemon({"kind": "quit"}) is True
            assert legacy.wait_for(1)[0] == {"kind": "quit"}
        finally:
            legacy.close()

    def test_stale_socket_file_is_not_fatal(self, paths):
        config.SOCKET_PATH.write_text("")  # a regular file, not a socket
        assert pet_event.send_to_daemon({"kind": "quit"}) is False

    def test_payload_is_newline_terminated(self, paths):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(config.SOCKET_PATH))
        srv.listen(1)
        try:
            assert pet_event.send_to_daemon({"kind": "quit"}) is True
            conn, _ = srv.accept()
            with conn:
                assert conn.recv(4096).decode("utf-8").endswith("\n")
        finally:
            srv.close()


# ── runtime state ─────────────────────────────────────────────────────────


class TestRuntimeState:
    def test_expected_state_reports_version_and_deps(self, paths):
        state = pet_event.expected_runtime_state()
        assert state["plugin_version"] == config.PLUGIN_VERSION
        assert state["dependencies"] == list(config.PINNED_PYTHON_DEPENDENCIES)

    def test_expected_state_includes_lockfile_hash(self, paths):
        assert config.UV_LOCKFILE_PATH.exists(), "repo should ship a uv lockfile"
        assert len(pet_event.expected_runtime_state()["uv_lock_sha256"]) == 64

    def test_lockfile_pins_every_declared_dependency(self):
        lock = config.UV_LOCKFILE_PATH.read_text("utf-8")
        for spec in config.PINNED_PYTHON_DEPENDENCIES:
            assert spec.lower() in lock.lower(), spec

    def test_load_runtime_state_missing_file(self, paths):
        assert pet_event.load_runtime_state() == {}

    def test_load_runtime_state_corrupt(self, paths):
        config.VENV_STATE_PATH.parent.mkdir(parents=True)
        config.VENV_STATE_PATH.write_text("{oops")
        assert pet_event.load_runtime_state() == {}

    def test_load_runtime_state_non_dict(self, paths):
        config.VENV_STATE_PATH.parent.mkdir(parents=True)
        config.VENV_STATE_PATH.write_text("[]")
        assert pet_event.load_runtime_state() == {}

    def test_write_then_load_roundtrips(self, paths):
        config.VENV_STATE_PATH.parent.mkdir(parents=True)
        pet_event.write_runtime_state()
        assert pet_event.load_runtime_state() == pet_event.expected_runtime_state()

    def test_write_failure_is_swallowed(self, paths):
        # Parent dir does not exist -> write raises, must be caught and logged.
        config.STATE_DIR.mkdir(exist_ok=True)
        pet_event.write_runtime_state()
        assert not config.VENV_STATE_PATH.exists()

    def test_matches_expected_false_on_stale_version(self, paths, monkeypatch):
        config.VENV_STATE_PATH.parent.mkdir(parents=True)
        pet_event.write_runtime_state()
        monkeypatch.setattr(config, "PLUGIN_VERSION", "999.0.0")
        assert pet_event.venv_matches_expected_runtime() is False

    def test_matches_expected_false_when_state_missing(self, paths):
        assert pet_event.venv_matches_expected_runtime() is False

    def test_matches_expected_false_when_dep_not_installed(self, paths, monkeypatch):
        config.VENV_STATE_PATH.parent.mkdir(parents=True)
        monkeypatch.setattr(config, "PINNED_PYTHON_DEPENDENCIES", ("Nonexistent==1.2.3",))
        pet_event.write_runtime_state()
        assert pet_event.venv_matches_expected_runtime() is False

    def test_matches_expected_false_for_unpinned_dep(self, paths, monkeypatch):
        config.VENV_STATE_PATH.parent.mkdir(parents=True)
        monkeypatch.setattr(config, "PINNED_PYTHON_DEPENDENCIES", ("pytest",))
        pet_event.write_runtime_state()
        assert pet_event.venv_matches_expected_runtime() is False

    def test_matches_expected_true_when_dep_versions_line_up(self, paths, monkeypatch):
        import importlib.metadata

        spec = f"pytest=={importlib.metadata.version('pytest')}"
        config.VENV_STATE_PATH.parent.mkdir(parents=True)
        monkeypatch.setattr(config, "PINNED_PYTHON_DEPENDENCIES", (spec,))
        pet_event.write_runtime_state()
        assert pet_event.venv_matches_expected_runtime() is True

    def test_installed_distribution_version_unknown_package(self):
        assert pet_event._installed_distribution_version("no-such-dist-xyz") == ""

    def test_sha256_matches_hashlib(self, tmp_path):
        import hashlib

        p = tmp_path / "blob.bin"
        p.write_bytes(b"pet4agents" * 1000)
        assert pet_event._sha256_file(p) == hashlib.sha256(p.read_bytes()).hexdigest()


class TestInPetVenv:
    def test_false_outside_venv(self, paths):
        assert pet_event.in_pet_venv() is False

    def test_true_when_executable_matches(self, paths, monkeypatch):
        monkeypatch.setattr(config, "VENV_PY", Path(sys.executable))
        assert pet_event.in_pet_venv() is True


# ── set-pet ───────────────────────────────────────────────────────────────


class TestCmdSetPet:
    def test_writes_pet_id(self, paths, capsys):
        pet_event.cmd_set_pet("kitty")
        assert json.loads(config.CONFIG_PATH.read_text())["pet_id"] == "kitty"
        assert "pet set to kitty" in capsys.readouterr().out

    def test_strips_whitespace(self, paths):
        pet_event.cmd_set_pet("  kitty \n")
        assert json.loads(config.CONFIG_PATH.read_text())["pet_id"] == "kitty"

    def test_preserves_other_keys(self, paths):
        config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_PATH.write_text(json.dumps({"look_radius": 42, "pet_id": "old"}))
        pet_event.cmd_set_pet("new")
        cfg = json.loads(config.CONFIG_PATH.read_text())
        assert cfg == {"look_radius": 42, "pet_id": "new"}

    def test_corrupt_config_is_replaced(self, paths):
        config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_PATH.write_text("{broken")
        pet_event.cmd_set_pet("kitty")
        assert json.loads(config.CONFIG_PATH.read_text()) == {"pet_id": "kitty"}

    @pytest.mark.parametrize("bad", ["", "   ", "../escape", "a/b", ".hidden"])
    def test_rejects_unsafe_ids(self, paths, bad, capsys):
        pet_event.cmd_set_pet(bad)
        assert "invalid pet id" in capsys.readouterr().out
        assert not config.CONFIG_PATH.exists()


class TestMain:
    def test_no_args_prints_usage(self, paths, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pet_event.py"])
        assert pet_event.main() == 0
        assert capsys.readouterr().out.strip()

    def test_set_pet_without_id_is_an_error(self, paths, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pet_event.py", "set-pet"])
        assert pet_event.main() == 1
        assert "usage" in capsys.readouterr().out

    def test_daemon_stop_without_daemon(self, paths, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pet_event.py", "daemon-stop"])
        assert pet_event.main() == 0
        assert "not running" in capsys.readouterr().out

    def test_daemon_spawn_event_without_payload(self, paths, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pet_event.py", "daemon-spawn-event"])
        assert pet_event.main() == 1

    def test_bad_base64_payload_does_not_spawn(self, paths, monkeypatch):
        called = []
        monkeypatch.setattr(pet_event, "ensure_venv_and_reexec", lambda: called.append(1))
        pet_event.cmd_daemon_spawn_event("!!!not-base64!!!")
        assert called == []


class TestCmdEvent:
    @pytest.fixture()
    def no_stdin(self, monkeypatch):
        class _Tty:
            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stdin", _Tty())

    def test_forwards_event_payload(self, fake_daemon, no_stdin, monkeypatch):
        monkeypatch.setattr(pet_event, "find_agent_info", lambda: (1234, "claude"))
        pet_event.cmd_event("Stop")
        msg = fake_daemon.wait_for(1)[0]
        assert msg["kind"] == "event"
        assert msg["event"] == "Stop"
        assert msg["parent_pid"] == 1234
        assert msg["agent_type"] == "claude"
        assert msg["cwd"] == os.getcwd()

    def test_reads_session_id_from_stdin(self, fake_daemon, tmp_path, monkeypatch, request):
        payload = json.dumps({"session_id": "abc", "cwd": "/somewhere"})
        stdin = tmp_path / "stdin.json"
        stdin.write_text(payload)
        fh = stdin.open()
        monkeypatch.setattr(sys, "stdin", fh)
        request.addfinalizer(fh.close)
        monkeypatch.setattr(pet_event, "find_agent_info", lambda: (0, "codex"))
        pet_event.cmd_event("SessionStart")
        msg = fake_daemon.wait_for(1)[0]
        assert msg["session_id"] == "abc"
        assert msg["cwd"] == "/somewhere"

    def test_malformed_stdin_does_not_crash(self, fake_daemon, tmp_path, monkeypatch, request):
        stdin = tmp_path / "stdin.txt"
        stdin.write_text("not json at all")
        fh = stdin.open()
        monkeypatch.setattr(sys, "stdin", fh)
        request.addfinalizer(fh.close)
        monkeypatch.setattr(pet_event, "find_agent_info", lambda: (0, ""))
        pet_event.cmd_event("Stop")
        assert fake_daemon.wait_for(1)[0]["event"] == "Stop"

    def test_non_session_start_does_not_spawn_daemon(self, paths, no_stdin, monkeypatch):
        spawned = []
        monkeypatch.setattr(pet_event, "spawn_daemon_event_worker", lambda p: spawned.append(p))
        monkeypatch.setattr(pet_event, "find_agent_info", lambda: (0, ""))
        pet_event.cmd_event("Stop")
        assert spawned == []

    def test_session_start_spawns_daemon_worker(self, paths, no_stdin, monkeypatch):
        spawned = []
        monkeypatch.setattr(pet_event, "spawn_daemon_event_worker", lambda p: spawned.append(p))
        monkeypatch.setattr(pet_event, "find_agent_info", lambda: (0, ""))
        pet_event.cmd_event("SessionStart")
        assert len(spawned) == 1 and spawned[0]["event"] == "SessionStart"
