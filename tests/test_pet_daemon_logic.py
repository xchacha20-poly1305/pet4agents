"""Tests for the Qt-free logic in `scripts/pet_daemon.py`.

`PetWindow` cannot be instantiated headlessly, so the state-machine tests bind
the real unbound methods onto a lightweight stand-in that supplies only the
attributes those methods touch.
"""
from __future__ import annotations

import json

import pytest

import config
from tests.conftest import import_pet_daemon, make_pet, write_user_config

pet_daemon = import_pet_daemon()


# ── pet directory helpers ─────────────────────────────────────────────────


class TestResolveSheetPath:
    def test_explicit_path_used(self, tmp_path):
        (tmp_path / "custom.png").write_bytes(b"x")
        got = pet_daemon._resolve_sheet_path(tmp_path, {"spritesheetPath": "custom.png"})
        assert got == tmp_path / "custom.png"

    def test_explicit_path_missing_returns_none(self, tmp_path):
        (tmp_path / "spritesheet.png").write_bytes(b"x")
        assert pet_daemon._resolve_sheet_path(tmp_path, {"spritesheetPath": "gone.png"}) is None

    def test_probes_conventional_names(self, tmp_path):
        (tmp_path / "spritesheet.png").write_bytes(b"x")
        assert pet_daemon._resolve_sheet_path(tmp_path, {}).name == "spritesheet.png"

    def test_webp_wins_over_png(self, tmp_path):
        (tmp_path / "spritesheet.webp").write_bytes(b"x")
        (tmp_path / "spritesheet.png").write_bytes(b"x")
        assert pet_daemon._resolve_sheet_path(tmp_path, {}).name == "spritesheet.webp"

    def test_no_sheet_returns_none(self, tmp_path):
        assert pet_daemon._resolve_sheet_path(tmp_path, {}) is None


class TestPositiveNumber:
    @pytest.mark.parametrize("value,expected", [(5, 5.0), (2.5, 2.5), (1e6, 1e6)])
    def test_positive_values_pass_through(self, value, expected):
        assert pet_daemon._positive_number(value, 99) == expected

    @pytest.mark.parametrize("value", [0, -1, -0.5, None, "600", [], {}, True, False])
    def test_invalid_values_use_fallback(self, value):
        assert pet_daemon._positive_number(value, 99) == 99.0

    def test_result_is_always_float(self):
        assert isinstance(pet_daemon._positive_number(3, 1), float)


class TestIsValidPetDir:
    def test_valid(self, tmp_path):
        d = make_pet(tmp_path, "kitty")
        assert pet_daemon._is_valid_pet_dir(d) is True

    def test_missing_dir(self, tmp_path):
        assert pet_daemon._is_valid_pet_dir(tmp_path / "nope") is False

    def test_file_instead_of_dir(self, tmp_path):
        f = tmp_path / "pet"
        f.write_text("x")
        assert pet_daemon._is_valid_pet_dir(f) is False

    def test_missing_pet_json(self, tmp_path):
        d = tmp_path / "kitty"
        d.mkdir()
        (d / "spritesheet.png").write_bytes(b"x")
        assert pet_daemon._is_valid_pet_dir(d) is False

    def test_corrupt_pet_json(self, tmp_path):
        d = make_pet(tmp_path, "kitty")
        (d / "pet.json").write_text("{broken")
        assert pet_daemon._is_valid_pet_dir(d) is False

    def test_missing_spritesheet(self, tmp_path):
        d = make_pet(tmp_path, "kitty", sheet_name=None)
        assert pet_daemon._is_valid_pet_dir(d) is False


class TestDiscoverPet:
    @pytest.fixture(autouse=True)
    def clear_env(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PET_ID", raising=False)
        monkeypatch.delenv("CODEX_PET_ID", raising=False)

    def test_none_when_nothing_installed(self, paths):
        assert pet_daemon.discover_pet() is None

    def test_first_codex_pet_when_unconfigured(self, paths):
        make_pet(config.CODEX_PETS_DIR, "zebra")
        make_pet(config.CODEX_PETS_DIR, "alpha")
        found = pet_daemon.discover_pet()
        assert found is not None and found[0].name == "alpha"

    def test_codex_root_wins_over_plugin_root(self, paths):
        make_pet(config.PLUGIN_PETS_DIR, "aaa")
        make_pet(config.CODEX_PETS_DIR, "zzz")
        assert pet_daemon.discover_pet()[0].name == "zzz"

    def test_plugin_root_used_when_codex_empty(self, paths):
        make_pet(config.PLUGIN_PETS_DIR, "bundled")
        assert pet_daemon.discover_pet()[0].name == "bundled"

    def test_config_pet_id_wins_over_scan_order(self, paths):
        make_pet(config.CODEX_PETS_DIR, "alpha")
        make_pet(config.CODEX_PETS_DIR, "chosen")
        write_user_config(paths, {"pet_id": "chosen"})
        assert pet_daemon.discover_pet()[0].name == "chosen"

    def test_env_wins_over_config(self, paths, monkeypatch):
        make_pet(config.CODEX_PETS_DIR, "chosen")
        make_pet(config.CODEX_PETS_DIR, "from-env")
        write_user_config(paths, {"pet_id": "chosen"})
        monkeypatch.setenv("CLAUDE_PET_ID", "from-env")
        assert pet_daemon.discover_pet()[0].name == "from-env"

    def test_codex_pet_id_env_also_honored(self, paths, monkeypatch):
        make_pet(config.PLUGIN_PETS_DIR, "aaa")
        make_pet(config.PLUGIN_PETS_DIR, "picked")
        monkeypatch.setenv("CODEX_PET_ID", "picked")
        assert pet_daemon.discover_pet()[0].name == "picked"

    def test_invalid_configured_pet_falls_back_to_scan(self, paths):
        make_pet(config.CODEX_PETS_DIR, "alpha")
        write_user_config(paths, {"pet_id": "ghost"})
        assert pet_daemon.discover_pet()[0].name == "alpha"

    def test_invalid_dirs_are_skipped(self, paths):
        make_pet(config.CODEX_PETS_DIR, "aaa-broken", sheet_name=None)
        make_pet(config.CODEX_PETS_DIR, "bbb-good")
        assert pet_daemon.discover_pet()[0].name == "bbb-good"

    def test_metadata_is_returned(self, paths):
        make_pet(config.CODEX_PETS_DIR, "kitty", meta={"name": "Kitty", "spriteVersionNumber": 2})
        _dir, meta = pet_daemon.discover_pet()
        assert meta["spriteVersionNumber"] == 2

    def test_pets_shipped_in_repo_are_discoverable(self):
        """Any pet dir committed to `pets/` must satisfy the discovery
        contract, otherwise it can never be selected at runtime."""
        root = config.PLUGIN_ROOT / "pets"
        for d in (root.iterdir() if root.exists() else []):
            if d.is_dir():
                assert pet_daemon._is_valid_pet_dir(d), d


# ── AnimationController ───────────────────────────────────────────────────


class TestAnimationController:
    def test_defaults_to_idle(self):
        a = pet_daemon.AnimationController()
        assert a.name == "idle" and a.frame_index == 0 and a.is_oneshot is False

    def test_set_resets_frame_index(self):
        a = pet_daemon.AnimationController()
        a.advance()
        a.set("waving", True)
        assert (a.name, a.is_oneshot, a.frame_index) == ("waving", True, 0)

    def test_set_to_same_state_keeps_frame_index(self):
        a = pet_daemon.AnimationController()
        a.advance()
        idx = a.frame_index
        a.set("idle", False)
        assert a.frame_index == idx

    def test_set_same_name_different_oneshot_resets(self):
        a = pet_daemon.AnimationController()
        a.set("jumping", False)
        a.advance()
        a.set("jumping", True)
        assert a.frame_index == 0

    def test_current_row_matches_table(self):
        a = pet_daemon.AnimationController()
        a.set("waving", True)
        assert a.current_row() == config.ANIMATIONS["waving"]["row"]

    def test_unknown_animation_falls_back_to_idle_spec(self):
        a = pet_daemon.AnimationController()
        a.set("does-not-exist", False)
        assert a.current_row() == config.ANIMATIONS["idle"]["row"]

    def test_current_duration_tracks_frame(self):
        a = pet_daemon.AnimationController()
        durs = config.ANIMATIONS["idle"]["durations"]
        for i, expected in enumerate(durs):
            assert a.current_duration_ms() == expected
            a.advance()
            assert a.frame_index == (i + 1) % len(durs)

    def test_duration_is_clamped_for_out_of_range_index(self):
        a = pet_daemon.AnimationController()
        a.frame_index = 999
        assert a.current_duration_ms() == config.ANIMATIONS["idle"]["durations"][-1]

    def test_advance_reports_cycle_completion(self):
        a = pet_daemon.AnimationController()
        n = len(config.ANIMATIONS["idle"]["durations"])
        results = [a.advance() for _ in range(n)]
        assert results[:-1] == [False] * (n - 1)
        assert results[-1] is True
        assert a.frame_index == 0

    def test_advance_loops_indefinitely(self):
        a = pet_daemon.AnimationController()
        n = len(config.ANIMATIONS["idle"]["durations"])
        completions = sum(a.advance() for _ in range(n * 5))
        assert completions == 5


# ── state machine ─────────────────────────────────────────────────────────


class FakeWindow:
    """Stand-in exposing exactly what the state-machine methods touch."""

    _active_animation = pet_daemon.PetWindow._active_animation
    _sync_anim = pet_daemon.PetWindow._sync_anim
    _close_intervals = pet_daemon.PetWindow._close_intervals
    apply_event = pet_daemon.PetWindow.apply_event
    _track_session = pet_daemon.PetWindow._track_session
    _should_quit_now = pet_daemon.PetWindow._should_quit_now
    _reap_dead_sessions = pet_daemon.PetWindow._reap_dead_sessions
    _maybe_revert_pet = pet_daemon.PetWindow._maybe_revert_pet
    _NO_SESSION_QUIT_DELAY_MS = pet_daemon.PetWindow._NO_SESSION_QUIT_DELAY_MS

    def __init__(self):
        self.base_stack = [(None, "idle")]
        self.oneshot_state = ""
        self.drag_state = ""
        self.anim = pet_daemon.AnimationController()
        self.active_sessions = {}
        self.quit_scheduled = 0
        self.switched_to = []

    # stubs for the Qt-touching collaborators
    def _restart_timer(self):
        pass

    def _maybe_schedule_quit(self):
        if self._should_quit_now():
            self.quit_scheduled += 1

    def _switch_pet_for_agent(self, agent_type):
        self.switched_to.append(agent_type)

    @property
    def stack_names(self):
        return [name for _opener, name in self.base_stack]


@pytest.fixture()
def win(paths):
    return FakeWindow()


class TestIntervalStack:
    def test_starts_idle(self, win):
        assert win._active_animation() == ("idle", False)

    def test_prompt_opens_running(self, win):
        win.apply_event("UserPromptSubmit", "s1")
        assert win._active_animation() == ("running", False)

    def test_tool_nests_inside_prompt(self, win):
        win.apply_event("UserPromptSubmit", "s1")
        win.apply_event("PreToolUse", "s1")
        assert win._active_animation() == ("waiting", False)
        win.apply_event("PostToolUse", "s1")
        assert win._active_animation() == ("running", False), "should fall back to outer loop"

    def test_repeated_tool_opens_stack_up(self, win):
        win.apply_event("UserPromptSubmit", "s1")
        win.apply_event("PreToolUse", "s1")
        win.apply_event("PreToolUse", "s1")
        assert win.stack_names == ["idle", "running", "waiting", "waiting"]

    def test_close_drains_every_matching_entry_at_the_top(self, win):
        """`_close_intervals` pops *while* the top opener matches, so a run of
        identical openers collapses in one close."""
        win.apply_event("UserPromptSubmit", "s1")
        win.apply_event("PreToolUse", "s1")
        win.apply_event("PreToolUse", "s1")
        win.apply_event("PostToolUse", "s1")
        assert win.stack_names == ["idle", "running"]

    def test_stop_tears_everything_down(self, win):
        for ev in ("UserPromptSubmit", "PreToolUse", "PreToolUse"):
            win.apply_event(ev, "s1")
        win.apply_event("Stop", "s1")
        assert win.stack_names == ["idle"]
        assert win.oneshot_state == "jumping"

    def test_permission_request_loops_until_tool_runs(self, win):
        win.apply_event("UserPromptSubmit", "s1")
        win.apply_event("PermissionRequest", "s1")
        assert win._active_animation() == ("waving", False)
        win.apply_event("PreToolUse", "s1")
        assert win.stack_names == ["idle", "running", "waiting"]

    def test_permission_denied_closes_and_flashes_failed(self, win):
        win.apply_event("PermissionRequest", "s1")
        win.apply_event("PermissionDenied", "s1")
        assert win.stack_names == ["idle"]
        assert win.oneshot_state == "failed"

    def test_elicitation_pair(self, win):
        win.apply_event("Elicitation", "s1")
        assert win._active_animation() == ("waving", False)
        win.apply_event("ElicitationResult", "s1")
        assert win.stack_names == ["idle"]

    def test_compaction_pair(self, win):
        win.apply_event("PreCompact", "s1")
        assert win._active_animation() == ("waiting", False)
        win.apply_event("PostCompact", "s1")
        assert win.stack_names == ["idle"]

    def test_close_stops_at_first_non_match(self, win):
        """PostToolUse must not pop past the enclosing UserPromptSubmit."""
        win.apply_event("UserPromptSubmit", "s1")
        win.apply_event("PreToolUse", "s1")
        win.apply_event("PostToolUse", "s1")
        win.apply_event("PostToolUse", "s1")  # spurious extra close
        assert win.stack_names == ["idle", "running"]

    def test_sentinel_is_never_popped(self, win):
        for _ in range(5):
            win.apply_event("Stop", "s1")
        assert win.base_stack == [(None, "idle")]

    def test_unknown_event_is_ignored(self, win):
        win.apply_event("UserPromptSubmit", "s1")
        before = list(win.base_stack)
        win.apply_event("SomeFutureHook", "s1")
        assert win.base_stack == before and win.oneshot_state == ""

    def test_post_tool_use_failure_closes_and_flashes(self, win):
        win.apply_event("PreToolUse", "s1")
        win.apply_event("PostToolUseFailure", "s1")
        assert win.stack_names == ["idle"]
        assert win.oneshot_state == "failed"


class TestAnimationPriority:
    def test_drag_beats_oneshot_and_base(self, win):
        win.apply_event("UserPromptSubmit", "s1")
        win.oneshot_state = "waving"
        win.drag_state = "running-right"
        assert win._active_animation() == ("running-right", False)

    def test_oneshot_beats_base(self, win):
        win.apply_event("UserPromptSubmit", "s1")
        win.oneshot_state = "jumping"
        assert win._active_animation() == ("jumping", True)

    def test_base_used_when_nothing_overlays(self, win):
        win.apply_event("UserPromptSubmit", "s1")
        assert win._active_animation() == ("running", False)

    def test_sync_anim_updates_controller(self, win):
        win.apply_event("UserPromptSubmit", "s1")
        assert win.anim.name == "running" and win.anim.is_oneshot is False
        win.apply_event("Stop", "s1")
        assert win.anim.name == "jumping" and win.anim.is_oneshot is True


class TestSessionTracking:
    def test_session_start_registers(self, win):
        win.apply_event("SessionStart", "s1", 100, "claude")
        assert win.active_sessions == {"s1": (100, "claude")}
        assert win.switched_to == ["claude"]

    def test_session_end_drains(self, win):
        win.apply_event("SessionStart", "s1", 100, "claude")
        win.apply_event("SessionEnd", "s1", 100, "claude")
        assert win.active_sessions == {}
        assert win.quit_scheduled == 1

    def test_mid_stream_session_is_learned(self, win):
        win.apply_event("UserPromptSubmit", "s1", 100, "codex")
        assert win.active_sessions == {"s1": (100, "codex")}
        assert win.switched_to == [], "only SessionStart switches pets"

    def test_pid_is_late_bound(self, win):
        win.apply_event("SessionStart", "s1", 0, "codex")
        win.apply_event("UserPromptSubmit", "s1", 555, "codex")
        assert win.active_sessions["s1"] == (555, "codex")

    def test_known_pid_is_not_overwritten_by_zero(self, win):
        win.apply_event("SessionStart", "s1", 555, "codex")
        win.apply_event("UserPromptSubmit", "s1", 0, "codex")
        assert win.active_sessions["s1"] == (555, "codex")

    def test_events_without_session_id_are_ignored_for_tracking(self, win):
        win.apply_event("Stop", "", 100, "claude")
        assert win.active_sessions == {}

    def test_multiple_sessions_tracked_independently(self, win):
        win.apply_event("SessionStart", "s1", 1, "claude")
        win.apply_event("SessionStart", "s2", 2, "codex")
        win.apply_event("SessionEnd", "s1", 1, "claude")
        assert list(win.active_sessions) == ["s2"]
        assert win.quit_scheduled == 0, "another session is still alive"

    def test_reverts_pet_when_one_tool_remains(self, win):
        win.apply_event("SessionStart", "s1", 1, "claude")
        win.apply_event("SessionStart", "s2", 2, "codex")
        win.switched_to.clear()
        win.apply_event("SessionEnd", "s1", 1, "claude")
        assert win.switched_to == ["codex"]

    def test_no_revert_when_two_tools_remain(self, win):
        win.apply_event("SessionStart", "s1", 1, "claude")
        win.apply_event("SessionStart", "s2", 2, "codex")
        win.apply_event("SessionStart", "s3", 3, "claude")
        win.switched_to.clear()
        win.apply_event("SessionEnd", "s3", 3, "claude")
        assert win.switched_to == []

    def test_unknown_session_end_is_harmless(self, win):
        win.apply_event("SessionEnd", "never-seen", 0, "claude")
        assert win.active_sessions == {}


class TestQuitPolicy:
    def test_quit_when_no_sessions(self, win):
        assert win._should_quit_now() is True

    def test_no_quit_with_active_session(self, win):
        win.apply_event("SessionStart", "s1", 1, "claude")
        assert win._should_quit_now() is False

    def test_stay_even_no_session_blocks_quit(self, win, paths):
        write_user_config(paths, {"stay_even_no_session": True})
        assert win._should_quit_now() is False

    def test_session_end_does_not_schedule_quit_when_staying(self, win, paths):
        write_user_config(paths, {"stay_even_no_session": True})
        win.apply_event("SessionStart", "s1", 1, "claude")
        win.apply_event("SessionEnd", "s1", 1, "claude")
        assert win.quit_scheduled == 0


class TestReapDeadSessions:
    def test_dead_pid_is_reaped(self, win, monkeypatch):
        win.active_sessions = {"s1": (12345, "claude")}
        monkeypatch.setattr(pet_daemon.os, "kill", _raiser(ProcessLookupError))
        win._reap_dead_sessions()
        assert win.active_sessions == {}
        assert win.quit_scheduled == 1

    def test_live_pid_is_kept(self, win, monkeypatch):
        win.active_sessions = {"s1": (12345, "claude")}
        monkeypatch.setattr(pet_daemon.os, "kill", lambda *a: None)
        win._reap_dead_sessions()
        assert list(win.active_sessions) == ["s1"]

    def test_permission_error_treated_as_alive(self, win, monkeypatch):
        win.active_sessions = {"s1": (12345, "claude")}
        monkeypatch.setattr(pet_daemon.os, "kill", _raiser(PermissionError))
        win._reap_dead_sessions()
        assert list(win.active_sessions) == ["s1"]

    def test_sessions_without_pid_are_never_reaped(self, win, monkeypatch):
        win.active_sessions = {"s1": (0, "codex")}
        monkeypatch.setattr(pet_daemon.os, "kill", _raiser(ProcessLookupError))
        win._reap_dead_sessions()
        assert list(win.active_sessions) == ["s1"]

    def test_only_dead_sessions_are_dropped(self, win, monkeypatch):
        win.active_sessions = {"alive": (1, "claude"), "dead": (2, "codex")}

        def kill(pid, _sig):
            if pid == 2:
                raise ProcessLookupError

        monkeypatch.setattr(pet_daemon.os, "kill", kill)
        win._reap_dead_sessions()
        assert list(win.active_sessions) == ["alive"]

    def test_no_sessions_is_a_noop(self, win, monkeypatch):
        monkeypatch.setattr(pet_daemon.os, "kill", _raiser(AssertionError))
        win._reap_dead_sessions()
        assert win.quit_scheduled == 0


def _raiser(exc):
    def _f(*_args, **_kwargs):
        raise exc()

    return _f


# ── plugin manifests ──────────────────────────────────────────────────────


class TestPluginManifests:
    @pytest.mark.parametrize("rel", [".claude-plugin/plugin.json", ".codex-plugin/plugin.json"])
    def test_manifest_is_valid_json_with_a_version(self, rel):
        data = json.loads((config.PLUGIN_ROOT / rel).read_text("utf-8"))
        assert isinstance(data.get("version"), str) and data["version"].strip()

    def test_manifest_versions_agree(self):
        versions = {
            json.loads((config.PLUGIN_ROOT / rel).read_text("utf-8"))["version"]
            for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
        }
        assert len(versions) == 1, f"plugin manifests disagree: {versions}"

    def test_plugin_version_matches_manifest(self):
        data = json.loads((config.PLUGIN_ROOT / ".codex-plugin/plugin.json").read_text("utf-8"))
        assert config.PLUGIN_VERSION == data["version"]
