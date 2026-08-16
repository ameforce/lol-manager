from __future__ import annotations

import json
from pathlib import Path

from lolmanager.core.auto_ban_refresh import (
    AUTO_BAN_REFRESH_RETRY_MS,
    AUTO_BAN_REFRESH_SUCCESS_MS,
    AutoBanRefreshCoordinator,
    collect_auto_ban_targets,
)
from lolmanager.core.opgg_counter_recommendations import (
    AUTO_BAN_VALUE,
    CounterRecommendation,
    RecommendationCacheResult,
)


def _write_config(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_collect_auto_ban_targets_includes_primary_and_reserves_and_dedupes(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "champion_config.json"
    _write_config(
        config_path,
        {
            "top": {
                "champion": "말파이트",
                "ban": AUTO_BAN_VALUE,
                "reserve_picks": [
                    {"champion": "오른", "ban": AUTO_BAN_VALUE},
                    {"champion": "말파이트", "ban": AUTO_BAN_VALUE},
                    {"champion": "아트록스", "ban": "피오라"},
                ],
            }
        },
    )

    targets = collect_auto_ban_targets(config_path)

    assert [(t.role, t.champion_name) for t in targets] == [
        ("top", "말파이트"),
        ("top", "오른"),
    ]


def test_refresh_coordinator_forces_startup_refresh_and_schedules_success(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "champion_config.json"
    _write_config(
        config_path,
        {"top": {"champion": "말파이트", "ban": AUTO_BAN_VALUE}},
    )
    scheduled_ms: list[int] = []
    refresh_calls: list[dict[str, object]] = []
    updated = []

    def schedule_after_ms(delay_ms: int, callback):
        scheduled_ms.append(delay_ms)
        if delay_ms == 0:
            callback()
        return f"after-{len(scheduled_ms)}"

    def run_background(callback):
        callback()

    def get_champion_dataset(_path, **_kwargs):
        return (
            ["말파이트"],
            {
                "top": [
                    (
                        "말파이트",
                        ("1티어", "blue"),
                        "/ko/lol/champions/malphite/build/top",
                    )
                ]
            },
            "cache_fresh",
        )

    def get_counter_recommendations(cache_path, **kwargs):
        refresh_calls.append(dict(kwargs))
        return RecommendationCacheResult(
            "opgg",
            (
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
            ),
            123.0,
        )

    coordinator = AutoBanRefreshCoordinator(
        config_path=config_path,
        schedule_after_ms=schedule_after_ms,
        run_background=run_background,
        on_updated=lambda: updated.append(True),
        get_champion_dataset=get_champion_dataset,
        get_counter_recommendations=get_counter_recommendations,
    )

    coordinator.start()

    assert scheduled_ms == [0, AUTO_BAN_REFRESH_SUCCESS_MS]
    assert refresh_calls[0]["max_age_sec"] == 0
    assert refresh_calls[0]["ranked_entries"] == [
        ("말파이트", "1티어", "/ko/lol/champions/malphite/build/top")
    ]
    assert updated == [True]


def test_refresh_coordinator_retries_failed_refresh_after_a_long_interval(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "champion_config.json"
    _write_config(
        config_path,
        {"mid": {"champion": "카타리나", "ban": AUTO_BAN_VALUE}},
    )
    scheduled_ms: list[int] = []

    def schedule_after_ms(delay_ms: int, callback):
        scheduled_ms.append(delay_ms)
        if delay_ms == 0:
            callback()
        return f"after-{len(scheduled_ms)}"

    def get_champion_dataset(_path, **_kwargs):
        return (
            ["카타리나"],
            {"mid": [("카타리나", "2티어", "/ko/lol/champions/katarina/build/mid")]},
            "cache_fresh",
        )

    coordinator = AutoBanRefreshCoordinator(
        config_path=config_path,
        schedule_after_ms=schedule_after_ms,
        run_background=lambda callback: callback(),
        on_updated=lambda: None,
        get_champion_dataset=get_champion_dataset,
        get_counter_recommendations=lambda _path, **_kwargs: RecommendationCacheResult(
            "cache_stale",
            tuple(),
            100.0,
        ),
    )

    coordinator.start()

    assert scheduled_ms == [0, AUTO_BAN_REFRESH_RETRY_MS]
    assert AUTO_BAN_REFRESH_RETRY_MS >= 60 * 60 * 1000


def test_refresh_coordinator_treats_missing_href_as_retryable_failure(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "champion_config.json"
    _write_config(
        config_path,
        {"support": {"champion": "룰루", "ban": AUTO_BAN_VALUE}},
    )
    scheduled_ms: list[int] = []

    def schedule_after_ms(delay_ms: int, callback):
        scheduled_ms.append(delay_ms)
        if delay_ms == 0:
            callback()
        return f"after-{len(scheduled_ms)}"

    coordinator = AutoBanRefreshCoordinator(
        config_path=config_path,
        schedule_after_ms=schedule_after_ms,
        run_background=lambda callback: callback(),
        on_updated=lambda: None,
        get_champion_dataset=lambda _path, **_kwargs: (
            ["룰루"],
            {"support": [("룰루", "3티어", "")]},
            "cache_fresh",
        ),
        fetch_champion_slug=lambda _role, _champion: None,
        get_counter_recommendations=lambda _path, **_kwargs: RecommendationCacheResult(
            "opgg",
            tuple(),
            123.0,
        ),
    )

    coordinator.start()

    assert scheduled_ms == [0, AUTO_BAN_REFRESH_RETRY_MS]


def test_refresh_coordinator_does_not_start_duplicate_in_flight_target(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "champion_config.json"
    _write_config(
        config_path,
        {"top": {"champion": "말파이트", "ban": AUTO_BAN_VALUE}},
    )
    started_workers = []

    def schedule_after_ms(delay_ms: int, callback):
        if delay_ms == 0:
            callback()
        return "after"

    coordinator = AutoBanRefreshCoordinator(
        config_path=config_path,
        schedule_after_ms=schedule_after_ms,
        run_background=lambda callback: started_workers.append(callback),
        on_updated=lambda: None,
        get_champion_dataset=lambda _path, **_kwargs: (
            ["말파이트"],
            {"top": [("말파이트", "1티어", "/ko/lol/champions/malphite/build/top")]},
            "cache_fresh",
        ),
        get_counter_recommendations=lambda _path, **_kwargs: RecommendationCacheResult(
            "opgg",
            tuple(),
            123.0,
        ),
    )

    coordinator.start()
    coordinator.refresh_configured_targets()

    assert len(started_workers) == 1
