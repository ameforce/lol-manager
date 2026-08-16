from __future__ import annotations

import json

from lolmanager.core.gui_preferences import (
    load_continue_after_game_preference,
    save_continue_after_game_preference,
)
from lolmanager.gui.app_gui import LolManagerGui, should_schedule_initial_auto_start


def test_first_launch_waits_for_explicit_continuation_choice(tmp_path) -> None:
    preferences_path = tmp_path / "gui_preferences.json"

    saved = load_continue_after_game_preference(preferences_path)

    assert saved is None
    assert (
        should_schedule_initial_auto_start(
            auto_start_requested=True,
            saved_continue_after_game=saved,
        )
        is False
    )


def test_saved_continuation_choice_restores_value_and_auto_start(tmp_path) -> None:
    preferences_path = tmp_path / "gui_preferences.json"
    save_continue_after_game_preference(preferences_path, True)

    saved = load_continue_after_game_preference(preferences_path)

    assert saved is True
    assert (
        should_schedule_initial_auto_start(
            auto_start_requested=True,
            saved_continue_after_game=saved,
        )
        is True
    )


def test_explicit_one_game_choice_also_restores_auto_start(tmp_path) -> None:
    preferences_path = tmp_path / "gui_preferences.json"
    save_continue_after_game_preference(preferences_path, False)

    saved = load_continue_after_game_preference(preferences_path)

    assert saved is False
    assert (
        should_schedule_initial_auto_start(
            auto_start_requested=True,
            saved_continue_after_game=saved,
        )
        is True
    )


def test_preference_save_preserves_other_gui_keys_and_replaces_invalid_file(
    tmp_path,
) -> None:
    preferences_path = tmp_path / "gui_preferences.json"
    preferences_path.write_text(
        json.dumps({"window_mode": "compact"}),
        encoding="utf-8",
    )

    save_continue_after_game_preference(preferences_path, True)
    payload = json.loads(preferences_path.read_text(encoding="utf-8"))

    assert payload == {
        "window_mode": "compact",
        "continue_after_game": True,
    }
    assert list(tmp_path.glob("gui_preferences.json*.tmp")) == []

    preferences_path.write_text("not-json", encoding="utf-8")
    save_continue_after_game_preference(preferences_path, False)
    assert load_continue_after_game_preference(preferences_path) is False


def test_gui_checkbox_change_persists_the_value(tmp_path) -> None:
    preferences_path = tmp_path / "gui_preferences.json"
    gui = LolManagerGui.__new__(LolManagerGui)
    gui.gui_preferences_path = preferences_path
    gui.continue_after_game_var = type("_Value", (), {"get": lambda self: True})()
    gui._continue_after_game_preference_initialized = False
    gui._append_log = lambda _line: None

    gui._on_continue_after_game_changed()

    assert load_continue_after_game_preference(preferences_path) is True
    assert gui._continue_after_game_preference_initialized is True
