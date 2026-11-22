from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional, Tuple

import psutil
from pywinauto import findwindows

from lolmanager.platform.paths import images_dir

GAME_TITLE = "League of Legends (TM) Client"
GAME_PROCESS = "league of legends.exe"

ALLOWED_PROCESSES = {
    "leagueclient.exe",
    "leagueclientuxrender.exe",
    "leagueclientux.exe",
}

_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_GA_ROOT = 2
_GW_HWNDPREV = 3
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x80000
_LWA_ALPHA = 2
WindowRect = tuple[int, int, int, int]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


@dataclass(frozen=True)
class LeagueWindow:
    hwnd: int
    rect: WindowRect
    minimized: bool
    visible: bool


_MONITOR_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_RECT), ctypes.c_ssize_t
)
_USER32 = None
if os.name == "nt":
    try:
        _USER32 = ctypes.WinDLL("user32", use_last_error=True)
        _USER32.GetForegroundWindow.argtypes = []
        _USER32.GetForegroundWindow.restype = ctypes.c_void_p
        _USER32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        _USER32.GetWindowThreadProcessId.restype = ctypes.c_uint
        _USER32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        _USER32.GetAncestor.restype = ctypes.c_void_p
        _USER32.WindowFromPoint.argtypes = [_POINT]
        _USER32.WindowFromPoint.restype = ctypes.c_void_p
        _USER32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        _USER32.GetWindowRect.restype = ctypes.c_int
        _USER32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        _USER32.IsWindowVisible.restype = ctypes.c_int
        _USER32.IsIconic.argtypes = [ctypes.c_void_p]
        _USER32.IsIconic.restype = ctypes.c_int
        _USER32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        _USER32.GetWindow.restype = ctypes.c_void_p
        _USER32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _USER32.GetWindowLongW.restype = ctypes.c_long
        _USER32.GetLayeredWindowAttributes.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_uint)
        ]
        _USER32.GetLayeredWindowAttributes.restype = ctypes.c_int
        _USER32.EnumDisplayMonitors.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, _MONITOR_CALLBACK, ctypes.c_ssize_t
        ]
        _USER32.EnumDisplayMonitors.restype = ctypes.c_int
    except Exception:
        _USER32 = None

_FOREGROUND_PID_CACHE: int = 0
_FOREGROUND_NAME_CF_CACHE: str = ""
_FOREGROUND_IS_LOL_CACHE: bool = False


def _foreground_process_name_casefold() -> str:
    if os.name != "nt":
        return ""
    if _USER32 is None:
        return ""

    hwnd = 0
    try:
        hwnd = int(_USER32.GetForegroundWindow() or 0)
    except Exception:
        hwnd = 0
    if not hwnd:
        return ""

    pid = ctypes.c_uint(0)
    try:
        _USER32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    except Exception:
        pid = ctypes.c_uint(0)
    pid_i = int(pid.value or 0)
    if pid_i <= 0:
        return ""

    global _FOREGROUND_PID_CACHE, _FOREGROUND_NAME_CF_CACHE
    if pid_i == _FOREGROUND_PID_CACHE:
        return str(_FOREGROUND_NAME_CF_CACHE or "")

    _FOREGROUND_PID_CACHE = pid_i

    try:
        name = str(psutil.Process(pid_i).name() or "")
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        name = ""
    except Exception:
        name = ""

    _FOREGROUND_NAME_CF_CACHE = name.strip().casefold()
    return str(_FOREGROUND_NAME_CF_CACHE or "")


def is_lol_foreground() -> bool:
    if os.name != "nt":
        return False
    if _USER32 is None:
        return False
    global _FOREGROUND_IS_LOL_CACHE

    n = _foreground_process_name_casefold()
    _FOREGROUND_IS_LOL_CACHE = bool(
        n in ALLOWED_PROCESSES or n == str(GAME_PROCESS).strip().casefold()
    )
    return bool(_FOREGROUND_IS_LOL_CACHE)


def is_league_client_foreground() -> bool:
    n = _foreground_process_name_casefold()
    return bool(n and n in ALLOWED_PROCESSES)


def is_game_foreground() -> bool:
    n = _foreground_process_name_casefold()
    return bool(n and n == str(GAME_PROCESS).strip().casefold())


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


def _try_get_extended_frame_bounds(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    if os.name != "nt":
        return None
    if not hwnd:
        return None
    if _DWMAPI is None:
        return None
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
        return None
    if hr != 0:
        return None
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _is_league_client_window(hwnd: int) -> bool:
    # Do not reuse the foreground PID cache for input authorization: a process
    # may have exited or its PID may have been reused since the last poll.
    if not hwnd or _USER32 is None:
        return False
    pid = ctypes.c_uint(0)
    if not _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
        return False
    return psutil.Process(pid.value).name().strip().casefold() in ALLOWED_PROCESSES


def is_league_client_window_target(
    window_rect: Optional[Tuple[int, int, int, int]],
    point: Optional[Tuple[int, int]] = None,
) -> bool:
    """Require the current client window, and optionally its exposed click point.

    Desktop captures include covering apps. A matching image or window title
    alone cannot authorize a click; any missing window/process evidence denies it.
    """
    if _USER32 is None or window_rect is None:
        return False
    try:
        left, top, right, bottom = window_rect
        if right <= left or bottom <= top or is_rect_minimized(window_rect):
            return False

        hwnd = int(_USER32.GetForegroundWindow() or 0)
        if not _is_league_client_window(hwnd):
            return False
        if not _USER32.IsWindowVisible(hwnd) or _USER32.IsIconic(hwnd):
            return False

        current_rect = _try_get_extended_frame_bounds(hwnd)
        if current_rect is None:
            rect = _RECT()
            if not _USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return False
            current_rect = (rect.left, rect.top, rect.right, rect.bottom)
        if current_rect != tuple(window_rect):
            return False

        if point is not None:
            x, y = point
            if not (left <= x < right and top <= y < bottom):
                return False
            target = int(_USER32.WindowFromPoint(_POINT(x, y)) or 0)
            if not target or int(_USER32.GetAncestor(target, _GA_ROOT) or 0) != hwnd:
                return False
            if not _is_league_client_window(target):
                return False

        return int(_USER32.GetForegroundWindow() or 0) == hwnd
    except Exception:
        # Closed windows, access-denied processes and unavailable Win32 APIs
        # must never turn an uncertain match into a desktop click.
        return False


def _is_cloaked(hwnd: int) -> bool:
    if _DWMAPI is None:
        return False
    cloaked = ctypes.c_uint()
    return bool(
        _DWMAPI.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0
        and cloaked.value
    )


def find_league_window(
    target_title: str = "League of Legends",
    *,
    visible_only: bool = True,
) -> Optional[LeagueWindow]:
    try:
        elements = findwindows.find_elements(
            title=target_title, visible_only=bool(visible_only)
        )
    except findwindows.ElementNotFoundError:
        return None

    hidden_candidate: Optional[LeagueWindow] = None
    for elem in elements:
        pid = getattr(elem, "process_id", None)
        name = None
        if pid:
            try:
                name = psutil.Process(pid).name().lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                name = None
        if name not in ALLOWED_PROCESSES:
            continue

        hwnd = 0
        try:
            hwnd = int(getattr(elem, "handle", 0) or 0)
        except Exception:
            hwnd = 0
        tup = _try_get_extended_frame_bounds(hwnd)
        if tup is None:
            rect = elem.rectangle
            tup = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

        minimized = is_rect_minimized(tup)
        visible = True
        if _USER32 is not None and hwnd:
            minimized = minimized or bool(_USER32.IsIconic(hwnd))
            visible = bool(_USER32.IsWindowVisible(hwnd)) and not _is_cloaked(hwnd)
        window = LeagueWindow(hwnd, tup, minimized, visible)
        if visible and not minimized:
            return window
        if visible or not visible_only:
            hidden_candidate = window
    return hidden_candidate


def find_league_window_rect(
    target_title: str = "League of Legends", *, visible_only: bool = True
) -> Optional[WindowRect]:
    window = find_league_window(target_title, visible_only=visible_only)
    return window.rect if window else None


def _intersect_rect(first: WindowRect, second: WindowRect) -> Optional[WindowRect]:
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    return (left, top, right, bottom) if left < right and top < bottom else None


def _subtract_rect(rect: WindowRect, cover: WindowRect) -> list[WindowRect]:
    overlap = _intersect_rect(rect, cover)
    if overlap is None:
        return [rect]
    left, top, right, bottom = rect
    x1, y1, x2, y2 = overlap
    return [
        (x, y, r, b)
        for x, y, r, b in ((left, top, right, y1), (left, y2, right, bottom),
                           (left, y1, x1, y2), (x2, y1, right, y2))
        if x < r and y < b
    ]


def _opaque_window_rect(hwnd: int) -> Optional[WindowRect]:
    if not _USER32.IsWindowVisible(hwnd) or _USER32.IsIconic(hwnd) or _is_cloaked(hwnd):
        return None
    pid = ctypes.c_uint()
    if not _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or pid.value == os.getpid():
        return None
    if _USER32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_LAYERED:
        alpha, flags = ctypes.c_ubyte(), ctypes.c_uint()
        if not _USER32.GetLayeredWindowAttributes(hwnd, None, ctypes.byref(alpha), ctypes.byref(flags)):
            return None
        if flags.value != _LWA_ALPHA or alpha.value != 255:
            return None
    return _try_get_extended_frame_bounds(hwnd)


def is_league_window_occluded(window: LeagueWindow) -> bool:
    if _USER32 is None or not window.hwnd:
        return False
    exposed: list[WindowRect] = []

    @_MONITOR_CALLBACK
    def add_monitor(_monitor, _dc, bounds, _data):
        size = bounds.contents
        overlap = _intersect_rect(window.rect, (size.left, size.top, size.right, size.bottom))
        if overlap is not None:
            exposed.append(overlap)
        return 1

    if not _USER32.EnumDisplayMonitors(None, None, add_monitor, 0):
        return False
    seen = {window.hwnd}
    hwnd = window.hwnd
    while exposed:
        hwnd = _USER32.GetWindow(hwnd, _GW_HWNDPREV)
        if not hwnd or hwnd in seen:
            return False
        seen.add(hwnd)
        cover = _opaque_window_rect(hwnd)
        if cover is not None:
            exposed = [remaining for rect in exposed for remaining in _subtract_rect(rect, cover)]
    return True


def is_league_client_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if not name:
            continue
        if name.lower() in ALLOWED_PROCESSES:
            return True
    return False


def window_size_from_rect(rect: Tuple[int, int, int, int]) -> Tuple[int, int]:
    left, top, right, bottom = rect
    return right - left, bottom - top


def is_rect_minimized(rect: Tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = rect
    return left < -10000 or top < -10000 or right < -10000 or bottom < -10000


def is_game_client_active() -> bool:
    try:
        elems = findwindows.find_elements(title=GAME_TITLE, visible_only=True)
        if elems:
            return True
    except findwindows.ElementNotFoundError:
        pass

    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if name and name.lower() == GAME_PROCESS:
            return True
    return False


def select_image_set(
    window_width: int, images_root: Optional[Path] = None
) -> Optional[Path]:
    images_root = images_dir() if images_root is None else images_root
    candidates = []
    for child in images_root.iterdir():
        if child.is_dir() and child.name.isdigit():
            size = int(child.name)
            candidates.append((abs(size - window_width), size, child))
    if not candidates:
        return None
    _, _, best_path = min(candidates, key=lambda x: (x[0], -x[1]))
    return best_path
