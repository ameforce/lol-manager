from __future__ import annotations

import json
from pathlib import Path

from lolmanager.core.champion_fetcher import (
    fetch_counters_from_detail,
    parse_counter_matchups_from_html,
)
from lolmanager.core.opgg_counter_recommendations import (
    build_label_name_map,
    build_recommendations,
    candidate_winrate_from_pick_winrate,
    default_counter_cache_path,
    format_recommendation_label,
    get_counter_recommendations,
    load_recommendation_cache,
    matchup_winrate_score,
    save_recommendation_cache,
    tier_score,
)


COUNTER_HTML = """
<html>
  <body>
    <section>
      <h2>말파이트 카운터</h2>
      <h3>상대하기 어려운 챔피언</h3>
      <ul>
        <li>
          <a href="/ko/lol/champions/quinn/build/top">
            <img alt="퀸" />
            <span>42.82%</span>
            <span>376 게임</span>
          </a>
        </li>
        <li>
          <a href="/ko/lol/champions/singed/build/top">
            <img alt="신지드" />
            <span>44.27%</span>
            <span>2,354 게임</span>
          </a>
        </li>
      </ul>
      <h3>상대하기 쉬운 챔피언</h3>
      <ul>
        <li><a><img alt="나서스" /><span>61.15%</span></a></li>
      </ul>
    </section>
  </body>
</html>
"""


class _Response:
    status_code = 200
    text = COUNTER_HTML


def test_scoring_orders_counter_candidates_by_tier_and_matchup_winrate() -> None:
    matchups = parse_counter_matchups_from_html(COUNTER_HTML, source_url="/malphite")
    ranked_entries = [
        ("신지드", ("2티어", "green"), "/ko/lol/champions/singed/build/top"),
        ("퀸", ("1티어", "blue"), "/ko/lol/champions/quinn/build/top"),
    ]

    recommendations = build_recommendations(
        role="top",
        configured_pick="말파이트",
        matchups=matchups,
        ranked_entries=ranked_entries,
        source_url="/malphite",
    )

    assert [r.champion for r in recommendations] == ["퀸", "신지드"]
    assert recommendations[0].tier == "1티어"
    assert recommendations[0].matchup_winrate == 57.18
    assert recommendations[0].total_score > recommendations[1].total_score


def test_score_helpers_use_documented_defaults() -> None:
    assert tier_score("OP") == 50
    assert tier_score("1티어") == 50
    assert tier_score("2티어") == 40
    assert tier_score("5티어") == 10
    assert tier_score("unknown") == 0

    assert candidate_winrate_from_pick_winrate(42.82) == 57.18
    assert matchup_winrate_score(57.18) == 35.9
    assert matchup_winrate_score(45.0) == 0


def test_sort_tie_break_uses_raw_matchup_winrate_after_score_clamp() -> None:
    html = """
    <section>
      <h3>상대하기 어려운 챔피언</h3>
      <ul>
        <li><a><img alt="니코" /><span>39.00%</span></a></li>
        <li><a><img alt="퀸" /><span>38.00%</span></a></li>
      </ul>
    </section>
    """

    recommendations = build_recommendations(
        role="top",
        configured_pick="말파이트",
        matchups=parse_counter_matchups_from_html(html, source_url="/malphite"),
        ranked_entries=[
            ("니코", ("1티어", "blue"), "/neeko"),
            ("퀸", ("1티어", "blue"), "/quinn"),
        ],
        source_url="/malphite",
    )

    assert [r.champion for r in recommendations] == ["퀸", "니코"]


def test_parser_reads_difficult_matchups_and_name_only_fetch_stays_compatible(
    monkeypatch,
) -> None:
    matchups = parse_counter_matchups_from_html(COUNTER_HTML, source_url="/malphite")

    assert [(m.champion, m.pick_winrate, m.games) for m in matchups] == [
        ("퀸", 42.82, 376),
        ("신지드", 44.27, 2354),
    ]
    assert matchups[0].source_order == 0
    assert "42.82%" in matchups[0].raw_text

    monkeypatch.setattr(
        "lolmanager.core.champion_fetcher.requests.get",
        lambda *_args, **_kwargs: _Response(),
    )

    assert fetch_counters_from_detail("/ko/lol/champions/malphite/build/top") == [
        "퀸",
        "신지드",
    ]


def test_label_mapping_formats_metadata_but_returns_plain_names() -> None:
    recommendations = build_recommendations(
        role="top",
        configured_pick="말파이트",
        matchups=parse_counter_matchups_from_html(COUNTER_HTML, source_url="/malphite"),
        ranked_entries=[("퀸", ("1티어", "blue"), "/quinn")],
        source_url="/malphite",
    )

    label = format_recommendation_label(recommendations[0])
    labels, label_to_name = build_label_name_map(recommendations)

    assert label == "퀸 (1티어, 57.2%, score 85.9)"
    assert labels[0] == label
    assert label_to_name[label] == "퀸"


def test_recommendation_cache_is_separate_versioned_and_stale_safe(tmp_path: Path) -> None:
    config_path = tmp_path / "champion_config.json"
    cache_path = default_counter_cache_path(config_path)
    recommendations = build_recommendations(
        role="top",
        configured_pick="말파이트",
        matchups=parse_counter_matchups_from_html(COUNTER_HTML, source_url="/malphite"),
        ranked_entries=[("퀸", ("1티어", "blue"), "/quinn")],
        source_url="/malphite",
    )

    save_recommendation_cache(
        cache_path,
        role="top",
        configured_pick="말파이트",
        recommendations=recommendations,
        fetched_at_unix=100.0,
    )

    assert cache_path.name == "opgg_counter_recommendation_cache.json"
    loaded = load_recommendation_cache(
        cache_path,
        role="top",
        configured_pick="말파이트",
        max_age_sec=60.0,
        now=120.0,
    )
    stale = load_recommendation_cache(
        cache_path,
        role="top",
        configured_pick="말파이트",
        max_age_sec=10.0,
        now=120.0,
    )

    assert loaded.status == "cache_fresh"
    assert [r.champion for r in loaded.recommendations] == ["퀸", "신지드"]
    assert stale.status == "cache_stale"

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["v"] = -1
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        load_recommendation_cache(
            cache_path,
            role="top",
            configured_pick="말파이트",
            max_age_sec=60.0,
            now=120.0,
        ).status
        == "cache_miss"
    )


def test_get_counter_recommendations_refreshes_stale_cache_and_falls_back(
    tmp_path: Path,
) -> None:
    cache_path = default_counter_cache_path(tmp_path / "champion_config.json")
    stale_recommendations = build_recommendations(
        role="top",
        configured_pick="말파이트",
        matchups=parse_counter_matchups_from_html(COUNTER_HTML, source_url="/old"),
        ranked_entries=[("퀸", ("1티어", "blue"), "/quinn")],
        source_url="/old",
    )
    save_recommendation_cache(
        cache_path,
        role="top",
        configured_pick="말파이트",
        recommendations=stale_recommendations,
        fetched_at_unix=100.0,
    )

    refreshed = get_counter_recommendations(
        cache_path,
        role="top",
        configured_pick="말파이트",
        ranked_entries=[("퀸", ("1티어", "blue"), "/quinn")],
        detail_href="/ko/lol/champions/malphite/build/top",
        max_age_sec=10.0,
        now=120.0,
        fetch_matchups=lambda _href, _limit: parse_counter_matchups_from_html(
            COUNTER_HTML.replace("376 게임", "999 게임"), source_url="/new"
        ),
    )

    assert refreshed.status == "opgg"
    assert refreshed.recommendations[0].games == 999

    failed = get_counter_recommendations(
        cache_path,
        role="top",
        configured_pick="말파이트",
        ranked_entries=[("퀸", ("1티어", "blue"), "/quinn")],
        detail_href="/ko/lol/champions/malphite/build/top",
        max_age_sec=0.0,
        now=1000.0,
        fetch_matchups=lambda _href, _limit: (_ for _ in ()).throw(
            RuntimeError("network down")
        ),
    )

    assert failed.status == "cache_stale"
    assert failed.recommendations[0].games == 999
