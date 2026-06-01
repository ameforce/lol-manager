import pytest

from lolmanager.core.champion_fetcher import sort_counter_candidates_by_role_rank
from lolmanager.core.opgg_champion_list import (
    fetch_opgg_champion_dataset,
    parse_position_entries_from_opgg_html,
)
from lolmanager.core.opgg_http import MAX_OPGG_CHAMPION_ROWS


class _OversizedOpggResponse:
    status_code = 200
    headers = {"Content-Length": "999999999"}
    encoding = "utf-8"

    def raise_for_status(self) -> None:
        pass

    @property
    def text(self) -> str:
        raise AssertionError("oversized response body should not be materialized")

    def iter_content(self, *_args: object, **_kwargs: object) -> list[bytes]:
        raise AssertionError("oversized response body should not be streamed")

    def close(self) -> None:
        pass


def test_sort_counter_candidates_prioritizes_ranked_opgg_counters() -> None:
    counters = ["가렌", "퀸", "말파이트", "다리우스"]
    ranked_entries = [
        ("다리우스", ("1티어", "blue"), "/ko/lol/champions/darius/build/top"),
        ("퀸", ("2티어", "green"), "/ko/lol/champions/quinn/build/top"),
        ("가렌", ("3티어", "yellow"), "/ko/lol/champions/garen/build/top"),
    ]

    assert sort_counter_candidates_by_role_rank(counters, ranked_entries) == [
        "다리우스",
        "퀸",
        "가렌",
        "말파이트",
    ]


def test_sort_counter_candidates_falls_back_to_counter_order_without_rank() -> None:
    counters = [" 말파이트 ", "가렌", "말파이트", "퀸"]

    assert sort_counter_candidates_by_role_rank(counters, []) == [
        "말파이트",
        "가렌",
        "퀸",
    ]


def test_position_entry_parser_caps_role_rows_before_full_scan() -> None:
    rows = "\n".join(
        f'''
        <tr>
          <td></td>
          <td>
            <div>
              <a href="/ko/lol/champions/champion-{idx}/build/top">
                <strong>챔피언{idx}</strong>
              </a>
            </div>
          </td>
        </tr>
        '''
        for idx in range(MAX_OPGG_CHAMPION_ROWS + 25)
    )

    entries = parse_position_entries_from_opgg_html(f"<table>{rows}</table>", "top")

    assert len(entries) == MAX_OPGG_CHAMPION_ROWS


def test_opgg_champion_dataset_rejects_oversized_role_response(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(_url: str, **kwargs: object) -> _OversizedOpggResponse:
        calls.append(kwargs)
        return _OversizedOpggResponse()

    monkeypatch.setattr("lolmanager.core.opgg_champion_list.requests.get", fake_get)

    with pytest.raises(ValueError, match="OP.GG response too large"):
        fetch_opgg_champion_dataset()

    assert calls[0]["stream"] is True
