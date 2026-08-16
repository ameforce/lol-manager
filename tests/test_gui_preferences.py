from __future__ import annotations

import json
from pathlib import Path

from lolmanager.gui import app_gui
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


def test_start_keeps_continuation_checkbox_enabled_and_passes_live_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _Value:
        def __init__(self, value: object) -> None:
            self.value = value

        def get(self) -> object:
            return self.value

        def set(self, value: object) -> None:
            self.value = value

    class _Widget:
        def __init__(self) -> None:
            self.configurations: list[dict[str, object]] = []

        def configure(self, **kwargs: object) -> None:
            self.configurations.append(kwargs)

    class _Process:
        def poll(self) -> None:
            return None

    class _Thread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

    commands: list[list[str]] = []
    monkeypatch.setattr(app_gui, "is_frozen", lambda: False)
    monkeypatch.setattr(
        app_gui.subprocess,
        "Popen",
        lambda cmd, **_kwargs: commands.append(list(cmd)) or _Process(),
    )
    monkeypatch.setattr(app_gui.threading, "Thread", _Thread)

    gui = LolManagerGui.__new__(LolManagerGui)
    gui.proc = None
    gui.work_dir = tmp_path
    gui.gui_preferences_path = tmp_path / "gui_preferences.json"
    gui.continue_after_game_var = _Value(True)
    gui.running_var = _Value("")
    gui.btn_start = _Widget()
    gui.btn_stop = _Widget()
    gui.chk_continue = _Widget()
    gui._check_config_ready = lambda: True
    gui._reset_match_timer_ui = lambda: None
    gui._reader_loop = lambda _proc: None
    gui._append_log = lambda _line: None
    gui._unexpected_cli_restart_count = 2
    gui._stop_requested_at = 1.0
    gui._restart_after_exit = True
    gui._auto_start_pending = True

    gui.start()

    assert gui.running_var.get() == "Running"
    assert gui.chk_continue.configurations == []
    assert commands == [
        [
            app_gui.sys.executable,
            "-m",
            "lolmanager",
            "--cli",
            "--continue-after-game-preference-path",
            str(gui.gui_preferences_path),
            "--continue-after-game",
        ]
    ]

    gui.proc = None
    commands.clear()
    save_continue_after_game_preference(gui.gui_preferences_path, False)
    monkeypatch.setattr(
        app_gui,
        "save_continue_after_game_preference",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    gui.continue_after_game_var.set(False)
    gui._on_continue_after_game_changed()
    assert gui.continue_after_game_var.get() is True

    gui.start()

    assert commands == [
        [
            app_gui.sys.executable,
            "-m",
            "lolmanager",
            "--cli",
            "--continue-after-game",
        ]
    ]
