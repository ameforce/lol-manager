from __future__ import annotations

import inspect
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lolmanager.gui.app_gui import (
    EXTERNAL_SYNC_MS,
    LolManagerGui,
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

    def test_opgg_shutdown_hook_is_only_in_client_auto_exit_branch(self) -> None:
        sync_source = inspect.getsource(LolManagerGui._sync_external_state)
        stop_source = inspect.getsource(LolManagerGui.stop)
        close_source = inspect.getsource(LolManagerGui._on_close)

        self.assertIn("LoL client closed. Exiting.", sync_source)
        self.assertIn("close_owned_opgg_for_current_session", sync_source)
        self.assertNotIn("close_owned_opgg_for_current_session", stop_source)
        self.assertNotIn("close_owned_opgg_for_current_session", close_source)


if __name__ == "__main__":
    unittest.main()
