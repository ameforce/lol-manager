from __future__ import annotations

import logging
import json
from pathlib import Path

from lolmanager.cli import entrypoint
from lolmanager.core.champion_config import ChampionConfig
from lolmanager.core.champion_fetcher import CounterMatchup
from lolmanager.core.opgg_counter_recommendations import (
    AUTO_BAN_LABEL,
    AUTO_BAN_VALUE,
    CounterRecommendation,
    default_counter_cache_path,
    save_recommendation_cache,
)
from lolmanager.gui.config_gui import (
    DISPLAY_SEPARATOR_PREFIX,
    ROLE_VAR_FIELD_NAMES,
    _RoleVars,
    attach_role_var_autosave_traces,
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
    assert display_value_to_champion_name(AUTO_BAN_LABEL) == AUTO_BAN_VALUE


def test_gui_ban_candidates_keep_auto_then_recommendations_above_all_champions() -> None:
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

    assert values[:4] == [
        AUTO_BAN_LABEL,
        f"{DISPLAY_SEPARATOR_PREFIX} 추천 밴 {DISPLAY_SEPARATOR_PREFIX}",
        "블라디미르 (2티어, 45.6%, score 62.0)",
        "제라스 (1티어, 48.9%, score 55.5)",
    ]
    assert f"{DISPLAY_SEPARATOR_PREFIX} 기타 챔피언 {DISPLAY_SEPARATOR_PREFIX}" in values
    assert values[-2:] == ["가렌", "아리"]


def test_runtime_auto_ban_uses_top_cached_recommendation(tmp_path: Path) -> None:
    config_path = tmp_path / "champion_config.json"
    cache_path = default_counter_cache_path(config_path)
    save_recommendation_cache(
        cache_path,
        role="top",
        configured_pick="말파이트",
        recommendations=[
            CounterRecommendation(
                role="top",
                configured_pick="말파이트",
                champion="퀸",
                tier="1티어",
                matchup_winrate=42.8,
                pick_winrate=42.8,
                tier_score_value=50,
                matchup_score=35.9,
                total_score=85.9,
                source_order=0,
            ),
            CounterRecommendation(
                role="top",
                configured_pick="말파이트",
                champion="신지드",
                tier="2티어",
                matchup_winrate=44.3,
                pick_winrate=44.3,
                tier_score_value=40,
                matchup_score=28.6,
                total_score=68.6,
                source_order=1,
            ),
        ],
        fetched_at_unix=100.0,
    )

    logger = logging.getLogger("test")

    assert (
        entrypoint.resolve_ban_name_for_runtime(
            cache_path,
            role="top",
            champion_name="말파이트",
            configured_ban=AUTO_BAN_VALUE,
            logger=logger,
            now=120.0,
        )
        == "퀸"
    )
    assert (
        entrypoint.resolve_ban_name_for_runtime(
            cache_path,
            role="top",
            champion_name="말파이트",
            configured_ban="가렌",
            logger=logger,
            now=120.0,
        )
        == "가렌"
    )


def test_gui_does_not_reuse_failed_ban_candidate_attempts() -> None:
    assert not should_reuse_ban_candidate_values(([], {}, "cache_miss"))
    assert not should_reuse_ban_candidate_values(([], {}, "opgg"))
    assert should_reuse_ban_candidate_values(
        (["퀸 (1티어, 42.8%, score 85.9)"], {"퀸 (1티어, 42.8%, score 85.9)": "퀸"}, "opgg")
    )


class _FakeVar:
    def __init__(self) -> None:
        self.callbacks = []

    def trace_add(self, mode: str, callback):
        self.callbacks.append((mode, callback))
        return f"trace-{len(self.callbacks)}"


def test_autosave_traces_watch_every_config_field() -> None:
    vars_by_role = {
        "mid": _RoleVars(
            champion=_FakeVar(),
            ban=_FakeVar(),
            pick_x=_FakeVar(),
            pick_y=_FakeVar(),
            reserve1_champion=_FakeVar(),
            reserve1_ban=_FakeVar(),
            reserve2_champion=_FakeVar(),
            reserve2_ban=_FakeVar(),
        )
    }
    changed = []

    handles = attach_role_var_autosave_traces(
        vars_by_role,
        lambda role, field: changed.append((role, field)),
    )

    assert len(handles) == len(ROLE_VAR_FIELD_NAMES)
    for field_name in ROLE_VAR_FIELD_NAMES:
        fake_var = getattr(vars_by_role["mid"], field_name)
        assert fake_var.callbacks[0][0] == "write"
        fake_var.callbacks[0][1]()

    assert changed == [("mid", field_name) for field_name in ROLE_VAR_FIELD_NAMES]


def test_config_load_strips_recommendation_metadata_before_runtime(tmp_path: Path) -> None:
    config_path = tmp_path / "champion_config.json"
    config_path.write_text(
        json.dumps(
            {
                "mid": {
                    "champion": "카타리나",
                    "ban": AUTO_BAN_VALUE,
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

    assert config.get("mid")["ban"] == AUTO_BAN_VALUE
    assert config.get_reserve_picks("mid") == [("오리아나", "제라스")]
