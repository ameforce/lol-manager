from __future__ import annotations

from collections import deque
import ctypes
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

import psutil
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from lolmanager.core.auto_ban_refresh import AutoBanRefreshCoordinator
from lolmanager.core.app_version import format_app_version_label, get_app_version
from lolmanager.core.champion_config import ChampionConfig
from lolmanager.core.gui_preferences import (
    load_continue_after_game_preference,
    save_continue_after_game_preference,
)
from lolmanager.core.role_setting_data import load_role_setting_data
from lolmanager.core.match_timing import format_duration_mmss, load_match_timing_stats
from lolmanager.gui.log_view_model import (
    ROLE_CLEAR_STATES,
    ROLE_LABEL_KO,
    compact_role_ban_label_for_main_ui,
    role_key_from_log_line,
)
from lolmanager.platform.paths import (
    champion_config_path,
    gui_preferences_path,
    match_timing_stats_path,
    resource_path,
    resource_root,
    user_data_dir,
)
from lolmanager.platform.runtime import is_frozen
from lolmanager.platform.external_apps import (
    LeagueClientExitGuard,
    close_owned_opgg_for_current_session,
    league_client_exe_path,
)
from lolmanager.platform.resolution_detector import (
    find_league_window_rect,
    is_game_client_active,
    is_league_client_foreground,
    is_rect_minimized,
)
from lolmanager.gui.ui_theme import (
    apply_frameless_window,
    apply_modern_theme,
    enable_window_drag,
    ttk_text_palette,
)


MAX_LOG_LINES = 2000
LOG_POLL_MS = 60
LOG_BURST_LIMIT = 200
STOP_GRACE_SEC = 3.0
EXTERNAL_SYNC_MS = 250
EXTERNAL_SYNC_INGAME_MS = 1500
CONFIG_APPLY_DEBOUNCE_SEC = 0.6
AUTO_EXIT_AFTER_CLIENT_CLOSED_SEC = 0.8
# The CLI owns an OP.GG process it launched.  Give it enough time to observe
# LeagueClient's exit and close that child before the GUI force-stops the CLI.
CLI_CLIENT_CLOSE_CLEANUP_GRACE_SEC = 3.0
CLI_UNEXPECTED_RESTART_LIMIT = 1
CLI_UNEXPECTED_RESTART_DELAY_MS = 1000
APP_USER_MODEL_ID = "LOLManager"
LAST_MSG_MAX_CHARS = 2048
MATCH_TIMER_TICK_MS = 250
MATCH_STATS_POLL_MIN_SEC = 1.0
PROC_USAGE_POLL_MS = 1000


class _GuiWarningLogger:
    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit

    def warning(self, msg: object, *args: object) -> None:
        text = str(msg)
        if args:
            try:
                text = text % args
            except Exception:
                text = " ".join([text, *(str(arg) for arg in args)])
        self._emit(f"[GUI] {text}\n")


def external_sync_delay_ms(*, in_game: bool) -> int:
    return EXTERNAL_SYNC_INGAME_MS if in_game else EXTERNAL_SYNC_MS


def should_schedule_initial_auto_start(
    *, auto_start_requested: bool, saved_continue_after_game: Optional[bool]
) -> bool:
    """Wait for an explicit continuation choice on the first GUI launch."""
    return bool(auto_start_requested and saved_continue_after_game is not None)


def should_auto_iconify_ingame(*, manager_has_focus: bool, root_iconic: bool) -> bool:
    """Keep an explicitly focused LOLManager usable while a game is active."""
    return not bool(manager_has_focus) and not bool(root_iconic)


def normalize_process_cpu_percent(
    total_cpu_percent: float,
    *,
    logical_cpu_count: Optional[int] = None,
) -> float:
    """Convert psutil's per-logical-CPU process total to a 0-100 display value."""
    try:
        total = float(total_cpu_percent)
    except (TypeError, ValueError):
        total = 0.0
    if logical_cpu_count is None:
        try:
            logical_cpu_count = psutil.cpu_count(logical=True)
        except Exception:
            logical_cpu_count = None
    try:
        capacity = max(1, int(logical_cpu_count or 1))
    except (TypeError, ValueError):
        capacity = 1
    return max(0.0, min(100.0, total / float(capacity)))


def should_recover_cli_exit(
    *,
    exit_code: Optional[int],
    stop_requested: bool,
    planned_restart: bool,
    league_running: bool,
    restart_count: int,
    restart_limit: int = CLI_UNEXPECTED_RESTART_LIMIT,
) -> bool:
    if planned_restart or stop_requested:
        return False
    if exit_code == 0:
        return False
    if not league_running:
        return False
    return int(restart_count) < max(0, int(restart_limit))


_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_MONITOR_DEFAULTTONEAREST = 2
_DWMWA_EXTENDED_FRAME_BOUNDS = 9

_GA_ROOT = 2

_HWND_TOP = 0
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2

_SW_SHOWNOACTIVATE = 4

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_NOOWNERZORDER = 0x0200

try:
    _USER32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _USER32.GetAncestor.restype = ctypes.c_void_p
    _USER32.GetForegroundWindow.argtypes = []
    _USER32.GetForegroundWindow.restype = ctypes.c_void_p
    _USER32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _USER32.ShowWindow.restype = ctypes.c_int
    _USER32.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    _USER32.SetWindowPos.restype = ctypes.c_int
except Exception:
    pass

_DWMAPI = None
if os.name == "nt":
    try:
        _DWMAPI = ctypes.WinDLL("dwmapi", use_last_error=True)
        _DWMAPI.DwmGetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        _DWMAPI.DwmGetWindowAttribute.restype = ctypes.c_long
    except Exception:
        _DWMAPI = None


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


@dataclass(frozen=True)
class _VirtualScreen:
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class _ExternalSyncSnapshot:
    config_mtime_ns: int
    in_game: bool
    rect: Optional[tuple[int, int, int, int]]
    minimized: bool
    league_running: bool
    league_foreground: bool


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


def _virtual_screen() -> _VirtualScreen:
    vx = int(_USER32.GetSystemMetrics(76))
    vy = int(_USER32.GetSystemMetrics(77))
    vw = int(_USER32.GetSystemMetrics(78))
    vh = int(_USER32.GetSystemMetrics(79))
    return _VirtualScreen(vx, vy, vx + vw, vy + vh)


def _work_area_for_rect(rect: tuple[int, int, int, int]) -> _VirtualScreen:
    if os.name != "nt":
        return _virtual_screen()

    try:
        r = _RECT(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    except Exception:
        return _virtual_screen()

    hmon = _USER32.MonitorFromRect(ctypes.byref(r), _MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return _virtual_screen()

    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    ok = bool(_USER32.GetMonitorInfoW(ctypes.c_void_p(int(hmon)), ctypes.byref(mi)))
    if not ok:
        return _virtual_screen()

    return _VirtualScreen(
        int(mi.rcWork.left),
        int(mi.rcWork.top),
        int(mi.rcWork.right),
        int(mi.rcWork.bottom),
    )


def _get_hwnd_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    if os.name != "nt":
        return None
    if not hwnd:
        return None
    if _DWMAPI is not None:
        rect = _RECT()
        try:
            hr = int(
                _DWMAPI.DwmGetWindowAttribute(
                    ctypes.c_void_p(int(hwnd)),
                    ctypes.c_uint(_DWMWA_EXTENDED_FRAME_BOUNDS),
                    ctypes.byref(rect),
                    ctypes.sizeof(rect),
                )
            )
        except Exception:
            hr = 1
        if hr == 0:
            return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    rect = _RECT()
    ok = bool(_USER32.GetWindowRect(ctypes.c_void_p(int(hwnd)), ctypes.byref(rect)))
    if not ok:
        return None
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _top_hwnd(hwnd: int) -> int:
    if os.name != "nt":
        return int(hwnd or 0)
    if not hwnd:
        return 0
    try:
        top = int(
            _USER32.GetAncestor(ctypes.c_void_p(int(hwnd)), ctypes.c_uint(_GA_ROOT))
            or 0
        )
    except Exception:
        top = 0
    return top or int(hwnd)


def _get_foreground_hwnd() -> int:
    if os.name != "nt":
        return 0
    try:
        return int(_USER32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _show_hwnd_noactivate(hwnd: int) -> None:
    if os.name != "nt":
        return
    if not hwnd:
        return
    try:
        _USER32.ShowWindow(ctypes.c_void_p(int(hwnd)), ctypes.c_int(_SW_SHOWNOACTIVATE))
    except Exception:
        pass


def _bring_hwnd_to_top_noactivate(hwnd: int) -> None:
    if os.name != "nt":
        return
    if not hwnd:
        return
    flags = int(_SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER)
    try:
        _USER32.SetWindowPos(
            ctypes.c_void_p(int(hwnd)),
            ctypes.c_void_p(int(_HWND_TOP)),
            0,
            0,
            0,
            0,
            ctypes.c_uint(flags),
        )
    except Exception:
        pass


def _set_hwnd_topmost_noactivate(hwnd: int, enabled: bool) -> None:
    if os.name != "nt":
        return
    if not hwnd:
        return
    insert_after = _HWND_TOPMOST if enabled else _HWND_NOTOPMOST
    flags = int(_SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER)
    try:
        _USER32.SetWindowPos(
            ctypes.c_void_p(int(hwnd)),
            ctypes.c_void_p(int(insert_after)),
            0,
            0,
            0,
            0,
            ctypes.c_uint(flags),
        )
    except Exception:
        pass


def _clamp(n: int, lo: int, hi: int) -> int:
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


def _apply_readable_fonts(root: tk.Tk) -> None:
    family = "맑은 고딕"
    try:
        default_font = tkfont.nametofont("TkDefaultFont")

        default_font.configure(family=family, size=9)
        tkfont.nametofont("TkTextFont").configure(family=family, size=9)
        tkfont.nametofont("TkMenuFont").configure(family=family, size=9)
        tkfont.nametofont("TkHeadingFont").configure(
            family=family, size=10, weight="bold"
        )
        tkfont.nametofont("TkCaptionFont").configure(family=family, size=9)
        tkfont.nametofont("TkTooltipFont").configure(family=family, size=9)
    except Exception:
        pass


def _apply_window_icon(root: tk.Tk) -> None:
    ico = resource_path("assets", "lolmanager.ico")
    try:
        if ico.exists():
            root.iconbitmap(str(ico))
    except Exception:
        pass


def _set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> None:
    if os.name != "nt":
        return

    if is_frozen():
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id))
    except Exception:
        pass


def _windows_create_no_window_flag() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


class LolManagerGui:
    def __init__(self, root: tk.Tk, *, auto_start: bool = True) -> None:
        self.root = root
        self.root.title("LOLManager")

        self.root.minsize(420, 210)
        try:
            self.root.resizable(False, False)
        except Exception:
            pass

        self.project_root = resource_root()

        self.work_dir = user_data_dir()
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        self.config_path = champion_config_path()
        self.gui_preferences_path = gui_preferences_path()
        saved_continue_after_game = load_continue_after_game_preference(
            self.gui_preferences_path
        )
        self._continue_after_game_preference_initialized = (
            saved_continue_after_game is not None
        )
        _apply_window_icon(self.root)

        self._ui_theme = apply_modern_theme(self.root)

        self.proc: Optional[subprocess.Popen[str]] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._log_q: "queue.Queue[object]" = queue.Queue()

        self._log_buf: "deque[str]" = deque(maxlen=MAX_LOG_LINES)
        self._log_window: Optional[tk.Toplevel] = None
        self._log_text: Optional[ScrolledText] = None
        self._log_last_line: str = ""
        self._last_msg_full: str = ""
        self._pending_last_msg_full: Optional[str] = None
        self._last_msg_last_width_px: int = 0
        self._last_msg_font: Optional[tkfont.Font] = None
        self._in_log_poll: bool = False
        self._log_text_pending: Optional[list[str]] = None
        self._ui_busy_until: float = 0.0

        self._external_sync_stop = threading.Event()
        self._external_sync_q: "queue.Queue[_ExternalSyncSnapshot]" = queue.Queue(
            maxsize=1
        )
        self._external_sync_thread: Optional[threading.Thread] = None
        self._external_sync_last: Optional[_ExternalSyncSnapshot] = None
        self._stop_requested_at: Optional[float] = None
        self._restart_after_exit = False
        self._unexpected_cli_restart_count = 0

        try:
            self._last_config_mtime_ns = int(self.config_path.stat().st_mtime_ns)
        except OSError:
            self._last_config_mtime_ns = 0
        self._pending_config_apply_at: Optional[float] = None
        self._auto_start_pending = should_schedule_initial_auto_start(
            auto_start_requested=auto_start,
            saved_continue_after_game=saved_continue_after_game,
        )
        self._auto_ban_refresher: Optional[AutoBanRefreshCoordinator] = None

        self._client_seen_once = False
        self._league_exit_guard = LeagueClientExitGuard(league_client_exe_path())
        self._auto_iconified = False
        self._client_closed_at: Optional[float] = None
        self._client_close_cleanup_deadline: Optional[float] = None
        self._last_client_rect: Optional[tuple[int, int, int, int]] = None
        self._last_geo_xy: Optional[tuple[int, int]] = None
        self._outer_wh: Optional[tuple[int, int]] = None
        self._compact_root_size: Optional[tuple[int, int]] = None
        self._closing = False

        self._is_frameless: bool = False
        self._snap_gap_px: int = 0
        self._snap_tol_px: int = 2
        self._lol_topmost_enabled: bool = False

        self.running_var = tk.StringVar(value="Stopped")
        self.client_state_var = tk.StringVar(value="UNKNOWN")
        self.my_turn_var = tk.StringVar(value="False")
        self.client_visible_var = tk.StringVar(value="Unknown")
        self.proc_usage_var = tk.StringVar(value="CPU -  MEM -")
        self.last_msg_var = tk.StringVar(value="")
        self.app_version_var = tk.StringVar(
            value=format_app_version_label(get_app_version())
        )
        self.role_key: Optional[str] = None
        self.role_var = tk.StringVar(value="미감지")
        self.continue_after_game_var = tk.BooleanVar(
            value=bool(saved_continue_after_game)
        )
        self._last_saved_continue_after_game = saved_continue_after_game

        self._proc_usage_after_id: Optional[str] = None
        self._proc_usage_procs: dict[int, psutil.Process] = {}

        self._role_setting_text: Optional[tk.Text] = None
        self._role_setting_data: Optional[dict[str, object]] = None
        self._pick_target_champion: Optional[str] = None
        self._role_setting_render_key: Optional[tuple[object, ...]] = None

        self._blocked_pick_norms: list[str] = []
        self._blocked_pick_norms_set: set[str] = set()

        self.match_avg_var = tk.StringVar(value="-")
        self.match_elapsed_var = tk.StringVar(value="-")
        self.match_slack_var = tk.StringVar(value="-")
        self._match_avg_sec: Optional[float] = None
        self._match_timer_started_at: Optional[float] = None
        self._match_stats_path = match_timing_stats_path()
        self._match_stats_mtime_ns: int = 0
        self._match_stats_last_poll_at: float = 0.0
        self._match_slack_color_kind: Optional[str] = None
        self._match_elapsed_last_text: str = "-"
        self._match_slack_last_text: str = "-"

        self._build_ui()

        self._status_process_color_kind: Optional[str] = None
        self._status_state_color_kind: Optional[str] = None
        self._status_turn_color_kind: Optional[str] = None
        self._install_status_color_traces()
        self._refresh_status_colors()

        try:
            self.root.update_idletasks()
            req_w = max(520, int(self.root.winfo_reqwidth()))
            req_h = max(210, int(self.root.winfo_reqheight()))
            self.root.geometry(f"{req_w}x{req_h}")
            self.root.minsize(req_w, req_h)
            self.root.maxsize(req_w, req_h)
            self._compact_root_size = (req_w, req_h)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        env_flag = os.environ.get("LOLMANAGER_FRAMELESS")
        if env_flag is None:
            frameless = True
        else:
            s = str(env_flag).strip().casefold()
            if s in {"0", "false", "no", "n", "off"}:
                frameless = False
            elif s in {"1", "true", "yes", "y", "on"}:
                frameless = True
            else:
                frameless = True

        if frameless:
            self._is_frameless = True

            self._snap_gap_px = 0

            env_gap = os.environ.get("LOLMANAGER_SNAP_GAP")
            if env_gap is not None:
                try:
                    g = int(str(env_gap).strip())
                    if g < -50:
                        g = -50
                    elif g > 200:
                        g = 200
                    self._snap_gap_px = int(g)
                except Exception:
                    pass
            apply_frameless_window(
                self.root,
                prefer_dark=getattr(self, "_ui_theme", None).is_dark
                if hasattr(self, "_ui_theme")
                else None,
            )
            try:
                enable_window_drag(
                    self.root, getattr(self, "_drag_handle", None) or self.root
                )
            except Exception:
                pass
        else:
            self._is_frameless = False
            self._snap_gap_px = 0

            try:
                getattr(self, "_btn_min", None).pack_forget()
                getattr(self, "_btn_close", None).pack_forget()
            except Exception:
                pass

        env_tol = os.environ.get("LOLMANAGER_SNAP_TOL_PX")
        if env_tol is not None:
            try:
                t = int(str(env_tol).strip())
                if t < 0:
                    t = 0
                elif t > 50:
                    t = 50
                self._snap_tol_px = int(t)
            except Exception:
                pass

        self.root.after(LOG_POLL_MS, self._poll_log_queue)
        self.root.after(EXTERNAL_SYNC_MS, self._sync_external_state)
        self.root.after(MATCH_TIMER_TICK_MS, self._tick_match_timer)
        self._poll_proc_usage()

        self._refresh_match_stats(force=True)

        self._auto_ban_refresher = AutoBanRefreshCoordinator(
            config_path=self.config_path,
            schedule_after_ms=self._schedule_auto_ban_refresh,
            run_background=self._run_auto_ban_refresh_background,
            on_updated=self._on_auto_ban_refresh_updated,
            logger=_GuiWarningLogger(
                lambda line: self.root.after(
                    0,
                    lambda line=line: self._append_log(line),
                )
            ),
        )
        self._auto_ban_refresher.start()

        self._start_external_sync_worker()

        try:
            self.root.after_idle(self._enforce_compact_root_size)
        except Exception:
            pass

        if self._auto_start_pending:
            self.root.after(250, self.start)

    def _schedule_auto_ban_refresh(
        self,
        delay_ms: int,
        callback: Callable[[], None],
    ) -> object:
        return self.root.after(max(0, int(delay_ms)), callback)

    def _run_auto_ban_refresh_background(self, callback: Callable[[], None]) -> None:
        threading.Thread(target=callback, daemon=True).start()

    def _on_auto_ban_refresh_updated(self) -> None:
        try:
            self.root.after(0, self._refresh_role_setting)
        except Exception:
            pass

    def _show_root_noactivate(self) -> None:
        if os.name != "nt":
            try:
                self.root.deiconify()
            except Exception:
                pass
            return

        hwnd = 0
        try:
            hwnd = _top_hwnd(int(self.root.winfo_id()))
        except Exception:
            hwnd = 0

        if hwnd:
            _show_hwnd_noactivate(hwnd)
            return
        try:
            self.root.deiconify()
        except Exception:
            pass

    def _is_root_iconic(self) -> bool:
        try:
            return str(self.root.state() or "").strip().casefold() == "iconic"
        except Exception:
            return False

    def _is_manager_window_foreground(self) -> bool:
        if os.name != "nt":
            return False
        foreground = _get_foreground_hwnd()
        if not foreground:
            return False

        handles: set[int] = set()
        try:
            handles.add(_top_hwnd(int(self.root.winfo_id())))
        except Exception:
            pass
        win = getattr(self, "_log_window", None)
        if win is not None:
            try:
                handles.add(_top_hwnd(int(win.winfo_id())))
            except Exception:
                pass
        return int(foreground) in handles

    def _enforce_compact_root_size(self) -> None:
        target = getattr(self, "_compact_root_size", None)
        if not target or self._is_root_iconic():
            return
        try:
            if str(self.root.state() or "").strip().casefold() == "zoomed":
                self.root.state("normal")
            width, height = (int(target[0]), int(target[1]))
            current_width = int(self.root.winfo_width())
            current_height = int(self.root.winfo_height())
        except Exception:
            return
        if width <= 0 or height <= 0:
            return
        if current_width == width and current_height == height:
            self._outer_wh = (width, height)
            return
        try:
            self.root.geometry(f"{width}x{height}")
            self.root.minsize(width, height)
            self.root.maxsize(width, height)
            self._outer_wh = (width, height)
        except Exception:
            pass

    def _apply_lol_topmost(self, enabled: bool) -> None:
        want = bool(enabled)
        if want == bool(getattr(self, "_lol_topmost_enabled", False)):
            return
        self._lol_topmost_enabled = want

        try:
            hwnd_root = _top_hwnd(int(self.root.winfo_id()))
        except Exception:
            hwnd_root = 0
        if hwnd_root:
            _set_hwnd_topmost_noactivate(hwnd_root, want)

        win = getattr(self, "_log_window", None)
        if win is not None:
            try:
                hwnd_log = _top_hwnd(int(win.winfo_id()))
            except Exception:
                hwnd_log = 0
            if hwnd_log:
                _set_hwnd_topmost_noactivate(hwnd_log, want)

    def _restore_active_window_after_topmost_release(self) -> None:
        if os.name != "nt":
            return

        fg = _get_foreground_hwnd()
        if not fg:
            return

        hwnd_root = 0
        try:
            hwnd_root = _top_hwnd(int(self.root.winfo_id()))
        except Exception:
            hwnd_root = 0

        hwnd_log = 0
        win = getattr(self, "_log_window", None)
        if win is not None:
            try:
                hwnd_log = _top_hwnd(int(win.winfo_id()))
            except Exception:
                hwnd_log = 0

        if fg and (fg == hwnd_root or (hwnd_log and fg == hwnd_log)):
            return

        _bring_hwnd_to_top_noactivate(fg)

    def _build_ui(self) -> None:
        root = ttk.Frame(self.root, padding=10)
        root.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(side=tk.TOP, fill=tk.X)

        header_drag = ttk.Frame(header)
        header_drag.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._drag_handle = header_drag

        ttk.Label(header_drag, text="LOLManager", font=("맑은 고딕", 11, "bold")).pack(
            side=tk.LEFT
        )

        header_actions = ttk.Frame(header)
        header_actions.pack(side=tk.RIGHT)
        ttk.Label(
            header_actions, textvariable=self.client_visible_var, foreground="#555555"
        ).pack(
            side=tk.LEFT,
            padx=(0, 8),
        )

        self._btn_min = ttk.Button(
            header_actions, text="–", width=3, command=self.root.iconify
        )
        self._btn_close = ttk.Button(
            header_actions, text="✕", width=3, command=self._on_close
        )
        self._btn_min.pack(side=tk.LEFT)
        self._btn_close.pack(side=tk.LEFT, padx=(6, 0))

        status = ttk.Frame(root)
        status.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))

        ttk.Label(status, text="Process").pack(side=tk.LEFT)
        self._lbl_status_process = ttk.Label(status, textvariable=self.running_var)
        self._lbl_status_process.pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(status, text="State").pack(side=tk.LEFT)
        self._lbl_status_state = ttk.Label(status, textvariable=self.client_state_var)
        self._lbl_status_state.pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(status, text="Turn").pack(side=tk.LEFT)
        self._lbl_status_turn = ttk.Label(status, textvariable=self.my_turn_var)
        self._lbl_status_turn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(status, textvariable=self.proc_usage_var, foreground="#555555").pack(
            side=tk.RIGHT
        )

        timing = ttk.Frame(root)
        timing.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        ttk.Label(timing, text="평균(매칭→인게임)").pack(side=tk.LEFT)
        ttk.Label(timing, textvariable=self.match_avg_var).pack(
            side=tk.LEFT, padx=(6, 12)
        )
        ttk.Label(timing, text="경과").pack(side=tk.LEFT)
        ttk.Label(timing, textvariable=self.match_elapsed_var).pack(
            side=tk.LEFT, padx=(6, 12)
        )
        ttk.Label(timing, text="여유").pack(side=tk.LEFT)
        self._lbl_match_slack = ttk.Label(timing, textvariable=self.match_slack_var)
        self._lbl_match_slack.pack(side=tk.LEFT, padx=(6, 0))

        role = ttk.Frame(root)
        role.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        ttk.Label(role, text="Role").pack(side=tk.LEFT)
        ttk.Label(role, textvariable=self.role_var).pack(side=tk.LEFT, padx=(6, 12))

        txt_role = tk.Text(
            role,
            height=1,
            width=1,
            wrap=tk.NONE,
            borderwidth=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            takefocus=0,
        )
        txt_role.pack(side=tk.LEFT, fill=tk.X, expand=True)
        try:
            bg, fg = ttk_text_palette()
            txt_role.configure(background=bg, foreground=fg, insertbackground=fg)
        except Exception:
            pass
        try:
            txt_role.configure(cursor="arrow")
        except Exception:
            pass
        txt_role.configure(state=tk.DISABLED)
        self._role_setting_text = txt_role

        self._render_role_setting()

        cfg = ttk.Frame(root)
        cfg.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        ttk.Label(cfg, text="Config").pack(side=tk.LEFT)
        ttk.Label(cfg, text=self.config_path.name, foreground="#555555").pack(
            side=tk.LEFT, padx=(6, 0)
        )

        btns = ttk.Frame(root)
        btns.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        self.btn_start = ttk.Button(btns, text="Start", command=self.start)
        self.btn_stop = ttk.Button(
            btns, text="Stop", command=self.stop, state=tk.DISABLED
        )
        self.btn_config = ttk.Button(btns, text="Config", command=self.open_config)
        self.btn_log = ttk.Button(btns, text="Log", command=self.toggle_log_window)
        self.chk_continue = ttk.Checkbutton(
            btns,
            text="다음 게임 계속",
            variable=self.continue_after_game_var,
            command=self._on_continue_after_game_changed,
        )

        self.btn_start.pack(side=tk.LEFT)
        self.btn_stop.pack(side=tk.LEFT, padx=6)
        self.btn_config.pack(side=tk.LEFT, padx=6)
        self.btn_log.pack(side=tk.LEFT)
        self.chk_continue.pack(side=tk.LEFT, padx=(14, 0))

        ttk.Separator(root).pack(side=tk.TOP, fill=tk.X, pady=(10, 8))

        last = ttk.Frame(root)
        last.pack(side=tk.TOP, fill=tk.X)

        self.lbl_last = ttk.Label(last, textvariable=self.last_msg_var, anchor="w")
        self.lbl_last.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            last,
            textvariable=self.app_version_var,
            foreground="#555555",
            anchor="e",
        ).pack(side=tk.RIGHT, padx=(8, 0))
        try:
            self.lbl_last.bind("<Configure>", self._on_last_label_configure)
        except Exception:
            pass

    def _install_status_color_traces(self) -> None:
        try:
            self.running_var.trace_add("write", self._on_running_var_write)
        except Exception:
            pass
        try:
            self.client_state_var.trace_add("write", self._on_client_state_var_write)
        except Exception:
            pass
        try:
            self.my_turn_var.trace_add("write", self._on_my_turn_var_write)
        except Exception:
            pass

    def _on_running_var_write(self, *_args: object) -> None:
        self._refresh_status_process_color()

    def _on_client_state_var_write(self, *_args: object) -> None:
        self._refresh_status_state_color()

    def _on_my_turn_var_write(self, *_args: object) -> None:
        self._refresh_status_turn_color()

    def _refresh_status_colors(self) -> None:
        self._refresh_status_process_color()
        self._refresh_status_state_color()
        self._refresh_status_turn_color()

    def _status_color_from_kind(self, kind: Optional[str]) -> str:
        is_dark = bool(getattr(getattr(self, "_ui_theme", None), "is_dark", False))
        if kind == "ok":
            return "#66bb6a" if is_dark else "#2e7d32"
        if kind == "warn":
            return "#ffb74d" if is_dark else "#ef6c00"
        if kind == "info":
            return "#64b5f6" if is_dark else "#1565c0"
        if kind == "bad":
            return "#ef5350" if is_dark else "#c62828"
        if kind == "muted":
            return "#9e9e9e" if is_dark else "#757575"
        return ""

    def _set_status_label_color(
        self, label: object, kind: Optional[str], cache_attr: str
    ) -> None:
        if kind == getattr(self, cache_attr, None):
            return
        setattr(self, cache_attr, kind)
        color = self._status_color_from_kind(kind)
        try:
            label.configure(foreground=color)
        except Exception:
            pass

    @staticmethod
    def _status_process_kind(text: object) -> Optional[str]:
        s = str(text or "").strip().casefold()
        if not s:
            return None
        if s == "running":
            return "ok"
        if s.startswith("stopping"):
            return "warn"
        if s.startswith("exited(") and s.endswith(")"):
            inner = s[7:-1].strip()
            if inner.lstrip("+-").isdigit():
                return "muted" if int(inner) == 0 else "bad"
            return "bad"
        if s.startswith("exited"):
            return "bad"
        if s == "stopped":
            return "muted"
        return None

    @staticmethod
    def _status_state_kind(text: object) -> Optional[str]:
        s = str(text or "").strip().upper()
        if not s:
            return None
        if s == "INGAME":
            return "ok"
        if s in {"MATCH_ACCEPT_WAIT"}:
            return "warn"
        if s in {
            "MATCH_FINDING",
            "PREPICK",
            "BANPICK",
            "PICK",
            "WAIT_GAME_START",
        }:
            return "info"
        if s in {"UNKNOWN", "LOBBY", "POSTGAME_SCORE"}:
            return "muted"
        return None

    @staticmethod
    def _status_turn_kind(text: object) -> Optional[str]:
        s = str(text or "").strip().casefold()
        if not s:
            return None
        if s in {"true", "1", "yes", "y", "on"}:
            return "ok"
        if s in {"false", "0", "no", "n", "off"}:
            return "muted"
        return None

    def _refresh_status_process_color(self) -> None:
        label = getattr(self, "_lbl_status_process", None)
        if label is None:
            return
        try:
            txt = self.running_var.get()
        except Exception:
            txt = ""
        kind = self._status_process_kind(txt)
        self._set_status_label_color(label, kind, "_status_process_color_kind")

    def _refresh_status_state_color(self) -> None:
        label = getattr(self, "_lbl_status_state", None)
        if label is None:
            return
        try:
            txt = self.client_state_var.get()
        except Exception:
            txt = ""
        kind = self._status_state_kind(txt)
        self._set_status_label_color(label, kind, "_status_state_color_kind")

    def _refresh_status_turn_color(self) -> None:
        label = getattr(self, "_lbl_status_turn", None)
        if label is None:
            return
        try:
            txt = self.my_turn_var.get()
        except Exception:
            txt = ""
        kind = self._status_turn_kind(txt)
        self._set_status_label_color(label, kind, "_status_turn_color_kind")

    def _on_last_label_configure(self, _event: object = None) -> None:
        self._update_last_message_label()

    @staticmethod
    def _sanitize_single_line(text: str) -> str:
        s = str(text).replace("\r", " ").replace("\n", " ").strip()
        if not s:
            return ""
        if len(s) > LAST_MSG_MAX_CHARS:
            s = s[:LAST_MSG_MAX_CHARS]
        return s

    @staticmethod
    def _ellipsize_to_width(text: str, font: tkfont.Font, max_width_px: int) -> str:
        if not text or max_width_px <= 0:
            return ""

        try:
            if font.measure(text) <= max_width_px:
                return text
        except Exception:
            return text

        ell = "..."
        try:
            ell_w = int(font.measure(ell))
        except Exception:
            return text

        if ell_w > max_width_px:
            return ""

        target_px = max_width_px - ell_w
        if target_px <= 0:
            return ell

        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            try:
                ok = font.measure(text[:mid]) <= target_px
            except Exception:
                ok = mid <= max(0, min(len(text), 32))
            if ok:
                lo = mid
            else:
                hi = mid - 1
        if lo <= 0:
            return ell
        return text[:lo] + ell

    def _queue_last_message(self, text: str) -> None:
        one = self._sanitize_single_line(text)
        if not one:
            return
        self._pending_last_msg_full = one

        if not self._in_log_poll:
            self._commit_pending_last_message()

    def _commit_pending_last_message(self) -> None:
        if self._pending_last_msg_full is None:
            return
        one = self._pending_last_msg_full
        self._pending_last_msg_full = None
        if one == self._last_msg_full:
            return
        self._last_msg_full = one
        self._update_last_message_label()

    def _update_last_message_label(self) -> None:
        if getattr(self, "lbl_last", None) is None:
            return

        full = self._last_msg_full
        if not full:
            if self._log_last_line:
                self._log_last_line = ""
                self.last_msg_var.set("")
            return

        try:
            width_px = int(self.lbl_last.winfo_width())
        except Exception:
            width_px = 0

        if width_px <= 1:
            try:
                self.root.after_idle(self._update_last_message_label)
            except Exception:
                pass
            return

        avail_px = self._ttk_text_avail_width_px(self.lbl_last, width_px)

        font = self._last_msg_font
        if font is None:
            font = self._resolve_ttk_widget_font(self.lbl_last)
            self._last_msg_font = font

        rendered = self._ellipsize_to_width(full, font, avail_px)
        if rendered == self._log_last_line and width_px == self._last_msg_last_width_px:
            return
        self._log_last_line = rendered
        self._last_msg_last_width_px = width_px
        self.last_msg_var.set(rendered)

    def _set_role(self, role_key: Optional[str]) -> None:
        if role_key == self.role_key:
            return
        self.role_key = role_key

        self._blocked_pick_norms.clear()
        self._blocked_pick_norms_set.clear()

        if not role_key:
            self.role_var.set("미감지")
            self._role_setting_data = None
            self._pick_target_champion = None
            self._role_setting_render_key = None
            self._render_role_setting()
            return

        self.role_var.set(ROLE_LABEL_KO.get(role_key, role_key))

        self._pick_target_champion = None
        self._refresh_role_setting()

    def _refresh_role_setting(self) -> None:
        if not self.role_key:
            return
        self._role_setting_data = self._load_role_setting_data(self.role_key)
        self._prune_blocked_picks()
        self._render_role_setting()

    @staticmethod
    def _norm_champ_name(name: object) -> str:
        s = str(name or "").strip()
        if not s:
            return ""
        return " ".join(s.split()).casefold()

    def _infer_active_norm(self, pick_names: list[str]) -> str:
        if not pick_names:
            return ""
        picks_norm = [self._norm_champ_name(x) for x in pick_names if str(x).strip()]
        if not picks_norm:
            return ""
        active_norm = self._norm_champ_name(self._pick_target_champion)
        if not active_norm or active_norm not in picks_norm:
            active_norm = picks_norm[0]
        return active_norm

    def _mark_blocked_norm(self, norm: str) -> None:
        n = str(norm or "").strip()
        if not n:
            return
        if n in self._blocked_pick_norms_set:
            return
        self._blocked_pick_norms_set.add(n)
        self._blocked_pick_norms.append(n)

        self._role_setting_render_key = None
        self._render_role_setting()

    def _mark_blocked_current_pick(self) -> None:
        if not self.role_key:
            return
        data = self._role_setting_data
        if not isinstance(data, dict) or not data or "error" in data:
            return

        primary = str(data.get("primary") or "").strip()
        reserves_obj = data.get("reserves")
        reserve_names: list[str] = []
        if isinstance(reserves_obj, (list, tuple)):
            for item in reserves_obj:
                nm = ""
                if isinstance(item, dict):
                    nm = str(item.get("champion") or "").strip()
                elif isinstance(item, (list, tuple)):
                    nm = str(item[0] or "").strip() if item else ""
                else:
                    nm = str(item or "").strip()
                if nm:
                    reserve_names.append(nm)

        pick_names: list[str] = []
        if primary:
            pick_names.append(primary)
        pick_names.extend(reserve_names)

        active_norm = self._infer_active_norm(pick_names)
        if active_norm:
            self._mark_blocked_norm(active_norm)

    def _prune_blocked_picks(self) -> None:
        if not self.role_key:
            self._blocked_pick_norms.clear()
            self._blocked_pick_norms_set.clear()
            return
        data = self._role_setting_data
        if not isinstance(data, dict) or not data or "error" in data:
            return

        primary = str(data.get("primary") or "").strip()
        reserves_obj = data.get("reserves")
        pick_norms: set[str] = set()
        if primary:
            pick_norms.add(self._norm_champ_name(primary))
        if isinstance(reserves_obj, (list, tuple)):
            for item in reserves_obj:
                nm = ""
                if isinstance(item, dict):
                    nm = str(item.get("champion") or "").strip()
                elif isinstance(item, (list, tuple)):
                    nm = str(item[0] or "").strip() if item else ""
                else:
                    nm = str(item or "").strip()
                if nm:
                    pick_norms.add(self._norm_champ_name(nm))

        if not pick_norms:
            self._blocked_pick_norms.clear()
            self._blocked_pick_norms_set.clear()
            return

        if self._blocked_pick_norms_set.issubset(pick_norms):
            return

        kept = [n for n in self._blocked_pick_norms if n in pick_norms]
        self._blocked_pick_norms = kept
        self._blocked_pick_norms_set = set(kept)

    def _set_pick_target_champion(self, champion_name: Optional[str]) -> None:
        new = str(champion_name or "").strip() or None
        if new == self._pick_target_champion:
            return
        self._pick_target_champion = new
        self._role_setting_render_key = None
        self._render_role_setting()

    def _load_role_setting_data(self, role_key: str) -> dict[str, object]:
        return load_role_setting_data(self.config_path, role_key)

    def _render_role_setting(self) -> None:
        txt = self._role_setting_text
        if txt is None:
            return

        role_key = self.role_key
        data = self._role_setting_data

        is_dark = getattr(getattr(self, "_ui_theme", None), "is_dark", None)
        if is_dark is True:
            active_color = "#3ddc97"
            inactive_color = "#ffd54f"
            blocked_color = "#ff6b6b"
        elif is_dark is False:
            active_color = "#0b7a4b"
            inactive_color = "#a67c00"
            blocked_color = "#c62828"
        else:
            active_color = "#19c37d"
            inactive_color = "#d4a000"
            blocked_color = "#ef5350"

        if not role_key:
            render_key = ("none", active_color, inactive_color)
            if render_key == self._role_setting_render_key:
                return
            self._role_setting_render_key = render_key
            try:
                txt.configure(state=tk.NORMAL)
                txt.delete("1.0", tk.END)
                txt.insert(tk.END, "설정: -")
            finally:
                txt.configure(state=tk.DISABLED)
            return

        if not isinstance(data, dict) or not data:
            data = {"error": "설정: -"}

        if "error" in data:
            msg = str(data.get("error") or "설정: 로드 실패")
            render_key = ("err", msg, active_color, inactive_color)
            if render_key == self._role_setting_render_key:
                return
            self._role_setting_render_key = render_key
            try:
                txt.configure(state=tk.NORMAL)
                txt.delete("1.0", tk.END)
                txt.insert(tk.END, msg)
            finally:
                txt.configure(state=tk.DISABLED)
            return

        primary = str(data.get("primary") or "").strip()
        primary_ban = str(data.get("ban") or "").strip()
        reserves_obj = data.get("reserves")
        reserve_pairs: list[tuple[str, str]] = []
        if isinstance(reserves_obj, (list, tuple)):
            for item in reserves_obj:
                champ = ""
                ban = ""
                if isinstance(item, dict):
                    champ = str(item.get("champion") or "").strip()
                    ban = str(item.get("ban") or "").strip()
                elif isinstance(item, (list, tuple)):
                    if len(item) >= 1:
                        champ = str(item[0] or "").strip()
                    if len(item) >= 2:
                        ban = str(item[1] or "").strip()
                else:
                    champ = str(item or "").strip()
                    ban = ""
                if champ:
                    reserve_pairs.append((champ, ban))
        coord = data.get("coord") if isinstance(data.get("coord"), tuple) else None

        pool: list[tuple[str, str]] = []
        if primary:
            pool.append((primary, primary_ban))
        pool.extend(reserve_pairs)
        pick_names = [c for (c, _b) in pool]
        picks_norm = [self._norm_champ_name(x) for x in pick_names]

        active_norm = self._infer_active_norm(pick_names)
        active_idx = -1
        if active_norm and picks_norm:
            for i, n in enumerate(picks_norm):
                if n == active_norm:
                    active_idx = i
                    break
        if active_idx < 0 and pool:
            active_idx = 0
            active_norm = picks_norm[0] if picks_norm else ""

        active_champ = pool[active_idx][0] if 0 <= active_idx < len(pool) else ""
        active_ban = pool[active_idx][1] if 0 <= active_idx < len(pool) else ""
        active_ban_display = compact_role_ban_label_for_main_ui(active_ban)

        blocked_norms = tuple(self._blocked_pick_norms)
        blocked_set = self._blocked_pick_norms_set

        render_key = (
            "ok",
            role_key,
            primary,
            primary_ban,
            active_ban_display,
            tuple(reserve_pairs),
            coord,
            active_norm,
            active_idx,
            blocked_norms,
            active_color,
            inactive_color,
            blocked_color,
        )
        if render_key == self._role_setting_render_key:
            return
        self._role_setting_render_key = render_key

        try:
            txt.configure(state=tk.NORMAL)
            txt.delete("1.0", tk.END)
            txt.tag_configure("active", foreground=active_color)
            txt.tag_configure("inactive", foreground=inactive_color)
            txt.tag_configure("blocked", foreground=blocked_color)
            txt.tag_configure("ban", foreground=blocked_color)

            if active_champ:
                tag = "blocked" if active_norm in blocked_set else "active"
                txt.insert(tk.END, active_champ, (tag,))
            else:
                txt.insert(tk.END, "설정 없음")

            if active_ban_display:
                txt.insert(tk.END, " | ")
                txt.insert(tk.END, "ban: ", ("ban",))
                txt.insert(tk.END, active_ban_display, ("ban",))

            if pool and len(pool) >= 2 and 0 <= active_idx < len(pool):
                blocked_rank = {n: i for i, n in enumerate(blocked_norms)}
                unblocked_idx: list[int] = []
                blocked_idx: list[int] = []
                for i in range(len(pool)):
                    if i == active_idx:
                        continue
                    n = (
                        picks_norm[i]
                        if i < len(picks_norm)
                        else self._norm_champ_name(pool[i][0])
                    )
                    if n in blocked_set:
                        blocked_idx.append(i)
                    else:
                        unblocked_idx.append(i)
                blocked_idx.sort(
                    key=lambda i: blocked_rank.get(picks_norm[i], 1_000_000)
                )
                display_idx = unblocked_idx + blocked_idx

                if display_idx:
                    txt.insert(tk.END, " | 예비: ")
                    first = True
                    for i in display_idx:
                        nm = str(pool[i][0] or "").strip()
                        if not nm:
                            continue
                        if not first:
                            txt.insert(tk.END, ", ")
                        first = False
                        n = (
                            picks_norm[i]
                            if i < len(picks_norm)
                            else self._norm_champ_name(nm)
                        )
                        tag = "blocked" if n in blocked_set else "inactive"
                        txt.insert(tk.END, nm, (tag,))

            if isinstance(coord, tuple) and len(coord) >= 2:
                try:
                    txt.insert(tk.END, f" | coord: ({int(coord[0])},{int(coord[1])})")
                except Exception:
                    pass
        finally:
            txt.configure(state=tk.DISABLED)

    def _append_log(self, line: str) -> None:
        if not line:
            return

        if "현재 상태 업데이트: client_state=" in line:
            try:
                state = line.split("client_state=", 1)[1].strip()
                self.client_state_var.set(state)
                if state in ROLE_CLEAR_STATES:
                    self._set_role(None)
                self._on_client_state_for_match_timer(state)
            except Exception:
                pass
        if "현재 상태 업데이트: is_my_pick_turn=" in line:
            try:
                self.my_turn_var.set(line.split("is_my_pick_turn=", 1)[1].strip())
            except Exception:
                pass
        role_key = role_key_from_log_line(line)
        if role_key:
            try:
                self._set_role(role_key)
            except Exception:
                pass

        if "챔피언 검색 입력:" in line:
            try:
                name = line.split("챔피언 검색 입력:", 1)[1].strip()
                if name:
                    self._set_pick_target_champion(name)
            except Exception:
                pass

        if "예비 픽을 시도합니다" in line:
            try:
                self._mark_blocked_current_pick()
            except Exception:
                pass
        if "예비 챔피언 전환 시도:" in line:
            try:
                after = line.split("예비 챔피언 전환 시도:", 1)[1].strip()

                parts = after.split(None, 1)
                if (
                    len(parts) == 2
                    and parts[0].startswith("#")
                    and parts[0][1:].isdigit()
                ):
                    self._set_pick_target_champion(parts[1].strip())
                elif after:
                    self._set_pick_target_champion(after)
            except Exception:
                pass

        if "밴/미선택으로 판단):" in line and (
            "예비 전환 후 Ready가 회색입니다" in line
            or "예비 챔피언도 비활성 Ready로 감지되었습니다" in line
        ):
            try:
                nm = line.rsplit("):", 1)[1].strip()
                if nm:
                    self._mark_blocked_norm(self._norm_champ_name(nm))
            except Exception:
                pass
        if "픽 준비 버튼 클릭 완료(예비 전환):" in line:
            try:
                name = line.split("픽 준비 버튼 클릭 완료(예비 전환):", 1)[1].strip()
                if name:
                    self._set_pick_target_champion(name)
            except Exception:
                pass

        s = str(line)
        if not s.endswith("\n"):
            s = s + "\n"

        self._log_buf.append(s)

        self._queue_last_message(s)

        if self._log_window is not None and self._log_text is not None:
            if self._in_log_poll and self._log_text_pending is not None:
                self._log_text_pending.append(s)
            else:
                try:
                    self._log_text.configure(state=tk.NORMAL)
                    self._log_text.insert(tk.END, s)
                    self._log_text.see(tk.END)
                except Exception:
                    pass
                finally:
                    try:
                        self._log_text.configure(state=tk.DISABLED)
                    except Exception:
                        pass

    @staticmethod
    def _parse_ttk_padding(pad: object) -> tuple[int, int]:
        if pad is None:
            return (0, 0)
        if isinstance(pad, (int, float)):
            v = int(pad)
            return (v, v)
        if isinstance(pad, (list, tuple)):
            vals = []
            for x in pad:
                try:
                    vals.append(int(float(x)))
                except Exception:
                    continue
            if not vals:
                return (0, 0)
            if len(vals) == 1:
                return (vals[0], vals[0])
            if len(vals) == 2:
                return (vals[0], vals[0])
            if len(vals) >= 4:
                return (vals[0], vals[2])
            return (vals[0], vals[0])
        if isinstance(pad, str):
            s = pad.strip()
            if not s:
                return (0, 0)
            parts = [p for p in s.replace(",", " ").split() if p]
            vals = []
            for p in parts:
                try:
                    vals.append(int(float(p)))
                except Exception:
                    continue
            if not vals:
                return (0, 0)
            if len(vals) == 1:
                return (vals[0], vals[0])
            if len(vals) == 2:
                return (vals[0], vals[0])
            if len(vals) >= 4:
                return (vals[0], vals[2])
            return (vals[0], vals[0])
        return (0, 0)

    def _ttk_text_avail_width_px(self, widget: ttk.Widget, width_px: int) -> int:
        pad_lr = (0, 0)
        try:
            pad_lr = self._parse_ttk_padding(widget.cget("padding"))
        except Exception:
            pad_lr = (0, 0)

        try:
            style_name = (
                str(widget.cget("style") or "").strip()
                or widget.winfo_class()
                or "TLabel"
            )
        except Exception:
            style_name = "TLabel"

        try:
            style = ttk.Style()
            style_pad = style.lookup(style_name, "padding") or style.lookup(
                "TLabel", "padding"
            )
            l2, r2 = self._parse_ttk_padding(style_pad)

            if pad_lr == (0, 0) and (l2 or r2):
                pad_lr = (l2, r2)
        except Exception:
            pass

        bw = 0
        try:
            bw = int(float(widget.cget("borderwidth") or 0))
        except Exception:
            bw = 0

        return max(0, int(width_px) - int(pad_lr[0]) - int(pad_lr[1]) - (2 * bw) - 1)

    def _resolve_ttk_widget_font(self, widget: ttk.Widget) -> tkfont.Font:
        try:
            f = str(widget.cget("font") or "").strip()
        except Exception:
            f = ""
        if f:
            try:
                return tkfont.nametofont(f)
            except Exception:
                try:
                    return tkfont.Font(font=f)
                except Exception:
                    pass

        try:
            style_name = (
                str(widget.cget("style") or "").strip()
                or widget.winfo_class()
                or "TLabel"
            )
        except Exception:
            style_name = "TLabel"
        try:
            style = ttk.Style()
            sf = style.lookup(style_name, "font") or style.lookup("TLabel", "font")
            sf = str(sf or "").strip()
        except Exception:
            sf = ""
        if sf:
            try:
                return tkfont.nametofont(sf)
            except Exception:
                try:
                    return tkfont.Font(font=sf)
                except Exception:
                    pass

        try:
            return tkfont.nametofont("TkDefaultFont")
        except Exception:
            return tkfont.Font()

        s = str(line)
        if not s.endswith("\n"):
            s = s + "\n"

        self._log_buf.append(s)

        self._queue_last_message(s)

        if self._log_window is not None and self._log_text is not None:
            try:
                self._log_text.configure(state=tk.NORMAL)
                self._log_text.insert(tk.END, s)
                self._log_text.see(tk.END)
                self._log_text.configure(state=tk.DISABLED)
            except Exception:
                pass

    def _reader_loop(self, proc: subprocess.Popen[str]) -> None:
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                self._log_q.put(line)
        except Exception as exc:
            self._log_q.put(f"[GUI] log reader error: {exc}\n")
        finally:
            try:
                proc.wait(timeout=0.2)
            except Exception:
                pass
            self._log_q.put(None)

    def _check_config_ready(self) -> bool:
        try:
            cfg = ChampionConfig(path=self.config_path)
        except Exception as exc:
            messagebox.showerror(
                "Config 오류",
                f"설정 파일을 읽지 못했습니다:\n{self.config_path}\n\n{exc}",
            )
            return False

        any_champ = False
        for role, entry in (cfg.data or {}).items():
            if not isinstance(entry, dict):
                continue
            champ = str(entry.get("champion") or "").strip()
            if champ:
                any_champ = True
                break
        if not any_champ:
            messagebox.showinfo(
                "설정 필요", "champion 설정이 비어있습니다. 설정 GUI를 엽니다."
            )
            self.open_config()
            return False
        return True

    def _persist_continue_after_game_preference(self) -> bool:
        try:
            value = bool(self.continue_after_game_var.get())
            save_continue_after_game_preference(self.gui_preferences_path, value)
        except Exception as exc:
            self._append_log(f"[GUI] 다음 게임 계속 설정 저장 실패: {exc}\n")
            return False
        self._continue_after_game_preference_initialized = True
        self._last_saved_continue_after_game = value
        return True

    def _on_continue_after_game_changed(self) -> None:
        if self._persist_continue_after_game_preference():
            return
        last_saved = getattr(self, "_last_saved_continue_after_game", None)
        if isinstance(last_saved, bool):
            self.continue_after_game_var.set(last_saved)
            self._append_log(
                "[GUI] 저장되지 않은 다음 게임 계속 변경을 이전 값으로 되돌렸습니다.\n"
            )

    def start(self, *, _auto_recover: bool = False) -> None:
        if self.proc and self.proc.poll() is None:
            return
        preference_saved = self._persist_continue_after_game_preference()
        if not self._check_config_ready():
            self._auto_start_pending = True
            return
        if not _auto_recover:
            self._unexpected_cli_restart_count = 0
        self._reset_match_timer_ui()

        if is_frozen():
            cmd = [sys.executable, "--cli"]
        else:
            cmd = [sys.executable, "-m", "lolmanager", "--cli"]
        if preference_saved:
            cmd.extend(
                [
                    "--continue-after-game-preference-path",
                    str(self.gui_preferences_path),
                ]
            )
        try:
            if bool(self.continue_after_game_var.get()):
                cmd.append("--continue-after-game")
        except Exception:
            pass
        flags = _windows_create_no_window_flag()

        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(self.work_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=flags,
            )
        except Exception as exc:
            self.proc = None
            messagebox.showerror(
                "Start 실패", f"자동화 프로세스를 시작하지 못했습니다:\n{exc}"
            )
            return

        self._stop_requested_at = None
        self._restart_after_exit = False
        self._auto_start_pending = False
        self.running_var.set("Running")
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)

        self._reader_thread = threading.Thread(
            target=self._reader_loop, args=(self.proc,), daemon=True
        )
        self._reader_thread.start()
        self._append_log("[GUI] Started automation process.\n")

    def stop(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is not None:
            return
        self._reset_match_timer_ui()
        try:
            self.proc.terminate()
        except Exception as exc:
            self._append_log(f"[GUI] terminate failed: {exc}\n")
        self._stop_requested_at = time.monotonic()
        self.running_var.set("Stopping...")
        self.btn_stop.configure(state=tk.DISABLED)
        try:
            self.chk_continue.configure(state=tk.NORMAL)
        except Exception:
            pass

    def open_config(self) -> None:
        if is_frozen():
            cmd = [sys.executable, "--config-gui"]
        else:
            cmd = [sys.executable, "-m", "lolmanager", "--config-gui"]
        flags = _windows_create_no_window_flag()
        try:
            subprocess.Popen(
                cmd,
                cwd=str(self.work_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except Exception as exc:
            messagebox.showerror(
                "Open Config 실패", f"설정 GUI를 열지 못했습니다:\n{exc}"
            )

    def clear_log(self) -> None:
        self._log_buf.clear()
        self._log_last_line = ""
        self._last_msg_full = ""
        self._pending_last_msg_full = None
        self._last_msg_last_width_px = 0
        self.last_msg_var.set("")
        if self._log_window is not None and self._log_text is not None:
            try:
                self._log_text.configure(state=tk.NORMAL)
                self._log_text.delete("1.0", tk.END)
                self._log_text.configure(state=tk.DISABLED)
            except Exception:
                pass

    def toggle_log_window(self) -> None:
        if self._log_window is not None:
            try:
                self._log_window.destroy()
            except Exception:
                pass
            self._log_window = None
            self._log_text = None
            try:
                self.btn_log.configure(text="Log")
            except Exception:
                pass
            return
        self._open_log_window()

    def _open_log_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("LOLManager Log")
        try:
            win.minsize(520, 320)
        except Exception:
            pass
        _apply_window_icon(win)

        outer = ttk.Frame(win, padding=10)
        outer.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        top = ttk.Frame(outer)
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(top, text="Log", font=("맑은 고딕", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="Clear", command=self.clear_log).pack(side=tk.RIGHT)

        txt = ScrolledText(outer, height=16, wrap=tk.WORD)
        txt.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(8, 0))

        try:
            bg, fg = ttk_text_palette()
            txt.configure(background=bg, foreground=fg, insertbackground=fg)
        except Exception:
            pass
        try:
            txt.configure(font=("Consolas", 9))
        except Exception:
            pass
        txt.configure(state=tk.NORMAL)
        try:
            txt.insert(tk.END, "".join(self._log_buf))
            txt.see(tk.END)
        finally:
            txt.configure(state=tk.DISABLED)

        def _on_close() -> None:
            self._log_window = None
            self._log_text = None
            try:
                self.btn_log.configure(text="Log")
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        win.protocol("WM_DELETE_WINDOW", _on_close)
        try:
            win.bind("<Configure>", lambda _e: self._mark_ui_busy(0.25))
        except Exception:
            pass
        self._log_window = win
        self._log_text = txt
        try:
            self.btn_log.configure(text="Log ✓")
        except Exception:
            pass

    def _finalize_process(self) -> None:
        proc = self.proc
        code = None
        if proc:
            code = proc.poll()
        stop_requested = self._stop_requested_at is not None
        planned_restart = bool(self._restart_after_exit)
        league_running = False
        try:
            league_running = bool(self._league_exit_guard.poll_is_running())
        except Exception:
            league_running = False

        self.proc = None
        self._stop_requested_at = None
        self._restart_after_exit = False
        self._reset_match_timer_ui()
        self.btn_stop.configure(state=tk.DISABLED)

        if planned_restart:
            self.running_var.set("Restarting")
            self.btn_start.configure(state=tk.DISABLED)
            self._append_log(
                f"[GUI] Automation process ended for planned restart (exit_code={code}).\n"
            )
            self.root.after(250, self.start)
            return

        if should_recover_cli_exit(
            exit_code=code,
            stop_requested=stop_requested,
            planned_restart=planned_restart,
            league_running=league_running,
            restart_count=self._unexpected_cli_restart_count,
        ):
            self._unexpected_cli_restart_count += 1
            self.running_var.set(
                "Restarting" if code is None else f"Restarting({code})"
            )
            self.btn_start.configure(state=tk.DISABLED)
            self._append_log(
                "[GUI] Automation process ended unexpectedly "
                f"(exit_code={code}, league_running={league_running}); restarting once.\n"
            )
            self.root.after(
                CLI_UNEXPECTED_RESTART_DELAY_MS,
                lambda: self.start(_auto_recover=True),
            )
            return

        if stop_requested:
            status = "Stopped" if code in (None, 0) else f"Stopped({code})"
            reason = "manual-stop"
        elif code == 0:
            status = "Exited(0)"
            reason = "normal-exit"
        else:
            status = "Exited" if code is None else f"Exited({code})"
            reason = "unexpected-exit"

        self.running_var.set(status)
        self.btn_start.configure(state=tk.NORMAL)
        try:
            self.chk_continue.configure(state=tk.NORMAL)
        except Exception:
            pass
        self._enforce_compact_root_size()
        self._append_log(
            "[GUI] Automation process ended "
            f"(exit_code={code}, reason={reason}, league_running={league_running}).\n"
        )

    def _poll_log_queue(self) -> None:
        self._in_log_poll = True
        self._log_text_pending = []
        try:
            if (
                self.proc
                and self._stop_requested_at is not None
                and self.proc.poll() is None
            ):
                if (time.monotonic() - self._stop_requested_at) >= STOP_GRACE_SEC:
                    try:
                        self.proc.kill()
                        self._append_log("[GUI] kill() applied (timeout).\n")
                    except Exception as exc:
                        self._append_log(f"[GUI] kill failed: {exc}\n")
                    self._stop_requested_at = None

            processed = 0
            while processed < LOG_BURST_LIMIT:
                try:
                    item = self._log_q.get_nowait()
                except queue.Empty:
                    break

                if item is None:
                    self._finalize_process()
                    break
                self._append_log(str(item))
                processed += 1
        finally:
            pending = self._log_text_pending
            self._log_text_pending = None
            self._in_log_poll = False

            if (
                pending
                and self._log_window is not None
                and self._log_text is not None
                and not self._is_ui_busy()
            ):
                try:
                    txt = self._log_text
                    txt.configure(state=tk.NORMAL)
                    txt.insert(tk.END, "".join(pending))
                    txt.see(tk.END)

                    try:
                        end_idx = str(txt.index("end-1c"))
                        end_line = int(end_idx.split(".", 1)[0])
                    except Exception:
                        end_line = 0
                    if end_line > (MAX_LOG_LINES + LOG_BURST_LIMIT):
                        cut = int(end_line - MAX_LOG_LINES)
                        if cut > 0:
                            txt.delete("1.0", f"{cut + 1}.0")
                except Exception:
                    pass
                finally:
                    try:
                        self._log_text.configure(state=tk.DISABLED)
                    except Exception:
                        pass

            self._commit_pending_last_message()
            self.root.after(LOG_POLL_MS, self._poll_log_queue)

    def _get_proc_usage_psproc(self, pid: int) -> tuple[Optional[psutil.Process], bool]:
        p = self._proc_usage_procs.get(pid)
        if p is not None:
            return (p, False)
        try:
            p = psutil.Process(int(pid))
            p.cpu_percent(interval=None)
        except Exception:
            return (None, False)
        self._proc_usage_procs[int(pid)] = p
        return (p, True)

    @staticmethod
    def _format_bytes_mb(n: int) -> str:
        try:
            b = int(n)
        except Exception:
            b = 0
        mb = float(b) / (1024.0 * 1024.0)
        if mb < 10.0:
            return f"{mb:.1f}MB"
        return f"{mb:.0f}MB"

    def _poll_proc_usage(self) -> None:
        after_id: Optional[str] = None
        try:
            if self._is_root_iconic():
                return

            root_pid = int(os.getpid())
            pids: set[int] = {root_pid}

            proc = self.proc
            if proc and proc.poll() is None:
                try:
                    cli_pid = int(getattr(proc, "pid", 0) or 0)
                except Exception:
                    cli_pid = 0
                if cli_pid:
                    pids.add(cli_pid)
                    cli_p, _ = self._get_proc_usage_psproc(cli_pid)
                    if cli_p is not None:
                        try:
                            for child in cli_p.children(recursive=True):
                                try:
                                    pid = int(getattr(child, "pid", 0) or 0)
                                except Exception:
                                    pid = 0
                                if pid:
                                    pids.add(pid)
                        except Exception:
                            pass

            for pid in list(self._proc_usage_procs.keys()):
                if pid not in pids:
                    self._proc_usage_procs.pop(pid, None)

            total_cpu = 0.0
            total_rss = 0
            for pid in pids:
                p, is_new = self._get_proc_usage_psproc(pid)
                if p is None:
                    continue
                try:
                    cpu = 0.0 if is_new else float(p.cpu_percent(interval=None) or 0.0)
                    rss = int(getattr(p.memory_info(), "rss", 0) or 0)
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    self._proc_usage_procs.pop(pid, None)
                    continue
                except Exception:
                    continue
                total_cpu += cpu
                total_rss += rss

            display_cpu = normalize_process_cpu_percent(total_cpu)
            self.proc_usage_var.set(
                f"CPU {display_cpu:.1f}%  MEM {self._format_bytes_mb(total_rss)}"
            )
        finally:
            try:
                after_id = str(
                    self.root.after(PROC_USAGE_POLL_MS, self._poll_proc_usage)
                )
            except Exception:
                after_id = None
            self._proc_usage_after_id = after_id

    def _mark_ui_busy(self, hold_sec: float = 0.25) -> None:
        try:
            dur = float(hold_sec)
        except Exception:
            dur = 0.25
        if dur <= 0:
            return
        until = time.monotonic() + dur
        prev = float(getattr(self, "_ui_busy_until", 0.0) or 0.0)
        if until > prev:
            self._ui_busy_until = float(until)

    def _is_ui_busy(self) -> bool:
        try:
            return time.monotonic() < float(getattr(self, "_ui_busy_until", 0.0) or 0.0)
        except Exception:
            return False

    def _start_external_sync_worker(self) -> None:
        t = getattr(self, "_external_sync_thread", None)
        if t is not None and t.is_alive():
            return
        try:
            self._external_sync_stop.clear()
        except Exception:
            pass
        self._external_sync_thread = threading.Thread(
            target=self._external_sync_worker, daemon=True
        )
        self._external_sync_thread.start()

    def _external_sync_poll_sec(self, in_game: bool) -> float:
        return max(0.05, float(external_sync_delay_ms(in_game=in_game)) / 1000.0)

    def _collect_external_sync_snapshot(self) -> _ExternalSyncSnapshot:
        try:
            st = self.config_path.stat()
            config_mtime_ns = int(st.st_mtime_ns)
        except OSError:
            config_mtime_ns = 0

        in_game = bool(is_game_client_active())

        try:
            rect = find_league_window_rect(visible_only=False)
        except Exception:
            rect = None
        minimized = bool(rect is not None and is_rect_minimized(rect))

        try:
            league_running = bool(self._league_exit_guard.poll_is_running())
        except Exception:
            league_running = False

        try:
            league_foreground = bool(is_league_client_foreground())
        except Exception:
            league_foreground = False

        return _ExternalSyncSnapshot(
            config_mtime_ns=config_mtime_ns,
            in_game=in_game,
            rect=rect,
            minimized=minimized,
            league_running=league_running,
            league_foreground=league_foreground,
        )

    def _external_sync_worker(self) -> None:
        stop = self._external_sync_stop
        q = self._external_sync_q
        next_at = time.monotonic()
        last_snapshot: Optional[_ExternalSyncSnapshot] = None

        while not stop.is_set():
            now = time.monotonic()
            if now < next_at:
                stop.wait(next_at - now)
                continue

            try:
                snapshot = self._collect_external_sync_snapshot()
            except Exception:
                snapshot = None

            if snapshot is not None:
                last_snapshot = snapshot
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(snapshot)
                except queue.Full:
                    pass

            next_at = now + self._external_sync_poll_sec(
                bool(last_snapshot.in_game) if last_snapshot is not None else False
            )

    def _sync_external_state(self) -> None:
        if bool(getattr(self, "_closing", False)):
            return
        prev_topmost = bool(getattr(self, "_lol_topmost_enabled", False))
        state = None
        try:
            while True:
                state = self._external_sync_q.get_nowait()
        except queue.Empty:
            state = state or getattr(self, "_external_sync_last", None)
        if not isinstance(state, _ExternalSyncSnapshot):
            self.root.after(EXTERNAL_SYNC_MS, self._sync_external_state)
            return
        self._external_sync_last = state

        mtime_ns = int(state.config_mtime_ns)

        if mtime_ns and mtime_ns != self._last_config_mtime_ns:
            self._last_config_mtime_ns = mtime_ns
            self._pending_config_apply_at = time.monotonic()
            self._append_log("[GUI] Config changed detected.\n")
            self._refresh_role_setting()
            refresher = getattr(self, "_auto_ban_refresher", None)
            if refresher is not None:
                try:
                    refresher.refresh_configured_targets()
                except Exception:
                    pass

        if self._pending_config_apply_at is not None:
            if (
                time.monotonic() - self._pending_config_apply_at
            ) >= CONFIG_APPLY_DEBOUNCE_SEC:
                self._pending_config_apply_at = None
                if self.proc and self.proc.poll() is None:
                    self._append_log("[GUI] Applying config change (auto restart).\n")
                    self._restart_after_exit = True
                    self.stop()
                elif self._auto_start_pending:
                    self.root.after(150, self.start)

        now = time.monotonic()
        if (now - float(self._match_stats_last_poll_at)) >= MATCH_STATS_POLL_MIN_SEC:
            self._match_stats_last_poll_at = float(now)
            self._refresh_match_stats(force=False)

        in_game = bool(state.in_game)
        league_running = bool(state.league_running)

        # `is_game_client_active()` can remain true briefly after LeagueClient has
        # exited.  The process guard is authoritative for our shutdown sequence.
        if not league_running:
            self.client_visible_var.set("Closed")
            want_topmost = False
            if self._client_seen_once:
                now = time.monotonic()
                if self._client_closed_at is None:
                    self._client_closed_at = now
                    self._client_close_cleanup_deadline = None
                elif (
                    now - self._client_closed_at
                ) >= AUTO_EXIT_AFTER_CLIENT_CLOSED_SEC:
                    proc = self.proc
                    proc_running = bool(proc is not None and proc.poll() is None)
                    if proc_running:
                        deadline = getattr(
                            self, "_client_close_cleanup_deadline", None
                        )
                        if deadline is None:
                            deadline = now + CLI_CLIENT_CLOSE_CLEANUP_GRACE_SEC
                            self._client_close_cleanup_deadline = deadline
                            self._append_log(
                                "[GUI] LoL client closed. Waiting for CLI to close owned OP.GG.\n"
                            )
                        if now < deadline:
                            self.root.after(EXTERNAL_SYNC_MS, self._sync_external_state)
                            return
                        self._append_log(
                            "[GUI] CLI cleanup wait timed out; forcing shutdown.\n"
                        )
                    self._append_log("[GUI] LoL client closed. Exiting.\n")
                    self._on_close(close_owned_opgg=True)
                    return
            else:
                self._client_closed_at = None
                self._client_close_cleanup_deadline = None
            if not self._is_ui_busy():
                self._apply_lol_topmost(want_topmost)
            if prev_topmost:
                self._restore_active_window_after_topmost_release()
            self.root.after(EXTERNAL_SYNC_MS, self._sync_external_state)
            return

        if in_game:
            self._client_closed_at = None
            self._client_close_cleanup_deadline = None
            try:
                self.client_visible_var.set("InGame")
            except Exception:
                pass

            manager_has_focus = self._is_manager_window_foreground()
            if should_auto_iconify_ingame(
                manager_has_focus=manager_has_focus,
                root_iconic=self._is_root_iconic(),
            ):
                try:
                    self.root.iconify()
                    self._auto_iconified = True
                except Exception:
                    pass
            win = getattr(self, "_log_window", None)
            if win is not None and not manager_has_focus:
                try:
                    if str(win.state() or "").strip().casefold() != "iconic":
                        win.iconify()
                except Exception:
                    pass

            want_topmost = False
            self._apply_lol_topmost(want_topmost)
            if prev_topmost and not want_topmost:
                self._restore_active_window_after_topmost_release()
            self.root.after(
                external_sync_delay_ms(in_game=True), self._sync_external_state
            )
            return

        rect = state.rect
        minimized = bool(state.minimized)
        league_foreground = bool(state.league_foreground)
        if rect is not None and not isinstance(rect, tuple):
            rect = None
        want_topmost = False

        if rect is not None:
            self._client_seen_once = True
            self._client_closed_at = None
            self._client_close_cleanup_deadline = None

            if minimized:
                self.client_visible_var.set("Minimized")
                want_topmost = False
                if not self._auto_iconified:
                    try:
                        self.root.iconify()
                        self._auto_iconified = True
                    except Exception:
                        pass
            else:
                self.client_visible_var.set("Visible")
                want_topmost = bool(league_foreground)
                if self._auto_iconified:
                    self._show_root_noactivate()
                    self._auto_iconified = False
                    self._enforce_compact_root_size()
                if not self._is_ui_busy():
                    self._snap_to_client(rect)
        else:
            if league_running:
                self.client_visible_var.set("Hidden")
                self._client_seen_once = True
                want_topmost = False
                if not self._auto_iconified:
                    try:
                        self.root.iconify()
                        self._auto_iconified = True
                    except Exception:
                        pass
                self._client_closed_at = None
                self._client_close_cleanup_deadline = None
        if not self._is_ui_busy():
            self._apply_lol_topmost(want_topmost)
        if prev_topmost and not want_topmost:
            self._restore_active_window_after_topmost_release()
        self.root.after(
            external_sync_delay_ms(in_game=in_game), self._sync_external_state
        )

    def _refresh_match_stats(self, *, force: bool) -> None:
        p = self._match_stats_path
        try:
            st = p.stat()
            mtime_ns = int(st.st_mtime_ns)
        except OSError:
            mtime_ns = 0

        if not force and mtime_ns == self._match_stats_mtime_ns:
            return
        self._match_stats_mtime_ns = mtime_ns

        durations, avg = load_match_timing_stats(p)
        self._match_avg_sec = avg
        if avg is None:
            self.match_avg_var.set("-")
        else:
            self.match_avg_var.set(f"{format_duration_mmss(avg)} ({len(durations)})")

    def _reset_match_timer_ui(self) -> None:
        self._match_timer_started_at = None
        self._match_elapsed_last_text = "-"
        self._match_slack_last_text = "-"
        if self.match_elapsed_var.get() != "-":
            self.match_elapsed_var.set("-")
        if self.match_slack_var.get() != "-":
            self.match_slack_var.set("-")
        self._set_match_slack_color(None)

    def _set_match_slack_color(self, kind: Optional[str]) -> None:
        if getattr(self, "_lbl_match_slack", None) is None:
            return
        if kind == self._match_slack_color_kind:
            return
        self._match_slack_color_kind = kind

        is_dark = bool(getattr(getattr(self, "_ui_theme", None), "is_dark", False))
        if kind == "pos":
            color = "#66bb6a" if is_dark else "#2e7d32"
        elif kind == "neg":
            color = "#ef5350" if is_dark else "#c62828"
        else:
            color = ""
        try:
            self._lbl_match_slack.configure(foreground=color)
        except Exception:
            pass

    def _on_client_state_for_match_timer(self, state: str) -> None:
        s = str(state).strip().upper()
        if s == "MATCH_FINDING":
            if self._match_timer_started_at is None:
                self._match_timer_started_at = time.monotonic()
            return
        if s in {"LOBBY", "UNKNOWN"}:
            if self._match_timer_started_at is not None:
                self._reset_match_timer_ui()
            return
        if s == "INGAME":
            self._reset_match_timer_ui()
            try:
                self.root.after(400, lambda: self._refresh_match_stats(force=False))
            except Exception:
                pass

    def _tick_match_timer(self) -> None:
        try:
            started = self._match_timer_started_at
            if started is None:
                if self.match_elapsed_var.get() != "-":
                    self.match_elapsed_var.set("-")
                if self.match_slack_var.get() != "-":
                    self.match_slack_var.set("-")
                self._set_match_slack_color(None)
                return
            elapsed = time.monotonic() - float(started)
            elapsed_txt = format_duration_mmss(elapsed)
            if elapsed_txt != self._match_elapsed_last_text:
                self._match_elapsed_last_text = elapsed_txt
                self.match_elapsed_var.set(elapsed_txt)

            avg = self._match_avg_sec
            if avg is None:
                if self._match_slack_last_text != "-":
                    self._match_slack_last_text = "-"
                    self.match_slack_var.set("-")
                self._set_match_slack_color(None)
            else:
                slack = float(avg) - float(elapsed)
                sign = "+" if slack >= 0.0 else "-"
                slack_txt = f"{sign}{format_duration_mmss(abs(slack))}"
                if slack_txt != self._match_slack_last_text:
                    self._match_slack_last_text = slack_txt
                    self.match_slack_var.set(slack_txt)
                self._set_match_slack_color("pos" if slack >= 0.0 else "neg")
        except Exception:
            pass
        finally:
            try:
                self.root.after(MATCH_TIMER_TICK_MS, self._tick_match_timer)
            except Exception:
                pass

    def _snap_to_client(self, rect: tuple[int, int, int, int]) -> None:
        left, top, right, _bottom = rect
        gap = int(getattr(self, "_snap_gap_px", 0))

        w = h = 1
        if self._outer_wh is not None:
            w, h = self._outer_wh
        else:
            try:
                self.root.update_idletasks()
            except Exception:
                return

            if bool(getattr(self, "_is_frameless", False)):
                w = int(self.root.winfo_width()) or int(self.root.winfo_reqwidth()) or 1
                h = (
                    int(self.root.winfo_height())
                    or int(self.root.winfo_reqheight())
                    or 1
                )
                self._outer_wh = (w, h)
            else:
                hwnd = 0
                try:
                    hwnd = int(self.root.winfo_id())
                except Exception:
                    hwnd = 0
                wrect = _get_hwnd_rect(hwnd)
                if wrect:
                    w = max(1, int(wrect[2] - wrect[0]))
                    h = max(1, int(wrect[3] - wrect[1]))
                    self._outer_wh = (w, h)
                else:
                    w = (
                        int(self.root.winfo_width())
                        or int(self.root.winfo_reqwidth())
                        or 1
                    )
                    h = (
                        int(self.root.winfo_height())
                        or int(self.root.winfo_reqheight())
                        or 1
                    )

        bounds = _work_area_for_rect(rect)

        left_x = int(left) - w - gap
        right_x = int(right) + gap
        if left_x >= bounds.left:
            x = left_x
        elif (right_x + w) <= bounds.right:
            x = right_x
        else:
            if (left_x + w) > bounds.left:
                x = left_x
            else:
                x = _clamp(left_x, bounds.left, max(bounds.left, bounds.right - w))

        y = _clamp(int(top), bounds.top, max(bounds.top, bounds.bottom - h))

        xy = (x, y)
        if self._last_geo_xy == xy and self._last_client_rect == rect:
            cur_xy: Optional[tuple[int, int]] = None
            try:
                hwnd = int(self.root.winfo_id())
            except Exception:
                hwnd = 0
            if hwnd:
                wrect = _get_hwnd_rect(hwnd)
                if wrect is not None:
                    cur_xy = (int(wrect[0]), int(wrect[1]))
            if cur_xy is None:
                try:
                    cur_xy = (int(self.root.winfo_x()), int(self.root.winfo_y()))
                except Exception:
                    cur_xy = None

            tol_px = 2
            try:
                tol_px = int(getattr(self, "_snap_tol_px", 2) or 2)
            except Exception:
                tol_px = 2

            if (
                cur_xy is not None
                and abs(int(cur_xy[0]) - int(x)) <= tol_px
                and abs(int(cur_xy[1]) - int(y)) <= tol_px
            ):
                return

        try:
            self.root.geometry(f"{int(x):+d}{int(y):+d}")
        except Exception:
            pass
        else:
            self._last_geo_xy = xy
            self._last_client_rect = rect

    def _on_close(self, *, close_owned_opgg: bool = False) -> None:
        if bool(getattr(self, "_closing", False)):
            return
        self._closing = True
        try:
            refresher = getattr(self, "_auto_ban_refresher", None)
            if refresher is not None:
                refresher.close()
        except Exception:
            pass
        try:
            after_id = getattr(self, "_proc_usage_after_id", None)
            if after_id:
                self.root.after_cancel(after_id)
        except Exception:
            pass
        try:
            self._external_sync_stop.set()
        except Exception:
            pass
        if close_owned_opgg:
            try:
                close_owned_opgg_for_current_session(
                    logger=logging.getLogger("lolmanager")
                )
            except Exception:
                pass
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=0.4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=0.4)
                except Exception:
                    pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main() -> None:
    work_dir = user_data_dir()
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        os.chdir(work_dir)
    except Exception:
        pass

    _set_app_user_model_id()
    root = tk.Tk()

    try:
        root.withdraw()

        root.update_idletasks()
    except Exception:
        pass
    _apply_readable_fonts(root)
    _apply_window_icon(root)

    LolManagerGui(root, auto_start=True)
    try:
        root.deiconify()
        root.lift()
    except Exception:
        pass
    root.mainloop()


if __name__ == "__main__":
    main()
