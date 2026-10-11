from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from lolmanager.cli import entrypoint
from lolmanager.core.aram import AramPreferences, choose_aram_bench_champion
from lolmanager.core.champion_config import ChampionConfig
from lolmanager.core.lcu_client import (
    LcuClient,
    LcuDecision,
    LcuLoopAction,
    LcuOutcome,
    LcuResult,
)


GRID = [
    {"id": 103, "name": "아리", "owned": True},
    {"id": 99, "name": "럭스", "freeToPlayForQueue": True},
    {"id": 22, "name": "애쉬", "owned": True},
]


def session(current=22, bench=(99, 103), **overrides):
    return {
        "gameId": 123,
        "benchEnabled": True,
        "benchChampions": [{"championId": cid} for cid in bench],
        "localPlayerCellId": 0,
        "myTeam": [{"cellId": 0, "championId": current}],
        **overrides,
    }


def client_with(*responses):
    client = LcuClient()
    client.request = mock.Mock(side_effect=responses)
    return client


def mode(queue_id=450):
    return LcuResult(200, {"gameData": {"queue": {"id": queue_id}}})


def writes(client):
    return [call.args for call in client.request.call_args_list if call.args[0] != "GET"]


@pytest.mark.parametrize(
    "current,bench,expected",
    [(22, (99, 103), 103), (22, (99,), 99), (103, (99,), None),
     (99, (22,), None), (99, (103,), 103), (1, (), None), (1, (22,), 22)],
)
def test_bench_selection_respects_priority_without_downgrading(current, bench, expected):
    assert choose_aram_bench_champion((103, 99, 22), current, bench) == expected


def test_preferences_are_opt_in_and_normalize_order_without_touching_ranked(tmp_path):
    path = tmp_path / "champions.json"
    ranked = {"champion": "아리", "ban": "제드"}
    path.write_text(json.dumps({"mid": ranked}), encoding="utf-8")
    config = ChampionConfig(path)
    assert AramPreferences.from_config(config.get("aram")) == AramPreferences()
    prefs = AramPreferences.from_config({
        "enabled": True,
        "preferred_champions": [" 아리 ", "럭스", "아 리", None, 1, ""],
    })
    assert prefs.preferred_champions == ("아리", "럭스")
    config.data["aram"] = prefs.as_config()
    config.save()
    loaded = ChampionConfig(path)
    assert loaded.get("mid") == ranked
    assert loaded.get("aram") == prefs.as_config()


@pytest.mark.parametrize("value", [None, [], "aram", {"enabled": "false"}, {"enabled": 1}])
def test_invalid_preferences_do_not_enable_automation(value):
    assert not AramPreferences.from_config(value).enabled


@pytest.mark.parametrize("queue_id", [450, 2400])
def test_swaps_highest_available_preference_and_confirms_local_champion(queue_id):
    client = client_with(
        mode(queue_id), LcuResult(200, session()), LcuResult(200, GRID),
        LcuResult(204), LcuResult(200, session(current=103)),
    )
    result = client.select_aram_preferred_champion_decision(("아리", "럭스", "애쉬"))
    assert result.ok and result.value == "아리"
    assert writes(client) == [("POST", "/lol-champ-select/v1/session/bench/swap/103")]


@pytest.mark.parametrize("queue_id", [420, 440, 400])
def test_other_modes_never_issue_aram_requests(queue_id):
    client = client_with(mode(queue_id))
    assert client.select_aram_preferred_champion_decision(("아리",)).status == LcuOutcome.UNSUPPORTED
    assert client.request.call_count == 1
    assert not writes(client)


@pytest.mark.parametrize(
    "payload,status",
    [(session(bench=()), LcuOutcome.NO_CURRENT_ACTION),
     (session(benchEnabled=False), LcuOutcome.NO_CURRENT_ACTION),
     (session(isSpectating=True), LcuOutcome.NO_CURRENT_ACTION),
     (session(benchChampions=None), LcuOutcome.MALFORMED_SESSION),
     (session(current=0), LcuOutcome.NO_LOCAL_PLAYER),
     (session(myTeam=[]), LcuOutcome.NO_LOCAL_PLAYER)],
)
def test_incomplete_or_inactive_bench_does_not_swap(payload, status):
    client = client_with(mode(), LcuResult(200, payload))
    assert client.select_aram_preferred_champion_decision(("아리",)).status == status
    assert not writes(client)


@pytest.mark.parametrize("current,bench", [(103, (99,)), (99, (22,))])
def test_already_preferred_current_champion_is_kept(current, bench):
    client = client_with(mode(), LcuResult(200, session(current, bench)), LcuResult(200, GRID))
    assert client.select_aram_preferred_champion_decision(("아리", "럭스", "애쉬")).status == LcuOutcome.NO_CURRENT_ACTION
    assert not writes(client)


@pytest.mark.parametrize("blocked", [{"owned": False}, {"disabled": True}])
def test_unusable_first_preference_falls_back_to_next_available_champion(blocked):
    grid = copy.deepcopy(GRID)
    grid[0].update(blocked)
    client = client_with(
        mode(), LcuResult(200, session()), LcuResult(200, grid),
        LcuResult(204), LcuResult(200, session(current=99)),
    )
    assert client.select_aram_preferred_champion_decision(("아리", "럭스")).value == "럭스"
    assert writes(client) == [("POST", "/lol-champ-select/v1/session/bench/swap/99")]


def test_unknown_preference_does_not_guess_or_swap():
    client = client_with(mode(), LcuResult(200, session()), LcuResult(200, GRID))
    assert client.select_aram_preferred_champion_decision(("없는 챔피언", "럭스")).status == LcuOutcome.CHAMPION_NOT_FOUND
    assert not writes(client)


@pytest.mark.parametrize(
    "response,status",
    [(LcuResult(409), LcuOutcome.ACTION_REJECTED),
     (LcuResult(404), LcuOutcome.ACTION_REJECTED),
     (LcuResult(None, error="ReadTimeout"), LcuOutcome.REQUEST_FAILED)],
)
def test_lost_bench_race_or_timeout_is_not_reported_as_success(response, status):
    client = client_with(mode(), LcuResult(200, session()), LcuResult(200, GRID), response)
    assert client.select_aram_preferred_champion_decision(("아리",)).status == status
    assert len(writes(client)) == 1


def test_delayed_swap_confirmation_does_not_send_duplicate_post(monkeypatch):
    monkeypatch.setattr("lolmanager.core.lcu_client.time.sleep", lambda _: None)
    client = client_with(
        mode(), LcuResult(200, session()), LcuResult(200, GRID), LcuResult(204),
        LcuResult(200, session()), LcuResult(200, session(current=103)),
    )
    assert client.select_aram_preferred_champion_decision(("아리",)).ok
    assert len(writes(client)) == 1


@pytest.mark.parametrize(
    "confirmed,status",
    [(LcuResult(200, session()), LcuOutcome.REQUEST_FAILED),
     (LcuResult(200, session(current=103, gameId=456)), LcuOutcome.NO_SESSION),
     (LcuResult(404), LcuOutcome.NO_SESSION)],
)
def test_unconfirmed_swap_or_new_session_is_not_success(monkeypatch, confirmed, status):
    monkeypatch.setattr("lolmanager.core.lcu_client.champ_select_action_confirm_timeout_sec", lambda: 0)
    client = client_with(
        mode(), LcuResult(200, session()), LcuResult(200, GRID), LcuResult(204), confirmed,
    )
    assert client.select_aram_preferred_champion_decision(("아리",)).status == status
    assert len(writes(client)) == 1


def test_empty_preferences_issue_no_api_calls():
    client = client_with()
    assert client.select_aram_preferred_champion_decision(()).status == LcuOutcome.NO_CURRENT_ACTION
    client.request.assert_not_called()


@pytest.mark.parametrize(
    "responses,status",
    [((LcuResult(None, error="lockfile unavailable"),), LcuOutcome.UNAVAILABLE),
     ((mode(), LcuResult(404)), LcuOutcome.NO_SESSION),
     ((mode(), LcuResult(200, [])), LcuOutcome.MALFORMED_SESSION),
     ((mode(), LcuResult(200, session()), LcuResult(500)), LcuOutcome.REQUEST_FAILED)],
)
def test_failed_reads_never_fall_back_to_blind_selection(responses, status):
    client = client_with(*responses)
    assert client.select_aram_preferred_champion_decision(("아리",)).status == status
    assert not writes(client)


@pytest.mark.parametrize("enabled,expected", [(True, 2), (False, 0)])
def test_game_start_loop_polls_aram_with_throttle_and_preserves_game_monitor(monkeypatch, enabled, expected):
    now = [0.0]
    monkeypatch.setattr(entrypoint.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(entrypoint.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    phases = iter(["ChampSelect"] * 6 + ["InProgress"])
    monkeypatch.setattr(entrypoint, "_poll_lcu_phase_attempt", lambda *_a, **_kw: SimpleNamespace(
        phase=next(phases), loop_action=LcuLoopAction.ACT_LCU,
    ))
    monkeypatch.setattr(entrypoint, "_LEAGUE_EXIT_GUARD", None)
    monkeypatch.setattr(entrypoint, "_visible_rect_for_image_scan", lambda *_: None)
    monitor = mock.Mock(return_value=False)
    monkeypatch.setattr(entrypoint, "monitor_ingame_and_postgame", monitor)
    lcu = mock.Mock()
    lcu.select_aram_preferred_champion_decision.side_effect = [
        RuntimeError("temporarily disconnected"),
        LcuDecision(LcuOutcome.SUCCESS, value="아리"),
    ]
    result = entrypoint._wait_for_game_start_and_monitor(
        lcu=lcu, tpl_end_next=Path("end"), tpl_end_one_more=Path("more"),
        tpl_find_match=Path("find"), tpl_finding_match=Path("finding"),
        tpl_accept=Path("accept"), tpl_confirm_templates=[], tpl_prepick=Path("prepick"),
        tpl_pick_decline=None, available_roles=[], threshold=0.85,
        confirm_check_interval=0.2, interval_sec=1.0,
        logger=logging.getLogger("test.aram"), continue_after_game=False,
        passive_champ_select=True,
        aram_preferences=AramPreferences(enabled, ("아리", "럭스")),
    )
    assert result is False
    assert lcu.select_aram_preferred_champion_decision.call_count == expected
    monitor.assert_called_once()
