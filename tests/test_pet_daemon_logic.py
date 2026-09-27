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

    def test_oneshot_reports_completion_after_its_cycles(self):
        a = pet_daemon.AnimationController()
        a.set("waving", True, repeats=1)
        n = len(config.ANIMATIONS["waving"]["durations"])
        results = [a.advance() for _ in range(n)]
        assert results == [False] * (n - 1) + [True]
        assert a.frame_index == 0

    def test_unlimited_animation_loops_indefinitely(self):
        a = pet_daemon.AnimationController()
        n = len(config.ANIMATIONS["idle"]["durations"])
        assert not any(a.advance() for _ in range(n * 5))
        assert a.shown == "idle" and not a.settled

    def test_interval_settles_into_idle_after_repeats(self):
        a = pet_daemon.AnimationController()
        a.set("running", False, repeats=3)
        n = len(config.ANIMATIONS["running"]["durations"])
        for _ in range(n * 3 - 1):
            assert a.advance() is False
            assert a.shown == "running"
        assert a.advance() is False, "an interval never reports completion"
        assert (a.name, a.shown, a.frame_index) == ("running", "idle", 0)
        assert a.current_row() == config.ANIMATIONS["idle"]["row"]
        idle_n = len(config.ANIMATIONS["idle"]["durations"])
        assert not any(a.advance() for _ in range(idle_n * 3))
        assert a.shown == "idle"

    def test_restart_replays_a_settled_animation(self):
        a = pet_daemon.AnimationController()
        a.set("running", False, repeats=1)
        for _ in config.ANIMATIONS["running"]["durations"]:
            a.advance()
        assert a.settled
        a.set("running", False, repeats=1)
        assert a.settled, "same state without restart keeps going"
        a.set("running", False, repeats=1, restart=True)
        assert (a.shown, a.frame_index) == ("running", 0)


# ── state machine ─────────────────────────────────────────────────────────


class FakeWindow:
    """Stand-in exposing exactly what the state-machine methods touch."""

    _active_animation = pet_daemon.PetWindow._active_animation
    _sync_anim = pet_daemon.PetWindow._sync_anim
    apply_event = pet_daemon.PetWindow.apply_event
    _advance_frame = pet_daemon.PetWindow._advance_frame
    _track_session = pet_daemon.PetWindow._track_session
    _should_quit_now = pet_daemon.PetWindow._should_quit_now
    _reap_dead_sessions = pet_daemon.PetWindow._reap_dead_sessions
    _maybe_revert_pet = pet_daemon.PetWindow._maybe_revert_pet
    _NO_SESSION_QUIT_DELAY_MS = pet_daemon.PetWindow._NO_SESSION_QUIT_DELAY_MS

    def __init__(self):
        self.state = "idle"
        self.state_settled = False
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

    def play(self, events, session="s1"):
        for ev in events:
            self.apply_event(ev, session)


@pytest.fixture()
def win(paths):
    return FakeWindow()


class TestStates:
    """Sequences below are the hook orders recorded from a live Claude Code
    2.1.283 session."""

    def test_starts_idle(self, win):
        assert win._active_animation() == ("idle", False)

    def test_plain_turn(self, win):
        win.play(["UserPromptSubmit"])
        assert win.state == "running"
        win.play(["PostToolUse"])
        assert win.state == "running", "tool calls don't change the state"
        win.play(["Stop"])
        assert win.state == "review" and win.oneshot_state == ""

    def test_approved_permission_resumes_running(self, win):
        win.play(["UserPromptSubmit", "PermissionRequest"])
        assert win.state == "waiting"
        win.play(["PostToolUse"])
        assert win.state == "running"
        win.play(["Stop"])
        assert win.state == "review"

    def test_denied_permission_waits_for_the_next_prompt(self, win):
        """A UI deny fires no hook; the next prompt replaces the state."""
        win.play(["UserPromptSubmit", "PermissionRequest"])
        assert win.state == "waiting"
        win.play(["UserPromptSubmit"])
        assert win.state == "running"

    def test_auto_mode_denial_resumes_and_flashes(self, win):
        win.play(["UserPromptSubmit", "PermissionRequest", "PermissionDenied"])
        assert win.state == "running" and win.oneshot_state == "failed"

    def test_background_subagent_tools_leave_review_alone(self, win):
        win.play(["UserPromptSubmit", "SubagentStart", "PostToolUse", "Stop",
                  "PostToolUse", "SubagentStop"])
        assert win.state == "review"

    def test_stop_failure(self, win):
        win.play(["UserPromptSubmit", "StopFailure"])
        assert win.state == "failed"

    def test_codex_interrupt_returns_to_idle(self, win):
        win.play(["UserPromptSubmit", "Interrupt"])
        assert win.state == "idle"

    def test_elicitation_pair(self, win):
        win.play(["UserPromptSubmit", "Elicitation"])
        assert win.state == "waiting"
        win.play(["ElicitationResult"])
        assert win.state == "running"

    def test_manual_compaction(self, win):
        win.play(["PreCompact"])
        assert win.state == "running"

    def test_idle_notification_needs_input(self, win):
        win.play(["UserPromptSubmit", "Stop", "Notification"])
        assert win.state == "waiting"

    def test_session_end_returns_to_idle(self, win):
        win.play(["UserPromptSubmit", "SessionEnd"])
        assert win.state == "idle" and win.oneshot_state == "waving"

    def test_unknown_event_is_ignored(self, win):
        win.play(["UserPromptSubmit"])
        win.anim.advance()
        win.play(["SomeFutureHook"])
        assert win.state == "running" and win.oneshot_state == ""
        assert win.anim.frame_index == 1

    def test_resolving_event_outside_waiting_changes_nothing(self, win):
        win.play(["UserPromptSubmit"])
        win.anim.advance()
        win.play(["PostToolUse"])
        assert win.anim.frame_index == 1

    def test_post_tool_use_failure_flashes_inside_the_turn(self, win):
        win.play(["UserPromptSubmit", "PostToolUseFailure"])
        assert win.state == "running" and win.oneshot_state == "failed"


class TestSettling:
    @staticmethod
    def _play_cycles(win, name, cycles):
        for _ in range(len(config.ANIMATIONS[name]["durations"]) * cycles):
            win._advance_frame()

    def test_state_settles_after_configured_repeats(self, win):
        win.play(["UserPromptSubmit"])
        assert win.anim.repeats == config.STATE_REPEATS
        self._play_cycles(win, "running", config.STATE_REPEATS)
        assert win.anim.shown == "idle" and win.state_settled
        assert win._active_animation() == ("running", False), "the state is kept"

    def test_same_state_again_replays(self, win):
        win.play(["UserPromptSubmit"])
        self._play_cycles(win, "running", config.STATE_REPEATS)
        win.play(["UserPromptSubmit"])
        assert (win.anim.shown, win.anim.frame_index) == ("running", 0)
        assert not win.state_settled

    def test_settled_state_stays_settled_after_a_oneshot(self, win):
        win.play(["UserPromptSubmit"])
        self._play_cycles(win, "running", config.STATE_REPEATS)
        win.play(["SubagentStart"])
        assert win.anim.shown == "review"
        self._play_cycles(win, "review", 1)
        assert win.oneshot_state is None
        assert (win.anim.name, win.anim.shown) == ("running", "idle")

    def test_unsettled_state_resumes_its_cycles_after_a_oneshot(self, win):
        win.play(["UserPromptSubmit", "SubagentStart"])
        self._play_cycles(win, "review", 1)
        assert win.anim.shown == "running"

    def test_oneshots_play_once(self, win):
        win.play(["SessionStart"])
        assert win.anim.repeats == 1

    def test_idle_never_settles(self, win):
        win.play(["UserPromptSubmit", "Interrupt"])
        assert win.anim.repeats is None

    def test_drag_loops_while_held(self, win):
        win.drag_state = "jumping"
        win._sync_anim()
        assert win.anim.repeats is None

    def test_event_during_drag_keeps_drag_frames(self, win):
        win.drag_state = "running-right"
        win._sync_anim()
        win.anim.advance()
        win.play(["UserPromptSubmit"])
        assert (win.anim.name, win.anim.frame_index) == ("running-right", 1)
        assert win.state == "running"


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
        win.apply_event("SubagentStop", "s1")
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
