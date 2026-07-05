from __future__ import annotations

import json
from pathlib import Path

from lolmanager.core.opgg_champion_list import (
    load_champion_list_cache,
    parse_position_entries_from_opgg_html,
)


def test_position_entry_parser_reads_tier_column_not_rank_change_icon() -> None:
    html = """
    <table>
      <tr>
        <td>
          <svg aria-label="rank change"><path fill="#9aa4af" /></svg>
        </td>
        <td>
          <a href="/ko/lol/champions/garen/build/top">
            <strong>가렌</strong>
          </a>
        </td>
        <td>
          <svg aria-label="tier">
            <path fill="#0093ff" d="M2 0h20v18.056L12 23 2 18.056z" />
            <path fill="#fff" d="M5 5h14v10H5z" />
          </svg>
        </td>
      </tr>
    </table>
    """

    assert parse_position_entries_from_opgg_html(html, "top") == [
        ("가렌", "1티어", "/ko/lol/champions/garen/build/top")
    ]


def test_stale_opgg_tier_cache_is_rejected_after_parser_contract_change(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "opgg_champion_list_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "v": 3,
                "fetched_at_unix": 1000.0,
                "champions": [f"챔피언{idx}" for idx in range(130)],
                "by_position": {
                    "top": [
                        {
                            "name": "가렌",
                            "tier": "4티어",
                            "href": "/ko/lol/champions/garen/build/top",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert load_champion_list_cache(cache_path) is None


def test_gui_champion_candidates_group_ranked_entries_by_tier_order() -> None:
    from lolmanager.gui.champion_candidate_values import (
        build_champion_candidate_values,
    )

    values = build_champion_candidate_values(
        ranked_entries=[
            ("가렌", "4티어", "/ko/lol/champions/garen/build/top"),
            ("말파이트", "1티어", "/ko/lol/champions/malphite/build/top"),
            ("갱플랭크", "OP", "/ko/lol/champions/gangplank/build/top"),
            ("케일", "4티어", "/ko/lol/champions/kayle/build/top"),
            ("신지드", "OP", "/ko/lol/champions/singed/build/top"),
            ("아리", "2티어", "/ko/lol/champions/ahri/build/top"),
            ("오른", "3티어", "/ko/lol/champions/ornn/build/top"),
            ("나서스", "5티어", "/ko/lol/champions/nasus/build/top"),
        ],
        all_champion_values=["가렌", "말파이트", "베인", "아리", "나서스"],
    )

    assert values == [
        "──────── OP ────────",
        "  1. 갱플랭크",
        "  2. 신지드",
        "──────── 1티어 ────────",
        "  3. 말파이트",
        "──────── 2티어 ────────",
        "  4. 아리",
        "──────── 3티어 ────────",
        "  5. 오른",
        "──────── 4티어 ────────",
        "  6. 가렌",
        "  7. 케일",
        "──────── 5티어 ────────",
        "  8. 나서스",
        "──────── 기타 ────────",
        "베인",
    ]
