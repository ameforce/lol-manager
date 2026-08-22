from __future__ import annotations

import inspect
import queue
import sys
import threading
import time
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lolmanager.gui.app_gui import (
    EXTERNAL_SYNC_MS,
    _ExternalSyncSnapshot,
    LolManagerGui,
    external_sync_delay_ms,
    normalize_process_cpu_percent,
    should_auto_iconify_ingame,
    should_recover_cli_exit,
)


class _Value:
    def __init__(self) -> None:
        self.value: object = None

    def set(self, value: object) -> None:
        self.value = value


class _Button:
    def __init__(self) -> None:
        self.state: object = None

    def configure(self, **kwargs: object) -> None:
        self.state = kwargs.get("state")


class _Proc:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True


class _Root:
    def __init__(self) -> None:
        self.iconify_calls = 0
        self.after_calls: list[tuple[int, object]] = []
        self.destroyed = False

    def iconify(self) -> None:
        self.iconify_calls += 1

    def after(self, delay: int, callback: object) -> str:
        self.after_calls.append((delay, callback))
        return str(len(self.after_calls))

    def destroy(self) -> None:
        self.destroyed = True


class _LogWindow:
    def __init__(self, *, iconic: bool = False) -> None:
        self.iconic = iconic
        self.iconify_calls = 0

    def state(self) -> str:
        return "iconic" if self.iconic else "normal"

    def iconify(self) -> None:
        self.iconic = True
        self.iconify_calls += 1


class _OneIterationStop:
    def __init__(self) -> None:
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        return self.calls > 1

    def wait(self, _seconds: float) -> None:
        return None


class GuiRuntimePolicyTests(unittest.TestCase):
    def _make_windowing_gui(self) -> LolManagerGui:
        gui = LolManagerGui.__new__(LolManagerGui)
        gui.proc = None
        gui.root = _Root()
        gui._log_window = _LogWindow()
        gui._external_sync_q = queue.Queue(maxsize=1)
        gui._external_sync_last = None
        gui._last_config_mtime_ns = 0
        gui._pending_config_apply_at = None
        gui._auto_start_pending = False
        gui._match_stats_last_poll_at = time.monotonic()
        gui._lol_topmost_enabled = False
        gui._auto_iconified = False
        gui._client_seen_once = False
        gui._client_closed_at = None
        gui.client_visible_var = _Value()
        gui._is_root_iconic = mock.Mock(return_value=False)
        gui._is_ui_busy = mock.Mock(return_value=False)
        gui._refresh_match_stats = mock.Mock()
        gui._append_log = mock.Mock()
        gui._apply_lol_topmost = mock.Mock()
        gui._restore_active_window_after_topmost_release = mock.Mock()
        gui._show_root_noactivate = mock.Mock()
        gui._snap_to_client = mock.Mock()
        return gui

    @staticmethod
    def _snapshot(
        *,
        in_game: bool,
        rect: tuple[int, int, int, int] | None = None,
        minimized: bool = False,
        league_running: bool = True,
        league_foreground: bool = False,
    ) -> _ExternalSyncSnapshot:
        return _ExternalSyncSnapshot(
            config_mtime_ns=0,
            in_game=in_game,
            rect=rect,
            minimized=minimized,
            league_running=league_running,
            league_foreground=league_foreground,
        )

    def test_external_sync_uses_slower_delay_in_game(self) -> None:
        self.assertEqual(external_sync_delay_ms(in_game=False), EXTERNAL_SYNC_MS)
        self.assertGreater(
            external_sync_delay_ms(in_game=True),
            external_sync_delay_ms(in_game=False),
        )

    def test_ingame_focus_policy_keeps_user_forced_manager_focus_visible(self) -> None:
        self.assertFalse(
            should_auto_iconify_ingame(
                manager_has_focus=True,
                root_iconic=False,
            )
        )
        self.assertTrue(
            should_auto_iconify_ingame(
                manager_has_focus=False,
                root_iconic=False,
            )
        )
        self.assertFalse(
            should_auto_iconify_ingame(
                manager_has_focus=False,
                root_iconic=True,
            )
        )

    def test_process_cpu_display_is_normalized_to_total_machine_capacity(self) -> None:
        self.assertEqual(normalize_process_cpu_percent(250.0, logical_cpu_count=4), 62.5)
        self.assertEqual(normalize_process_cpu_percent(900.0, logical_cpu_count=4), 100.0)
        self.assertEqual(normalize_process_cpu_percent(-5.0, logical_cpu_count=4), 0.0)

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
        self.assertIn("self._on_close(close_opgg=True)", sync_source)
        self.assertNotIn("close_running_opgg", stop_source)
        self.assertIn("if close_opgg:", close_source)
        self.assertIn("close_running_opgg", close_source)

    def test_stop_terminates_only_cli_process(self) -> None:
        gui = LolManagerGui.__new__(LolManagerGui)
        gui.proc = _Proc()
        gui._external_sync_stop = threading.Event()
        gui._stop_requested_at = None
        gui.running_var = _Value()
        gui.btn_stop = _Button()
        gui._reset_match_timer_ui = mock.Mock()
        gui._append_log = mock.Mock()

        gui.stop()

        self.assertTrue(gui.proc.terminated)
        self.assertFalse(gui._external_sync_stop.is_set())
        self.assertEqual(gui.running_var.value, "Stopping...")

    def test_external_snapshot_uses_game_process_without_cli(self) -> None:
        gui = LolManagerGui.__new__(LolManagerGui)
        gui.config_path = mock.Mock()
        gui.config_path.stat.return_value = mock.Mock(st_mtime_ns=123)
        gui._league_exit_guard = mock.Mock()
        gui._league_exit_guard.poll_is_running.return_value = True
        rect = (10, 20, 1290, 740)

        with (
            mock.patch(
                "lolmanager.gui.app_gui.is_game_client_active", return_value=True
            ),
            mock.patch(
                "lolmanager.gui.app_gui.find_league_window_rect", return_value=rect
            ),
            mock.patch(
                "lolmanager.gui.app_gui.is_rect_minimized", return_value=False
            ),
            mock.patch(
                "lolmanager.gui.app_gui.is_league_client_foreground",
                return_value=True,
            ),
        ):
            snapshot = gui._collect_external_sync_snapshot()

        self.assertTrue(snapshot.in_game)
        self.assertEqual(snapshot.rect, rect)
        self.assertTrue(snapshot.league_running)
        self.assertTrue(snapshot.league_foreground)

    def test_stopped_automation_recovers_window_after_game_exit(self) -> None:
        gui = self._make_windowing_gui()
        gui._external_sync_q.put(self._snapshot(in_game=True))

        gui._sync_external_state()

        self.assertEqual(gui.client_visible_var.value, "InGame")
        self.assertEqual(gui.root.iconify_calls, 1)
        self.assertEqual(gui._log_window.iconify_calls, 1)
        self.assertTrue(gui._auto_iconified)
        gui._show_root_noactivate.assert_not_called()
        gui._snap_to_client.assert_not_called()
        self.assertEqual(
            gui.root.after_calls[-1][0],
            external_sync_delay_ms(in_game=True),
        )

        rect = (10, 20, 1290, 740)
        gui._external_sync_q.put(
            self._snapshot(
                in_game=False,
                rect=rect,
                league_foreground=True,
            )
        )

        gui._sync_external_state()

        self.assertEqual(gui.client_visible_var.value, "Visible")
        gui._show_root_noactivate.assert_called_once_with()
        gui._snap_to_client.assert_called_once_with(rect)
        self.assertFalse(gui._auto_iconified)
        self.assertEqual(
            gui.root.after_calls[-1][0],
            external_sync_delay_ms(in_game=False),
        )

    def test_user_focused_manager_is_not_iconified_during_game(self) -> None:
        gui = self._make_windowing_gui()
        gui._is_manager_window_foreground = mock.Mock(return_value=True)
        gui._external_sync_q.put(self._snapshot(in_game=True))

        gui._sync_external_state()

        self.assertEqual(gui.client_visible_var.value, "InGame")
        self.assertEqual(gui.root.iconify_calls, 0)
        self.assertEqual(gui._log_window.iconify_calls, 0)

    def test_closed_league_process_wins_over_stale_ingame_snapshot(self) -> None:
        gui = self._make_windowing_gui()
        gui._client_seen_once = True
        gui._client_closed_at = 10.0
        gui._on_close = mock.Mock()
        gui._external_sync_q.put(
            self._snapshot(in_game=True, league_running=False)
        )

        with mock.patch("lolmanager.gui.app_gui.time.monotonic", return_value=20.0):
            gui._sync_external_state()

        gui._on_close.assert_called_once_with(close_opgg=True)
        self.assertEqual(gui.root.iconify_calls, 0)

    def test_client_close_waits_for_cli_owned_opgg_cleanup_before_forcing_exit(self) -> None:
        gui = self._make_windowing_gui()
        gui._client_seen_once = True
        gui._client_closed_at = 10.0
        gui.proc = mock.Mock()
        gui.proc.poll.return_value = None
        gui._on_close = mock.Mock()
        gui._external_sync_q.put(
            self._snapshot(in_game=True, league_running=False)
        )

        with mock.patch("lolmanager.gui.app_gui.time.monotonic", return_value=20.0):
            gui._sync_external_state()

        gui._on_close.assert_not_called()
        self.assertEqual(gui._client_close_cleanup_deadline, 23.0)
        self.assertEqual(gui.root.after_calls[-1][0], EXTERNAL_SYNC_MS)

        gui.proc.poll.return_value = 0
        gui._external_sync_q.put(
            self._snapshot(in_game=True, league_running=False)
        )
        with mock.patch("lolmanager.gui.app_gui.time.monotonic", return_value=20.1):
            gui._sync_external_state()

        gui._on_close.assert_called_once_with(close_opgg=True)

    def test_user_minimized_window_is_not_auto_restored_after_game(self) -> None:
        gui = self._make_windowing_gui()
        gui._is_root_iconic.return_value = True
        gui._external_sync_q.put(self._snapshot(in_game=True))
        gui._sync_external_state()

        self.assertEqual(gui.root.iconify_calls, 0)
        self.assertFalse(gui._auto_iconified)

        gui._external_sync_q.put(
            self._snapshot(
                in_game=False,
                rect=(10, 20, 1290, 740),
                league_foreground=True,
            )
        )
        gui._sync_external_state()

        gui._show_root_noactivate.assert_not_called()

    def test_hidden_minimized_and_closed_client_policies_are_preserved(self) -> None:
        cases = (
            (self._snapshot(in_game=False), "Hidden", 1),
            (
                self._snapshot(
                    in_game=False,
                    rect=(-32000, -32000, -30720, -31280),
                    minimized=True,
                ),
                "Minimized",
                1,
            ),
            (
                self._snapshot(
                    in_game=False,
                    league_running=False,
                ),
                "Closed",
                0,
            ),
        )
        for snapshot, expected_visibility, expected_iconify_calls in cases:
            with self.subTest(visibility=expected_visibility):
                gui = self._make_windowing_gui()
                gui._external_sync_q.put(snapshot)
                gui._sync_external_state()
                self.assertEqual(gui.client_visible_var.value, expected_visibility)
                self.assertEqual(gui.root.iconify_calls, expected_iconify_calls)

    def test_failed_snapshot_collection_keeps_last_published_state(self) -> None:
        gui = LolManagerGui.__new__(LolManagerGui)
        previous = self._snapshot(in_game=True)
        gui._external_sync_stop = _OneIterationStop()
        gui._external_sync_q = queue.Queue(maxsize=1)
        gui._external_sync_q.put(previous)
        gui._collect_external_sync_snapshot = mock.Mock(
            side_effect=RuntimeError("probe failed")
        )
        gui._external_sync_poll_sec = mock.Mock(return_value=0.25)

        gui._external_sync_worker()

        self.assertIs(gui._external_sync_q.get_nowait(), previous)

    def test_app_close_stops_external_sync_worker(self) -> None:
        gui = LolManagerGui.__new__(LolManagerGui)
        gui._auto_ban_refresher = None
        gui._proc_usage_after_id = None
        gui._external_sync_stop = threading.Event()
        gui.proc = None
        gui.root = _Root()

        gui._on_close()

        self.assertTrue(gui._external_sync_stop.is_set())
        self.assertTrue(gui.root.destroyed)


if __name__ == "__main__":
    unittest.main()
