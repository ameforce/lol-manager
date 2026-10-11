from __future__ import annotations

from unittest import mock

import pytest

from lolmanager.core.aram import AramPreferences
from lolmanager.core.champion_config import ChampionConfig


@pytest.fixture(scope="module")
def tk_interpreter():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


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
