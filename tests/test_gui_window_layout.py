from __future__ import annotations

import ctypes
import os
from unittest import mock

import pytest

from lolmanager.gui import app_gui
from lolmanager.gui.ui_theme import apply_frameless_window


@pytest.fixture(scope="module")
def layout_root(tk_interpreter):
    root = tk_interpreter
    # Measure real layout/native geometry without displaying a test window.
    root.attributes("-alpha", 0.0)
    root.deiconify()
    root.update()
    yield root
    root.withdraw()


@pytest.mark.parametrize("updates_enabled", [False, True])
def test_footer_reserves_version_space_when_log_and_update_status_change(layout_root, updates_enabled):
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk

    root = layout_root
    root.geometry("520x100+100+100")
    gui = app_gui.LolManagerGui.__new__(app_gui.LolManagerGui)
    gui.root = root
    gui._updates_enabled = updates_enabled
    gui.last_msg_var = tk.StringVar(root)
    gui.app_version_var = tk.StringVar(root, value="v1.1.34.0")
    gui.update_status_var = tk.StringVar(root)
    gui._last_msg_full = "[GUI] 긴 상태 메시지 및 자동화 프로세스 종료 사유 " * 50
    gui._last_msg_font = None
    gui._last_msg_last_width_px = 0
    gui._log_last_line = ""
    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="x")
    try:
        gui._build_status_footer(frame)
        for status in ("", "Update 최신", "Update 다운로드 실패", "Update v123.123.123"):
            gui.update_status_var.set(status)
            root.update_idletasks()
            gui._update_last_message_label()
            root.update_idletasks()
            assert gui.lbl_version.winfo_width() >= gui.lbl_version.winfo_reqwidth()
            assert gui.lbl_last.winfo_rootx() + gui.lbl_last.winfo_width() <= gui.lbl_version.winfo_rootx()
            assert gui.last_msg_var.get() != gui._last_msg_full
            last = gui.lbl_update_status if updates_enabled else gui.lbl_version
            assert last.winfo_width() >= last.winfo_reqwidth()
            assert last.winfo_rootx() + last.winfo_width() <= frame.winfo_rootx() + frame.winfo_width() - 10
    finally:
        frame.destroy()


def snap_gui():
    gui = app_gui.LolManagerGui.__new__(app_gui.LolManagerGui)
    gui.root = mock.Mock()
    gui.root.winfo_id.return_value = 10
    gui._last_geo_xy = None
    gui._last_client_rect = None
    gui._snap_gap_px = 0
    return gui


@pytest.mark.parametrize("client,bounds,expected", [
    ((640, 336, 1920, 1056), (0, 0, 1920, 1080), (104, 336)),
    ((100, 100, 1000, 800), (0, 0, 1920, 1080), (1000, 100)),
    ((-1280, -100, 0, 620), (-1920, -200, 0, 880), (-1816, -100)),
    ((640, 900, 1920, 1620), (0, 0, 1920, 1080), (104, 836)),
])
def test_docking_uses_actual_native_size_and_monitor_coordinates(monkeypatch, client, bounds, expected):
    gui = snap_gui()
    # Old code used 520 here and overlapped League by 16 pixels.
    gui._outer_wh = (520, 210)
    frame = (120, 336, 656, 580)
    monkeypatch.setattr(app_gui, "_top_hwnd", lambda _: 20)
    monkeypatch.setattr(app_gui, "_get_hwnd_rect", lambda hwnd: frame if hwnd == 20 else None)
    monkeypatch.setattr(app_gui, "_work_area_for_rect", lambda _: app_gui._VirtualScreen(*bounds))
    move = mock.Mock(return_value=True)
    monkeypatch.setattr(app_gui, "_move_hwnd_frame_to", move)
    gui._snap_to_client(client)
    move.assert_called_once_with(20, frame, *expected)
    gui.root.geometry.assert_not_called()


def test_docking_remeasures_after_size_change_and_skips_already_aligned_window(monkeypatch):
    gui = snap_gui()
    client = (1000, 100, 1900, 800)
    frames = [(0, 100, 536, 344), (464, 100, 1000, 344), (464, 100, 1268, 466)]
    monkeypatch.setattr(app_gui, "_top_hwnd", lambda _: 20)
    monkeypatch.setattr(app_gui, "_get_hwnd_rect", mock.Mock(side_effect=frames))
    monkeypatch.setattr(app_gui, "_work_area_for_rect", lambda _: app_gui._VirtualScreen(0, 0, 1920, 1080))
    move = mock.Mock(return_value=True)
    monkeypatch.setattr(app_gui, "_move_hwnd_frame_to", move)
    for _ in frames:
        gui._snap_to_client(client)
    assert [call.args[-2:] for call in move.call_args_list] == [(464, 100), (196, 100)]


@pytest.mark.skipif(os.name != "nt", reason="Windows native frame behavior")
def test_docking_does_not_guess_when_native_frame_cannot_be_read(monkeypatch):
    gui = snap_gui()
    monkeypatch.setattr(app_gui, "_get_hwnd_rect", lambda _: None)
    move = mock.Mock()
    monkeypatch.setattr(app_gui, "_move_hwnd_frame_to", move)
    gui._snap_to_client((640, 336, 1920, 1056))
    move.assert_not_called()
    gui.root.geometry.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows native frame behavior")
def test_native_move_accounts_for_invisible_borders_without_activation(monkeypatch):
    def get_window_rect(_hwnd, out):
        rect = ctypes.cast(out, ctypes.POINTER(app_gui._RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = (112, 300, 664, 588)
        return 1

    user32 = mock.Mock()
    user32.GetWindowRect.side_effect = get_window_rect
    user32.SetWindowPos.return_value = 1
    monkeypatch.setattr(app_gui, "_USER32", user32)
    assert app_gui._move_hwnd_frame_to(20, (120, 300, 656, 580), -600, -100)
    args = user32.SetWindowPos.call_args.args
    assert args[2:6] == (-608, -100, 0, 0)
    assert args[-1] & app_gui._SWP_NOACTIVATE
    assert args[-1] & app_gui._SWP_NOZORDER


@pytest.mark.skipif(os.name != "nt", reason="Windows native frame behavior")
def test_real_frameless_window_snaps_flush_after_chrome_removal(layout_root, monkeypatch):
    root = layout_root
    root.geometry("520x210+120+336")
    root.resizable(False, False)
    root.update_idletasks()
    assert apply_frameless_window(root)
    root.update_idletasks()
    gui = snap_gui()
    gui.root = root
    monkeypatch.setattr(app_gui, "_work_area_for_rect", lambda _: app_gui._VirtualScreen(0, 0, 1920, 1080))
    gui._snap_to_client((640, 336, 1920, 1056))
    root.update_idletasks()
    frame = app_gui._get_hwnd_rect(app_gui._top_hwnd(root.winfo_id()))
    assert frame is not None
    assert frame[2] == 640
    assert frame[1] == 336
