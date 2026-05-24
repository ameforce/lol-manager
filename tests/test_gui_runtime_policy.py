from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lolmanager.gui.app_gui import (
    EXTERNAL_SYNC_MS,
    external_sync_delay_ms,
    should_recover_cli_exit,
)


class GuiRuntimePolicyTests(unittest.TestCase):
    def test_external_sync_uses_slower_delay_in_game(self) -> None:
        self.assertEqual(external_sync_delay_ms(in_game=False), EXTERNAL_SYNC_MS)
        self.assertGreater(
            external_sync_delay_ms(in_game=True),
            external_sync_delay_ms(in_game=False),
        )

    def test_recover_cli_exit_only_for_unexpected_live_client_exit(self) -> None:
        self.assertTrue(
            should_recover_cli_exit(
                exit_code=1,
                stop_requested=False,
                planned_restart=False,
                league_running=True,
                restart_count=0,
                restart_limit=1,
            )
        )
        self.assertFalse(
            should_recover_cli_exit(
                exit_code=0,
                stop_requested=False,
                planned_restart=False,
                league_running=True,
                restart_count=0,
                restart_limit=1,
            )
        )
        self.assertFalse(
            should_recover_cli_exit(
                exit_code=1,
                stop_requested=True,
                planned_restart=False,
                league_running=True,
                restart_count=0,
                restart_limit=1,
            )
        )
        self.assertFalse(
            should_recover_cli_exit(
                exit_code=1,
                stop_requested=False,
                planned_restart=False,
                league_running=True,
                restart_count=1,
                restart_limit=1,
            )
        )


if __name__ == "__main__":
    unittest.main()
