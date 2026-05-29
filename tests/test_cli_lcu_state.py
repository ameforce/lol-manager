from __future__ import annotations

import logging
import sys
from pathlib import Path
import unittest
from unittest import mock

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
    PHASE_PRE_END_OF_GAME,
    PHASE_READY_CHECK,
    PHASE_WAITING_FOR_STATS,
    LcuOutcome,
)


class _FakeLcu:
    def __init__(self) -> None:
        self.accept_calls = 0

    def accept_ready_check(self) -> bool:
        self.accept_calls += 1
        return True


class _FakePhaseLcu:
    def __init__(self, phase: str | None) -> None:
        self.phase = phase

    def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> str | None:
        return self.phase

    def consume_phase_transition(self, phase: str):
        return None


class _FakeWriteLcu:
    def __init__(
        self,
        *,
        start_result: bool = True,
        select_result: bool = True,
        phase: str | None = None,
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


class _FakeRoleLcu:
    def __init__(self, result: LcuDecision) -> None:
        self.result = result
        self.position_calls = 0

    def get_local_player_position(self) -> LcuDecision:
        self.position_calls += 1
        return self.result


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

    def test_lcu_ready_check_accept_is_cooled_down(self) -> None:
        fake = _FakeLcu()
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        entrypoint._last_lcu_ready_accept_at.clear()

        with mock.patch.object(entrypoint.time, "monotonic", side_effect=[100.0, 100.1]):
            self.assertTrue(entrypoint._accept_ready_check_via_lcu(fake, "stage", logger))
            self.assertTrue(entrypoint._accept_ready_check_via_lcu(fake, "stage", logger))

        self.assertEqual(fake.accept_calls, 1)

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

    def test_start_matchmaking_via_lcu_uses_lcu_first(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakeWriteLcu(start_result=True)

        self.assertTrue(entrypoint._start_matchmaking_via_lcu(fake, "stage", logger))
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

    def test_lobby_confirm_template_does_not_click_popup_confirm(self) -> None:
        logger = logging.getLogger("lolmanager-test-cli-lcu")
        fake = _FakePhaseLcu(None)
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
            accepted, finding = entrypoint.poll_match_state(
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

        self.assertFalse(accepted)
        self.assertFalse(finding)
        find_matches.assert_called_once()
        click_screen.assert_not_called()

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

    def test_champ_select_action_attempt_waits_when_lcu_is_unavailable(self) -> None:
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
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "unavailable")

    def test_champ_select_action_attempt_waits_on_lcu_exception(self) -> None:
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

        result = entrypoint._champ_select_action_attempt_via_lcu(
            _FailingDecisionLcu(),
            "아리",
            action_type="pick",
            complete=True,
            stage="픽 준비",
            logger=logger,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.loop_action, LcuLoopAction.WAIT_AUTHORITATIVE)
        self.assertEqual(result.outcome, "exception")

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
            "fallback-only",
        )
        self.assertEqual(
            entrypoint.LCU_UI_ACTION_CLASSIFICATION["postgame_end_buttons"],
            "unverified-candidate",
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

    def test_role_detection_falls_back_to_image_when_lcu_has_no_role(self) -> None:
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

        self.assertEqual(role, "support")
        self.assertEqual(fake.position_calls, 1)
        image_detector.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
