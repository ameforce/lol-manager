from __future__ import annotations

import logging

from lolmanager.cli import entrypoint
from lolmanager.core.champion_fetcher import CounterMatchup
from lolmanager.gui.config_gui import display_value_to_champion_name


def test_cli_ban_prompt_displays_recommendation_labels_and_returns_plain_name(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        entrypoint,
        "fetch_counter_matchups_from_detail",
        lambda _href, limit=10: [
            CounterMatchup("퀸", 42.82, 376, 0, "42.82% 376 게임", "/quinn"),
            CounterMatchup("신지드", 44.27, 2354, 1, "44.27% 2,354 게임", "/singed"),
        ],
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    selected = entrypoint.prompt_ban_selection(
        "top",
        "말파이트",
        "/ko/lol/champions/malphite/build/top",
        logging.getLogger("test"),
        ranked_entries=[
            ("신지드", ("2티어", "green"), "/singed"),
            ("퀸", ("1티어", "blue"), "/quinn"),
        ],
    )

    out = capsys.readouterr().out
    assert selected == "퀸"
    assert "퀸 (1티어, 57.2%, score 85.9)" in out


def test_gui_display_value_maps_recommendation_label_to_plain_name() -> None:
    label_to_name = {"퀸 (1티어, 57.2%, score 85.9)": "퀸"}

    assert (
        display_value_to_champion_name(
            "퀸 (1티어, 57.2%, score 85.9)", label_to_name=label_to_name
        )
        == "퀸"
    )
    assert display_value_to_champion_name("  3. 아리", label_to_name={}) == "아리"
    assert display_value_to_champion_name("──────── 1티어 ────────") == ""
