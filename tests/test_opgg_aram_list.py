from __future__ import annotations

import json
from unittest import mock

import pytest

from lolmanager.core import opgg_aram_list as aram
from lolmanager.core.opgg_champion_list import save_champion_list_cache


def flight_html(champions):
    data = '1:["$","$L43",null,' + json.dumps({"champions": champions}, ensure_ascii=False) + ']\n'
    # The champion array and even individual strings can span script chunks.
    chunks = [data[:55], data[55:88], data[88:]]
    return '<table><tr><td>Loading</td></tr></table>' + ''.join(
        f'<script>self.__next_f.push({json.dumps([1, chunk], ensure_ascii=False)})</script>'
        for chunk in chunks
    )


def test_parser_uses_aram_rank_instead_of_alphabetical_payload_order():
    html = flight_html([
        {"name": "가렌", "key": "garen", "tier": 3, "rank": 52},
        {"name": "럭스", "key": "lux", "tier": 1, "rank": 2},
        {"name": "스웨인", "key": "swain", "tier": 0, "rank": 1},
        {"name": "징크스", "key": "jinx", "tier": 1, "rank": 3},
        {"name": "럭스", "key": "lux", "tier": 1, "rank": 2},
        {"name": "미확인", "key": "unknown", "tier": None, "rank": None},
    ])
    assert aram.parse_aram_entries_from_opgg_html(html) == [
        ("스웨인", "OP", "/ko/lol/modes/aram/swain/build"),
        ("럭스", "1티어", "/ko/lol/modes/aram/lux/build"),
        ("징크스", "1티어", "/ko/lol/modes/aram/jinx/build"),
        ("가렌", "3티어", "/ko/lol/modes/aram/garen/build"),
    ]


@pytest.mark.parametrize("payload", [
    {"name": "가렌", "key": "../ranked/garen", "tier": 1, "rank": 1},
    {"name": "가렌", "key": "garen", "tier": True, "rank": 1},
    {"name": "가렌", "key": "garen", "tier": 1, "rank": -1},
    {"name": "가렌", "key": "garen", "tier": 7, "rank": 1},
])
def test_parser_skips_invalid_champion_rows(payload):
    assert aram.parse_aram_entries_from_opgg_html(flight_html([payload])) == []


def test_fetch_rejects_placeholder_page_and_bounds_response_size(monkeypatch):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.headers = {}
    response.encoding = "utf-8"
    response.iter_content.return_value = [b'<table><tr><td>Loading</td></tr></table>']
    request = mock.Mock(return_value=response)
    monkeypatch.setattr(aram.requests, "get", request)
    with pytest.raises(ValueError, match="0개"):
        aram.fetch_opgg_aram_entries()
    assert request.call_args.args == (aram.OPGG_ARAM_URL_KO,)
    assert request.call_args.kwargs["stream"] is True
    response.headers = {"Content-Length": "5000000"}
    with pytest.raises(ValueError, match="too large"):
        aram.fetch_opgg_aram_entries()


def cache_entries():
    return [(f"챔피언{n}", "1티어", f"/ko/lol/modes/aram/champion{n}/build") for n in range(130)]


def test_aram_cache_is_independent_and_preserves_order_on_network_failure(tmp_path, monkeypatch):
    path = aram.default_aram_cache_path(tmp_path / "champion_config.json")
    entries = cache_entries()
    fetch = mock.Mock(return_value=entries)
    monkeypatch.setattr(aram, "fetch_opgg_aram_entries", fetch)
    monkeypatch.setattr(aram.time, "time", lambda: 1000)
    assert aram.get_aram_champion_entries(path) == (entries, "opgg")
    assert aram.get_aram_champion_entries(path) == (entries, "cache_fresh")
    fetch.assert_called_once()
    monkeypatch.setattr(aram.time, "time", lambda: 1000 + aram.ARAM_CACHE_MAX_AGE_SEC + 1)
    fetch.side_effect = RuntimeError("offline")
    assert aram.get_aram_champion_entries(path) == (entries, "cache_stale")
    assert not (tmp_path / "opgg_champion_list_cache.json").exists()


def test_ranked_cache_is_never_presented_as_aram_tiers(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    entries = cache_entries()
    save_champion_list_cache(path, [e[0] for e in entries], by_position={"top": entries})
    monkeypatch.setattr(aram, "fetch_opgg_aram_entries", mock.Mock(side_effect=RuntimeError("offline")))
    assert aram.get_aram_champion_entries(path) == ([], "unavailable")


def test_cache_write_failure_does_not_hide_live_rankings(tmp_path, monkeypatch):
    entries = cache_entries()
    monkeypatch.setattr(aram, "fetch_opgg_aram_entries", lambda _: entries)
    monkeypatch.setattr(aram, "save_champion_list_cache", mock.Mock(side_effect=OSError("read only")))
    assert aram.get_aram_champion_entries(tmp_path / "cache.json") == (entries, "opgg")
