from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from lolmanager.gui import app_gui


@pytest.mark.parametrize(("payload", "ready"), [
    ({"aram": {"enabled": True, "preferred_champions": ["아리"]}}, True),
    ({"aram": {"enabled": False, "preferred_champions": ["아리"]}}, False),
    ({"aram": {"enabled": True, "preferred_champions": []}}, False),
    ({"mid": {"champion": "아리"}}, True),
    ({"unrelated": {"champion": "아리"}}, False),
    ({"mid": {"champion": ""}}, False),
    ({}, False),
])
def test_gui_start_accepts_a_configured_supported_mode(tmp_path, monkeypatch, payload, ready):
    path = tmp_path / "champion_config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    gui = app_gui.LolManagerGui.__new__(app_gui.LolManagerGui)
    gui.config_path = path
    gui.open_config = Mock()
    monkeypatch.setattr(app_gui.messagebox, "showinfo", Mock())
    assert gui._check_config_ready() is ready
    assert gui.open_config.called is not ready
