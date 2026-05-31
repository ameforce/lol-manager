from __future__ import annotations

import logging
import sys
from pathlib import Path
import unittest
from unittest import mock

from requests import Timeout

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

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


class _FakeBlockingModalLcu:
    def __init__(self, result: LcuDecision) -> None:
        self.result = result
        self.dismiss_calls = 0

    def dismiss_blocking_modal_decision(self) -> LcuDecision:
        self.dismiss_calls += 1
        return self.result


class CliLcuStateTests(unittest.TestCase):
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
            mock.patch.object(entrypoint, "ensure_active_rect", return_value=(0, 0, 1280, 720)),
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

    def test_postgame_end_of_game_dismisses_stats_via_lcu_without_image_scan(
        self,
    ) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameContinueLcu(
            LcuDecision(LcuOutcome.SUCCESS, reason="dismissed")
        )

        with (
            mock.patch.object(entrypoint, "ensure_active_rect") as ensure_rect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
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
        ensure_rect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_postgame_request_failure_keeps_ui_only_end_button_scan(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseDecisionLcu(
            LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout")
        )

        with (
            mock.patch.object(entrypoint, "ensure_active_rect", return_value=(0, 0, 1280, 720)),
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

    def test_postgame_none_phase_blocks_find_match_fallback(self) -> None:
        class _FakePostgameSequenceLcu(_FakePhaseDecisionSequenceLcu):
            def __init__(self) -> None:
                super().__init__(
                    [
                        LcuDecision(LcuOutcome.SUCCESS, value=PHASE_END_OF_GAME),
                        LcuDecision(LcuOutcome.SUCCESS, value=PHASE_NONE),
                    ]
                )
                self.start_calls = 0

            def is_end_of_game_stats_available(self) -> bool:
                return False

            def start_matchmaking_decision(self) -> LcuDecision:
                self.start_calls += 1
                return LcuDecision(LcuOutcome.REQUEST_FAILED, reason="ReadTimeout")

        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameSequenceLcu()

        with (
            mock.patch.object(entrypoint, "ensure_active_rect", return_value=(0, 0, 1280, 720)),
            mock.patch.object(entrypoint, "detect_champion_select", return_value=False),
            mock.patch.object(
                entrypoint,
                "poll_match_state",
                return_value=entrypoint.MatchPollAttempt(
                    False,
                    False,
                    LcuLoopAction.ACT_LCU,
                    "end_of_game",
                    PHASE_END_OF_GAME,
                ),
            ),
            mock.patch.object(entrypoint, "search_and_act") as search,
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

        self.assertEqual(fake.start_calls, 0)
        search.assert_not_called()

    def test_postgame_pre_end_honor_attempt_returns_without_image_fallback(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePostgameHonorLcu(
            LcuDecision(LcuOutcome.UNSUPPORTED, reason="no confirmed honor route")
        )

        with (
            mock.patch.object(entrypoint, "ensure_active_rect") as ensure_rect,
            mock.patch.object(entrypoint, "search_and_act") as search,
            mock.patch.object(entrypoint, "poll_match_state") as poll,
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
        ensure_rect.assert_not_called()
        search.assert_not_called()
        poll.assert_not_called()

    def test_blocking_modal_attempt_treats_unsupported_as_terminal(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeBlockingModalLcu(
            LcuDecision(LcuOutcome.UNSUPPORTED, reason="no confirmed modal route")
        )

        result = entrypoint._dismiss_blocking_modal_lcu_attempt(
            fake, "사이클 시작", logger
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.ABORT_LOG)
        self.assertEqual(result.outcome, "unsupported")
        self.assertEqual(fake.dismiss_calls, 1)

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
            "ui-only",
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
            "lcu-only-terminal",
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
