"""Tests for `scripts/config.py`: atlas geometry, animation table invariants,
the event→animation tables, and animation-duration overrides."""
from __future__ import annotations

import importlib
import json
import math

import pytest

import config
from tests.conftest import write_user_config


# ── atlas geometry ────────────────────────────────────────────────────────


class TestAtlasGeometry:
    def test_v1_dimensions(self):
        assert config.ATLAS_W == 1536
        assert config.atlas_height(config.SPRITE_V1) == 1872

    def test_v2_dimensions(self):
        assert config.atlas_height(config.SPRITE_V2) == 2288

    def test_unknown_version_falls_back_to_v1(self):
        assert config.atlas_height(99) == config.atlas_height(config.SPRITE_V1)

    @pytest.mark.parametrize(
        "height,expected",
        [(1872, config.SPRITE_V1), (2288, config.SPRITE_V2), (0, None), (2000, None)],
    )
    def test_sprite_version_for_height(self, height, expected):
        assert config.sprite_version_for_height(height) == expected

    def test_height_roundtrip(self):
        for version in config.ATLAS_ROWS_BY_VERSION:
            assert config.sprite_version_for_height(config.atlas_height(version)) == version

    def test_cell_grid_divides_atlas(self):
        assert config.ATLAS_W % config.CELL_W == 0
        assert config.ATLAS_W // config.CELL_W == config.ATLAS_COLS
        for version, rows in config.ATLAS_ROWS_BY_VERSION.items():
            assert config.atlas_height(version) % config.CELL_H == 0


# ── v2 look cells ─────────────────────────────────────────────────────────


class TestLookCells:
    @pytest.mark.parametrize(
        "index,cell",
        [(0, (9, 0)), (4, (9, 4)), (7, (9, 7)), (8, (10, 0)), (12, (10, 4)), (15, (10, 7))],
    )
    def test_known_directions(self, index, cell):
        assert config.look_cell(index) == cell

    def test_wraps_around(self):
        assert config.look_cell(16) == config.look_cell(0)
        assert config.look_cell(17) == config.look_cell(1)

    def test_negative_index_wraps(self):
        assert config.look_cell(-1) == config.look_cell(15)

    def test_all_cells_are_distinct_and_in_bounds(self):
        cells = {config.look_cell(i) for i in range(config.LOOK_DIRECTIONS)}
        assert len(cells) == config.LOOK_DIRECTIONS
        v2_rows = config.ATLAS_ROWS_BY_VERSION[config.SPRITE_V2]
        for row, col in cells:
            assert config.LOOK_FIRST_ROW <= row < v2_rows
            assert 0 <= col < config.ATLAS_COLS

    def test_look_cells_only_exist_on_v2(self):
        v1_rows = config.ATLAS_ROWS_BY_VERSION[config.SPRITE_V1]
        assert config.LOOK_FIRST_ROW >= v1_rows

    def test_degrees_per_step(self):
        assert math.isclose(config.LOOK_DEGREES_PER_STEP, 22.5)

    def test_neutral_cell_in_bounds(self):
        row, col = config.LOOK_NEUTRAL_CELL
        assert 0 <= col < config.ATLAS_COLS
        assert 0 <= row < config.ATLAS_ROWS_BY_VERSION[config.SPRITE_V1]


# ── animation table invariants ────────────────────────────────────────────


class TestAnimationTable:
    def test_every_animation_is_classified(self):
        classified = config.LOOPING_STATES | config.ONESHOT_STATES
        assert set(config.ANIMATIONS) == classified

    def test_looping_and_oneshot_are_disjoint(self):
        assert not (config.LOOPING_STATES & config.ONESHOT_STATES)

    def test_idle_is_present(self):
        """`AnimationController._spec` falls back to `idle` for unknown names."""
        assert "idle" in config.ANIMATIONS

    @pytest.mark.parametrize("name", sorted(config.ANIMATIONS))
    def test_animation_spec_is_well_formed(self, name):
        spec = config.ANIMATIONS[name]
        assert 0 <= spec["row"] < config.ATLAS_ROWS_BY_VERSION[config.SPRITE_V1]
        durs = spec["durations"]
        assert 0 < len(durs) <= config.ATLAS_COLS
        assert all(isinstance(d, int) and d > 0 for d in durs)

    def test_rows_are_unique(self):
        rows = [spec["row"] for spec in config.ANIMATIONS.values()]
        assert len(rows) == len(set(rows))

    def test_animation_rows_do_not_collide_with_look_rows(self):
        for spec in config.ANIMATIONS.values():
            assert spec["row"] < config.LOOK_FIRST_ROW


# ── event tables ──────────────────────────────────────────────────────────


class TestEventTables:
    def test_open_animations_are_looping(self):
        for event, anim in config.INTERVAL_OPEN.items():
            assert anim in config.ANIMATIONS, event
            assert anim in config.LOOPING_STATES or anim in config.ONESHOT_STATES

    def test_oneshot_animations_exist(self):
        for event, anim in config.ONESHOTS.items():
            assert anim in config.ANIMATIONS, event

    def test_close_sets_reference_real_openers(self):
        """Everything an event claims to close must actually open an interval,
        otherwise the entry is dead weight that can never match a stack entry."""
        for event, closes in config.INTERVAL_CLOSE.items():
            for opener in closes:
                assert opener in config.INTERVAL_OPEN, f"{event} closes non-opener {opener}"

    def test_every_opener_is_closed_by_something(self):
        closable = set().union(*config.INTERVAL_CLOSE.values())
        assert set(config.INTERVAL_OPEN) <= closable

    def test_terminal_events_close_all_openers(self):
        for event in ("Stop", "StopFailure", "SessionEnd"):
            assert config.INTERVAL_CLOSE[event] == set(config.INTERVAL_OPEN), event

    def test_no_event_closes_itself(self):
        for event, closes in config.INTERVAL_CLOSE.items():
            assert event not in closes, event

    def test_hooks_json_covers_every_mapped_event(self):
        hooks = json.loads((config.PLUGIN_ROOT / "hooks/hooks.json").read_text("utf-8"))
        registered = set(hooks["hooks"])
        mapped = set(config.INTERVAL_OPEN) | set(config.INTERVAL_CLOSE) | set(config.ONESHOTS)
        assert mapped <= registered, mapped - registered

    def test_codex_hooks_are_a_subset_of_claude_hooks(self):
        root = config.PLUGIN_ROOT
        claude = json.loads((root / "hooks/hooks.json").read_text("utf-8"))
        codex = json.loads((root / "hooks/codex-hooks.json").read_text("utf-8"))
        assert set(codex["hooks"]) <= set(claude["hooks"])

    def test_codex_hooks_file_has_only_hooks_key(self):
        """Codex's schema is stricter than Claude's — no top-level extras."""
        codex = json.loads((config.PLUGIN_ROOT / "hooks/codex-hooks.json").read_text("utf-8"))
        assert list(codex) == ["hooks"]


# ── user config ───────────────────────────────────────────────────────────


class TestLoadUserConfig:
    def test_missing_file_returns_defaults(self, paths):
        assert config.load_user_config() == config.DEFAULT_USER_CONFIG

    def test_corrupt_json_falls_back_to_defaults(self, paths):
        config.CONFIG_PATH.write_text("{not json")
        assert config.load_user_config() == config.DEFAULT_USER_CONFIG

    def test_non_dict_json_falls_back_to_defaults(self, paths):
        config.CONFIG_PATH.write_text("[1, 2, 3]")
        assert config.load_user_config() == config.DEFAULT_USER_CONFIG

    def test_partial_config_is_merged_with_defaults(self, paths):
        write_user_config(paths, {"pet_id": "kitty"})
        cfg = config.load_user_config()
        assert cfg["pet_id"] == "kitty"
        assert cfg["look_radius"] == config.DEFAULT_USER_CONFIG["look_radius"]

    def test_unknown_keys_are_preserved(self, paths):
        write_user_config(paths, {"future_option": 42})
        assert config.load_user_config()["future_option"] == 42

    def test_defaults_are_not_mutated_by_callers(self, paths):
        cfg = config.load_user_config()
        cfg["pet_id"] = "mutated"
        assert config.DEFAULT_USER_CONFIG["pet_id"] == ""

    def test_default_keys_documented_in_readme(self):
        readme = (config.PLUGIN_ROOT / "README.md").read_text("utf-8")
        for key in config.DEFAULT_USER_CONFIG:
            assert key in readme, f"{key} is undocumented in README.md"


# ── animation duration overrides ──────────────────────────────────────────


def _reload_config_with(tmp_root, overrides):
    """Re-import config so `_apply_animation_duration_overrides` runs against
    a config file we control, then restore the pristine module."""
    cfg_dir = tmp_root / "cfgroot" / "pet4agents"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps({"animation_durations": overrides}))
    return cfg_dir


class TestAnimationDurationOverrides:
    @pytest.fixture()
    def reload_config(self, tmp_path, monkeypatch):
        def _reload(overrides):
            _reload_config_with(tmp_path, overrides)
            monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfgroot"))
            return importlib.reload(config)

        yield _reload
        # Always restore the module other tests (and conftest) hold a ref to.
        monkeypatch.undo()
        importlib.reload(config)

    def test_valid_override_applied(self, reload_config):
        mod = reload_config({"idle": [10, 20, 30, 40, 50, 60]})
        assert mod.ANIMATIONS["idle"]["durations"] == [10, 20, 30, 40, 50, 60]

    def test_floats_are_coerced_to_int(self, reload_config):
        mod = reload_config({"idle": [10.7, 20.0, 30, 40, 50, 60]})
        assert mod.ANIMATIONS["idle"]["durations"] == [10, 20, 30, 40, 50, 60]

    def test_wrong_length_ignored(self, reload_config):
        mod = reload_config({"idle": [10, 20]})
        assert mod.ANIMATIONS["idle"]["durations"] == [280, 110, 110, 140, 140, 320]

    def test_unknown_animation_ignored(self, reload_config):
        mod = reload_config({"nope": [1, 2, 3]})
        assert "nope" not in mod.ANIMATIONS

    @pytest.mark.parametrize(
        "bad", [[0, 1, 1, 1, 1, 1], [-5, 1, 1, 1, 1, 1], ["a", 1, 1, 1, 1, 1],
                [True, 1, 1, 1, 1, 1], [None, 1, 1, 1, 1, 1]]
    )
    def test_invalid_entries_reject_whole_list(self, reload_config, bad):
        mod = reload_config({"idle": bad})
        assert mod.ANIMATIONS["idle"]["durations"] == [280, 110, 110, 140, 140, 320]

    def test_non_dict_override_ignored(self, reload_config):
        mod = reload_config(["idle"])
        assert mod.ANIMATIONS["idle"]["durations"] == [280, 110, 110, 140, 140, 320]

    def test_non_list_value_ignored(self, reload_config):
        mod = reload_config({"idle": 100})
        assert mod.ANIMATIONS["idle"]["durations"] == [280, 110, 110, 140, 140, 320]

    def test_one_animation_override_leaves_others_alone(self, reload_config):
        mod = reload_config({"waving": [1, 2, 3, 4]})
        assert mod.ANIMATIONS["waving"]["durations"] == [1, 2, 3, 4]
        assert mod.ANIMATIONS["idle"]["durations"] == [280, 110, 110, 140, 140, 320]
