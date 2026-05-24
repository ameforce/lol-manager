from __future__ import annotations

import ctypes
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


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


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


def find_league_window_rect(
    target_title: str = "League of Legends",
    *,
    visible_only: bool = True,
) -> Optional[Tuple[int, int, int, int]]:
    try:
        elements = findwindows.find_elements(
            title=target_title, visible_only=bool(visible_only)
        )
    except findwindows.ElementNotFoundError:
        return None

    minimized_candidate: Optional[Tuple[int, int, int, int]] = None
    for elem in elements:
        pid = getattr(elem, "process_id", None)
        name = None
        if pid:
            try:
                name = psutil.Process(pid).name().lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                name = None
        if name and name not in ALLOWED_PROCESSES:
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

        if not is_rect_minimized(tup):
            return tup
        minimized_candidate = tup
    return minimized_candidate


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
