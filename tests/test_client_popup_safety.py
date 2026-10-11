from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest import mock

import psutil
import pytest

from lolmanager.cli import entrypoint
from lolmanager.core import image_search
from lolmanager.platform import resolution_detector as windows


RECT = (100, 200, 1380, 920)
POINT = (740, 632)
POPUP_ROUTES = ("blocking_modal", "match_poll", "pick", "legacy_confirm")


@pytest.fixture
def client_window(monkeypatch):
    api = mock.Mock()
    api.GetForegroundWindow.return_value = 100
    api.IsWindowVisible.return_value = True
    api.IsIconic.return_value = False
    api.WindowFromPoint.return_value = 101
    api.GetAncestor.return_value = 100

    def get_pid(hwnd, pid):
        pid._obj.value = hwnd
        return 1

    api.GetWindowThreadProcessId.side_effect = get_pid
    names = {100: "LeagueClientUx.exe", 101: "LeagueClientUxRender.exe", 200: "chrome.exe"}
    process = mock.Mock(side_effect=lambda pid: SimpleNamespace(name=lambda: names[pid]))
    bounds = mock.Mock(return_value=RECT)
    monkeypatch.setattr(windows, "_USER32", api)
    monkeypatch.setattr(windows, "_try_get_extended_frame_bounds", bounds)
    monkeypatch.setattr(windows.psutil, "Process", process)
    return SimpleNamespace(api=api, names=names, process=process, bounds=bounds)


@pytest.fixture
def mouse_input(monkeypatch):
    # Never send real desktop input, including cursor restoration or fallbacks.
    click = mock.Mock()
    send_input = mock.Mock(return_value=3)
    restore = mock.Mock()
    monkeypatch.setattr(image_search.mouse, "click", click)
    monkeypatch.setattr(image_search, "_USER32", SimpleNamespace(SendInput=send_input))
    monkeypatch.setattr(image_search, "_virtual_screen", lambda: (0, 0, 2000, 1200))
    monkeypatch.setattr(image_search, "_restore_cursor_pos", restore)
    monkeypatch.setattr(image_search, "_get_cursor_pos", lambda: (10, 20))
    monkeypatch.setattr(image_search, "_assert_point_in_virtual_screen", lambda x, y: None)
    monkeypatch.setattr(image_search.time, "sleep", lambda _: None)
    return SimpleNamespace(click=click, send_input=send_input, restore=restore)


def test_client_and_its_renderer_child_are_allowed(client_window):
    assert windows.is_league_client_window_target(RECT)
    assert windows.is_league_client_window_target(RECT, POINT)
    assert image_search.click_league_client(None, POINT) is False


@pytest.mark.parametrize(
    "name", ["chrome.exe", "LOLManager.exe", "RiotClientUx.exe", "League of Legends.exe", ""]
)
def test_other_foreground_processes_are_denied(client_window, name):
    client_window.names[100] = name
    assert not windows.is_league_client_window_target(RECT, POINT)


@pytest.mark.parametrize("failure", [psutil.AccessDenied(100), psutil.NoSuchProcess(100)])
def test_unreadable_process_is_denied_even_with_cached_client_pid(client_window, monkeypatch, failure):
    monkeypatch.setattr(windows, "_FOREGROUND_PID_CACHE", 100)
    monkeypatch.setattr(windows, "_FOREGROUND_NAME_CF_CACHE", "leagueclientux.exe")
    client_window.process.side_effect = failure
    assert not windows.is_league_client_window_target(RECT, POINT)


@pytest.mark.parametrize("method", ["GetForegroundWindow", "GetWindowThreadProcessId"])
def test_missing_window_or_pid_is_denied(client_window, method):
    getattr(client_window.api, method).side_effect = None
    getattr(client_window.api, method).return_value = 0
    assert not windows.is_league_client_window_target(RECT, POINT)


@pytest.mark.parametrize("method,value", [("IsWindowVisible", False), ("IsIconic", True)])
def test_hidden_or_minimized_client_is_denied(client_window, method, value):
    getattr(client_window.api, method).return_value = value
    assert not windows.is_league_client_window_target(RECT, POINT)


@pytest.mark.parametrize("rect", [None, (0, 0, 0, 0), (-32000, -32000, -31900, -31900),
                                  (101, 200, 1381, 920), (100, 200, 1200, 800)])
def test_invalid_moved_or_resized_capture_is_denied(client_window, rect):
    assert not windows.is_league_client_window_target(rect, POINT)


@pytest.mark.parametrize("point", [(99, 632), (1380, 632), (740, 199), (740, 920)])
def test_point_outside_client_is_denied(client_window, point):
    assert not windows.is_league_client_window_target(RECT, point)


def test_overlay_is_denied_even_while_client_has_focus(client_window):
    client_window.api.WindowFromPoint.return_value = 200
    client_window.api.GetAncestor.return_value = 200
    assert not windows.is_league_client_window_target(RECT, POINT)


def test_other_client_window_is_not_the_captured_window(client_window):
    client_window.names[200] = "LeagueClientUx.exe"
    client_window.api.WindowFromPoint.return_value = 200
    client_window.api.GetAncestor.return_value = 200
    assert not windows.is_league_client_window_target(RECT, POINT)


def test_child_from_unrelated_process_is_denied(client_window):
    client_window.names[101] = "chrome.exe"
    assert not windows.is_league_client_window_target(RECT, POINT)


def test_focus_change_during_window_validation_is_denied(client_window):
    client_window.api.GetForegroundWindow.side_effect = [100, 200]
    assert not windows.is_league_client_window_target(RECT, POINT)


def test_native_window_errors_are_denied(client_window, monkeypatch):
    client_window.api.WindowFromPoint.side_effect = OSError("window disappeared")
    assert not windows.is_league_client_window_target(RECT, POINT)
    monkeypatch.setattr(windows, "_USER32", None)
    assert not windows.is_league_client_window_target(RECT, POINT)


def test_window_rect_fallback_still_requires_current_geometry(client_window):
    client_window.bounds.return_value = None

    def get_rect(hwnd, rect):
        rect._obj.left, rect._obj.top, rect._obj.right, rect._obj.bottom = RECT
        return 1

    client_window.api.GetWindowRect.side_effect = get_rect
    assert windows.is_league_client_window_target(RECT, POINT)
    client_window.api.GetWindowRect.side_effect = None
    client_window.api.GetWindowRect.return_value = 0
    assert not windows.is_league_client_window_target(RECT, POINT)


@pytest.mark.parametrize("pid", [None, 200, 300])
def test_title_match_requires_verified_client_process(client_window, monkeypatch, pid):
    def process(process_id):
        if process_id == 300:
            raise psutil.AccessDenied(process_id)
        return SimpleNamespace(name=lambda: client_window.names[process_id])

    client_window.process.side_effect = process
    monkeypatch.setattr(windows.findwindows, "find_elements", lambda **_: [
        SimpleNamespace(process_id=pid, handle=200),
        SimpleNamespace(process_id=100, handle=100),
    ])
    assert windows.find_league_window_rect() == RECT
    client_window.bounds.assert_called_once_with(100)


@pytest.fixture
def popup(monkeypatch, tmp_path, client_window, mouse_input):
    template = tmp_path / "client_confirm-button.png"
    template.write_bytes(b"template loaded by mocked matcher")
    matches = {"confirm#0": (POINT, object(), 0.99)}
    matcher = mock.Mock(return_value=matches)
    monkeypatch.setattr(entrypoint, "find_template_matches_once", matcher)
    monkeypatch.setattr(entrypoint, "RUNTIME_STATE", entrypoint.RuntimeState(
        client_state=entrypoint.ClientState.POSTGAME_SCORE,
    ))
    lcu = SimpleNamespace(
        get_gameflow_phase=lambda **_: entrypoint.PHASE_END_OF_GAME,
        consume_phase_transition=lambda _: None,
    )
    logger = logging.getLogger("test-client-popup")

    def invoke(route):
        if route == "blocking_modal":
            return entrypoint._dismiss_blocking_modal_ui_fallback(RECT, [template], 0.85, route, logger)
        if route == "match_poll":
            return entrypoint.poll_match_state(
                RECT, route, tmp_path / "finding.png", tmp_path / "accept.png",
                0.85, 0, logger, tpl_confirm_templates=[template], lcu=lcu,
            )
        if route == "pick":
            return entrypoint.try_pick_popups(RECT, [template], None, 0.85, logger)
        return entrypoint.try_confirm(RECT, template, 0.85, logger)

    return SimpleNamespace(invoke=invoke, matcher=matcher, matches=matches)


@pytest.mark.parametrize("route", POPUP_ROUTES)
def test_popup_routes_skip_scanning_other_apps(popup, client_window, mouse_input, route):
    client_window.api.GetForegroundWindow.return_value = 200
    popup.invoke(route)
    popup.matcher.assert_not_called()
    mouse_input.click.assert_not_called()
    mouse_input.send_input.assert_not_called()
    assert entrypoint.RUNTIME_STATE.last_popup_click_at == {}


@pytest.mark.parametrize("route", POPUP_ROUTES)
def test_popup_routes_click_exposed_client_button(popup, mouse_input, route):
    popup.invoke(route)
    assert popup.matcher.call_args.kwargs["fresh_capture"] is True
    mouse_input.send_input.assert_called_once()
    mouse_input.click.assert_not_called()
    assert len(entrypoint.RUNTIME_STATE.last_popup_click_at) == 1


@pytest.mark.parametrize("route", POPUP_ROUTES)
@pytest.mark.parametrize("change", ["focus", "overlay", "move"])
def test_popup_routes_recheck_target_after_detection(popup, client_window, mouse_input, route, change):
    def match(*args, **kwargs):
        if change == "focus":
            client_window.api.GetForegroundWindow.return_value = 200
        elif change == "overlay":
            client_window.api.WindowFromPoint.return_value = 200
            client_window.api.GetAncestor.return_value = 200
        else:
            client_window.bounds.return_value = (200, 200, 1480, 920)
        return popup.matches

    popup.matcher.side_effect = match
    popup.invoke(route)
    popup.matcher.assert_called_once()
    mouse_input.click.assert_not_called()
    mouse_input.send_input.assert_not_called()
    mouse_input.restore.assert_not_called()
    assert entrypoint.RUNTIME_STATE.last_popup_click_at == {}


def test_guarded_click_dispatches_one_native_packet_without_pywinauto_waits(client_window, mouse_input):
    assert image_search.click_league_client(RECT, POINT)
    mouse_input.send_input.assert_called_once()
    count, pointer, _size = mouse_input.send_input.call_args.args
    assert count == 3
    assert [packet.ii.mi.dwFlags for packet in pointer._obj] == [
        image_search._MOUSEEVENTF_MOVE | image_search._MOUSEEVENTF_ABSOLUTE
        | image_search._MOUSEEVENTF_VIRTUALDESK,
        image_search._MOUSEEVENTF_LEFTDOWN,
        image_search._MOUSEEVENTF_LEFTUP,
    ]
    mouse_input.click.assert_not_called()
    mouse_input.restore.assert_called_once_with((10, 20))


def test_failed_native_input_does_not_fall_back_to_delayed_click(client_window, mouse_input, monkeypatch):
    mouse_input.send_input.return_value = 0
    monkeypatch.setattr(image_search.ctypes, "get_last_error", lambda: 5)
    with pytest.raises(RuntimeError, match="전송하지 못했습니다"):
        image_search.click_league_client(RECT, POINT)
    mouse_input.send_input.assert_called_once()
    mouse_input.click.assert_not_called()
