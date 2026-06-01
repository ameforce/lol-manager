from __future__ import annotations

from functools import lru_cache
import ctypes
import logging
import os
from pathlib import Path
from typing import Optional, Tuple
import time

import cv2
import numpy as np
from mss import mss
from pywinauto import mouse, keyboard
import pyperclip

logger = logging.getLogger(__name__)

_PYWINAUTO_LITERAL_ESCAPES = {
    "{": "{{}",
    "}": "{}}",
    "+": "{+}",
    "^": "{^}",
    "%": "{%}",
    "~": "{~}",
    "(": "{(}",
    ")": "{)}",
}


def _escape_pywinauto_literal_text(value: object) -> str:
    return "".join(
        _PYWINAUTO_LITERAL_ESCAPES.get(ch, ch) for ch in str(value or "")
    )

_PROFILE_IMAGE = False
try:
    _p = str(os.environ.get("LOLMANAGER_PROFILE_IMAGE", "") or "").strip().casefold()
    _PROFILE_IMAGE = _p not in {"", "0", "false", "no", "n", "off"}
except Exception:
    _PROFILE_IMAGE = False

_DISABLE_GRAB_CACHE = False
_DISABLE_ROI_CACHE = False
try:
    _v = str(os.environ.get("LOLMANAGER_DISABLE_GRAB_CACHE", "") or "").strip().casefold()
    _DISABLE_GRAB_CACHE = _v in {"1", "true", "yes", "y", "on"}
except Exception:
    _DISABLE_GRAB_CACHE = False
try:
    _v = str(os.environ.get("LOLMANAGER_DISABLE_ROI_CACHE", "") or "").strip().casefold()
    _DISABLE_ROI_CACHE = _v in {"1", "true", "yes", "y", "on"}
except Exception:
    _DISABLE_ROI_CACHE = False

_PROFILE_LOG_EVERY_MATCHES = 200
_PROF_GRAB_CALLS = 0
_PROF_GRAB_MISSES = 0
_PROF_GRAB_SEC = 0.0
_PROF_MATCH_CALLS = 0
_PROF_MATCH_SEC = 0.0


def _profile_maybe_log() -> None:
    if not _PROFILE_IMAGE:
        return
    if _PROF_MATCH_CALLS <= 0:
        return
    if int(_PROF_MATCH_CALLS) % int(_PROFILE_LOG_EVERY_MATCHES) != 0:
        return

    miss_rate = (
        float(_PROF_GRAB_MISSES) / float(_PROF_GRAB_CALLS) if _PROF_GRAB_CALLS else 0.0
    )
    avg_grab_ms = (
        (float(_PROF_GRAB_SEC) / float(_PROF_GRAB_MISSES)) * 1000.0
        if _PROF_GRAB_MISSES
        else 0.0
    )
    avg_match_ms = (float(_PROF_MATCH_SEC) / float(_PROF_MATCH_CALLS)) * 1000.0
    logger.info(
        "[profile:image] grab_calls=%d miss=%d(%.1f%%) avg_grab=%.2fms match_calls=%d avg_match=%.2fms",
        int(_PROF_GRAB_CALLS),
        int(_PROF_GRAB_MISSES),
        float(miss_rate * 100.0),
        float(avg_grab_ms),
        int(_PROF_MATCH_CALLS),
        float(avg_match_ms),
    )


def _match_template_maxloc(
    search_gray: np.ndarray,
    template_gray: np.ndarray,
) -> tuple[float, Tuple[int, int]]:
    global _PROF_MATCH_CALLS, _PROF_MATCH_SEC
    t0 = time.perf_counter() if _PROFILE_IMAGE else 0.0
    res = cv2.matchTemplate(search_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
    if _PROFILE_IMAGE:
        _PROF_MATCH_CALLS += 1
        _PROF_MATCH_SEC += float(time.perf_counter() - float(t0))
        _profile_maybe_log()
    return float(max_val), (int(max_loc[0]), int(max_loc[1]))


@lru_cache(maxsize=1)
def _get_mss() -> mss:
    return mss()


_GRAB_CACHE_MAX_AGE_SEC = 0.050
_LAST_GRAB_RECT: Optional[Tuple[int, int, int, int]] = None
_LAST_GRAB_AT_MONO: float = 0.0
_LAST_GRAB_BGRA: Optional[np.ndarray] = None
_LAST_GRAB_GRAY: Optional[np.ndarray] = None
_LAST_GRAB_FRAME_TOKEN: int = 0


def _grab_region_bgra(rect: Tuple[int, int, int, int]) -> np.ndarray:
    global _LAST_GRAB_RECT, _LAST_GRAB_AT_MONO, _LAST_GRAB_BGRA, _LAST_GRAB_GRAY
    global _LAST_GRAB_FRAME_TOKEN
    global _PROF_GRAB_CALLS, _PROF_GRAB_MISSES, _PROF_GRAB_SEC

    left, top, right, bottom = rect
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise ValueError("잘못된 캡처 영역입니다.")

    now = time.monotonic()
    if _PROFILE_IMAGE:
        _PROF_GRAB_CALLS += 1
    if (
        not _DISABLE_GRAB_CACHE
        and _GRAB_CACHE_MAX_AGE_SEC > 0.0
        and _LAST_GRAB_RECT == rect
        and _LAST_GRAB_BGRA is not None
        and (now - float(_LAST_GRAB_AT_MONO)) <= _GRAB_CACHE_MAX_AGE_SEC
    ):
        return _LAST_GRAB_BGRA

    sct = _get_mss()
    monitor = {"left": left, "top": top, "width": width, "height": height}
    t0 = time.perf_counter() if _PROFILE_IMAGE else 0.0
    shot = sct.grab(monitor)

    bgra = np.frombuffer(shot.raw, dtype=np.uint8).reshape((height, width, 4))
    if _PROFILE_IMAGE:
        _PROF_GRAB_MISSES += 1
        _PROF_GRAB_SEC += float(time.perf_counter() - float(t0))
    _LAST_GRAB_FRAME_TOKEN += 1
    _LAST_GRAB_RECT = rect
    _LAST_GRAB_AT_MONO = float(now)
    _LAST_GRAB_BGRA = bgra
    _LAST_GRAB_GRAY = None
    return bgra


def _grab_region_bgra_and_gray(
    rect: Tuple[int, int, int, int],
) -> tuple[np.ndarray, np.ndarray]:
    global _LAST_GRAB_GRAY

    bgra = _grab_region_bgra(rect)

    now = time.monotonic()
    if (
        _LAST_GRAB_RECT == rect
        and _LAST_GRAB_GRAY is not None
        and (now - float(_LAST_GRAB_AT_MONO)) <= _GRAB_CACHE_MAX_AGE_SEC
    ):
        return bgra, _LAST_GRAB_GRAY

    gray = cv2.cvtColor(bgra, cv2.COLOR_BGRA2GRAY)
    _LAST_GRAB_GRAY = gray
    return bgra, gray


_TEMPLATE_ROI_CACHE: dict[str, tuple[Tuple[int, int, int, int], float]] = {}
_TEMPLATE_MISS_CACHE: dict[str, float] = {}
_TEMPLATE_MATCH_RESULT_CACHE: dict[
    tuple[str, int, int, int, Tuple[int, int, int, int]],
    tuple[float, Tuple[int, int], Tuple[int, int, int, int]],
] = {}
_TEMPLATE_MATCH_RESULT_CACHE_MAX = 256


def _template_cache_key(template_path: Path, screen_w: int, screen_h: int) -> str:
    return f"{str(template_path)}|{int(screen_w)}x{int(screen_h)}"


def _template_miss_cache_key(
    template_path: Path, screen_w: int, screen_h: int, threshold: float
) -> str:
    return f"{_template_cache_key(template_path, screen_w, screen_h)}|thr={float(threshold):.4f}"


def _clamp_roi(
    roi: Tuple[int, int, int, int], screen_w: int, screen_h: int
) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = roi
    x1 = max(0, min(int(x1), int(screen_w)))
    y1 = max(0, min(int(y1), int(screen_h)))
    x2 = max(0, min(int(x2), int(screen_w)))
    y2 = max(0, min(int(y2), int(screen_h)))
    return (x1, y1, x2, y2)


def _roi_for_hit(
    top_left_x: int,
    top_left_y: int,
    template_w: int,
    template_h: int,
    screen_w: int,
    screen_h: int,
) -> Tuple[int, int, int, int]:
    cx = int(top_left_x) + int(template_w) // 2
    cy = int(top_left_y) + int(template_h) // 2
    mx = max(48, int(template_w) * 2)
    my = max(48, int(template_h) * 2)
    return _clamp_roi((cx - mx, cy - my, cx + mx, cy + my), screen_w, screen_h)


def _get_cached_roi(key: str) -> Optional[Tuple[int, int, int, int]]:
    item = _TEMPLATE_ROI_CACHE.get(key)
    if not item:
        return None
    roi, _ts = item
    return roi


def _set_cached_roi(key: str, roi: Tuple[int, int, int, int]) -> None:
    _TEMPLATE_ROI_CACHE[key] = (roi, float(time.monotonic()))


def _clear_cached_roi(key: str) -> None:
    _TEMPLATE_ROI_CACHE.pop(key, None)


def _has_recent_template_miss(key: str) -> bool:
    missed_at = _TEMPLATE_MISS_CACHE.get(key)
    if missed_at is None:
        return False
    if time.monotonic() - float(missed_at) <= _GRAB_CACHE_MAX_AGE_SEC:
        return True
    _TEMPLATE_MISS_CACHE.pop(key, None)
    return False


def _set_recent_template_miss(key: str) -> None:
    _TEMPLATE_MISS_CACHE[key] = float(time.monotonic())


def _clear_recent_template_miss(key: str) -> None:
    _TEMPLATE_MISS_CACHE.pop(key, None)


def _get_cached_match_result(
    cache_key: tuple[str, int, int, int, Tuple[int, int, int, int]],
) -> Optional[tuple[float, Tuple[int, int], Tuple[int, int, int, int]]]:
    return _TEMPLATE_MATCH_RESULT_CACHE.get(cache_key)


def _set_cached_match_result(
    cache_key: tuple[str, int, int, int, Tuple[int, int, int, int]],
    result: tuple[float, Tuple[int, int], Tuple[int, int, int, int]],
) -> None:
    if len(_TEMPLATE_MATCH_RESULT_CACHE) >= _TEMPLATE_MATCH_RESULT_CACHE_MAX:
        try:
            oldest_key = next(iter(_TEMPLATE_MATCH_RESULT_CACHE))
            _TEMPLATE_MATCH_RESULT_CACHE.pop(oldest_key, None)
        except StopIteration:
            pass
    _TEMPLATE_MATCH_RESULT_CACHE[cache_key] = result


_USER32 = ctypes.WinDLL("user32", use_last_error=True)

_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000


_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_I(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _INPUT_I)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_USER32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
_USER32.GetCursorPos.restype = ctypes.c_bool
_USER32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_USER32.SetCursorPos.restype = ctypes.c_bool


def _get_cursor_pos() -> Optional[tuple[int, int]]:
    pt = _POINT()
    if not _USER32.GetCursorPos(ctypes.byref(pt)):
        err = ctypes.get_last_error()
        logger.debug("GetCursorPos 실패: last_error=%d", err)
        return None
    return int(pt.x), int(pt.y)


def _restore_cursor_pos(pos: tuple[int, int]) -> None:
    x, y = pos
    if not _USER32.SetCursorPos(int(x), int(y)):
        err = ctypes.get_last_error()
        logger.debug("SetCursorPos 실패: (%d, %d) last_error=%d", x, y, err)


@lru_cache(maxsize=1)
def _virtual_screen() -> tuple[int, int, int, int]:
    vx = int(_USER32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
    vy = int(_USER32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    vw = int(_USER32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
    vh = int(_USER32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    return vx, vy, vw, vh


def _normalize_point(point: Tuple[int, int]) -> tuple[int, int]:
    try:
        return int(point[0]), int(point[1])
    except Exception:
        return int(round(float(point[0]))), int(round(float(point[1])))


def _assert_point_in_virtual_screen(x: int, y: int) -> None:
    vx, vy, vw, vh = _virtual_screen()
    if vw <= 0 or vh <= 0:
        return
    if not (vx <= x < vx + vw and vy <= y < vy + vh):
        raise ValueError(
            f"클릭 좌표가 가상 스크린 범위를 벗어났습니다: ({x}, {y}) "
            f"(virtual=left={vx},top={vy},w={vw},h={vh})"
        )


def _sendinput_click(x: int, y: int) -> bool:
    vx, vy, vw, vh = _virtual_screen()
    if vw <= 1 or vh <= 1:
        return False

    nx = int((x - vx) * 65535 / (vw - 1))
    ny = int((y - vy) * 65535 / (vh - 1))

    move = _INPUT(
        type=_INPUT_MOUSE,
        ii=_INPUT_I(
            mi=_MOUSEINPUT(
                dx=nx,
                dy=ny,
                mouseData=0,
                dwFlags=_MOUSEEVENTF_MOVE
                | _MOUSEEVENTF_ABSOLUTE
                | _MOUSEEVENTF_VIRTUALDESK,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    down = _INPUT(
        type=_INPUT_MOUSE,
        ii=_INPUT_I(
            mi=_MOUSEINPUT(
                dx=0,
                dy=0,
                mouseData=0,
                dwFlags=_MOUSEEVENTF_LEFTDOWN,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    up = _INPUT(
        type=_INPUT_MOUSE,
        ii=_INPUT_I(
            mi=_MOUSEINPUT(
                dx=0,
                dy=0,
                mouseData=0,
                dwFlags=_MOUSEEVENTF_LEFTUP,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )

    inputs = (_INPUT * 3)(move, down, up)
    sent = int(_USER32.SendInput(3, ctypes.byref(inputs), ctypes.sizeof(_INPUT)))
    if sent == 3:
        return True

    err = ctypes.get_last_error()
    logger.debug("SendInput 실패: sent=%d last_error=%d", sent, err)
    return False


@lru_cache(maxsize=128)
def _load_template_gray_cached(path_str: str, mtime_ns: int) -> Optional[np.ndarray]:
    _ = mtime_ns
    template_bgr = cv2.imread(path_str, cv2.IMREAD_COLOR)
    if template_bgr is None:
        return None
    return cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)


def _load_template_gray(template_path: Path) -> Optional[np.ndarray]:
    if not template_path.exists():
        return None
    try:
        mtime_ns = template_path.stat().st_mtime_ns
    except OSError:
        return None
    return _load_template_gray_cached(str(template_path), mtime_ns)


def find_best_template(
    window_rect: Tuple[int, int, int, int],
    templates: list[tuple[str, Path]],
    threshold: float = 0.85,
) -> Optional[tuple[str, float, float]]:
    if not templates:
        return None

    _bgra, screen_gray = _grab_region_bgra_and_gray(window_rect)

    best_name: Optional[str] = None
    best_score = -1.0
    second_score = -1.0

    for name, tpl_path in templates:
        tpl_gray = _load_template_gray(tpl_path)
        if tpl_gray is None:
            continue

        max_val, _max_loc = _match_template_maxloc(screen_gray, tpl_gray)

        if max_val > best_score:
            second_score = best_score
            best_score = max_val
            best_name = name
        elif max_val > second_score:
            second_score = max_val

    if best_name is None:
        return None

    logger.debug(
        "다중 템플릿 best=%s score=%.3f second=%.3f threshold=%.3f",
        best_name,
        best_score,
        second_score,
        threshold,
    )

    if best_score < threshold:
        return None

    return (best_name, float(best_score), float(second_score))


def find_template_center(
    window_rect: Tuple[int, int, int, int],
    template_path: Path,
    threshold: float = 0.85,
) -> Optional[Tuple[int, int]]:
    if not template_path.exists():
        logger.error("템플릿이 없습니다: %s", template_path)
        return None

    rect_w = int(window_rect[2]) - int(window_rect[0])
    rect_h = int(window_rect[3]) - int(window_rect[1])
    miss_key = _template_miss_cache_key(template_path, rect_w, rect_h, threshold)
    if not _DISABLE_ROI_CACHE and _has_recent_template_miss(miss_key):
        return None

    _bgra, screen_gray = _grab_region_bgra_and_gray(window_rect)
    screen_h, screen_w = screen_gray.shape[:2]
    template_gray = _load_template_gray(template_path)
    if template_gray is None:
        logger.error("템플릿 로드 실패: %s", template_path)
        return None

    template_h, template_w = template_gray.shape
    key = _template_cache_key(template_path, screen_w, screen_h)
    cached_roi = None if _DISABLE_ROI_CACHE else _get_cached_roi(key)

    def _try_match(
        roi: Optional[Tuple[int, int, int, int]],
    ) -> tuple[float, Tuple[int, int], Tuple[int, int, int, int]]:
        if roi is None:
            full_roi = (0, 0, screen_w, screen_h)
            result_key = (
                key,
                _LAST_GRAB_FRAME_TOKEN,
                id(screen_gray),
                id(template_gray),
                full_roi,
            )
            cached = _get_cached_match_result(result_key)
            if cached is not None:
                return cached
            max_val, max_loc = _match_template_maxloc(screen_gray, template_gray)
            result = (
                float(max_val),
                (int(max_loc[0]), int(max_loc[1])),
                full_roi,
            )
            _set_cached_match_result(result_key, result)
            return result

        x1, y1, x2, y2 = _clamp_roi(roi, screen_w, screen_h)
        if x2 <= x1 or y2 <= y1:
            return -1.0, (0, 0), (0, 0, screen_w, screen_h)
        search_gray = screen_gray[y1:y2, x1:x2]
        sh, sw = search_gray.shape[:2]
        if sh < template_h or sw < template_w:
            return -1.0, (0, 0), (x1, y1, x2, y2)
        roi_box = (x1, y1, x2, y2)
        result_key = (
            key,
            _LAST_GRAB_FRAME_TOKEN,
            id(screen_gray),
            id(template_gray),
            roi_box,
        )
        cached = _get_cached_match_result(result_key)
        if cached is not None:
            return cached
        max_val, max_loc = _match_template_maxloc(search_gray, template_gray)
        result = (
            float(max_val),
            (int(max_loc[0]) + x1, int(max_loc[1]) + y1),
            roi_box,
        )
        _set_cached_match_result(result_key, result)
        return result

    used_roi = cached_roi is not None
    max_val, max_loc, used_roi_box = _try_match(cached_roi if cached_roi is not None else None)
    if used_roi and max_val < threshold:
        max_val, max_loc, used_roi_box = _try_match(None)

    logger.debug(
        "템플릿[%s] 매칭 score=%.3f roi=(%d,%d,%d,%d)",
        template_path.name,
        float(max_val),
        int(used_roi_box[0]),
        int(used_roi_box[1]),
        int(used_roi_box[2]),
        int(used_roi_box[3]),
    )
    if max_val < threshold:
        if used_roi and not _DISABLE_ROI_CACHE:
            _clear_cached_roi(key)
        if not _DISABLE_ROI_CACHE:
            _set_recent_template_miss(miss_key)
        logger.debug(
            "템플릿[%s] 매칭 스코어 미달: %.3f < %.3f",
            template_path.name,
            max_val,
            threshold,
        )
        return None

    top_left = (int(max_loc[0]), int(max_loc[1]))
    if not _DISABLE_ROI_CACHE:
        _clear_recent_template_miss(miss_key)
        _set_cached_roi(
            key,
            _roi_for_hit(top_left[0], top_left[1], template_w, template_h, screen_w, screen_h),
        )
    center_x = window_rect[0] + top_left[0] + template_w // 2
    center_y = window_rect[1] + top_left[1] + template_h // 2
    return (center_x, center_y)


def find_template_match(
    window_rect: Tuple[int, int, int, int],
    template_path: Path,
    threshold: float = 0.85,
) -> Optional[Tuple[Tuple[int, int], np.ndarray, float]]:
    if not template_path.exists():
        logger.error("템플릿이 없습니다: %s", template_path)
        return None

    rect_w = int(window_rect[2]) - int(window_rect[0])
    rect_h = int(window_rect[3]) - int(window_rect[1])
    miss_key = _template_miss_cache_key(template_path, rect_w, rect_h, threshold)
    if not _DISABLE_ROI_CACHE and _has_recent_template_miss(miss_key):
        return None

    screen_bgra, screen_gray = _grab_region_bgra_and_gray(window_rect)
    screen_h, screen_w = screen_gray.shape[:2]
    template_gray = _load_template_gray(template_path)
    if template_gray is None:
        logger.error("템플릿 로드 실패: %s", template_path)
        return None

    template_h, template_w = template_gray.shape

    key = _template_cache_key(template_path, screen_w, screen_h)
    cached_roi = None if _DISABLE_ROI_CACHE else _get_cached_roi(key)

    def _try_match(
        roi: Optional[Tuple[int, int, int, int]],
    ) -> tuple[float, Tuple[int, int]]:
        if roi is None:
            full_roi = (0, 0, screen_w, screen_h)
            result_key = (
                key,
                _LAST_GRAB_FRAME_TOKEN,
                id(screen_gray),
                id(template_gray),
                full_roi,
            )
            cached = _get_cached_match_result(result_key)
            if cached is not None:
                return cached[0], cached[1]
            max_val, max_loc = _match_template_maxloc(screen_gray, template_gray)
            result = (
                float(max_val),
                (int(max_loc[0]), int(max_loc[1])),
                full_roi,
            )
            _set_cached_match_result(result_key, result)
            return result[0], result[1]

        x1, y1, x2, y2 = _clamp_roi(roi, screen_w, screen_h)
        if x2 <= x1 or y2 <= y1:
            return -1.0, (0, 0)
        search_gray = screen_gray[y1:y2, x1:x2]
        sh, sw = search_gray.shape[:2]
        if sh < template_h or sw < template_w:
            return -1.0, (0, 0)
        roi_box = (x1, y1, x2, y2)
        result_key = (
            key,
            _LAST_GRAB_FRAME_TOKEN,
            id(screen_gray),
            id(template_gray),
            roi_box,
        )
        cached = _get_cached_match_result(result_key)
        if cached is not None:
            return cached[0], cached[1]
        max_val, max_loc = _match_template_maxloc(search_gray, template_gray)
        result = (
            float(max_val),
            (int(max_loc[0]) + x1, int(max_loc[1]) + y1),
            roi_box,
        )
        _set_cached_match_result(result_key, result)
        return result[0], result[1]

    used_roi = cached_roi is not None
    max_val, max_loc = _try_match(cached_roi if cached_roi is not None else None)
    if used_roi and max_val < threshold:
        max_val, max_loc = _try_match(None)
    if max_val < threshold:
        if used_roi and not _DISABLE_ROI_CACHE:
            _clear_cached_roi(key)
        if not _DISABLE_ROI_CACHE:
            _set_recent_template_miss(miss_key)
        return None

    x, y = int(max_loc[0]), int(max_loc[1])
    if not _DISABLE_ROI_CACHE:
        _clear_recent_template_miss(miss_key)
        _set_cached_roi(key, _roi_for_hit(x, y, template_w, template_h, screen_w, screen_h))

    roi_bgr = screen_bgra[y : y + template_h, x : x + template_w, :3]

    center_x = window_rect[0] + x + template_w // 2
    center_y = window_rect[1] + y + template_h // 2
    return (center_x, center_y), roi_bgr, float(max_val)


def find_template_matches_once(
    window_rect: Tuple[int, int, int, int],
    templates: list[tuple[str, Path]],
    threshold: float = 0.85,
    *,
    search_rois: Optional[dict[str, Tuple[int, int, int, int]]] = None,
) -> dict[str, tuple[Tuple[int, int], np.ndarray, float]]:
    if not templates:
        return {}

    screen_bgra, screen_gray = _grab_region_bgra_and_gray(window_rect)
    screen_h, screen_w = screen_gray.shape[:2]

    matches: dict[str, tuple[Tuple[int, int], np.ndarray, float]] = {}
    local_match_cache: dict[
        tuple[int, Tuple[int, int, int, int]],
        tuple[float, Tuple[int, int], Tuple[int, int, int, int]],
    ] = {}
    for name, tpl_path in templates:
        tpl_gray = _load_template_gray(tpl_path)
        if tpl_gray is None:
            continue

        template_h, template_w = tpl_gray.shape

        forced_roi = None
        if search_rois:
            forced_roi = search_rois.get(name)

        key = _template_cache_key(tpl_path, screen_w, screen_h)
        miss_key = _template_miss_cache_key(tpl_path, screen_w, screen_h, threshold)
        if (
            forced_roi is None
            and not _DISABLE_ROI_CACHE
            and _has_recent_template_miss(miss_key)
        ):
            continue
        cached_roi = None if _DISABLE_ROI_CACHE else _get_cached_roi(key)

        def _match_once(
            roi: Optional[Tuple[int, int, int, int]],
        ) -> tuple[float, Tuple[int, int], Tuple[int, int, int, int]]:
            if roi is None:
                full_roi = (0, 0, screen_w, screen_h)
                local_key = (id(tpl_gray), full_roi)
                cached = local_match_cache.get(local_key)
                if cached is not None:
                    return cached
                max_val, max_loc = _match_template_maxloc(screen_gray, tpl_gray)
                result = (
                    float(max_val),
                    (int(max_loc[0]), int(max_loc[1])),
                    full_roi,
                )
                local_match_cache[local_key] = result
                return result
            x1, y1, x2, y2 = _clamp_roi(roi, screen_w, screen_h)
            if x2 <= x1 or y2 <= y1:
                return -1.0, (0, 0), (0, 0, screen_w, screen_h)
            search_gray = screen_gray[y1:y2, x1:x2]
            sh, sw = search_gray.shape[:2]
            if sh < template_h or sw < template_w:
                return -1.0, (0, 0), (x1, y1, x2, y2)
            roi_box = (x1, y1, x2, y2)
            local_key = (id(tpl_gray), roi_box)
            cached = local_match_cache.get(local_key)
            if cached is not None:
                return cached
            max_val, max_loc = _match_template_maxloc(search_gray, tpl_gray)
            result = (
                float(max_val),
                (int(max_loc[0]) + x1, int(max_loc[1]) + y1),
                roi_box,
            )
            local_match_cache[local_key] = result
            return result

        roi_to_use = forced_roi if forced_roi is not None else cached_roi
        used_cached = (forced_roi is None) and (cached_roi is not None)

        max_val, max_loc, used_roi_box = _match_once(roi_to_use)
        if used_cached and max_val < threshold:
            max_val, max_loc, used_roi_box = _match_once(None)
        if max_val < threshold:
            if used_cached and not _DISABLE_ROI_CACHE:
                _clear_cached_roi(key)
            if forced_roi is None and not _DISABLE_ROI_CACHE:
                _set_recent_template_miss(miss_key)
            continue

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "템플릿[%s] 매칭 score=%.3f roi=(%d,%d,%d,%d)",
                tpl_path.name,
                float(max_val),
                int(used_roi_box[0]),
                int(used_roi_box[1]),
                int(used_roi_box[2]),
                int(used_roi_box[3]),
            )

        x, y = int(max_loc[0]), int(max_loc[1])
        if not _DISABLE_ROI_CACHE:
            _clear_recent_template_miss(miss_key)
            _set_cached_roi(key, _roi_for_hit(x, y, template_w, template_h, screen_w, screen_h))

        roi_bgr = screen_bgra[y : y + template_h, x : x + template_w, :3]

        center_x = window_rect[0] + x + template_w // 2
        center_y = window_rect[1] + y + template_h // 2
        matches[name] = ((center_x, center_y), roi_bgr, float(max_val))

    return matches


def is_probably_disabled_gray_button(
    roi_bgr: np.ndarray,
    *,
    low_spread_threshold: int = 18,
    grayish_ratio_threshold: float = 0.75,
) -> bool:
    if roi_bgr is None or roi_bgr.size == 0:
        return False

    sample = roi_bgr[::4, ::4]
    if sample.size == 0:
        sample = roi_bgr

    b = sample[..., 0].astype(np.int16, copy=False)
    g = sample[..., 1].astype(np.int16, copy=False)
    r = sample[..., 2].astype(np.int16, copy=False)

    maxc = np.maximum(np.maximum(b, g), r)
    minc = np.minimum(np.minimum(b, g), r)
    spread = maxc - minc

    grayish_ratio = float(np.mean(spread < low_spread_threshold))
    return grayish_ratio >= grayish_ratio_threshold


def click_screen(point: Tuple[int, int]) -> None:
    x, y = _normalize_point(point)
    _assert_point_in_virtual_screen(x, y)

    prev_pos = _get_cursor_pos()
    try:
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                mouse.click(button="left", coords=(x, y))
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(0.05 * attempt)
                    continue

        if _sendinput_click(x, y):
            logger.debug("SendInput fallback 클릭 성공: (%d, %d)", x, y)
            return

        raise RuntimeError(
            "마우스 클릭 실패(재시도/SendInput fallback 포함). "
            "윈도우 잠금/보안 데스크톱(UAC)/원격 데스크톱 비활성/입력 차단 상태일 수 있습니다."
        ) from last_exc
    finally:
        if prev_pos is not None and prev_pos != (x, y):
            _restore_cursor_pos(prev_pos)


def click_relative(
    window_rect: Tuple[int, int, int, int], relative: Tuple[int, int]
) -> None:
    left, top, _, _ = window_rect
    abs_point = (left + int(relative[0]), top + int(relative[1]))
    click_screen(abs_point)


def search_and_act(
    window_rect: Tuple[int, int, int, int],
    template_path: Path,
    threshold: float = 0.85,
    click: bool = False,
    keys: Optional[str] = None,
    post_input_sleep: float = 0.0,
) -> bool:
    center = find_template_center(window_rect, template_path, threshold)
    if not center:
        return False
    if click:
        try:
            click_screen(center)
        except Exception as exc:
            logger.warning("클릭 실패: %s @ %s (%s)", template_path.name, center, exc)
            return False
        logger.info("클릭 완료: %s @ %s", template_path.name, center)
    if keys:
        if isinstance(keys, (list, tuple)):
            keys = " ".join(map(str, keys))
        else:
            keys = str(keys)
        try:
            pyperclip.copy(keys)

            copied = False
            for _ in range(5):
                if pyperclip.paste() == keys:
                    copied = True
                    break
                time.sleep(0.1)
            if not copied:
                logger.warning("클립보드 반영이 지연되어 직접 타이핑으로 대체합니다.")
                raise RuntimeError("clipboard not set")

            logger.debug("클립보드 내용 확인: %s", pyperclip.paste())
            keyboard.send_keys("^a{BACKSPACE}")
            keyboard.send_keys("^v")
            if post_input_sleep > 0:
                time.sleep(post_input_sleep)
            logger.info("키 입력(클립보드 붙여넣기) 완료: %s", keys)
        except Exception as exc:
            logger.warning("클립보드 붙여넣기 실패, 직접 타이핑 시도: %s", exc)
            try:
                keyboard.send_keys("^a{BACKSPACE}")
                keyboard.send_keys(
                    _escape_pywinauto_literal_text(keys),
                    with_spaces=True,
                    with_tabs=True,
                    with_newlines=True,
                )
                if post_input_sleep > 0:
                    time.sleep(post_input_sleep)
                logger.info("키 입력(직접 타이핑) 완료: %s", keys)
            except Exception as exc2:
                logger.error("키 입력 실패(직접 타이핑도 실패): %s", exc2)
                return False
    return True


def find_and_click(
    window_rect: Tuple[int, int, int, int],
    template_path: Path,
    threshold: float = 0.85,
) -> bool:
    center = find_template_center(window_rect, template_path, threshold)
    if not center:
        return False
    try:
        click_screen(center)
    except Exception as exc:
        logger.warning("버튼 클릭 실패: %s @ %s (%s)", template_path.name, center, exc)
        return False
    logger.info("버튼 클릭 완료: %s @ %s", template_path.name, center)
    return True
