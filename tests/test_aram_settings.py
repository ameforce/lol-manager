from __future__ import annotations

from unittest import mock

import pytest

from lolmanager.core.aram import AramPreferences
from lolmanager.core.champion_config import ChampionConfig


@pytest.fixture
def tk_root(tk_interpreter):
    yield tk_interpreter
    for child in tk_interpreter.winfo_children():
        child.destroy()


def test_panel_adds_valid_champions_reorders_removes_and_reloads(tk_root):
    from lolmanager.gui.aram_settings import AramSettingsPanel

    changed = mock.Mock()
    panel = AramSettingsPanel(tk_root, preferences=AramPreferences(), on_change=changed)
    panel.set_champion_names(["아리", "럭스", "애쉬"])
    panel.enable_check.invoke()
    for name in ("아리", "럭스", "아 리", "없는 챔피언"):
        panel.champion.set(name)
        panel._add()
    assert panel.get_preferences() == AramPreferences(True, ("아리", "럭스"))
    assert changed.call_count == 3

    panel.champions.selection_set(1)
    panel._move(-1)
    assert panel.get_preferences().preferred_champions == ("럭스", "아리")
    panel._remove()
    assert panel.get_preferences().preferred_champions == ("아리",)
    changed.reset_mock()
    panel.set_preferences(AramPreferences(False, ("애쉬", "럭스")))
    assert panel.get_preferences() == AramPreferences(False, ("애쉬", "럭스"))
    changed.assert_not_called()


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.parametrize("names_first", [True, False])
def test_tier_candidates_save_plain_names_without_changing_preference_priority(tk_root, names_first):
    from lolmanager.gui.aram_settings import AramSettingsPanel

    changed = mock.Mock()
    preferences = AramPreferences(True, ("가렌", "럭스"))
    panel = AramSettingsPanel(tk_root, preferences=preferences, on_change=changed)
    entries = [("스웨인", "OP", "/swain"), ("럭스", "1티어", "/lux"), ("가렌", "3티어", "/garen")]
    actions = [lambda: panel.set_champion_names(["가렌", "럭스", "스웨인", "아리"]),
               lambda: panel.set_ranked_entries(entries, "opgg")]
    for action in actions if names_first else reversed(actions):
        action()
    assert panel.champion_combo.cget("values") == (
        "──────── OP ────────", "  1. 스웨인 (OP)",
        "──────── 기타 ────────", "아리",
    )
    assert panel.champions.get(0, "end") == ("가렌 (3티어)", "럭스 (1티어)")
    assert panel.get_preferences() == preferences
    changed.assert_not_called()
    for value in ("──────── OP ────────", "  2. 럭스 (1티어)", "  1. 스웨인 (OP)"):
        panel.champion.set(value)
        panel._add()
    assert panel.get_preferences().preferred_champions == ("가렌", "럭스", "스웨인")
    changed.assert_called_once()
    panel.set_ranked_entries(entries[::-1], "cache_stale")
    assert panel.get_preferences().preferred_champions == ("가렌", "럭스", "스웨인")
    assert "갱신 실패" in panel.tier_status.get()
    assert panel.champion_combo.cget("values") == ("아리",)


def test_tier_failure_keeps_alphabetical_candidates_and_existing_preferences(tk_root):
    from lolmanager.gui.aram_settings import AramSettingsPanel

    preferences = AramPreferences(True, ("럭스", "가렌"))
    panel = AramSettingsPanel(tk_root, preferences=preferences, on_change=mock.Mock())
    panel.set_champion_names(["아리", "가렌", "럭스"])
    panel.set_ranked_entries([], "unavailable")
    assert panel.champion_combo.cget("values") == ("아리",)
    assert "이름순" in panel.tier_status.get()
    assert panel.get_preferences() == preferences


def test_tier_sort_preserves_manually_swapped_slots_after_reload(tk_root):
    from lolmanager.gui.aram_settings import AramSettingsPanel

    panel = AramSettingsPanel(
        tk_root, preferences=AramPreferences(True, ("가렌", "럭스", "애쉬", "아리", "제드")),
        on_change=mock.Mock(),
    )
    panel.set_ranked_entries([
        ("아리", "OP", "/ahri"), ("제드", "1티어", "/zed"), ("가렌", "2티어", "/garen"),
        ("럭스", "3티어", "/lux"), ("애쉬", "5티어", "/ashe"),
    ], "opgg")
    panel.champions.selection_set(2)
    panel._move(-1)
    panel.set_preferences(AramPreferences.from_config(panel.get_preferences().as_config()))
    panel.sort_button.invoke()
    assert panel.get_preferences().preferred_champions == ("아리", "애쉬", "럭스", "제드", "가렌")
    assert panel.get_preferences().manually_ranked_champions == ("애쉬", "럭스")
    panel.champions.selection_set(1)
    panel._remove()
    assert "  1. 애쉬 (5티어)" in panel.champion_combo.cget("values")


def test_settings_window_persists_aram_edits_on_close_with_ranked_settings_intact(tk_root, tmp_path, monkeypatch):
    from lolmanager.gui import config_gui
    from lolmanager.gui.aram_settings import AramSettingsPanel

    path = tmp_path / "champions.json"
    config = ChampionConfig(path)
    config.set("mid", "아리", ban_champion="제드")
    monkeypatch.setattr("tkinter.Tk", lambda: tk_root)
    # Reuse one Tcl interpreter across the GUI tests; close still exits mainloop.
    monkeypatch.setattr(tk_root, "destroy", tk_root.quit)
    monkeypatch.setattr(config_gui.threading, "Thread", mock.Mock())
    monkeypatch.setattr(config_gui, "load_champion_list_cache", lambda _: None)
    errors = []

    def edit_and_close():
        try:
            panel = next(widget for widget in descendants(tk_root) if isinstance(widget, AramSettingsPanel))
            notebook = panel.master
            assert notebook.tab(panel, "text") == "칼바람"
            notebook.select(panel)
            panel.enable_check.invoke()
            panel.champion.set("럭스")
            panel._add()
            close = tk_root.protocol("WM_DELETE_WINDOW")
            assert close
            tk_root.tk.call(close)
        except Exception as exc:
            errors.append(exc)
            tk_root.destroy()

    tk_root.after(0, edit_and_close)
    config_gui.run_config_gui(path)
    assert not errors
    saved = ChampionConfig(path)
    assert AramPreferences.from_config(saved.get("aram")) == AramPreferences(True, ("럭스",))
    assert saved.get("mid") == {"champion": "아리", "ban": "제드"}
