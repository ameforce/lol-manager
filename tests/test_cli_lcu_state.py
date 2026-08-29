from __future__ import annotations

import logging
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Optional, cast
import unittest
from unittest import mock

from requests import Timeout

from lolmanager.cli import entrypoint
from lolmanager.core.lcu_client import (
    LcuDecision,
    LcuLoopAction,
    PHASE_CHAMP_SELECT,
    PHASE_END_OF_GAME,
    PHASE_IN_PROGRESS,
    PHASE_LOBBY,
    PHASE_MATCHMAKING,
    PHASE_NONE,
    PHASE_PRE_END_OF_GAME,
    PHASE_READY_CHECK,
    PHASE_RECONNECT,
    PHASE_WAITING_FOR_STATS,
    PHASE_WATCH_IN_PROGRESS,
    LcuOutcome,
)
from lolmanager.core.opgg_counter_recommendations import AUTO_BAN_VALUE

ROOT = Path(__file__).resolve().parents[1]


class _FakeLcu:
    def __init__(self, *, accept_result: bool = True) -> None:
        self.accept_result = accept_result
        self.accept_calls = 0

    def accept_ready_check(self) -> bool:
        self.accept_calls += 1
        return self.accept_result


class _FakeReadyDecisionLcu:
    def __init__(self, phase: str | None, result: LcuDecision) -> None:
        self.phase = phase
        self.result = result
        self.accept_calls = 0

    def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> str | None:
        return self.phase

    def consume_phase_transition(self, phase: str | None):
        return None

    def accept_ready_check_decision(self) -> LcuDecision:
        self.accept_calls += 1
        return self.result


class _FakePhaseLcu:
    def __init__(self, phase: str | None) -> None:
        self.phase = phase

    def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> str | None:
        return self.phase

    def consume_phase_transition(self, phase: str):
        return None


class _FakePhaseDecisionLcu:
    def __init__(self, result: LcuDecision) -> None:
        self.result = result

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        return self.result

    def consume_phase_transition(self, phase: str):
        return None


class _FakePhaseDecisionSequenceLcu:
    def __init__(self, results: list[LcuDecision]) -> None:
        self.results = list(results)
        self.phase_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        result = self.results[min(self.phase_calls, len(self.results) - 1)]
        self.phase_calls += 1
        return result

    def consume_phase_transition(self, phase: str):
        return None


class _FakeReadyCheckTransitionLcu:
    def __init__(self, phases: list[str | None]) -> None:
        self.phases = list(phases)
        self.phase_calls = 0
        self.accept_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        phase = self.phases[min(self.phase_calls, len(self.phases) - 1)]
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=phase)

    def consume_phase_transition(self, phase: str):
        return None

    def accept_ready_check_decision(self) -> LcuDecision:
        self.accept_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="accepted")


class _FakeWriteLcu:
    def __init__(
        self,
        *,
        start_result: bool = True,
        select_result: bool = True,
        phase: str | None = PHASE_LOBBY,
    ) -> None:
        self.start_result = start_result
        self.select_result = select_result
        self.phase = phase
        self.start_calls = 0
        self.select_calls: list[dict[str, object]] = []

    def start_matchmaking(self) -> bool:
        self.start_calls += 1
        return self.start_result

    def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> str | None:
        return self.phase

    def consume_phase_transition(self, phase: str | None):
        return None

    def select_champ_select_champion(
        self, champion_name: object, *, action_type: str, complete: bool = False
    ) -> bool:
        self.select_calls.append(
            {
                "champion_name": champion_name,
                "action_type": action_type,
                "complete": complete,
            }
        )
        return self.select_result


class _FakeMatchmakingDecisionLcu:
    def __init__(self, phase: str | None, result: LcuDecision) -> None:
        self.phase = phase
        self.result = result
        self.start_calls = 0

    def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> str | None:
        return self.phase

    def consume_phase_transition(self, phase: str | None):
        return None

    def start_matchmaking_decision(self) -> LcuDecision:
        self.start_calls += 1
        return self.result


class _FakeRoleLcu:
    def __init__(self, result: LcuDecision) -> None:
        self.result = result
        self.position_calls = 0

    def get_local_player_position(self) -> LcuDecision:
        self.position_calls += 1
        return self.result


class _FakeRoleSequenceLcu:
    def __init__(self, results: list[LcuDecision]) -> None:
        self.results = list(results)
        self.position_calls = 0

    def get_local_player_position(self) -> LcuDecision:
        result = self.results[min(self.position_calls, len(self.results) - 1)]
        self.position_calls += 1
        return result


class _FakeDecisionLcu:
    def __init__(self, result: LcuDecision) -> None:
        self.result = result
        self.select_calls: list[dict[str, object]] = []

    def select_champ_select_champion_decision(
        self, champion_name: object, *, action_type: str, complete: bool = False
    ) -> LcuDecision:
        self.select_calls.append(
            {
                "champion_name": champion_name,
                "action_type": action_type,
                "complete": complete,
            }
        )
        return self.result


class _FakeDecisionSequenceLcu:
    def __init__(self, results: list[LcuDecision]) -> None:
        self.results = list(results)
        self.select_calls: list[dict[str, object]] = []

    def select_champ_select_champion_decision(
        self, champion_name: object, *, action_type: str, complete: bool = False
    ) -> LcuDecision:
        self.select_calls.append(
            {
                "champion_name": champion_name,
                "action_type": action_type,
                "complete": complete,
            }
        )
        if len(self.select_calls) <= len(self.results):
            return self.results[len(self.select_calls) - 1]
        return self.results[-1]


class _FakeChampSelectRetryPhaseLcu(_FakeDecisionLcu):
    def __init__(self, phase: str) -> None:
        super().__init__(LcuDecision(LcuOutcome.NO_CURRENT_ACTION))
        self.phase = phase
        self.phase_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=self.phase)

    def consume_phase_transition(self, phase: str):
        return None


class _FakeLocalActionLcu:
    def __init__(self, active_action_type: str | None) -> None:
        self.active_action_type = active_action_type
        self.action_calls: list[dict[str, object]] = []

    def get_local_action_state(
        self, action_type: str, *, require_in_progress: bool = False
    ) -> LcuDecision:
        self.action_calls.append(
            {
                "action_type": action_type,
                "require_in_progress": require_in_progress,
            }
        )
        if action_type == self.active_action_type and require_in_progress:
            return LcuDecision(LcuOutcome.SUCCESS, value=object())
        return LcuDecision(LcuOutcome.NO_CURRENT_ACTION)


class _FakePostgameHonorLcu:
    def __init__(self, result: LcuDecision) -> None:
        self.result = result
        self.phase_calls = 0
        self.honor_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_PRE_END_OF_GAME)

    def consume_phase_transition(self, phase: str):
        return None

    def is_end_of_game_stats_available(self) -> bool:
        return False

    def honor_random_eligible_teammate_decision(self) -> LcuDecision:
        self.honor_calls += 1
        return self.result


class _FakePostgameContinueLcu:
    def __init__(self, result: LcuDecision) -> None:
        self.result = result
        self.phase_calls = 0
        self.dismiss_stats_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME)

    def consume_phase_transition(self, phase: str):
        return None

    def is_end_of_game_stats_available(self) -> bool:
        return True

    def dismiss_end_of_game_stats_decision(self) -> LcuDecision:
        self.dismiss_stats_calls += 1
        return self.result


class _FakePostgameContinueToLobbyLcu:
    def __init__(self) -> None:
        self.phase_calls = 0
        self.dismiss_stats_calls = 0
        self.start_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        if self.phase_calls == 1:
            return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME)
        return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_LOBBY)

    def consume_phase_transition(self, phase: str):
        return None

    def is_end_of_game_stats_available(self) -> bool:
        return self.phase_calls <= 1

    def dismiss_end_of_game_stats_decision(self) -> LcuDecision:
        self.dismiss_stats_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="dismissed")

    def start_matchmaking_decision(self) -> LcuDecision:
        self.start_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="matchmaking search accepted")


class _FakePostgameContinueToNoneLcu:
    def __init__(
        self,
        start_result: LcuDecision,
        *,
        play_again_result: LcuDecision | None = None,
    ) -> None:
        self.start_result = start_result
        self.play_again_result = play_again_result or LcuDecision(
            LcuOutcome.UNSUPPORTED, reason="play again unavailable"
        )
        self.phase_calls = 0
        self.dismiss_stats_calls = 0
        self.play_again_calls = 0
        self.start_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        if self.phase_calls == 1:
            return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME)
        return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_NONE)

    def consume_phase_transition(self, phase: str):
        return None

    def is_end_of_game_stats_available(self) -> bool:
        return self.phase_calls <= 1

    def dismiss_end_of_game_stats_decision(self) -> LcuDecision:
        self.dismiss_stats_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="dismissed")

    def play_again_decision(self) -> LcuDecision:
        self.play_again_calls += 1
        return self.play_again_result

    def start_matchmaking_decision(self) -> LcuDecision:
        self.start_calls += 1
        return self.start_result


class _FakePostgamePlayAgainToLobbyLcu:
    def __init__(self, start_result: Optional[LcuDecision] = None) -> None:
        self.phase_calls = 0
        self.play_again_calls = 0
        self.dismiss_stats_calls = 0
        self.start_calls = 0
        self.start_result = start_result or LcuDecision(
            LcuOutcome.SUCCESS, reason="matchmaking search accepted"
        )

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        if self.phase_calls == 1:
            return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME)
        if self.phase_calls in {2, 3}:
            return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_LOBBY)
        return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_CHAMP_SELECT)

    def consume_phase_transition(self, phase: str):
        return None

    def play_again_decision(self) -> LcuDecision:
        self.play_again_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="play again accepted")

    def dismiss_end_of_game_stats_decision(self) -> LcuDecision:
        self.dismiss_stats_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="dismissed")

    def is_end_of_game_stats_available(self) -> bool:
        return self.phase_calls == 1

    def start_matchmaking_decision(self) -> LcuDecision:
        self.start_calls += 1
        return self.start_result


class _FakePostgameRejectedPlayAgainToLobbyLcu:
    def __init__(self) -> None:
        self.phase_calls = 0
        self.play_again_calls = 0
        self.dismiss_stats_calls = 0
        self.start_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        if self.dismiss_stats_calls == 0:
            return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME)
        return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_LOBBY)

    def consume_phase_transition(self, phase: str):
        return None

    def play_again_decision(self) -> LcuDecision:
        self.play_again_calls += 1
        return LcuDecision(LcuOutcome.ACTION_REJECTED, reason="play again rejected")

    def dismiss_end_of_game_stats_decision(self) -> LcuDecision:
        self.dismiss_stats_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="dismissed")

    def is_end_of_game_stats_available(self) -> bool:
        return self.dismiss_stats_calls == 0

    def start_matchmaking_decision(self) -> LcuDecision:
        self.start_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="matchmaking search accepted")


class _FakePostgameLobbyStartLcu:
    def __init__(self) -> None:
        self.phase_calls = 0
        self.start_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_LOBBY)

    def consume_phase_transition(self, phase: str):
        return None

    def start_matchmaking_decision(self) -> LcuDecision:
        self.start_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="matchmaking search accepted")


class _FakePostgamePreEndToLobbyLcu:
    def __init__(self) -> None:
        self.phase_calls = 0
        self.honor_calls = 0
        self.dismiss_stats_calls = 0
        self.start_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        if self.phase_calls <= 2:
            return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_PRE_END_OF_GAME)
        if self.phase_calls == 3:
            return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME)
        return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_LOBBY)

    def consume_phase_transition(self, phase: str):
        return None

    def is_end_of_game_stats_available(self) -> bool:
        return self.phase_calls == 3

    def honor_random_eligible_teammate_decision(self) -> LcuDecision:
        self.honor_calls += 1
        return LcuDecision(LcuOutcome.NO_CURRENT_ACTION)

    def dismiss_end_of_game_stats_decision(self) -> LcuDecision:
        self.dismiss_stats_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="dismissed")

    def start_matchmaking_decision(self) -> LcuDecision:
        self.start_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="matchmaking search accepted")


class _FakePostgamePreEndRankedNotificationLcu:
    def __init__(self) -> None:
        self.phase_calls = 0
        self.honor_calls = 0
        self.dismiss_calls = 0
        self.dismiss_stats_calls = 0
        self.start_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        if self.phase_calls == 1:
            return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_PRE_END_OF_GAME)
        if self.phase_calls == 2:
            return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME)
        return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_LOBBY)

    def consume_phase_transition(self, phase: str):
        return None

    def honor_random_eligible_teammate_decision(self) -> LcuDecision:
        self.honor_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="honor submitted")

    def dismiss_blocking_modal_decision(self) -> LcuDecision:
        self.dismiss_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="ranked notification acknowledged")

    def is_end_of_game_stats_available(self) -> bool:
        return self.phase_calls == 2

    def dismiss_end_of_game_stats_decision(self) -> LcuDecision:
        self.dismiss_stats_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="dismissed")

    def start_matchmaking_decision(self) -> LcuDecision:
        self.start_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="matchmaking search accepted")


class _FakeBlockingModalLcu:
    def __init__(self, result: LcuDecision) -> None:
        self.result = result
        self.dismiss_calls = 0

    def dismiss_blocking_modal_decision(self) -> LcuDecision:
        self.dismiss_calls += 1
        return self.result


class _FakePhaseBlockingModalLcu(_FakePhaseLcu):
    def __init__(self, phase: str | None, result: LcuDecision) -> None:
        super().__init__(phase)
        self.result = result
        self.dismiss_calls = 0

    def dismiss_blocking_modal_decision(self) -> LcuDecision:
        self.dismiss_calls += 1
        return self.result


class _FakeCliChampionConfig:
    path = Path("champions.json")

    def get(self, role: str):
        if role == "mid":
            return {"champion": "아리", "ban": ""}
        return {}

    def get_reserve_picks(self, role: str):
        return []


class _FakeReserveCliChampionConfig:
    path = Path("champions.json")

    def get(self, role: str):
        if role == "mid":
            return {"champion": "카타리나", "ban": ""}
        return {}

    def get_reserve_picks(self, role: str):
        if role == "mid":
            return [("오리아나", ""), ("트위스티드 페이트", "")]
        return []


class _FakeCliChampSelectLcu:
    def __init__(self) -> None:
        self.phase_calls = 0
        self.position_calls = 0
        self.select_calls: list[dict[str, object]] = []

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        phases = [PHASE_LOBBY, PHASE_CHAMP_SELECT, PHASE_CHAMP_SELECT]
        phase = phases[min(self.phase_calls, len(phases) - 1)]
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=phase)

    def consume_phase_transition(self, phase: str):
        return None

    def get_local_player_position(self) -> LcuDecision:
        self.position_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value="mid")

    def select_champ_select_champion_decision(
        self, champion_name: object, *, action_type: str, complete: bool = False
    ) -> LcuDecision:
        self.select_calls.append(
            {
                "champion_name": champion_name,
                "action_type": action_type,
                "complete": complete,
            }
        )
        if len(self.select_calls) == 1:
            return LcuDecision(LcuOutcome.SUCCESS, reason="prepick accepted")
        return LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout")


class _FakeReserveFallbackLcu:
    def __init__(self, complete_results: list[LcuDecision]) -> None:
        self.complete_results = list(complete_results)
        self.phase_calls = 0
        self.position_calls = 0
        self.select_calls: list[dict[str, object]] = []

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        phases = [PHASE_LOBBY, PHASE_CHAMP_SELECT, PHASE_CHAMP_SELECT]
        phase = phases[min(self.phase_calls, len(phases) - 1)]
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=phase)

    def consume_phase_transition(self, phase: str):
        return None

    def get_local_player_position(self) -> LcuDecision:
        self.position_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value="mid")

    def get_local_action_state(
        self, action_type: str, *, require_in_progress: bool = False
    ) -> LcuDecision:
        return LcuDecision(LcuOutcome.NO_CURRENT_ACTION)

    def select_champ_select_champion_decision(
        self, champion_name: object, *, action_type: str, complete: bool = False
    ) -> LcuDecision:
        self.select_calls.append(
            {
                "champion_name": champion_name,
                "action_type": action_type,
                "complete": complete,
            }
        )
        if not complete:
            return LcuDecision(LcuOutcome.SUCCESS, reason="prepick accepted")
        complete_call_count = sum(
            1 for call in self.select_calls if bool(call["complete"])
        )
        result_index = min(complete_call_count - 1, len(self.complete_results) - 1)
        return self.complete_results[result_index]


class _FakeCliReadyCheckRematchLcu(_FakeCliChampSelectLcu):
    def __init__(self) -> None:
        super().__init__()
        self.accept_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        phases = [
            PHASE_LOBBY,
            PHASE_READY_CHECK,
            PHASE_MATCHMAKING,
            PHASE_READY_CHECK,
            PHASE_CHAMP_SELECT,
            PHASE_CHAMP_SELECT,
            PHASE_CHAMP_SELECT,
        ]
        phase = phases[min(self.phase_calls, len(phases) - 1)]
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=phase)

    def accept_ready_check_decision(self) -> LcuDecision:
        self.accept_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="ready accepted")


class _FakeCliChampSelectDodgeRematchLcu(_FakeCliReadyCheckRematchLcu):
    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        phases = [
            PHASE_LOBBY,
            PHASE_CHAMP_SELECT,
            PHASE_MATCHMAKING,
            PHASE_MATCHMAKING,
            PHASE_READY_CHECK,
            PHASE_MATCHMAKING,
            PHASE_READY_CHECK,
            PHASE_CHAMP_SELECT,
            PHASE_CHAMP_SELECT,
            PHASE_CHAMP_SELECT,
        ]
        phase = phases[min(self.phase_calls, len(phases) - 1)]
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=phase)

    def get_local_player_position(self) -> LcuDecision:
        self.position_calls += 1
        if self.position_calls == 1:
            return LcuDecision(LcuOutcome.NO_POSITION, reason="dodge before role")
        return LcuDecision(LcuOutcome.SUCCESS, value="mid")


class _FakeCliChampSelectDodgeRematchBanLcu(
    _FakeCliChampSelectDodgeRematchLcu
):
    def __init__(self) -> None:
        super().__init__()
        self.last_champ_select_action_timings = {
            "assignment_elapsed_sec": 0.1,
            "completion_elapsed_sec": 0.1,
        }

    def select_champ_select_champion_decision(
        self, champion_name: object, *, action_type: str, complete: bool = False
    ) -> LcuDecision:
        self.select_calls.append(
            {
                "champion_name": champion_name,
                "action_type": action_type,
                "complete": complete,
            }
        )
        return LcuDecision(LcuOutcome.SUCCESS, reason="fresh rematch action accepted")


class _FakeCliFinalPickDodgeRematchLcu(_FakeCliChampSelectLcu):
    def __init__(self) -> None:
        super().__init__()
        self.accept_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        phases = [
            PHASE_LOBBY,
            PHASE_CHAMP_SELECT,
            PHASE_CHAMP_SELECT,
            PHASE_LOBBY,
            PHASE_MATCHMAKING,
            PHASE_READY_CHECK,
            PHASE_CHAMP_SELECT,
        ]
        phase = phases[min(self.phase_calls, len(phases) - 1)]
        self.phase_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, value=phase)

    def accept_ready_check_decision(self) -> LcuDecision:
        self.accept_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="ready accepted")

    def select_champ_select_champion_decision(
        self, champion_name: object, *, action_type: str, complete: bool = False
    ) -> LcuDecision:
        self.select_calls.append(
            {
                "champion_name": champion_name,
                "action_type": action_type,
                "complete": complete,
            }
        )
        call_count = len(self.select_calls)
        if call_count in {1, 3}:
            return LcuDecision(LcuOutcome.SUCCESS, reason="prepick accepted")
        if call_count == 2:
            return LcuDecision(LcuOutcome.NO_SESSION, reason="dodge removed session")
        raise RuntimeError("rematch final pick reached")


class _FakeCliReservePickDodgeLcu(_FakeReserveFallbackLcu):
    def __init__(self) -> None:
        super().__init__(
            [
                LcuDecision(LcuOutcome.ACTION_REJECTED, reason="primary unavailable"),
                LcuDecision(LcuOutcome.NO_SESSION, reason="dodge removed session"),
            ]
        )

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        phases: list[str | Exception] = [
            PHASE_LOBBY,
            PHASE_CHAMP_SELECT,
            PHASE_CHAMP_SELECT,
            PHASE_CHAMP_SELECT,
            PHASE_MATCHMAKING,
            RuntimeError("outer cycle restarted after reserve dodge"),
        ]
        phase = phases[min(self.phase_calls, len(phases) - 1)]
        self.phase_calls += 1
        if isinstance(phase, Exception):
            raise phase
        return LcuDecision(LcuOutcome.SUCCESS, value=phase)


class _FakeCliManualQueueCancelLcu:
    def __init__(self) -> None:
        self.phase_calls = 0
        self.start_calls = 0
        self.post_start_phase_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        self.phase_calls += 1
        if self.start_calls == 0:
            phase = PHASE_LOBBY
        else:
            self.post_start_phase_calls += 1
            phase = (
                PHASE_MATCHMAKING
                if self.post_start_phase_calls == 1
                else PHASE_LOBBY
            )
        return LcuDecision(LcuOutcome.SUCCESS, value=phase)

    def consume_phase_transition(self, phase: str):
        return None

    def start_matchmaking_decision(self) -> LcuDecision:
        self.start_calls += 1
        if self.start_calls > 1:
            raise AssertionError(
                f"manual queue cancel must not requeue (phase_calls={self.phase_calls})"
            )
        return LcuDecision(LcuOutcome.SUCCESS, reason="matchmaking search accepted")


class _FakeChampSelectPhaseGuardLcu:
    def __init__(self, phases: list[LcuDecision | str]) -> None:
        self.phases = list(phases)
        self.phase_calls = 0
        self.accept_calls = 0

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        phase = self.phases[min(self.phase_calls, len(self.phases) - 1)]
        self.phase_calls += 1
        if isinstance(phase, LcuDecision):
            return phase
        return LcuDecision(LcuOutcome.SUCCESS, value=phase)

    def consume_phase_transition(self, phase: str):
        return None

    def accept_ready_check_decision(self) -> LcuDecision:
        self.accept_calls += 1
        return LcuDecision(LcuOutcome.SUCCESS, reason="ready accepted")


class CliLcuStateTests(unittest.TestCase):
    def _run_cli_champ_select_until_runtime_sleep(
        self, fake_lcu: _FakeReserveFallbackLcu
    ) -> tuple[mock.Mock, mock.Mock, mock.Mock]:
        minimized_rect = (-32000, -32000, -31900, -31900)
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.UNKNOWN
        entrypoint.RUNTIME_STATE["is_my_pick_turn"] = False

        def stop_at_next_sleep(_seconds: float) -> None:
            raise RuntimeError("stop after champ-select sleep")

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(
                    should_exit=mock.Mock(return_value=False)
                ),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=fake_lcu),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeReserveCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                side_effect=[(0, 0, 1280, 720), minimized_rect, minimized_rect],
            ),
            mock.patch.object(
                entrypoint,
                "_dismiss_blocking_modal_lcu_attempt",
                return_value=entrypoint.LcuActionAttempt(
                    False, LcuLoopAction.FALLBACK_IMAGE, "not_found"
                ),
            ),
            mock.patch.object(entrypoint, "resolve_ban_name_for_runtime", return_value=""),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "click_relative") as click_relative,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=stop_at_next_sleep,
            ),
            self.assertRaises(RuntimeError),
        ):
            entrypoint.cli_main([])

        return search, click_relative, click_screen

    def test_cli_main_rejects_unknown_option_before_runtime_setup(self) -> None:
        fake_gui = types.ModuleType("lolmanager.gui.config_gui")
        fake_gui.run_config_gui = mock.Mock()

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ) as configure_logging,
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.dict(sys.modules, {"lolmanager.gui.config_gui": fake_gui}),
            self.assertRaises(SystemExit) as raised,
        ):
            entrypoint.cli_main(["--config-gui", "--debgu"])

        self.assertEqual(raised.exception.code, 2)
        configure_logging.assert_not_called()
        fake_gui.run_config_gui.assert_not_called()

    def test_cli_main_stops_after_authoritative_manual_queue_cancel(self) -> None:
        fake_lcu = _FakeCliManualQueueCancelLcu()
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.UNKNOWN

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(should_exit=mock.Mock(return_value=False)),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=fake_lcu),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint, "find_league_window_rect", return_value=(0, 0, 1280, 720)
            ),
            mock.patch.object(
                entrypoint,
                "_dismiss_blocking_modal_lcu_attempt",
                return_value=entrypoint.LcuActionAttempt(
                    False, LcuLoopAction.FALLBACK_IMAGE, "not_found"
                ),
            ),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint.time, "sleep", return_value=None),
            self.assertLogs("lolmanager", level="INFO") as logs,
        ):
            entrypoint.cli_main([])

        self.assertEqual(fake_lcu.start_calls, 1)
        self.assertGreaterEqual(fake_lcu.phase_calls, 7)
        self.assertIn("사용자 매칭 취소", "\n".join(logs.output))
        search.assert_not_called()

    def test_matchmaking_cancel_requires_authoritative_lobby_after_finding(
        self,
    ) -> None:
        lobby = entrypoint.MatchPollAttempt(
            False, False, LcuLoopAction.ACT_LCU, "lobby", PHASE_LOBBY
        )
        waiting_lobby = entrypoint.MatchPollAttempt(
            False,
            False,
            LcuLoopAction.WAIT_AUTHORITATIVE,
            "request_failed",
            PHASE_LOBBY,
        )
        matchmaking = entrypoint.MatchPollAttempt(
            False,
            True,
            LcuLoopAction.ACT_LCU,
            "matchmaking",
            PHASE_MATCHMAKING,
        )

        self.assertTrue(entrypoint._is_authoritative_matchmaking_cancel(True, lobby))
        self.assertFalse(entrypoint._is_authoritative_matchmaking_cancel(None, lobby))
        self.assertFalse(entrypoint._is_authoritative_matchmaking_cancel(False, lobby))
        self.assertFalse(
            entrypoint._is_authoritative_matchmaking_cancel(True, waiting_lobby)
        )
        self.assertFalse(
            entrypoint._is_authoritative_matchmaking_cancel(True, matchmaking)
        )

    def test_cli_main_routes_postgame_before_cycle_ui_wait(self) -> None:
        postgame_message = "postgame routed before cycle ui fallback"

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(
                    should_exit=mock.Mock(return_value=False)
                ),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=object()),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint,
                "_poll_lcu_phase_attempt",
                side_effect=[
                    entrypoint.PhaseLcuAttempt(
                        PHASE_LOBBY, LcuLoopAction.ACT_LCU, "startup"
                    ),
                    entrypoint.PhaseLcuAttempt(
                        PHASE_END_OF_GAME, LcuLoopAction.ACT_LCU, "postgame"
                    ),
                ],
            ),
            mock.patch.object(
                entrypoint,
                "_visible_rect_or_wait",
                side_effect=RuntimeError("cycle start ui fallback should not wait"),
            ) as visible_wait,
            mock.patch.object(
                entrypoint,
                "_visible_rect_for_image_scan",
                return_value=(0, 0, 1280, 720),
            ) as visible_scan,
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(entrypoint, "_dismiss_blocking_modal_lcu_attempt") as modal_lcu,
            mock.patch.object(
                entrypoint,
                "process_postgame",
                side_effect=RuntimeError(postgame_message),
            ) as process_postgame,
            self.assertRaisesRegex(RuntimeError, postgame_message),
        ):
            entrypoint.cli_main([])

        self.assertEqual(visible_scan.call_count, 1)
        visible_wait.assert_not_called()
        modal_lcu.assert_not_called()
        process_postgame.assert_called_once()

    def test_cli_main_minimized_ingame_reaches_monitor_without_startup_visible_wait(
        self,
    ) -> None:
        monitor_message = "ingame monitor reached from minimized startup"

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(
                    should_exit=mock.Mock(return_value=False)
                ),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=object()),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint,
                "_poll_lcu_phase_attempt",
                side_effect=[
                    entrypoint.PhaseLcuAttempt(
                        PHASE_IN_PROGRESS, LcuLoopAction.ACT_LCU, "startup"
                    ),
                    entrypoint.PhaseLcuAttempt(
                        PHASE_IN_PROGRESS, LcuLoopAction.ACT_LCU, "cycle"
                    ),
                ],
            ),
            mock.patch.object(
                entrypoint,
                "_visible_rect_or_wait",
                side_effect=RuntimeError("startup visible wait should not block ingame"),
            ) as visible_wait,
            mock.patch.object(
                entrypoint, "_visible_rect_for_image_scan", return_value=None
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ) as select_images,
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(entrypoint, "_dismiss_blocking_modal_lcu_attempt"),
            mock.patch.object(
                entrypoint,
                "monitor_ingame_and_postgame",
                side_effect=RuntimeError(monitor_message),
            ) as monitor,
            self.assertRaisesRegex(RuntimeError, monitor_message),
        ):
            entrypoint.cli_main([])

        visible_wait.assert_not_called()
        select_images.assert_called_once_with(1280)
        monitor.assert_called_once()

    def test_cli_main_minimized_champ_select_reaches_lcu_pick_before_image(
        self,
    ) -> None:
        fake_lcu = _FakeCliChampSelectLcu()
        minimized_rect = (-32000, -32000, -31900, -31900)

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(
                    should_exit=mock.Mock(return_value=False)
                ),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=fake_lcu),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                side_effect=[(0, 0, 1280, 720), minimized_rect, minimized_rect],
            ),
            mock.patch.object(
                entrypoint,
                "_dismiss_blocking_modal_lcu_attempt",
                return_value=entrypoint.LcuActionAttempt(
                    False, LcuLoopAction.FALLBACK_IMAGE, "not_found"
                ),
            ),
            mock.patch.object(entrypoint, "resolve_ban_name_for_runtime", return_value=""),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "click_relative") as click_relative,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=RuntimeError("stop after minimized pick wait"),
            ),
            self.assertRaisesRegex(RuntimeError, "stop after minimized pick wait"),
        ):
            entrypoint.cli_main([])

        self.assertEqual(fake_lcu.position_calls, 1)
        self.assertEqual(
            fake_lcu.select_calls,
            [
                {"champion_name": "아리", "action_type": "pick", "complete": False},
                {"champion_name": "아리", "action_type": "pick", "complete": True},
            ],
        )
        search.assert_not_called()
        click_relative.assert_not_called()
        click_screen.assert_not_called()

    def test_cli_main_reserve_pick_when_primary_lcu_action_rejected(
        self,
    ) -> None:
        fake_lcu = _FakeReserveFallbackLcu(
            [
                LcuDecision(
                    LcuOutcome.ACTION_REJECTED,
                    reason="primary champion unavailable",
                ),
                LcuDecision(LcuOutcome.SUCCESS, reason="reserve accepted"),
            ]
        )

        search, click_relative, click_screen = (
            self._run_cli_champ_select_until_runtime_sleep(fake_lcu)
        )

        self.assertEqual(
            fake_lcu.select_calls[:3],
            [
                {"champion_name": "카타리나", "action_type": "pick", "complete": False},
                {"champion_name": "카타리나", "action_type": "pick", "complete": True},
                {"champion_name": "오리아나", "action_type": "pick", "complete": True},
            ],
        )
        search.assert_not_called()
        click_relative.assert_not_called()
        click_screen.assert_not_called()

    def test_cli_main_reserve_pick_tries_second_reserve_when_first_reserve_rejected(
        self,
    ) -> None:
        fake_lcu = _FakeReserveFallbackLcu(
            [
                LcuDecision(
                    LcuOutcome.ACTION_REJECTED,
                    reason="primary champion unavailable",
                ),
                LcuDecision(
                    LcuOutcome.ACTION_REJECTED,
                    reason="first reserve unavailable",
                ),
                LcuDecision(LcuOutcome.SUCCESS, reason="second reserve accepted"),
            ]
        )

        search, click_relative, click_screen = (
            self._run_cli_champ_select_until_runtime_sleep(fake_lcu)
        )

        self.assertEqual(
            fake_lcu.select_calls[:4],
            [
                {"champion_name": "카타리나", "action_type": "pick", "complete": False},
                {"champion_name": "카타리나", "action_type": "pick", "complete": True},
                {"champion_name": "오리아나", "action_type": "pick", "complete": True},
                {
                    "champion_name": "트위스티드 페이트",
                    "action_type": "pick",
                    "complete": True,
                },
            ],
        )
        search.assert_not_called()
        click_relative.assert_not_called()
        click_screen.assert_not_called()

    def test_cli_main_ready_check_rematch_reaches_lcu_pick_before_image(
        self,
    ) -> None:
        fake_lcu = _FakeCliReadyCheckRematchLcu()
        minimized_rect = (-32000, -32000, -31900, -31900)
        entrypoint._last_lcu_ready_accept_at.clear()

        def stop_after_pick_wait(_seconds: float) -> None:
            if len(fake_lcu.select_calls) >= 2:
                raise RuntimeError("stop after rematch pick wait")

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(entrypoint, "LCU_READY_ACCEPT_COOLDOWN_SEC", 0.0),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(
                    should_exit=mock.Mock(return_value=False)
                ),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=fake_lcu),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                side_effect=[(0, 0, 1280, 720), minimized_rect, minimized_rect],
            ),
            mock.patch.object(
                entrypoint,
                "_dismiss_blocking_modal_lcu_attempt",
                return_value=entrypoint.LcuActionAttempt(
                    False, LcuLoopAction.FALLBACK_IMAGE, "not_found"
                ),
            ),
            mock.patch.object(entrypoint, "resolve_ban_name_for_runtime", return_value=""),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "click_relative") as click_relative,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=stop_after_pick_wait,
            ),
            self.assertRaisesRegex(RuntimeError, "stop after rematch pick wait"),
        ):
            entrypoint.cli_main([])

        self.assertGreaterEqual(fake_lcu.accept_calls, 1)
        self.assertGreaterEqual(fake_lcu.phase_calls, 5)
        self.assertEqual(fake_lcu.position_calls, 1)
        self.assertEqual(
            fake_lcu.select_calls,
            [
                {"champion_name": "아리", "action_type": "pick", "complete": False},
                {"champion_name": "아리", "action_type": "pick", "complete": True},
            ],
        )
        search.assert_not_called()
        click_relative.assert_not_called()
        click_screen.assert_not_called()

    def test_cli_main_champ_select_dodge_rematch_reaches_lcu_pick_before_image(
        self,
    ) -> None:
        fake_lcu = _FakeCliChampSelectDodgeRematchLcu()
        minimized_rect = (-32000, -32000, -31900, -31900)
        entrypoint._last_lcu_ready_accept_at.clear()

        def stop_after_pick_wait(_seconds: float) -> None:
            if len(fake_lcu.select_calls) >= 2:
                raise RuntimeError("stop after dodge rematch pick wait")

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(entrypoint, "LCU_READY_ACCEPT_COOLDOWN_SEC", 0.0),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(
                    should_exit=mock.Mock(return_value=False)
                ),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=fake_lcu),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                side_effect=[(0, 0, 1280, 720), minimized_rect, minimized_rect],
            ),
            mock.patch.object(
                entrypoint,
                "_dismiss_blocking_modal_lcu_attempt",
                return_value=entrypoint.LcuActionAttempt(
                    False, LcuLoopAction.FALLBACK_IMAGE, "not_found"
                ),
            ),
            mock.patch.object(entrypoint, "resolve_ban_name_for_runtime", return_value=""),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "click_relative") as click_relative,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=stop_after_pick_wait,
            ),
            self.assertRaisesRegex(RuntimeError, "stop after dodge rematch pick wait"),
        ):
            entrypoint.cli_main([])

        self.assertGreaterEqual(fake_lcu.accept_calls, 2)
        self.assertGreaterEqual(fake_lcu.phase_calls, 8)
        self.assertEqual(fake_lcu.position_calls, 2)
        self.assertEqual(
            fake_lcu.select_calls,
            [
                {"champion_name": "아리", "action_type": "pick", "complete": False},
                {"champion_name": "아리", "action_type": "pick", "complete": True},
            ],
        )
        search.assert_not_called()
        click_relative.assert_not_called()
        click_screen.assert_not_called()

    def test_cli_main_dodge_rematch_runs_fresh_ban_before_final_pick(self) -> None:
        fake_lcu = _FakeCliChampSelectDodgeRematchBanLcu()
        minimized_rect = (-32000, -32000, -31900, -31900)
        entrypoint._last_lcu_ready_accept_at.clear()

        def stop_after_rematch_actions(_seconds: float) -> None:
            if len(fake_lcu.select_calls) >= 3:
                raise RuntimeError("stop after fresh rematch ban")

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(entrypoint, "LCU_READY_ACCEPT_COOLDOWN_SEC", 0.0),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(should_exit=mock.Mock(return_value=False)),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=fake_lcu),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                side_effect=[(0, 0, 1280, 720), minimized_rect, minimized_rect],
            ),
            mock.patch.object(
                entrypoint,
                "_dismiss_blocking_modal_lcu_attempt",
                return_value=entrypoint.LcuActionAttempt(
                    False, LcuLoopAction.FALLBACK_IMAGE, "not_found"
                ),
            ),
            mock.patch.object(
                entrypoint, "resolve_ban_name_for_runtime", return_value="제드"
            ),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "click_relative") as click_relative,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=stop_after_rematch_actions,
            ),
            self.assertRaisesRegex(RuntimeError, "fresh rematch ban"),
        ):
            entrypoint.cli_main([])

        self.assertEqual(
            fake_lcu.select_calls[:3],
            [
                {"champion_name": "아리", "action_type": "pick", "complete": False},
                {"champion_name": "제드", "action_type": "ban", "complete": True},
                {"champion_name": "아리", "action_type": "pick", "complete": True},
            ],
        )
        search.assert_not_called()
        click_relative.assert_not_called()
        click_screen.assert_not_called()

    def test_cli_main_final_pick_no_session_recovers_lobby_matchmaking_ready_check(
        self,
    ) -> None:
        fake_lcu = _FakeCliFinalPickDodgeRematchLcu()
        minimized_rect = (-32000, -32000, -31900, -31900)
        entrypoint._last_lcu_ready_accept_at.clear()

        def fail_if_final_pick_waits(seconds: float) -> None:
            if seconds == 1.0 and len(fake_lcu.select_calls) == 2:
                raise RuntimeError("stuck in final pick wait")

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(entrypoint, "LCU_READY_ACCEPT_COOLDOWN_SEC", 0.0),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(should_exit=mock.Mock(return_value=False)),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=fake_lcu),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                side_effect=[(0, 0, 1280, 720), minimized_rect, minimized_rect],
            ),
            mock.patch.object(
                entrypoint,
                "_dismiss_blocking_modal_lcu_attempt",
                return_value=entrypoint.LcuActionAttempt(
                    False, LcuLoopAction.FALLBACK_IMAGE, "not_found"
                ),
            ),
            mock.patch.object(entrypoint, "resolve_ban_name_for_runtime", return_value=""),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "click_relative") as click_relative,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
            mock.patch.object(
                entrypoint.time, "sleep", side_effect=fail_if_final_pick_waits
            ),
            self.assertRaisesRegex(RuntimeError, "rematch final pick reached"),
        ):
            entrypoint.cli_main([])

        self.assertEqual(fake_lcu.accept_calls, 1)
        self.assertEqual(fake_lcu.position_calls, 2)
        self.assertEqual(fake_lcu.phase_calls, 7)
        self.assertEqual(len(fake_lcu.select_calls), 4)
        search.assert_not_called()
        click_relative.assert_not_called()
        click_screen.assert_not_called()

    def test_cli_main_reserve_pick_no_session_restarts_outer_cycle(self) -> None:
        fake_lcu = _FakeCliReservePickDodgeLcu()
        minimized_rect = (-32000, -32000, -31900, -31900)

        def fail_if_reserve_waits(seconds: float) -> None:
            if seconds == 1.0 and len(fake_lcu.select_calls) >= 3:
                raise RuntimeError("stuck in reserve pick wait")

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(should_exit=mock.Mock(return_value=False)),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=fake_lcu),
            mock.patch.object(
                entrypoint, "ChampionConfig", _FakeReserveCliChampionConfig
            ),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                side_effect=[(0, 0, 1280, 720), minimized_rect, minimized_rect],
            ),
            mock.patch.object(
                entrypoint,
                "_dismiss_blocking_modal_lcu_attempt",
                return_value=entrypoint.LcuActionAttempt(
                    False, LcuLoopAction.FALLBACK_IMAGE, "not_found"
                ),
            ),
            mock.patch.object(entrypoint, "resolve_ban_name_for_runtime", return_value=""),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "click_relative") as click_relative,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
            mock.patch.object(
                entrypoint.time, "sleep", side_effect=fail_if_reserve_waits
            ),
            self.assertRaisesRegex(
                RuntimeError, "outer cycle restarted after reserve dodge"
            ),
        ):
            entrypoint.cli_main([])

        self.assertEqual(fake_lcu.position_calls, 1)
        self.assertEqual(fake_lcu.phase_calls, 6)
        self.assertEqual(len(fake_lcu.select_calls), 3)
        search.assert_not_called()
        click_relative.assert_not_called()
        click_screen.assert_not_called()

    def test_cli_main_ready_check_timeout_restarts_outer_cycle(self) -> None:
        poll_phase_results: list[entrypoint.PhaseLcuAttempt | Exception] = [
            entrypoint.PhaseLcuAttempt(PHASE_LOBBY, LcuLoopAction.ACT_LCU, "startup"),
            entrypoint.PhaseLcuAttempt(PHASE_LOBBY, LcuLoopAction.ACT_LCU, "cycle"),
            RuntimeError("outer cycle restarted after timeout"),
        ]

        def poll_phase(*_args: object, **_kwargs: object) -> entrypoint.PhaseLcuAttempt:
            result = poll_phase_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with (
            mock.patch.object(
                entrypoint, "configure_runtime_logging", return_value=Path("runtime.log")
            ),
            mock.patch.object(entrypoint, "install_exception_logger"),
            mock.patch.object(entrypoint, "ensure_external_apps_running_once"),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(
                entrypoint,
                "LeagueClientExitGuard",
                return_value=mock.Mock(
                    should_exit=mock.Mock(return_value=False)
                ),
            ),
            mock.patch.object(entrypoint, "LcuClient", return_value=object()),
            mock.patch.object(entrypoint, "ChampionConfig", _FakeCliChampionConfig),
            mock.patch.object(
                entrypoint,
                "default_counter_cache_path",
                return_value=Path("counter-cache.json"),
            ),
            mock.patch.object(
                entrypoint, "select_image_set", return_value=Path("images/1280")
            ),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint, "_visible_rect_for_image_scan", return_value=(0, 0, 1280, 720)
            ),
            mock.patch.object(
                entrypoint,
                "_dismiss_blocking_modal_lcu_attempt",
                return_value=entrypoint.LcuActionAttempt(
                    False, LcuLoopAction.FALLBACK_IMAGE, "not_found"
                ),
            ),
            mock.patch.object(
                entrypoint,
                "_poll_lcu_phase_attempt",
                side_effect=poll_phase,
            ),
            mock.patch.object(
                entrypoint,
                "poll_match_state",
                side_effect=[
                    entrypoint.MatchPollAttempt(
                        True,
                        True,
                        LcuLoopAction.ACT_LCU,
                        "ready accepted",
                        PHASE_READY_CHECK,
                    ),
                    RuntimeError("same accept loop repeated after timeout"),
                ],
            ) as poll_match,
            mock.patch.object(
                entrypoint,
                "_wait_for_champ_select_after_match_accept",
                return_value=entrypoint.PhaseLcuAttempt(
                    None, LcuLoopAction.WAIT_AUTHORITATIVE, "timeout"
                ),
            ) as wait_champ_select,
            self.assertRaisesRegex(RuntimeError, "outer cycle restarted after timeout"),
        ):
            entrypoint.cli_main([])

        self.assertEqual(poll_match.call_count, 1)
        wait_champ_select.assert_called_once()

    def test_ensure_active_rect_times_out_when_window_missing(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        with (
            mock.patch.object(entrypoint, "find_league_window_rect", return_value=None),
            mock.patch.object(entrypoint.time, "monotonic", side_effect=[0.0, 0.0, 0.5, 1.0]),
            mock.patch.object(entrypoint.time, "sleep") as sleep,
            self.assertRaisesRegex(
                entrypoint.LeagueWindowLookupTimeout,
                "missing",
            ),
        ):
            entrypoint.ensure_active_rect(logger, poll=0.5, timeout_sec=1.0)

        sleep.assert_has_calls([mock.call(0.5), mock.call(0.5)])

    def test_ensure_active_rect_times_out_when_window_minimized(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        minimized_rect = (-32000, -32000, -31900, -31900)

        with (
            mock.patch.object(
                entrypoint, "find_league_window_rect", return_value=minimized_rect
            ),
            mock.patch.object(entrypoint.time, "monotonic", side_effect=[0.0, 0.0, 0.25, 0.5]),
            mock.patch.object(entrypoint.time, "sleep") as sleep,
            self.assertRaisesRegex(
                entrypoint.LeagueWindowLookupTimeout,
                "minimized",
            ),
        ):
            entrypoint.ensure_active_rect(logger, poll=0.25, timeout_sec=0.5)

        sleep.assert_has_calls([mock.call(0.25), mock.call(0.25)])

    def test_window_visibility_snapshot_classifies_visible_minimized_and_missing(
        self,
    ) -> None:
        self.assertTrue(hasattr(entrypoint, "get_league_window_visibility"))
        visible_rect = (0, 0, 1280, 720)
        minimized_rect = (-32000, -32000, -31900, -31900)

        with mock.patch.object(
            entrypoint, "find_league_window_rect", return_value=visible_rect
        ):
            visible = entrypoint.get_league_window_visibility()

        self.assertEqual(visible.state.value, "visible")
        self.assertEqual(visible.rect, visible_rect)

        with mock.patch.object(
            entrypoint, "find_league_window_rect", return_value=minimized_rect
        ):
            minimized = entrypoint.get_league_window_visibility()

        self.assertEqual(minimized.state.value, "minimized")
        self.assertIsNone(minimized.rect)

        with mock.patch.object(entrypoint, "find_league_window_rect", return_value=None):
            missing = entrypoint.get_league_window_visibility()

        self.assertEqual(missing.state.value, "missing")
        self.assertIsNone(missing.rect)

    def test_visible_rect_or_wait_logs_restore_wait_without_raising(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        stage = "테스트 복원 대기"
        minimized_rect = (-32000, -32000, -31900, -31900)
        entrypoint._last_restore_wait_log_state.pop(stage, None)

        with (
            mock.patch.object(
                entrypoint, "find_league_window_rect", return_value=minimized_rect
            ),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(entrypoint.time, "sleep") as sleep,
            self.assertLogs(logger, level="INFO") as logs,
        ):
            rect = entrypoint._visible_rect_or_wait(logger, stage, 0.25)

        self.assertIsNone(rect)
        sleep.assert_called_once_with(0.25)
        self.assertTrue(
            any("클라이언트 복원 대기" in message for message in logs.output)
        )

    def test_lcu_phase_maps_to_runtime_state(self) -> None:
        self.assertEqual(
            entrypoint._client_state_from_lcu_phase(PHASE_LOBBY),
            entrypoint.ClientState.LOBBY,
        )
        self.assertEqual(
            entrypoint._client_state_from_lcu_phase(PHASE_MATCHMAKING),
            entrypoint.ClientState.MATCH_FINDING,
        )
        self.assertEqual(
            entrypoint._client_state_from_lcu_phase(PHASE_READY_CHECK),
            entrypoint.ClientState.MATCH_ACCEPT_WAIT,
        )
        self.assertEqual(
            entrypoint._client_state_from_lcu_phase(PHASE_CHAMP_SELECT),
            entrypoint.ClientState.PREPICK,
        )
        self.assertEqual(
            entrypoint._client_state_from_lcu_phase(PHASE_IN_PROGRESS),
            entrypoint.ClientState.INGAME,
        )
        self.assertEqual(
            entrypoint._client_state_from_lcu_phase(PHASE_RECONNECT),
            entrypoint.ClientState.INGAME,
        )
        self.assertEqual(
            entrypoint._client_state_from_lcu_phase(PHASE_WATCH_IN_PROGRESS),
            entrypoint.ClientState.INGAME,
        )

    def test_lcu_ready_check_accept_is_cooled_down(self) -> None:
        fake = _FakeLcu()
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        entrypoint._last_lcu_ready_accept_at.clear()

        with mock.patch.object(entrypoint.time, "monotonic", side_effect=[100.0, 100.1]):
            self.assertTrue(entrypoint._accept_ready_check_via_lcu(fake, "stage", logger))
            self.assertTrue(entrypoint._accept_ready_check_via_lcu(fake, "stage", logger))

        self.assertEqual(fake.accept_calls, 1)

    def test_lcu_ready_check_legacy_false_waits_without_image_fallback(self) -> None:
        fake = _FakeLcu(accept_result=False)
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        entrypoint._last_lcu_ready_accept_at.clear()

        result = entrypoint._accept_ready_check_lcu_attempt(fake, "stage", logger)

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "legacy_false")

    def test_lcu_wrappers_reraise_unexpected_exceptions(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        class _PhaseFaultLcu:
            def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> str:
                raise RuntimeError("phase fault")

        class _ReadyFaultLcu:
            def accept_ready_check(self) -> bool:
                raise RuntimeError("ready fault")

        class _StartFaultLcu(_FakePhaseLcu):
            def start_matchmaking(self) -> bool:
                raise RuntimeError("start fault")

        class _RoleFaultLcu:
            def get_local_player_position(self) -> LcuDecision:
                raise RuntimeError("role fault")

        with self.assertRaisesRegex(RuntimeError, "phase fault"):
            entrypoint._poll_lcu_phase_attempt(_PhaseFaultLcu(), logger, "phase")
        with self.assertRaisesRegex(RuntimeError, "ready fault"):
            entrypoint._accept_ready_check_lcu_attempt(
                _ReadyFaultLcu(), "ready", logger
            )
        with self.assertRaisesRegex(RuntimeError, "start fault"):
            entrypoint._start_matchmaking_lcu_attempt(
                _StartFaultLcu(PHASE_LOBBY), "start", logger
            )
        with self.assertRaisesRegex(RuntimeError, "role fault"):
            entrypoint._detect_role_via_lcu(_RoleFaultLcu(), stage="role", logger=logger)

    def test_lcu_phase_transition_hook_is_optional(self) -> None:
        class _PhaseOnlyLcu:
            def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> str:
                return PHASE_LOBBY

        logger = logging.getLogger("lolmanager-test-cli-lcu")

        attempt = entrypoint._poll_lcu_phase_attempt(_PhaseOnlyLcu(), logger, "phase")

        self.assertEqual(attempt.phase, PHASE_LOBBY)
        self.assertEqual(attempt.loop_action, LcuLoopAction.ACT_LCU)

    def test_champ_select_phase_poll_dismisses_blocking_modal_before_returning(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseBlockingModalLcu(
            PHASE_CHAMP_SELECT,
            LcuDecision(LcuOutcome.SUCCESS, reason="pick-order swap declined"),
        )

        attempt = entrypoint._poll_lcu_phase_attempt(fake, logger, "phase")

        self.assertEqual(attempt.phase, PHASE_CHAMP_SELECT)
        self.assertEqual(attempt.loop_action, LcuLoopAction.ACT_LCU)
        self.assertEqual(attempt.outcome, "success")
        self.assertEqual(fake.dismiss_calls, 1)

    def test_champ_select_phase_poll_keeps_phase_when_modal_route_empty(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseBlockingModalLcu(
            PHASE_CHAMP_SELECT,
            LcuDecision(LcuOutcome.NO_CURRENT_ACTION, reason="no current action"),
        )

        attempt = entrypoint._poll_lcu_phase_attempt(fake, logger, "phase")

        self.assertEqual(attempt.phase, PHASE_CHAMP_SELECT)
        self.assertEqual(attempt.loop_action, LcuLoopAction.ACT_LCU)
        self.assertEqual(attempt.outcome, "success")
        self.assertEqual(fake.dismiss_calls, 1)

    def test_lcu_phase_transition_hook_logs_and_reraises_unexpected_exception(
        self,
    ) -> None:
        class _HookFaultLcu(_FakePhaseLcu):
            def consume_phase_transition(self, phase: str):
                raise RuntimeError("hook fault")

        logger = logging.getLogger("lolmanager-test-cli-lcu")

        with (
            self.assertLogs(logger, level="ERROR") as logs,
            self.assertRaisesRegex(RuntimeError, "hook fault"),
        ):
            entrypoint._poll_lcu_phase_attempt(
                _HookFaultLcu(PHASE_LOBBY), logger, "phase"
            )

        self.assertIn("phase 전이 hook", "\n".join(logs.output))

    def test_match_reset_ignores_find_match_image_during_lcu_champ_select(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(PHASE_CHAMP_SELECT)

        with (
            mock.patch.object(entrypoint, "poll_match_state", return_value=(False, False)) as poll,
            mock.patch.object(entrypoint, "search_and_act", return_value=True) as search,
        ):
            reset = entrypoint.detect_match_reset(
                (0, 0, 1280, 720),
                "챔피언 선택",
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                lcu=fake,
            )

        self.assertFalse(reset)
        poll.assert_not_called()
        search.assert_not_called()

    def test_match_reset_exits_when_lcu_reaches_in_progress(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(PHASE_IN_PROGRESS)

        with (
            mock.patch.object(entrypoint, "poll_match_state", return_value=(False, False)) as poll,
            mock.patch.object(entrypoint, "search_and_act", return_value=False) as search,
        ):
            reset = entrypoint.detect_match_reset(
                (0, 0, 1280, 720),
                "밴 검색",
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                lcu=fake,
            )

        self.assertTrue(reset)
        poll.assert_not_called()
        search.assert_not_called()

    def test_match_reset_uses_lcu_lobby_without_find_match_image(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(PHASE_LOBBY)

        with (
            mock.patch.object(entrypoint, "poll_match_state", return_value=(False, False)) as poll,
            mock.patch.object(entrypoint, "search_and_act", return_value=False) as search,
        ):
            reset = entrypoint.detect_match_reset(
                (0, 0, 1280, 720),
                "챔피언 선택",
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                lcu=fake,
            )

        self.assertTrue(reset)
        poll.assert_not_called()
        search.assert_not_called()

    def test_match_reset_does_not_use_find_match_image_during_authoritative_phase(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        for phase in (
            PHASE_NONE,
            PHASE_RECONNECT,
            PHASE_WATCH_IN_PROGRESS,
            PHASE_PRE_END_OF_GAME,
        ):
            with (
                self.subTest(phase=phase),
                mock.patch.object(
                    entrypoint,
                    "poll_match_state",
                    return_value=entrypoint.MatchPollAttempt(
                        False, False, LcuLoopAction.ACT_LCU, "success"
                    ),
                ),
                mock.patch.object(entrypoint, "search_and_act") as search,
            ):
                reset = entrypoint.detect_match_reset(
                    (0, 0, 1280, 720),
                    "챔피언 선택",
                    Path("lobby_find-match-button.png"),
                    Path("lobby_finding-match-text.png"),
                    Path("lobby_accept-button.png"),
                    0.85,
                    0.2,
                    logger,
                    lcu=_FakePhaseLcu(phase),
                )

            self.assertTrue(reset)
            search.assert_not_called()

    def test_match_reset_revokes_stale_transport_fallback_after_phase_recovers(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        for phase, expected_reset in (
            (PHASE_CHAMP_SELECT, False),
            (PHASE_LOBBY, True),
            (PHASE_NONE, True),
        ):
            with (
                self.subTest(phase=phase),
                mock.patch.object(entrypoint, "search_and_act") as search,
            ):
                reset = entrypoint.detect_match_reset(
                    (0, 0, 1280, 720),
                    "챔피언 선택",
                    Path("lobby_find-match-button.png"),
                    Path("lobby_finding-match-text.png"),
                    Path("lobby_accept-button.png"),
                    0.85,
                    0.2,
                    logger,
                    lcu=_FakePhaseDecisionSequenceLcu(
                        [
                            LcuDecision(
                                LcuOutcome.REQUEST_FAILED, reason="ReadTimeout"
                            ),
                            LcuDecision(LcuOutcome.SUCCESS, value=phase),
                        ]
                    ),
                )

            self.assertEqual(reset, expected_reset)
            search.assert_not_called()

    def test_match_poll_exposes_authoritative_champ_select_exit_phase(self) -> None:
        for phase in (PHASE_LOBBY, PHASE_READY_CHECK, PHASE_IN_PROGRESS, PHASE_NONE):
            with self.subTest(phase=phase):
                exit_phase = (
                    entrypoint._authoritative_champ_select_exit_from_match_poll(
                        entrypoint.MatchPollAttempt(
                            False, False, LcuLoopAction.ACT_LCU, "success", phase
                        )
                    )
                )

            self.assertEqual(exit_phase, phase)

        self.assertIsNone(
            entrypoint._authoritative_champ_select_exit_from_match_poll(
                entrypoint.MatchPollAttempt(
                    False,
                    False,
                    LcuLoopAction.ACT_LCU,
                    "champ_select",
                    PHASE_CHAMP_SELECT,
                )
            )
        )

    def test_start_matchmaking_via_lcu_uses_lcu_first(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeWriteLcu(start_result=True)

        self.assertTrue(entrypoint._start_matchmaking_via_lcu(fake, "stage", logger))
        self.assertEqual(fake.start_calls, 1)

    def test_start_matchmaking_legacy_false_waits_without_image_fallback(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeWriteLcu(start_result=False)

        result = entrypoint._start_matchmaking_lcu_attempt(fake, "stage", logger)

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "legacy_false")

    def test_start_matchmaking_waits_when_lcu_phase_is_malformed(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseDecisionLcu(LcuDecision(LcuOutcome.MALFORMED_RESPONSE))

        result = entrypoint._start_matchmaking_lcu_attempt(fake, "stage", logger)

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "phase_blocked")

    def test_start_matchmaking_waits_when_lcu_phase_is_none(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeWriteLcu(start_result=True, phase=PHASE_NONE)

        result = entrypoint._start_matchmaking_lcu_attempt(fake, "stage", logger)

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "phase_blocked")
        self.assertEqual(fake.start_calls, 0)

    def test_start_matchmaking_waits_on_lcu_semantic_rejection(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeMatchmakingDecisionLcu(
            PHASE_LOBBY,
            LcuDecision(
                LcuOutcome.ACTION_REJECTED,
                reason="matchmaking search rejected",
            ),
        )

        result = entrypoint._start_matchmaking_lcu_attempt(fake, "stage", logger)

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "action_rejected")
        self.assertEqual(fake.start_calls, 1)

    def test_start_matchmaking_falls_back_on_lcu_request_failure(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeMatchmakingDecisionLcu(
            PHASE_LOBBY,
            LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout"),
        )

        result = entrypoint._start_matchmaking_lcu_attempt(fake, "stage", logger)

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.FALLBACK_IMAGE)
        self.assertEqual(result.outcome, "request_failed")
        self.assertEqual(fake.start_calls, 1)

    def test_start_matchmaking_via_lcu_skips_postgame_phases(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        postgame_phases = (
            PHASE_WAITING_FOR_STATS,
            PHASE_PRE_END_OF_GAME,
            PHASE_END_OF_GAME,
        )

        for phase in postgame_phases:
            with self.subTest(phase=phase):
                fake = _FakeWriteLcu(start_result=True, phase=phase)

                started = entrypoint._start_matchmaking_via_lcu(
                    fake, "매칭 단계", logger
                )

                self.assertFalse(started)
                self.assertEqual(fake.start_calls, 0)

    def test_start_matchmaking_via_lcu_skips_active_game_phases(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        for phase in (PHASE_RECONNECT, PHASE_WATCH_IN_PROGRESS):
            with self.subTest(phase=phase):
                fake = _FakeWriteLcu(start_result=True, phase=phase)

                started = entrypoint._start_matchmaking_via_lcu(
                    fake, "매칭 단계", logger
                )

                self.assertFalse(started)
                self.assertEqual(fake.start_calls, 0)

    def test_lobby_confirm_template_does_not_click_popup_confirm(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(PHASE_LOBBY)
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.LOBBY
        confirm_template = (
            ROOT
            / "src"
            / "lolmanager"
            / "resources"
            / "images"
            / "1280"
            / "client_confirm-button-2.png"
        )

        with (
            mock.patch.object(
                entrypoint,
                "find_template_matches_once",
                return_value={"confirm#0": ((524, 683), object(), 0.99)},
            ) as find_matches,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
        ):
            result = entrypoint.poll_match_state(
                (0, 0, 1280, 720),
                "사이클 진입",
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                tpl_confirm_templates=[confirm_template],
                lcu=fake,
            )
            accepted, finding = result

        self.assertFalse(accepted)
        self.assertFalse(finding)
        find_matches.assert_not_called()
        click_screen.assert_not_called()

    def test_legacy_none_phase_waits_without_image_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(None)

        with (
            mock.patch.object(entrypoint, "find_template_matches_once") as find_matches,
            mock.patch.object(entrypoint.time, "sleep") as sleep,
        ):
            result = entrypoint.poll_match_state(
                (0, 0, 1280, 720),
                "매칭 단계",
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                lcu=fake,
            )
            accepted, finding = result

        self.assertFalse(accepted)
        self.assertFalse(finding)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        find_matches.assert_not_called()
        sleep.assert_called_once_with(0.2)

    def test_unknown_legacy_phase_waits_without_image_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu("FuturePhase")

        with (
            mock.patch.object(entrypoint, "find_template_matches_once") as find_matches,
            mock.patch.object(entrypoint.time, "sleep") as sleep,
        ):
            result = entrypoint.poll_match_state(
                (0, 0, 1280, 720),
                "매칭 단계",
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                lcu=fake,
            )
            accepted, finding = result

        self.assertFalse(accepted)
        self.assertFalse(finding)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        find_matches.assert_not_called()
        sleep.assert_called_once_with(0.2)

    def test_authoritative_postgame_phase_keeps_ui_only_popup_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(PHASE_END_OF_GAME)
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.POSTGAME_SCORE
        confirm_template = (
            ROOT
            / "src"
            / "lolmanager"
            / "resources"
            / "images"
            / "1280"
            / "client_confirm-button-2.png"
        )

        with (
            mock.patch.object(
                entrypoint,
                "find_template_matches_once",
                return_value={"confirm#0": ((524, 683), object(), 0.99)},
            ) as find_matches,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
        ):
            result = entrypoint.poll_match_state(
                (0, 0, 1280, 720),
                "엔드 이후",
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                tpl_confirm_templates=[confirm_template],
                lcu=fake,
            )
            accepted, finding = result

        self.assertFalse(accepted)
        self.assertFalse(finding)
        find_matches.assert_called_once()
        click_screen.assert_called_once_with((524, 683))

    def test_champion_select_detection_skips_images_when_lcu_phase_is_lobby(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(PHASE_LOBBY)
        image_dir = ROOT / "src" / "lolmanager" / "resources" / "images" / "1280"

        with mock.patch.object(entrypoint, "find_template_matches_once") as find_matches:
            detected = entrypoint.detect_champion_select(
                (0, 0, 1280, 720),
                "사이클 진입",
                image_dir / "prepick_search-text.png",
                [("mid", image_dir / "pick_position_mid_text.png")],
                0.85,
                logger,
                lcu=fake,
            )

        self.assertFalse(detected)
        find_matches.assert_not_called()

    def test_champion_select_detection_skips_images_for_malformed_lcu_phase(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseDecisionLcu(LcuDecision(LcuOutcome.MALFORMED_RESPONSE))
        image_dir = ROOT / "src" / "lolmanager" / "resources" / "images" / "1280"

        with mock.patch.object(entrypoint, "find_template_matches_once") as find_matches:
            detected = entrypoint.detect_champion_select(
                (0, 0, 1280, 720),
                "사이클 진입",
                image_dir / "prepick_search-text.png",
                [("mid", image_dir / "pick_position_mid_text.png")],
                0.85,
                logger,
                lcu=fake,
            )

        self.assertFalse(detected)
        find_matches.assert_not_called()

    def test_match_poll_skips_images_for_malformed_lcu_phase(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseDecisionLcu(LcuDecision(LcuOutcome.MALFORMED_RESPONSE))

        with (
            mock.patch.object(entrypoint, "find_template_matches_once") as find_matches,
            mock.patch.object(entrypoint.time, "sleep") as sleep,
        ):
            result = entrypoint.poll_match_state(
                (0, 0, 1280, 720),
                "매칭 단계",
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                lcu=fake,
            )
            accepted, finding = result

        self.assertFalse(accepted)
        self.assertFalse(finding)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        find_matches.assert_not_called()
        sleep.assert_called_once_with(0.2)

    def test_ingame_monitor_stays_in_lcu_in_progress_until_end_phase(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseDecisionSequenceLcu(
            [
                LcuDecision(LcuOutcome.SUCCESS, value=PHASE_IN_PROGRESS),
                LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME),
            ]
        )
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.UNKNOWN

        with (
            mock.patch.object(entrypoint.time, "sleep") as sleep,
            mock.patch.object(entrypoint, "is_game_client_active") as game_active,
            mock.patch.object(entrypoint, "process_postgame") as process_postgame,
        ):
            entrypoint.monitor_ingame_and_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.phase_calls, 2)
        sleep.assert_called_once_with(1.0)
        game_active.assert_not_called()
        process_postgame.assert_called_once()

    def test_ingame_monitor_stays_active_during_reconnect_phases(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        for phase in (PHASE_RECONNECT, PHASE_WATCH_IN_PROGRESS):
            with (
                self.subTest(phase=phase),
                mock.patch.object(entrypoint.time, "sleep") as sleep,
                mock.patch.object(entrypoint, "is_game_client_active") as game_active,
                mock.patch.object(entrypoint, "process_postgame") as process_postgame,
            ):
                fake = _FakePhaseDecisionSequenceLcu(
                    [
                        LcuDecision(LcuOutcome.SUCCESS, value=phase),
                        LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME),
                    ]
                )
                entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.UNKNOWN

                entrypoint.monitor_ingame_and_postgame(
                    Path("end_next-button.png"),
                    Path("end_one-more-button.png"),
                    Path("lobby_find-match-button.png"),
                    Path("lobby_finding-match-text.png"),
                    Path("lobby_accept-button.png"),
                    [],
                    Path("prepick_search-text.png"),
                    [],
                    0.85,
                    0.2,
                    1.0,
                    logger,
                    lcu=fake,
                )

            self.assertEqual(fake.phase_calls, 2)
            sleep.assert_called_once_with(1.0)
            game_active.assert_not_called()
            process_postgame.assert_called_once()

    def test_postgame_returns_when_lcu_reenters_active_game_phase(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        for phase in (PHASE_IN_PROGRESS, PHASE_RECONNECT, PHASE_WATCH_IN_PROGRESS):
            with (
                self.subTest(phase=phase),
                mock.patch.object(entrypoint, "ensure_active_rect") as ensure_rect,
            ):
                entrypoint.process_postgame(
                    Path("end_next-button.png"),
                    Path("end_one-more-button.png"),
                    Path("lobby_find-match-button.png"),
                    Path("lobby_finding-match-text.png"),
                    Path("lobby_accept-button.png"),
                    [],
                    Path("prepick_search-text.png"),
                    [],
                    0.85,
                    0.2,
                    1.0,
                    logger,
                    lcu=_FakePhaseLcu(phase),
                )

            ensure_rect.assert_not_called()

    def test_postgame_malformed_phase_waits_without_image_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseDecisionLcu(LcuDecision(LcuOutcome.MALFORMED_RESPONSE))

        with (
            mock.patch.object(entrypoint, "ensure_active_rect") as ensure_rect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=RuntimeError("stop after one postgame iteration"),
            ) as sleep,
            self.assertRaisesRegex(RuntimeError, "stop after one postgame iteration"),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        sleep.assert_called_once_with(1.0)
        ensure_rect.assert_not_called()
        search.assert_not_called()

    def test_postgame_none_phase_waits_without_image_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        with (
            mock.patch.object(entrypoint, "ensure_active_rect") as ensure_rect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=RuntimeError("stop after one postgame iteration"),
            ) as sleep,
            self.assertRaisesRegex(RuntimeError, "stop after one postgame iteration"),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=_FakePhaseLcu(PHASE_NONE),
            )

        sleep.assert_called_once_with(1.0)
        ensure_rect.assert_not_called()
        search.assert_not_called()

    def test_postgame_confirmed_phase_keeps_ui_only_end_button_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        class _FakePostgamePhaseLcu(_FakePhaseLcu):
            def is_end_of_game_stats_available(self) -> bool:
                return False

        with (
            mock.patch.object(
                entrypoint, "find_league_window_rect", return_value=(0, 0, 1280, 720)
            ),
            mock.patch.object(entrypoint, "detect_champion_select", return_value=False),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(entrypoint, "search_and_act", return_value=True) as search,
            mock.patch.object(
                entrypoint,
                "poll_match_state",
                return_value=entrypoint.MatchPollAttempt(
                    False, False, LcuLoopAction.WAIT_AUTHORITATIVE, "wait"
                ),
            ),
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=RuntimeError("stop after one postgame iteration"),
            ),
            self.assertRaisesRegex(RuntimeError, "stop after one postgame iteration"),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=_FakePostgamePhaseLcu(PHASE_END_OF_GAME),
            )

        search.assert_called_once_with(
            (0, 0, 1280, 720),
            Path("end_next-button.png"),
            threshold=0.85,
            click=True,
        )

    def test_postgame_end_of_game_dismisses_stats_then_requeues_via_lcu(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameContinueToLobbyLcu()

        with (
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                return_value=(0, 0, 1280, 720),
            ),
            mock.patch.object(entrypoint, "detect_champion_select", return_value=False),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(
                entrypoint,
                "poll_match_state",
                return_value=entrypoint.MatchPollAttempt(
                    False, False, LcuLoopAction.ACT_LCU, "lobby", PHASE_LOBBY
                ),
            ),
            mock.patch.object(entrypoint.time, "sleep", return_value=None),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.dismiss_stats_calls, 1)
        self.assertEqual(fake.start_calls, 1)
        search.assert_not_called()

    def test_postgame_end_of_game_none_requeues_via_lcu_without_ui_wait(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameContinueToNoneLcu(
            LcuDecision(LcuOutcome.SUCCESS, reason="matchmaking search accepted")
        )

        with (
            mock.patch.object(entrypoint, "_visible_rect_or_wait") as visible_rect,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=[None, RuntimeError("unexpected postgame none wait")],
            ),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.dismiss_stats_calls, 1)
        self.assertEqual(fake.play_again_calls, 1)
        self.assertEqual(fake.start_calls, 1)
        self.assertGreaterEqual(fake.phase_calls, 3)
        visible_rect.assert_not_called()
        detect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_end_of_game_none_uses_play_again_before_lobby_search(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameContinueToNoneLcu(
            LcuDecision(LcuOutcome.SUCCESS, reason="matchmaking search accepted"),
            play_again_result=LcuDecision(
                LcuOutcome.SUCCESS, reason="play again accepted"
            ),
        )

        with (
            mock.patch.object(entrypoint, "_visible_rect_or_wait") as visible_rect,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=[None, None, RuntimeError("stop after play again")],
            ),
            self.assertRaisesRegex(RuntimeError, "stop after play again"),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.dismiss_stats_calls, 0)
        self.assertEqual(fake.play_again_calls, 1)
        self.assertEqual(fake.start_calls, 0)
        visible_rect.assert_not_called()
        detect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_end_of_game_uses_play_again_before_dismiss_stats(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameContinueToNoneLcu(
            LcuDecision(LcuOutcome.SUCCESS, reason="matchmaking search accepted"),
            play_again_result=LcuDecision(
                LcuOutcome.SUCCESS, reason="play again accepted"
            ),
        )

        with (
            mock.patch.object(
                entrypoint,
                "_visible_rect_or_wait",
                side_effect=RuntimeError("visible wait should not gate play again"),
            ) as visible_rect,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=RuntimeError("stop after end play again"),
            ),
            self.assertRaisesRegex(RuntimeError, "stop after end play again"),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [Path("client_confirm-button.png")],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.play_again_calls, 1)
        self.assertEqual(fake.dismiss_stats_calls, 0)
        self.assertEqual(fake.start_calls, 0)
        visible_rect.assert_not_called()
        detect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_lobby_phase_requeues_via_lcu_before_return(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameLobbyStartLcu()

        with (
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                return_value=(0, 0, 1280, 720),
            ),
            mock.patch.object(entrypoint, "detect_champion_select", return_value=False),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(
                entrypoint,
                "poll_match_state",
                return_value=entrypoint.MatchPollAttempt(
                    False, False, LcuLoopAction.ACT_LCU, "lobby", PHASE_LOBBY
                ),
            ),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.start_calls, 1)
        search.assert_not_called()

    def test_postgame_play_again_lobby_transition_requeues_via_lcu(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgamePlayAgainToLobbyLcu()

        with (
            mock.patch.object(entrypoint, "_visible_rect_or_wait") as visible_rect,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
            mock.patch.object(entrypoint.time, "sleep", return_value=None),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.play_again_calls, 1)
        self.assertEqual(fake.dismiss_stats_calls, 0)
        self.assertEqual(fake.start_calls, 1)
        self.assertGreaterEqual(fake.phase_calls, 3)
        visible_rect.assert_not_called()
        detect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_play_again_lobby_start_failure_retries_lcu_only(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgamePlayAgainToLobbyLcu(
            LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout")
        )

        with (
            mock.patch.object(
                entrypoint,
                "_visible_rect_or_wait",
                side_effect=RuntimeError("ui fallback should not run"),
            ) as visible_rect,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=[None, RuntimeError("stop after lcu retry")],
            ),
            self.assertRaisesRegex(RuntimeError, "stop after lcu retry"),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.play_again_calls, 1)
        self.assertEqual(fake.dismiss_stats_calls, 0)
        self.assertEqual(fake.start_calls, 1)
        visible_rect.assert_not_called()
        detect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_rejected_play_again_falls_back_to_dismiss_stats_once(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameRejectedPlayAgainToLobbyLcu()

        with (
            mock.patch.object(entrypoint, "_visible_rect_or_wait") as visible_rect,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=[None, RuntimeError("play-again livelock")],
            ),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.play_again_calls, 1)
        self.assertEqual(fake.dismiss_stats_calls, 1)
        self.assertEqual(fake.start_calls, 1)
        visible_rect.assert_not_called()
        detect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_request_failure_keeps_ui_only_end_button_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseDecisionLcu(
            LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout")
        )

        with (
            mock.patch.object(
                entrypoint, "find_league_window_rect", return_value=(0, 0, 1280, 720)
            ),
            mock.patch.object(entrypoint, "detect_champion_select", return_value=False),
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(entrypoint, "search_and_act", return_value=True) as search,
            mock.patch.object(
                entrypoint,
                "poll_match_state",
                return_value=entrypoint.MatchPollAttempt(
                    False, False, LcuLoopAction.WAIT_AUTHORITATIVE, "wait"
                ),
            ),
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=RuntimeError("stop after one postgame iteration"),
            ),
            self.assertRaisesRegex(RuntimeError, "stop after one postgame iteration"),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        search.assert_called_once_with(
            (0, 0, 1280, 720),
            Path("end_next-button.png"),
            threshold=0.85,
            click=True,
        )

    def test_postgame_minimized_request_failure_waits_without_image_or_ux(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseDecisionLcu(
            LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout")
        )

        with (
            mock.patch.object(
                entrypoint,
                "find_league_window_rect",
                return_value=(-32000, -32000, -31900, -31900),
            ),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint.Path, "exists", return_value=True),
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=RuntimeError("stop after minimized postgame wait"),
            ),
            self.assertRaisesRegex(
                RuntimeError, "stop after minimized postgame wait"
            ),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        search.assert_not_called()
        detect.assert_not_called()

    def test_postgame_none_after_end_stats_failed_lcu_start_waits_without_fallback(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameContinueToNoneLcu(
            LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout")
        )

        with (
            mock.patch.object(entrypoint, "_visible_rect_or_wait") as visible_rect,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=[None, RuntimeError("stop after postgame none start fail")],
            ),
            self.assertRaisesRegex(
                RuntimeError, "stop after postgame none start fail"
            ),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.dismiss_stats_calls, 1)
        self.assertEqual(fake.start_calls, 1)
        visible_rect.assert_not_called()
        detect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_pre_end_honor_attempt_waits_without_image_fallback(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameHonorLcu(
            LcuDecision(LcuOutcome.UNSUPPORTED, reason="no confirmed honor route")
        )

        with (
            mock.patch.object(entrypoint, "_visible_rect_or_wait") as visible_rect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
            mock.patch.object(
                entrypoint.time,
                "sleep",
                side_effect=RuntimeError("stop after preend wait"),
            ),
            self.assertRaisesRegex(RuntimeError, "stop after preend wait"),
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.honor_calls, 1)
        visible_rect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_pre_end_stays_until_lobby_then_requeues_via_lcu(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgamePreEndToLobbyLcu()

        with (
            mock.patch.object(entrypoint, "_visible_rect_or_wait") as visible_rect,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(
                entrypoint,
                "poll_match_state",
                return_value=entrypoint.MatchPollAttempt(
                    False, False, LcuLoopAction.ACT_LCU, "lobby", PHASE_LOBBY
                ),
            ) as poll,
            mock.patch.object(entrypoint.time, "sleep", return_value=None) as sleep,
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.honor_calls, 1)
        self.assertEqual(fake.dismiss_stats_calls, 1)
        self.assertEqual(fake.start_calls, 1)
        self.assertGreaterEqual(fake.phase_calls, 4)
        sleep.assert_called()
        visible_rect.assert_not_called()
        detect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_pre_end_dismisses_ranked_notification_via_lcu(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgamePreEndRankedNotificationLcu()

        with (
            mock.patch.object(entrypoint, "_visible_rect_or_wait") as visible_rect,
            mock.patch.object(entrypoint, "detect_champion_select") as detect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
            mock.patch.object(entrypoint.time, "sleep", return_value=None) as sleep,
        ):
            entrypoint.process_postgame(
                Path("end_next-button.png"),
                Path("end_one-more-button.png"),
                Path("lobby_find-match-button.png"),
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                [],
                Path("prepick_search-text.png"),
                [],
                0.85,
                0.2,
                1.0,
                logger,
                lcu=fake,
            )

        self.assertEqual(fake.honor_calls, 1)
        self.assertEqual(fake.dismiss_calls, 1)
        self.assertEqual(fake.dismiss_stats_calls, 1)
        self.assertEqual(fake.start_calls, 1)
        sleep.assert_called()
        visible_rect.assert_not_called()
        detect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_cycle_start_postgame_phase_routes_to_postgame_handler(self) -> None:
        for phase in (PHASE_WAITING_FOR_STATS, PHASE_PRE_END_OF_GAME, PHASE_END_OF_GAME):
            with self.subTest(phase=phase):
                self.assertTrue(entrypoint._should_process_postgame_at_cycle(phase))

        for phase in (PHASE_NONE, PHASE_LOBBY, PHASE_MATCHMAKING):
            with self.subTest(phase=phase):
                self.assertFalse(entrypoint._should_process_postgame_at_cycle(phase))

    def test_confirm_template_candidates_include_report_feedback_thanks_button(
        self,
    ) -> None:
        selected = ROOT / "src" / "lolmanager" / "resources" / "images" / "1280"

        candidates = entrypoint._client_confirm_template_candidates(selected)

        self.assertIn(selected / "client_thanks-button.png", candidates)

    def test_blocking_modal_attempt_allows_image_fallback_when_lcu_has_no_route(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeBlockingModalLcu(
            LcuDecision(LcuOutcome.UNSUPPORTED, reason="no confirmed modal route")
        )

        result = entrypoint._dismiss_blocking_modal_lcu_attempt(
            fake, "사이클 시작", logger
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.FALLBACK_IMAGE)
        self.assertEqual(result.outcome, "unsupported")
        self.assertEqual(fake.dismiss_calls, 1)

    def test_blocking_modal_attempt_allows_image_fallback_when_lcu_response_malformed(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeBlockingModalLcu(
            LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="pick-order swap response is not a list",
            )
        )

        result = entrypoint._dismiss_blocking_modal_lcu_attempt(
            fake, "픽 팝업", logger
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.FALLBACK_IMAGE)
        self.assertEqual(result.outcome, "malformed_response")
        self.assertEqual(fake.dismiss_calls, 1)

    def test_blocking_modal_attempt_allows_image_fallback_without_lcu_route(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(PHASE_END_OF_GAME)

        result = entrypoint._dismiss_blocking_modal_lcu_attempt(
            fake, "사이클 시작", logger
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.FALLBACK_IMAGE)
        self.assertEqual(result.outcome, "not_supported")

    def test_stale_blocking_modal_ui_fallback_clicks_visible_template_without_ux(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        image_dir = ROOT / "src" / "lolmanager" / "resources" / "images" / "1280"

        with (
            mock.patch.object(
                entrypoint,
                "find_template_matches_once",
                return_value={"confirm#2": ((640, 432), object(), 0.99)},
            ) as find_matches,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
        ):
            handled = entrypoint._dismiss_blocking_modal_ui_fallback(
                (0, 0, 1280, 720),
                entrypoint._client_confirm_template_candidates(image_dir),
                0.85,
                "사이클 시작",
                logger,
            )

        self.assertTrue(handled)
        click_screen.assert_called_once_with((640, 432))
        find_matches.assert_called_once()
        self.assertEqual(
            find_matches.call_args.kwargs["search_rois"]["confirm#2"],
            entrypoint._popup_button_search_roi((0, 0, 1280, 720)),
        )

    def test_blocking_modal_ui_fallback_skips_minimized_rect_without_ux(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        image_dir = ROOT / "src" / "lolmanager" / "resources" / "images" / "1280"
        minimized_rect = (-32000, -32000, -31900, -31900)

        with (
            mock.patch.object(entrypoint, "find_template_matches_once") as find_matches,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
        ):
            handled = entrypoint._dismiss_blocking_modal_ui_fallback(
                minimized_rect,
                entrypoint._client_confirm_template_candidates(image_dir),
                0.85,
                "사이클 시작",
                logger,
            )

        self.assertFalse(handled)
        find_matches.assert_not_called()
        click_screen.assert_not_called()

    def test_match_poll_dismisses_blocking_modal_via_lcu_before_image_confirm(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.POSTGAME_SCORE
        fake = _FakePhaseBlockingModalLcu(
            PHASE_END_OF_GAME,
            LcuDecision(LcuOutcome.SUCCESS, reason="modal dismissed"),
        )

        with mock.patch.object(entrypoint, "find_template_matches_once") as find_matches:
            result = entrypoint.poll_match_state(
                (0, 0, 1280, 720),
                "사이클 진입",
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                tpl_confirm_templates=[Path("client_confirm-button-2.png")],
                lcu=fake,
            )

        self.assertEqual(fake.dismiss_calls, 1)
        self.assertEqual(result.loop_action, LcuLoopAction.ACT_LCU)
        self.assertEqual(result.outcome, "success")
        find_matches.assert_not_called()

    def test_ready_check_semantic_rejection_skips_image_accept_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeReadyDecisionLcu(
            PHASE_READY_CHECK,
            LcuDecision(
                LcuOutcome.ACTION_REJECTED,
                reason="ready check accept rejected",
            ),
        )

        with mock.patch.object(entrypoint, "find_template_matches_once") as find_matches:
            result = entrypoint.poll_match_state(
                (0, 0, 1280, 720),
                "매칭 단계",
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                lcu=fake,
            )
            accepted, finding = result

        self.assertFalse(accepted)
        self.assertFalse(finding)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(fake.accept_calls, 1)
        find_matches.assert_not_called()

    def test_ready_check_request_failure_allows_image_accept_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeReadyDecisionLcu(
            PHASE_READY_CHECK,
            LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout"),
        )

        with mock.patch.object(
            entrypoint, "find_template_matches_once", return_value={}
        ) as find_matches:
            accepted, finding = entrypoint.poll_match_state(
                (0, 0, 1280, 720),
                "매칭 단계",
                Path("lobby_finding-match-text.png"),
                Path("lobby_accept-button.png"),
                0.85,
                0.2,
                logger,
                lcu=fake,
            )

        self.assertFalse(accepted)
        self.assertFalse(finding)
        self.assertEqual(fake.accept_calls, 1)
        find_matches.assert_called_once()

    def test_wait_for_champ_select_after_accept_handles_rematch_ready_check(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeReadyCheckTransitionLcu(
            [
                PHASE_READY_CHECK,
                PHASE_MATCHMAKING,
                PHASE_READY_CHECK,
                PHASE_CHAMP_SELECT,
            ]
        )
        entrypoint._last_lcu_ready_accept_at.clear()

        with (
            mock.patch.object(entrypoint, "LCU_READY_ACCEPT_COOLDOWN_SEC", 0.0),
            mock.patch.object(entrypoint.time, "sleep") as sleep,
        ):
            result = entrypoint._wait_for_champ_select_after_match_accept(
                cast(Any, fake),
                "매칭 단계",
                logger,
                interval_sec=0.1,
                timeout_sec=5.0,
            )

        self.assertEqual(result.phase, PHASE_CHAMP_SELECT)
        self.assertEqual(result.loop_action, LcuLoopAction.ACT_LCU)
        self.assertEqual(fake.accept_calls, 2)
        self.assertGreaterEqual(sleep.call_count, 2)

    def test_wait_for_champ_select_after_accept_returns_lobby_without_requeue(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeReadyCheckTransitionLcu([PHASE_LOBBY])

        with mock.patch.object(entrypoint.time, "sleep") as sleep:
            result = entrypoint._wait_for_champ_select_after_match_accept(
                cast(Any, fake),
                "매칭 단계",
                logger,
                interval_sec=0.1,
                timeout_sec=5.0,
            )

        self.assertEqual(result.phase, PHASE_LOBBY)
        self.assertEqual(result.loop_action, LcuLoopAction.ACT_LCU)
        self.assertEqual(fake.accept_calls, 0)
        sleep.assert_not_called()

    def test_champ_select_action_via_lcu_passes_action_type_and_completion(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeWriteLcu(select_result=True)

        self.assertTrue(
            entrypoint._champ_select_action_via_lcu(
                fake,
                "아리",
                action_type="pick",
                complete=True,
                stage="픽 준비",
                logger=logger,
            )
        )
        self.assertEqual(
            fake.select_calls,
            [{"champion_name": "아리", "action_type": "pick", "complete": True}],
        )

    def test_champ_select_action_attempt_waits_when_lcu_has_no_current_action(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeDecisionLcu(LcuDecision(LcuOutcome.NO_CURRENT_ACTION))

        result = entrypoint._champ_select_action_attempt_via_lcu(
            fake,
            "아리",
            action_type="pick",
            complete=True,
            stage="픽 준비",
            logger=logger,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "no_current_action")
        self.assertEqual(len(fake.select_calls), 1)

    def test_champ_select_action_attempt_falls_back_when_lcu_is_unavailable(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeDecisionLcu(LcuDecision(LcuOutcome.UNAVAILABLE))

        result = entrypoint._champ_select_action_attempt_via_lcu(
            fake,
            "아리",
            action_type="pick",
            complete=True,
            stage="픽 준비",
            logger=logger,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.FALLBACK_IMAGE)
        self.assertEqual(result.outcome, "unavailable")

    def test_champ_select_action_attempt_reraises_unexpected_lcu_exception(self) -> None:
        class _FailingDecisionLcu:
            def select_champ_select_champion_decision(
                self,
                champion_name: object,
                *,
                action_type: str,
                complete: bool = False,
            ) -> LcuDecision:
                raise RuntimeError("LCU down")

        logger = logging.getLogger("lolmanager-test-cli-lcu")

        with self.assertRaisesRegex(RuntimeError, "LCU down"):
            entrypoint._champ_select_action_attempt_via_lcu(
                _FailingDecisionLcu(),
                "아리",
                action_type="pick",
                complete=True,
                stage="픽 준비",
                logger=logger,
            )

    def test_champ_select_action_attempt_falls_back_on_request_exception(self) -> None:
        class _FailingDecisionLcu:
            def select_champ_select_champion_decision(
                self,
                champion_name: object,
                *,
                action_type: str,
                complete: bool = False,
            ) -> LcuDecision:
                raise Timeout("LCU down")

        logger = logging.getLogger("lolmanager-test-cli-lcu")

        result = entrypoint._champ_select_action_attempt_via_lcu(
            _FailingDecisionLcu(),
            "아리",
            action_type="pick",
            complete=True,
            stage="픽 준비",
            logger=logger,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.FALLBACK_IMAGE)
        self.assertEqual(result.outcome, "request_exception")

    def test_champ_select_action_attempt_waits_when_champion_not_found(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeDecisionLcu(
            LcuDecision(
                LcuOutcome.CHAMPION_NOT_FOUND,
                reason="champion not found in LCU grid",
            )
        )

        result = entrypoint._champ_select_action_attempt_via_lcu(
            fake,
            "없는챔피언",
            action_type="pick",
            complete=True,
            stage="픽 준비",
            logger=logger,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "champion_not_found")

    def test_champ_select_action_attempt_waits_on_rejected_lcu_write(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeDecisionLcu(
            LcuDecision(
                LcuOutcome.ACTION_REJECTED,
                reason="champ-select action complete not confirmed",
            )
        )

        result = entrypoint._champ_select_action_attempt_via_lcu(
            fake,
            "아리",
            action_type="pick",
            complete=True,
            stage="픽 준비",
            logger=logger,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "action_rejected")

    def test_wait_champ_select_action_retries_lcu_wait_in_place(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeDecisionSequenceLcu(
            [
                LcuDecision(LcuOutcome.NO_CURRENT_ACTION),
                LcuDecision(LcuOutcome.SUCCESS),
            ]
        )

        with (
            mock.patch.object(entrypoint.time, "monotonic", side_effect=[100.0, 100.1]),
            mock.patch.object(entrypoint.time, "sleep") as sleep,
        ):
            result = entrypoint._wait_champ_select_action_via_lcu(
                fake,
                "블라디미르",
                action_type="ban",
                complete=True,
                stage="밴",
                logger=logger,
                interval_sec=0.05,
                timeout_sec=1.0,
            )

        self.assertTrue(result.completed)
        self.assertEqual(
            fake.select_calls,
            [
                {
                    "champion_name": "블라디미르",
                    "action_type": "ban",
                    "complete": True,
                },
                {
                    "champion_name": "블라디미르",
                    "action_type": "ban",
                    "complete": True,
                },
            ],
        )
        sleep.assert_called_once_with(0.05)

    def test_wait_champ_select_action_exits_on_authoritative_phase_change(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        for phase in (
            PHASE_LOBBY,
            PHASE_READY_CHECK,
            PHASE_IN_PROGRESS,
            PHASE_RECONNECT,
            PHASE_WATCH_IN_PROGRESS,
        ):
            with self.subTest(phase=phase):
                fake = _FakeChampSelectRetryPhaseLcu(phase)

                with (
                    mock.patch.object(entrypoint.time, "monotonic", return_value=100.0),
                    mock.patch.object(entrypoint.time, "sleep") as sleep,
                ):
                    result = entrypoint._wait_champ_select_action_via_lcu(
                        fake,
                        "블라디미르",
                        action_type="ban",
                        complete=True,
                        stage="밴",
                        logger=logger,
                        interval_sec=0.05,
                        timeout_sec=20.0,
                    )

                self.assertFalse(result.completed)
                self.assertEqual(result.loop_action, LcuLoopAction.ACT_LCU)
                self.assertEqual(result.outcome, f"phase_exit:{phase}")
                self.assertEqual(fake.phase_calls, 1)
                sleep.assert_not_called()

    def test_champ_select_phase_guard_recovers_no_session_dodge_rematch_sequence(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeChampSelectPhaseGuardLcu(
            [PHASE_LOBBY, PHASE_MATCHMAKING, PHASE_READY_CHECK, PHASE_CHAMP_SELECT]
        )
        entrypoint._last_lcu_ready_accept_at.clear()
        wait_attempt = entrypoint.ChampSelectLcuAttempt(
            False,
            LcuLoopAction.WAIT_AUTHORITATIVE,
            LcuOutcome.NO_SESSION.value,
        )

        guarded_attempts = []
        handled = []
        with mock.patch.object(entrypoint, "LCU_READY_ACCEPT_COOLDOWN_SEC", 0.0):
            for _ in range(4):
                guarded = entrypoint._guard_champ_select_phase_exit(
                    wait_attempt, fake, "픽 준비", logger
                )
                guarded_attempts.append(guarded)
                handled.append(
                    entrypoint._handle_champ_select_phase_exit(
                        guarded, fake, "픽 준비", logger
                    )
                )

        self.assertEqual(
            [attempt.outcome for attempt in guarded_attempts],
            [
                f"phase_exit:{PHASE_LOBBY}",
                f"phase_exit:{PHASE_MATCHMAKING}",
                f"phase_exit:{PHASE_READY_CHECK}",
                LcuOutcome.NO_SESSION.value,
            ],
        )
        self.assertEqual(handled, [True, True, True, False])
        self.assertEqual(fake.accept_calls, 1)
        self.assertEqual(fake.phase_calls, 4)

    def test_champ_select_phase_guard_handles_direct_ready_check_after_no_current_action(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeChampSelectPhaseGuardLcu([PHASE_READY_CHECK])
        entrypoint._last_lcu_ready_accept_at.clear()

        with mock.patch.object(entrypoint, "LCU_READY_ACCEPT_COOLDOWN_SEC", 0.0):
            guarded = entrypoint._guard_champ_select_phase_exit(
                entrypoint.ChampSelectLcuAttempt(
                    False,
                    LcuLoopAction.WAIT_AUTHORITATIVE,
                    LcuOutcome.NO_CURRENT_ACTION.value,
                ),
                fake,
                "픽 준비",
                logger,
            )
            handled = entrypoint._handle_champ_select_phase_exit(
                guarded, fake, "픽 준비", logger
            )

        self.assertTrue(handled)
        self.assertEqual(guarded.loop_action, LcuLoopAction.ACT_LCU)
        self.assertEqual(guarded.outcome, f"phase_exit:{PHASE_READY_CHECK}")
        self.assertEqual(fake.accept_calls, 1)

    def test_champ_select_phase_guard_exits_reserve_pick_on_matchmaking(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeChampSelectPhaseGuardLcu([PHASE_MATCHMAKING])

        guarded = entrypoint._guard_champ_select_phase_exit(
            entrypoint.ChampSelectLcuAttempt(
                False,
                LcuLoopAction.WAIT_AUTHORITATIVE,
                LcuOutcome.NO_SESSION.value,
            ),
            fake,
            "예비 픽 준비",
            logger,
        )

        self.assertEqual(guarded.loop_action, LcuLoopAction.ACT_LCU)
        self.assertEqual(guarded.outcome, f"phase_exit:{PHASE_MATCHMAKING}")

    def test_champ_select_phase_guard_keeps_champ_select_and_only_falls_back_on_lcu_failure(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        wait_attempt = entrypoint.ChampSelectLcuAttempt(
            False,
            LcuLoopAction.WAIT_AUTHORITATIVE,
            LcuOutcome.NO_CURRENT_ACTION.value,
        )
        fake = _FakeChampSelectPhaseGuardLcu(
            [
                PHASE_CHAMP_SELECT,
                LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout"),
            ]
        )

        still_waiting = entrypoint._guard_champ_select_phase_exit(
            wait_attempt, fake, "픽 준비", logger
        )
        fallback = entrypoint._guard_champ_select_phase_exit(
            wait_attempt, fake, "픽 준비", logger
        )

        self.assertEqual(still_waiting, wait_attempt)
        self.assertEqual(fallback.loop_action, LcuLoopAction.FALLBACK_IMAGE)
        self.assertEqual(fallback.outcome, "phase_probe:request_failed")

    def test_champ_select_phase_exit_handler_accepts_ready_check(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        attempt = entrypoint.ChampSelectLcuAttempt(
            False,
            LcuLoopAction.ACT_LCU,
            f"phase_exit:{PHASE_READY_CHECK}",
        )

        with mock.patch.object(entrypoint, "_accept_ready_check_via_lcu") as accept:
            handled = entrypoint._handle_champ_select_phase_exit(
                attempt, object(), "밴", logger
            )

        self.assertTrue(handled)
        accept.assert_called_once_with(mock.ANY, "밴", logger)

    def test_champ_select_phase_exit_handler_restarts_other_phases(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        for phase in (PHASE_LOBBY, PHASE_IN_PROGRESS, PHASE_RECONNECT):
            with (
                self.subTest(phase=phase),
                mock.patch.object(entrypoint, "_accept_ready_check_via_lcu") as accept,
            ):
                handled = entrypoint._handle_champ_select_phase_exit(
                    entrypoint.ChampSelectLcuAttempt(
                        False,
                        LcuLoopAction.ACT_LCU,
                        f"phase_exit:{phase}",
                    ),
                    object(),
                    "프리픽",
                    logger,
                )

            self.assertTrue(handled)
            accept.assert_not_called()

    def test_champ_select_phase_exit_clears_pick_turn_before_rematch(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        entrypoint.RUNTIME_STATE["is_my_pick_turn"] = True
        entrypoint.RUNTIME_STATE["my_pick_turn_updated_at"] = 1.0

        try:
            with mock.patch.object(entrypoint.time, "monotonic", return_value=25.0):
                handled = entrypoint._handle_champ_select_phase_exit(
                    entrypoint.ChampSelectLcuAttempt(
                        False,
                        LcuLoopAction.ACT_LCU,
                        f"phase_exit:{PHASE_MATCHMAKING}",
                    ),
                    object(),
                    "밴",
                    logger,
                )

            self.assertTrue(handled)
            self.assertFalse(entrypoint.RUNTIME_STATE["is_my_pick_turn"])
            self.assertEqual(
                entrypoint.RUNTIME_STATE["my_pick_turn_updated_at"],
                25.0,
            )
        finally:
            entrypoint.RUNTIME_STATE["is_my_pick_turn"] = False

    def test_champ_select_session_reset_clears_stale_runtime_state(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.BANPICK
        entrypoint.RUNTIME_STATE["is_my_pick_turn"] = True
        entrypoint._my_pick_turn_miss_streak = 2

        try:
            with mock.patch.object(entrypoint.time, "monotonic", return_value=30.0):
                handled = entrypoint._handle_champ_select_phase_exit(
                    entrypoint.ChampSelectLcuAttempt(
                        False,
                        LcuLoopAction.ACT_LCU,
                        "session_reset:identity_changed",
                    ),
                    object(),
                    "밴",
                    logger,
                )

            self.assertTrue(handled)
            self.assertEqual(
                entrypoint.RUNTIME_STATE["client_state"],
                entrypoint.ClientState.UNKNOWN,
            )
            self.assertFalse(entrypoint.RUNTIME_STATE["is_my_pick_turn"])
            self.assertEqual(entrypoint._my_pick_turn_miss_streak, 0)
        finally:
            entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.UNKNOWN
            entrypoint.RUNTIME_STATE["is_my_pick_turn"] = False
            entrypoint._my_pick_turn_miss_streak = 0

    def test_missing_ban_skips_ban_action_without_lcu_write(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        with mock.patch.object(entrypoint, "_wait_champ_select_action_via_lcu") as wait:
            result = entrypoint._ban_champ_select_attempt_or_skip(
                object(),
                "",
                logger=logger,
                interval_sec=0.1,
            )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "missing_ban")
        wait.assert_not_called()

    def test_failed_auto_ban_resolution_skips_ban_action_without_lcu_write(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")

        with tempfile.TemporaryDirectory() as tmp:
            resolved = entrypoint.resolve_ban_name_for_runtime(
                Path(tmp) / "missing-counter-cache.json",
                role="top",
                champion_name="말파이트",
                configured_ban=AUTO_BAN_VALUE,
                logger=logger,
                now=100.0,
            )

        self.assertEqual(resolved, "")

        with mock.patch.object(entrypoint, "_wait_champ_select_action_via_lcu") as wait:
            result = entrypoint._ban_champ_select_attempt_or_skip(
                object(),
                resolved,
                logger=logger,
                interval_sec=0.1,
            )

        self.assertEqual(result.outcome, "missing_ban")
        wait.assert_not_called()

    def test_lcu_champ_select_action_state_marks_ban_turn(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeLocalActionLcu("ban")
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.PREPICK

        action_type = entrypoint._apply_lcu_champ_select_action_state(
            fake, 100.0, logger, "테스트"
        )

        self.assertEqual(action_type, "ban")
        self.assertEqual(
            entrypoint.RUNTIME_STATE["client_state"],
            entrypoint.ClientState.BANPICK,
        )

    def test_lcu_champ_select_action_state_marks_pick_turn(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeLocalActionLcu("pick")
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.BANPICK
        entrypoint.RUNTIME_STATE["is_my_pick_turn"] = False

        action_type = entrypoint._apply_lcu_champ_select_action_state(
            fake, 100.0, logger, "테스트"
        )

        self.assertEqual(action_type, "pick")
        self.assertEqual(
            entrypoint.RUNTIME_STATE["client_state"],
            entrypoint.ClientState.PICK,
        )
        self.assertTrue(entrypoint.RUNTIME_STATE["is_my_pick_turn"])

    def test_champ_select_phase_does_not_regress_wait_game_start_to_prepick(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(PHASE_CHAMP_SELECT)
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.WAIT_GAME_START

        phase = entrypoint._poll_lcu_phase(fake, logger, "인게임 시작 대기")

        self.assertEqual(phase, PHASE_CHAMP_SELECT)
        self.assertEqual(
            entrypoint.RUNTIME_STATE["client_state"],
            entrypoint.ClientState.WAIT_GAME_START,
        )

    def test_ui_action_classification_marks_unverified_image_paths(self) -> None:
        self.assertEqual(
            entrypoint.LCU_UI_ACTION_CLASSIFICATION["champ_select_pick"],
            "lcu-first",
        )
        self.assertEqual(
            entrypoint.LCU_UI_ACTION_CLASSIFICATION["pick_popups"],
            "lcu-first",
        )
        self.assertEqual(
            entrypoint.LCU_UI_ACTION_CLASSIFICATION["pick_myturn"],
            "fallback-only",
        )
        self.assertEqual(
            entrypoint.LCU_UI_ACTION_CLASSIFICATION["postgame_end_buttons"],
            "ui-only",
        )
        self.assertEqual(
            entrypoint.LCU_UI_ACTION_CLASSIFICATION["postgame_continue"],
            "lcu-first",
        )
        self.assertEqual(
            entrypoint.LCU_UI_ACTION_CLASSIFICATION["postgame_honor_vote"],
            "lcu-only-terminal",
        )
        self.assertEqual(
            entrypoint.LCU_UI_ACTION_CLASSIFICATION["blocking_modals"],
            "lcu-first",
        )

    def test_pick_popup_scan_does_not_include_semantic_myturn_template(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        rect = (0, 0, 1280, 720)

        with mock.patch.object(
            entrypoint, "find_template_matches_once", return_value={}
        ) as find_matches:
            entrypoint.try_pick_popups(rect, [], None, 0.85, logger)

        find_matches.assert_called_once_with(
            rect, [], threshold=0.85, search_rois={}
        )

    def test_pick_popups_dismisses_confirm_via_lcu_before_image_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        rect = (0, 0, 1280, 720)
        fake = _FakeBlockingModalLcu(
            LcuDecision(LcuOutcome.SUCCESS, reason="modal dismissed")
        )

        with mock.patch.object(entrypoint, "find_template_matches_once") as find_matches:
            handled = entrypoint.try_pick_popups(
                rect,
                [Path("client_confirm-button-2.png")],
                None,
                0.85,
                logger,
                lcu=fake,
            )

        self.assertTrue(handled)
        self.assertEqual(fake.dismiss_calls, 1)
        find_matches.assert_not_called()

    def test_pick_popups_clicks_only_decline_when_both_actions_match(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        rect = (0, 0, 1280, 720)
        entrypoint._last_popup_click_at.clear()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tpl_confirm = root / "client_confirm-button-2.png"
            tpl_decline = root / "pick_decline-button.png"
            tpl_confirm.write_bytes(b"placeholder")
            tpl_decline.write_bytes(b"placeholder")

            matches = {
                "decline": ((320, 420), object(), 0.90),
                "confirm#0": ((640, 420), object(), 0.99),
            }
            with (
                mock.patch.object(
                    entrypoint, "find_template_matches_once", return_value=matches
                ),
                mock.patch.object(entrypoint, "click_screen") as click_screen,
                mock.patch.object(entrypoint.time, "monotonic", return_value=100.0),
            ):
                handled = entrypoint.try_pick_popups(
                    rect, [tpl_confirm], tpl_decline, 0.85, logger
                )

        self.assertTrue(handled)
        click_screen.assert_called_once_with((320, 420))

    def test_myturn_image_fallback_helper_updates_pick_turn(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        rect = (0, 0, 1280, 720)
        entrypoint.RUNTIME_STATE["client_state"] = entrypoint.ClientState.PICK
        entrypoint.RUNTIME_STATE["is_my_pick_turn"] = False

        with mock.patch.object(
            entrypoint,
            "find_template_matches_once",
            return_value={"myturn": ((640, 360), object(), 0.99)},
        ) as find_matches:
            entrypoint._update_my_pick_turn_from_image(
                rect, Path("pick_myturn-text.png"), 0.85, logger
            )

        self.assertTrue(entrypoint.RUNTIME_STATE["is_my_pick_turn"])
        find_matches.assert_called_once_with(
            rect,
            [("myturn", Path("pick_myturn-text.png"))],
            threshold=0.85,
        )

    def test_role_detection_uses_lcu_before_image_fallback(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeRoleLcu(LcuDecision(LcuOutcome.SUCCESS, value="mid"))
        image_detector = mock.Mock(return_value="top")

        role = entrypoint._detect_role_lcu_first(
            fake,
            stage="포지션 탐색",
            logger=logger,
            image_detector=image_detector,
        )

        self.assertEqual(role, "mid")
        self.assertEqual(fake.position_calls, 1)
        image_detector.assert_not_called()

    def test_role_detection_waits_when_lcu_has_no_role(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeRoleLcu(
            LcuDecision(LcuOutcome.NO_POSITION, reason="position unavailable")
        )
        image_detector = mock.Mock(return_value="support")

        role = entrypoint._detect_role_lcu_first(
            fake,
            stage="포지션 탐색",
            logger=logger,
            image_detector=image_detector,
        )

        self.assertIsNone(role)
        self.assertEqual(fake.position_calls, 1)
        image_detector.assert_not_called()

    def test_role_detection_falls_back_to_image_when_lcu_request_fails(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeRoleLcu(
            LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout")
        )
        image_detector = mock.Mock(return_value="support")

        role = entrypoint._detect_role_lcu_first(
            fake,
            stage="포지션 탐색",
            logger=logger,
            image_detector=image_detector,
        )

        self.assertEqual(role, "support")
        self.assertEqual(fake.position_calls, 1)
        image_detector.assert_called_once_with()

    def test_role_detection_minimized_image_fallback_yields_to_lcu_loop(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeRoleLcu(
            LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout")
        )
        minimized_rect = (-32000, -32000, -31900, -31900)

        def image_detector() -> Optional[str]:
            rect = entrypoint._visible_rect_or_wait(logger, "포지션 탐색", 0.2)
            if rect is None:
                return None
            return "support"

        with (
            mock.patch.object(
                entrypoint, "find_league_window_rect", return_value=minimized_rect
            ),
            mock.patch.object(entrypoint, "_LEAGUE_EXIT_GUARD", None),
            mock.patch.object(entrypoint, "find_best_template") as find_best_template,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "click_relative") as click_relative,
            mock.patch.object(entrypoint, "click_screen") as click_screen,
            mock.patch.object(entrypoint.time, "sleep") as sleep,
        ):
            attempt = entrypoint._detect_role_lcu_first_with_retry_attempt(
                fake,
                stage="포지션 탐색",
                logger=logger,
                image_detector=image_detector,
                interval_sec=0.2,
            )

        self.assertIsNone(attempt.role)
        self.assertEqual(attempt.loop_action, LcuLoopAction.FALLBACK_IMAGE)
        self.assertEqual(fake.position_calls, 1)
        sleep.assert_called_once_with(0.2)
        find_best_template.assert_not_called()
        search.assert_not_called()
        click_relative.assert_not_called()
        click_screen.assert_not_called()

    def test_role_detection_retries_semantic_wait_with_sleep(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeRoleSequenceLcu(
            [
                LcuDecision(LcuOutcome.NO_POSITION, reason="position unavailable"),
                LcuDecision(LcuOutcome.SUCCESS, value="mid"),
            ]
        )
        image_detector = mock.Mock(return_value="support")

        with mock.patch.object(entrypoint.time, "sleep") as sleep:
            role = entrypoint._detect_role_lcu_first_with_retry(
                fake,
                stage="포지션 탐색",
                logger=logger,
                image_detector=image_detector,
                interval_sec=1.0,
            )

        self.assertEqual(role, "mid")
        self.assertEqual(fake.position_calls, 2)
        image_detector.assert_not_called()
        sleep.assert_called_once_with(1.0)

    def test_role_detection_retry_exits_when_champ_select_ends(self) -> None:
        class _RolePhaseLcu(_FakeRoleLcu):
            def get_gameflow_phase_decision(
                self, *, max_age_sec: float = 0.25
            ) -> LcuDecision:
                return LcuDecision(LcuOutcome.SUCCESS, value=PHASE_LOBBY)

            def consume_phase_transition(self, phase: str):
                return None

        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _RolePhaseLcu(
            LcuDecision(LcuOutcome.NO_POSITION, reason="position unavailable")
        )
        image_detector = mock.Mock(return_value="support")

        with mock.patch.object(entrypoint.time, "sleep") as sleep:
            attempt = entrypoint._detect_role_lcu_first_with_retry_attempt(
                fake,
                stage="포지션 탐색",
                logger=logger,
                image_detector=image_detector,
                interval_sec=1.0,
            )

        self.assertIsNone(attempt.role)
        self.assertEqual(attempt.loop_action, LcuLoopAction.ACT_LCU)
        self.assertEqual(attempt.outcome, "phase_exit:Lobby")
        image_detector.assert_not_called()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class AutoLobbyCreatePolicyTests(unittest.TestCase):
    def test_should_click_popup_confirm_only_in_lobby_phase(self) -> None:
        self.assertTrue(
            entrypoint._should_click_popup_confirm_at_cycle_start(PHASE_LOBBY)
        )
        self.assertFalse(
            entrypoint._should_click_popup_confirm_at_cycle_start(None)
        )
        self.assertFalse(
            entrypoint._should_click_popup_confirm_at_cycle_start(PHASE_NONE)
        )
        self.assertFalse(
            entrypoint._should_click_popup_confirm_at_cycle_start(PHASE_CHAMP_SELECT)
        )

    def test_should_attempt_auto_lobby_create_requires_home_screen_and_interval(
        self,
    ) -> None:
        self.assertTrue(entrypoint._should_attempt_auto_lobby_create(None, 10.0))
        self.assertTrue(
            entrypoint._should_attempt_auto_lobby_create(PHASE_NONE, 10.0)
        )
        self.assertFalse(entrypoint._should_attempt_auto_lobby_create(None, 5.0))
        self.assertFalse(
            entrypoint._should_attempt_auto_lobby_create(PHASE_LOBBY, 999.0)
        )
        self.assertFalse(
            entrypoint._should_attempt_auto_lobby_create(PHASE_CHAMP_SELECT, 999.0)
        )

    def test_maybe_auto_create_lobby_creates_once_per_interval(self) -> None:
        class _FakeCreateLcu:
            def __init__(self) -> None:
                self.create_calls = 0

            def create_lobby_decision(self) -> LcuDecision:
                self.create_calls += 1
                return LcuDecision(LcuOutcome.SUCCESS, reason="created")

        fake = _FakeCreateLcu()
        entrypoint._last_auto_lobby_create_at = 0.0

        with mock.patch.object(entrypoint.time, "monotonic", return_value=100.0):
            entrypoint._maybe_auto_create_lobby_for_home_screen(
                cast(Any, fake), None, logging.getLogger("test")
            )
            self.assertEqual(fake.create_calls, 1)

            entrypoint._maybe_auto_create_lobby_for_home_screen(
                cast(Any, fake), None, logging.getLogger("test")
            )
            self.assertEqual(fake.create_calls, 1)

        with mock.patch.object(entrypoint.time, "monotonic", return_value=115.0):
            entrypoint._maybe_auto_create_lobby_for_home_screen(
                cast(Any, fake), None, logging.getLogger("test")
            )
            self.assertEqual(fake.create_calls, 2)

    def test_maybe_auto_create_lobby_skips_non_home_phases(self) -> None:
        class _FakeCreateLcu:
            def __init__(self) -> None:
                self.create_calls = 0

            def create_lobby_decision(self) -> LcuDecision:
                self.create_calls += 1
                return LcuDecision(LcuOutcome.SUCCESS, reason="created")

        fake = _FakeCreateLcu()
        entrypoint._last_auto_lobby_create_at = 0.0

        entrypoint._maybe_auto_create_lobby_for_home_screen(
            cast(Any, fake), PHASE_LOBBY, logging.getLogger("test")
        )

        self.assertEqual(fake.create_calls, 0)
