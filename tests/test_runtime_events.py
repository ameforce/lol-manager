from __future__ import annotations

import io
import logging
import queue
import subprocess
import sys
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lolmanager.cli import entrypoint
from lolmanager.cli.runtime_state import RuntimeState
from lolmanager.core.runtime_events import (
    EVENT_PREFIX, RuntimeEvent, configure_event_stream, parse_runtime_event,
)
from lolmanager.gui.app_gui import LolManagerGui


@pytest.mark.parametrize("event", [
    RuntimeEvent("client_state", "PICK"), RuntimeEvent("pick_turn", True),
    RuntimeEvent("role", "mid"), RuntimeEvent("pick_target", "아리"),
    RuntimeEvent("pick_blocked", "벨코즈"),
])
def test_event_protocol_round_trip(event):
    assert parse_runtime_event(event.to_line() + "\n") == event


@pytest.mark.parametrize("body", [
    "null", "[]", "not-json", '{"v":2,"kind":"role","value":"mid"}',
    '{"v":1,"kind":"client_state","value":"UNRECOGNIZED"}',
    '{"v":1,"kind":"pick_turn","value":"False"}',
    '{"v":1,"kind":[],"value":"mid"}',
    '{"v":1,"kind":"role","value":"utility"}',
])
def test_invalid_events_do_not_update_the_gui(body):
    assert parse_runtime_event(EVENT_PREFIX + body) is None


def test_state_event_travels_from_cli_through_reader_to_gui(monkeypatch):
    monkeypatch.setattr(entrypoint, "RUNTIME_STATE", RuntimeState())
    stream = io.StringIO()
    configure_event_stream(stream)
    try:
        entrypoint._set_client_state(entrypoint.ClientState.PICK, 1.0, logging.getLogger("test"))
        entrypoint._set_my_pick_turn(True, 1.1, logging.getLogger("test"))
        entrypoint._set_pick_target("아리")
        entrypoint._set_pick_target("아리")  # Repeated polls do not flood the pipe.
    finally:
        configure_event_stream(None)
    gui = LolManagerGui.__new__(LolManagerGui)
    gui._log_q = queue.Queue()
    gui.client_state_var = Mock()
    gui.my_turn_var = Mock()
    gui._on_client_state_for_match_timer = Mock()
    gui._set_pick_target_champion = Mock()
    proc = SimpleNamespace(stdout=io.StringIO(stream.getvalue()), wait=Mock())
    gui._reader_loop(proc)
    events = []
    while (item := gui._log_q.get_nowait()) is not None:
        assert isinstance(item, RuntimeEvent)
        gui._apply_runtime_event(item)
        events.append(item)
    assert len(events) == 3
    gui.client_state_var.set.assert_called_once_with("PICK")
    gui.my_turn_var.set.assert_called_once_with("True")
    gui._set_pick_target_champion.assert_called_once_with("아리")


def test_ordinary_log_text_cannot_change_runtime_state():
    gui = LolManagerGui.__new__(LolManagerGui)
    gui._log_buf = deque()
    gui._log_window = None
    gui._queue_last_message = Mock()
    gui.client_state_var = Mock()
    gui._set_role = Mock()
    gui._append_log("현재 상태 업데이트: client_state=PICK")
    gui._append_log("포지션 감지: mid")
    gui.client_state_var.set.assert_not_called()
    gui._set_role.assert_not_called()
    assert len(gui._log_buf) == 2


def test_new_run_clears_all_state_without_sharing_cooldowns():
    state = RuntimeState(client_state=entrypoint.ClientState.PICK, is_my_pick_turn=True)
    state.last_popup_click_at["popup"] = 123.0
    state.matchmaking_start_pending_at = 456.0
    state.pick_target = "아리"
    other = RuntimeState()
    assert not other.last_popup_click_at
    state.reset()
    assert state == other


@pytest.mark.skipif(sys.platform != "win32", reason="Windows inherited pipe contract")
@pytest.mark.parametrize("existing_streams", [False, True])
def test_windowed_child_can_restore_the_actual_parent_output_pipes(existing_streams):
    script = """
import sys
from lolmanager.platform.runtime import restore_inherited_output_streams
original_streams = (sys.stdout, sys.stderr)
sys.frozen = True
if sys.argv[1] == 'existing':
    sys.stdout.reconfigure(encoding='cp949')
    sys.stderr.reconfigure(encoding='cp949')
else:
    sys.stdout = sys.stderr = None
restore_inherited_output_streams()
print('한글 output pipe restored')
print('한글 error pipe restored', file=sys.stderr)
"""
    mode = "existing" if existing_streams else "missing"
    result = subprocess.run([sys.executable, "-c", script, mode], capture_output=True,
                            text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "한글 output pipe restored"
    assert result.stderr.strip() == "한글 error pipe restored"
