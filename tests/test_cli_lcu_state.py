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
    PHASE_CHAMP_SELECT,
    PHASE_IN_PROGRESS,
    PHASE_LOBBY,
    PHASE_MATCHMAKING,
    PHASE_READY_CHECK,
)


class _FakeLcu:
    def __init__(self) -> None:
        self.accept_calls = 0

    def accept_ready_check(self) -> bool:
        self.accept_calls += 1
        return True


class _FakePhaseLcu:
    def __init__(self, phase: str) -> None:
        self.phase = phase

    def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> str:
        return self.phase

    def consume_phase_transition(self, phase: str):
        return None


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


if __name__ == "__main__":
    unittest.main()
