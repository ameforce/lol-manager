from __future__ import annotations

import logging

import pytest

from lolmanager.cli import entrypoint
from lolmanager.core.lcu_client import (
    ChampSelectAction,
    ChampSelectSnapshot,
    LcuDecision,
    LcuLoopAction,
    LcuOutcome,
    champ_select_session_identity,
    champ_select_time_left_seconds,
    completed_champ_select_champion_ids,
)


def test_completed_bans_and_picks_become_unavailable_before_local_turn() -> None:
    session = {
        "actions": [
            [
                {"type": "ban", "completed": True, "championId": 1},
                {"type": "pick", "completed": True, "championId": 2},
                {"type": "pick", "completed": False, "championId": 3},
            ]
        ],
        "bans": {"myTeamBans": [4, 0], "theirTeamBans": [5]},
    }

    assert completed_champ_select_champion_ids(session) == frozenset({1, 2, 4, 5})


def test_champ_select_timer_normalizes_lcu_milliseconds() -> None:
    assert champ_select_time_left_seconds(
        {"timer": {"adjustedTimeLeftInPhase": 27_500}}
    ) == 27.5
    assert champ_select_time_left_seconds(
        {"timer": {"adjustedTimeLeftInPhase": 4.5}}
    ) == 4.5


def test_champ_select_session_identity_uses_game_id() -> None:
    assert champ_select_session_identity({"gameId": 12345}) == "12345"
    assert champ_select_session_identity({"gameId": "67890"}) == "67890"
    assert champ_select_session_identity({}) is None


def test_candidate_choice_moves_to_first_unblocked_reserve_before_turn() -> None:
    assert entrypoint.choose_available_pick_index(
        pick_pool=[("아리", ""), ("오리아나", "")],
        candidate_ids={0: 1, 1: 2},
        unavailable_ids=frozenset({1}),
        current_index=0,
    ) == 1


class _UnavailablePrimaryLcu:
    def __init__(self) -> None:
        self.select_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.snapshot = ChampSelectSnapshot(
            local_player_cell_id=1,
            assigned_position="mid",
            local_player={},
            actions=(
                ChampSelectAction(
                    id=7,
                    type="ban",
                    is_in_progress=True,
                    completed=False,
                ),
            ),
            raw={
                "gameId": 1001,
                "actions": [
                    [
                        {
                            "type": "pick",
                            "completed": True,
                            "championId": 1,
                        }
                    ]
                ],
                "timer": {"adjustedTimeLeftInPhase": 3_000},
            },
        )

    def resolve_champ_select_champion_id_decision(self, name: str) -> LcuDecision:
        return LcuDecision(LcuOutcome.SUCCESS, value={"아리": 1, "오리아나": 2}[name])

    def get_champ_select_snapshot(self) -> LcuDecision:
        return LcuDecision(LcuOutcome.SUCCESS, value=self.snapshot)

    def select_champ_select_champion_decision(
        self, *args: object, **kwargs: object
    ) -> LcuDecision:
        self.select_calls.append((args, kwargs))
        return LcuDecision(LcuOutcome.SUCCESS)


def test_banned_primary_is_replaced_before_local_pick_turn(tmp_path) -> None:
    lcu = _UnavailablePrimaryLcu()

    index, champion, ban, attempt, _ids = (
        entrypoint._wait_for_late_ban_and_reconcile_pick_pool(
            lcu,
            [("아리", "제드"), ("오리아나", "르블랑")],
            current_index=0,
            counter_cache_path=tmp_path / "counter.json",
            role="mid",
            logger=logging.getLogger("test.unavailable-primary"),
            interval_sec=1.0,
        )
    )

    assert (index, champion, ban) == (1, "오리아나", "르블랑")
    assert attempt.completed is True
    assert lcu.select_calls[0] == (
        ("오리아나",),
        {"action_type": "pick", "complete": False},
    )
    assert lcu.select_calls[1] == (
        ("르블랑",),
        {"action_type": "ban", "complete": True},
    )


class _LateBanLcu:
    def __init__(self, *, time_left_ms: int = 20_000) -> None:
        self.select_calls: list[object] = []
        self.snapshot = ChampSelectSnapshot(
            local_player_cell_id=1,
            assigned_position="mid",
            local_player={},
            actions=(
                ChampSelectAction(
                    id=7,
                    type="ban",
                    is_in_progress=True,
                    completed=False,
                ),
            ),
            raw={
                "gameId": 1001,
                "actions": [[]],
                "timer": {"adjustedTimeLeftInPhase": time_left_ms},
            },
        )

    def resolve_champ_select_champion_id_decision(self, name: str) -> LcuDecision:
        return LcuDecision(LcuOutcome.SUCCESS, value={"아리": 1, "오리아나": 2}[name])

    def get_champ_select_snapshot(self) -> LcuDecision:
        return LcuDecision(LcuOutcome.SUCCESS, value=self.snapshot)

    def select_champ_select_champion_decision(self, *args: object, **kwargs: object) -> LcuDecision:
        self.select_calls.append((args, kwargs))
        return LcuDecision(LcuOutcome.SUCCESS)


class _SessionBoundaryLcu(_LateBanLcu):
    def __init__(self, results: list[LcuDecision]) -> None:
        super().__init__(time_left_ms=15_000)
        self.results = list(results)

    def get_champ_select_snapshot(self) -> LcuDecision:
        return self.results.pop(0)


def test_ban_waits_for_late_commit_window_while_reconciling_candidates(
    monkeypatch,
    tmp_path,
) -> None:
    lcu = _LateBanLcu()
    sleeps: list[float] = []

    def stop_after_delay(seconds: float) -> None:
        sleeps.append(seconds)
        raise RuntimeError("stop after delayed-ban wait")

    monkeypatch.setattr(entrypoint, "resolve_ban_name_for_runtime", lambda *_a, **_k: "제드")
    monkeypatch.setattr(entrypoint.time, "sleep", stop_after_delay)

    with pytest.raises(RuntimeError, match="delayed-ban"):
        entrypoint._wait_for_late_ban_and_reconcile_pick_pool(
            lcu,
            [("아리", "제드"), ("오리아나", "르블랑")],
            current_index=0,
            counter_cache_path=tmp_path / "counter.json",
            role="mid",
            logger=logging.getLogger("test.late-ban"),
            interval_sec=1.0,
        )

    assert sleeps == [1.0]
    assert lcu.select_calls == []


def test_ban_starts_with_five_second_budget_before_completion_target(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    assert entrypoint.BAN_COMPLETION_TARGET_SEC == 10.0
    assert (
        entrypoint.BAN_COMMIT_WINDOW_SEC
        - entrypoint.BAN_COMPLETION_TARGET_SEC
        >= 5.0
    )
    lcu = _LateBanLcu(
        time_left_ms=int(entrypoint.BAN_COMMIT_WINDOW_SEC * 1000)
    )
    monkeypatch.setattr(
        entrypoint,
        "resolve_ban_name_for_runtime",
        lambda *_a, **_k: "제드",
    )
    caplog.set_level(logging.INFO)

    _index, _champion, _ban, attempt, _ids = (
        entrypoint._wait_for_late_ban_and_reconcile_pick_pool(
            lcu,
            [("아리", "제드")],
            current_index=0,
            counter_cache_path=tmp_path / "counter.json",
            role="mid",
            logger=logging.getLogger("test.ban-completion-target"),
            interval_sec=1.0,
        )
    )

    assert attempt.completed is True
    assert lcu.select_calls == [
        (("제드",), {"action_type": "ban", "complete": True})
    ]
    assert "target_complete=10.0" in caplog.text
    assert "start_window=15.0" in caplog.text
    assert "completed=True" in caplog.text


def test_ban_does_not_write_when_session_identity_changes_before_commit(
    monkeypatch,
    tmp_path,
) -> None:
    first = _LateBanLcu(time_left_ms=15_000).snapshot
    second = ChampSelectSnapshot(
        local_player_cell_id=first.local_player_cell_id,
        assigned_position=first.assigned_position,
        local_player=first.local_player,
        actions=first.actions,
        raw={**first.raw, "gameId": 1002},
    )
    lcu = _SessionBoundaryLcu(
        [
            LcuDecision(LcuOutcome.SUCCESS, value=first),
            LcuDecision(LcuOutcome.SUCCESS, value=second),
        ]
    )
    monkeypatch.setattr(
        entrypoint,
        "resolve_ban_name_for_runtime",
        lambda *_a, **_k: "제드",
    )

    _index, _champion, _ban, attempt, _ids = (
        entrypoint._wait_for_late_ban_and_reconcile_pick_pool(
            lcu,
            [("아리", "제드")],
            current_index=0,
            counter_cache_path=tmp_path / "counter.json",
            role="mid",
            logger=logging.getLogger("test.session-change-no-write"),
            interval_sec=1.0,
        )
    )

    assert attempt.completed is False
    assert attempt.loop_action == LcuLoopAction.ACT_LCU
    assert attempt.outcome == "session_reset:identity_changed"
    assert lcu.select_calls == []


@pytest.mark.parametrize(
    ("result", "expected_outcome"),
    [
        (
            LcuDecision(LcuOutcome.NO_SESSION, reason="dodge removed session"),
            "session_reset:no_session",
        ),
        (
            LcuDecision(
                LcuOutcome.SUCCESS,
                value=ChampSelectSnapshot(
                    local_player_cell_id=1,
                    assigned_position="mid",
                    local_player={},
                    actions=(),
                    raw={"timer": {"adjustedTimeLeftInPhase": 15_000}},
                ),
            ),
            "session_reset:missing_identity",
        ),
    ],
)
def test_ban_does_not_write_from_unavailable_or_unidentified_session(
    monkeypatch,
    tmp_path,
    result,
    expected_outcome,
) -> None:
    lcu = _SessionBoundaryLcu([result])
    monkeypatch.setattr(
        entrypoint,
        "resolve_ban_name_for_runtime",
        lambda *_a, **_k: "제드",
    )

    _index, _champion, _ban, attempt, _ids = (
        entrypoint._wait_for_late_ban_and_reconcile_pick_pool(
            lcu,
            [("아리", "제드")],
            current_index=0,
            counter_cache_path=tmp_path / "counter.json",
            role="mid",
            logger=logging.getLogger("test.session-unavailable-no-write"),
            interval_sec=1.0,
        )
    )

    assert attempt.outcome == expected_outcome
    assert lcu.select_calls == []
