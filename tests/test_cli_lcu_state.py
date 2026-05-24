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


if __name__ == "__main__":
    unittest.main()
