from __future__ import annotations

import logging
import json
from pathlib import Path

from lolmanager.cli import entrypoint
from lolmanager.core.champion_config import ChampionConfig
from lolmanager.core.champion_fetcher import CounterMatchup
from lolmanager.gui.config_gui import (
    DISPLAY_SEPARATOR_PREFIX,
    build_ban_candidate_values,
    display_value_to_champion_name,
    should_reuse_ban_candidate_values,
)


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
    assert "퀸 (1티어, 42.8%, score 85.9)" in out


def test_gui_display_value_maps_recommendation_label_to_plain_name() -> None:
    label_to_name = {"퀸 (1티어, 42.8%, score 85.9)": "퀸"}

    assert (
        display_value_to_champion_name(
            "퀸 (1티어, 42.8%, score 85.9)", label_to_name=label_to_name
        )
        == "퀸"
    )
    assert (
        display_value_to_champion_name("블라디미르 (2티어, 45.6%, score 62.0)")
        == "블라디미르"
    )
    assert display_value_to_champion_name("  3. 아리", label_to_name={}) == "아리"
    assert display_value_to_champion_name("──────── 1티어 ────────") == ""


def test_gui_ban_candidates_keep_recommendations_above_all_champions() -> None:
    values = build_ban_candidate_values(
        labels=[
            "블라디미르 (2티어, 45.6%, score 62.0)",
            "제라스 (1티어, 48.9%, score 55.5)",
        ],
        label_to_name={
            "블라디미르 (2티어, 45.6%, score 62.0)": "블라디미르",
            "제라스 (1티어, 48.9%, score 55.5)": "제라스",
        },
        all_champion_values=["가렌", "블라디미르", "아리", "제라스"],
    )

    assert values[:3] == [
        f"{DISPLAY_SEPARATOR_PREFIX} 추천 밴 {DISPLAY_SEPARATOR_PREFIX}",
        "블라디미르 (2티어, 45.6%, score 62.0)",
        "제라스 (1티어, 48.9%, score 55.5)",
    ]
    assert f"{DISPLAY_SEPARATOR_PREFIX} 기타 챔피언 {DISPLAY_SEPARATOR_PREFIX}" in values
    assert values[-2:] == ["가렌", "아리"]


def test_gui_does_not_reuse_failed_ban_candidate_attempts() -> None:
    assert not should_reuse_ban_candidate_values(([], {}, "cache_miss"))
    assert not should_reuse_ban_candidate_values(([], {}, "opgg"))
    assert should_reuse_ban_candidate_values(
        (["퀸 (1티어, 42.8%, score 85.9)"], {"퀸 (1티어, 42.8%, score 85.9)": "퀸"}, "opgg")
    )


def test_config_load_strips_recommendation_metadata_before_runtime(tmp_path: Path) -> None:
    config_path = tmp_path / "champion_config.json"
    config_path.write_text(
        json.dumps(
            {
                "mid": {
                    "champion": "카타리나",
                    "ban": "블라디미르 (2티어, 45.6%, score 62.0)",
                    "reserve_picks": [
                        {
                            "champion": "오리아나",
                            "ban": "제라스 (1티어, 48.9%, score 55.5)",
                        }
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = ChampionConfig(config_path)

    assert config.get("mid")["ban"] == "블라디미르"
    assert config.get_reserve_picks("mid") == [("오리아나", "제라스")]
