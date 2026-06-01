from __future__ import annotations

import pytest

from lolmanager.core.champion_fetcher import (
    fetch_top_champions,
    sort_counter_candidates_by_role_rank,
)
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


class _OpggTextResponse:
    status_code = 200
    headers: dict[str, str] = {}
    encoding = "utf-8"

    def __init__(self, text: str) -> None:
        self._text = text

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int = 8192) -> list[bytes]:
        return [
            self._text.encode("utf-8")[idx : idx + chunk_size]
            for idx in range(0, len(self._text), chunk_size)
        ]

    def close(self) -> None:
        pass


def _opgg_table_rows(position: str, count: int, prefix: str) -> str:
    return "\n".join(
        f'''
        <tr>
          <td></td>
          <td>
            <div>
              <a href="/ko/lol/champions/{prefix}-{idx}/build/{position}">
                <strong>{prefix}챔피언{idx}</strong>
              </a>
            </div>
          </td>
        </tr>
        '''
        for idx in range(count)
    )


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


def test_position_entry_parser_handles_card_dom_with_image_names() -> None:
    html = """
    <main>
      <article class="champion-card">
        <a href="/ko/lol/champions/quinn/build/top">
          <img alt="퀸" />
          <span>승률</span>
        </a>
        <svg><path fill="#0093ff" d="M2 0h20v18.056L12 23 2 18.056z" /></svg>
      </article>
      <article class="champion-card">
        <a href="/ko/lol/champions/singed/build/top">
          <span class="champion-name">신지드</span>
        </a>
        <svg><path fill="#00bba3" d="M2 0h20v18.056L12 23 2 18.056z" /></svg>
      </article>
    </main>
    """

    assert parse_position_entries_from_opgg_html(html, "top") == [
        ("퀸", "1티어", "/ko/lol/champions/quinn/build/top"),
        ("신지드", "2티어", "/ko/lol/champions/singed/build/top"),
    ]


def test_opgg_champion_dataset_rejects_oversized_role_response(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(_url: str, **kwargs: object) -> _OversizedOpggResponse:
        calls.append(kwargs)
        return _OversizedOpggResponse()

    monkeypatch.setattr("lolmanager.core.opgg_champion_list.requests.get", fake_get)

    with pytest.raises(ValueError, match="OP.GG response too large"):
        fetch_opgg_champion_dataset()

    assert calls[0]["stream"] is True


def test_opgg_champion_dataset_reports_empty_role_with_parser_context(
    monkeypatch,
) -> None:
    html_by_role = {
        "top": "<html><body><p>no champion links</p></body></html>",
        "jungle": f"<table>{_opgg_table_rows('jungle', 31, 'jungle')}</table>",
        "mid": f"<table>{_opgg_table_rows('mid', 31, 'mid')}</table>",
        "adc": f"<table>{_opgg_table_rows('adc', 31, 'adc')}</table>",
        "support": f"<table>{_opgg_table_rows('support', 31, 'support')}</table>",
    }

    def fake_get(url: str, **_kwargs: object) -> _OpggTextResponse:
        position = str(url).rsplit("position=", maxsplit=1)[-1]
        return _OpggTextResponse(html_by_role[position])

    monkeypatch.setattr("lolmanager.core.opgg_champion_list.requests.get", fake_get)

    with pytest.raises(RuntimeError, match="top.*supported selectors"):
        fetch_opgg_champion_dataset()


def test_fetch_top_champions_reuses_resilient_http_parser(
    monkeypatch,
) -> None:
    html = """
    <main>
      <article>
        <a href="/ko/lol/champions/quinn/build/top">
          <img alt="퀸" />
        </a>
        <svg><path fill="#0093ff" d="M2 0h20v18.056L12 23 2 18.056z" /></svg>
      </article>
    </main>
    """

    monkeypatch.setattr(
        "lolmanager.core.champion_fetcher.requests.get",
        lambda *_args, **_kwargs: _OpggTextResponse(html),
    )
    monkeypatch.setattr(
        "lolmanager.core.champion_fetcher._scrape",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("playwright fallback should not run")
        ),
    )

    assert fetch_top_champions("top") == [
        ("퀸", ("1티어", "blue"), "/ko/lol/champions/quinn/build/top")
    ]
