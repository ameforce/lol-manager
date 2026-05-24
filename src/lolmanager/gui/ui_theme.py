from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional, Tuple


@dataclass(frozen=True)
class AppliedTheme:
    engine: str
    theme: str
    is_dark: Optional[bool]


_TTK_NATIVE_THEMES = {"vista", "clam", "alt", "default", "classic"}


def _windows_apps_use_light_theme() -> Optional[bool]:
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _typ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(int(value))
    except Exception:
        return None


def _default_ttkbootstrap_theme() -> Tuple[str, Optional[bool]]:
    light = _windows_apps_use_light_theme()
    if light is None:
        return ("flatly", None)
    if light:
        return ("flatly", False)
    return ("darkly", True)


def _apply_windows_chrome(root, *, prefer_dark: Optional[bool]) -> None:
    if os.name != "nt":
        return

    def _kick() -> None:
        try:
            import ctypes
        except Exception:
            return

        hwnd = 0
        try:
            hwnd = int(root.winfo_id())
        except Exception:
            hwnd = 0
        if not hwnd:
            return

        try:
            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        except Exception:
            return

        def _set(attr: int, value: int) -> None:
            try:
                v = ctypes.c_int(int(value))
                dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(int(hwnd)),
                    ctypes.c_uint(int(attr)),
                    ctypes.byref(v),
                    ctypes.sizeof(v),
                )
            except Exception:
                return

        _set(33, 2)

        if prefer_dark is not None:
            _set(20, 1 if prefer_dark else 0)
            _set(19, 1 if prefer_dark else 0)

    try:
        root.after_idle(_kick)
    except Exception:
        try:
            _kick()
        except Exception:
            pass


def apply_modern_theme(root, *, prefer: Optional[str] = None) -> AppliedTheme:
    import tkinter as tk
    from tkinter import ttk

    requested = (prefer or os.environ.get("LOLMANAGER_TTK_THEME") or "auto").strip()
    requested_lc = requested.casefold()

    if requested_lc in _TTK_NATIVE_THEMES:
        style = ttk.Style()
        names = {n.casefold() for n in style.theme_names()}
        theme = (
            "vista"
            if "vista" in names
            else ("clam" if "clam" in names else style.theme_use())
        )
        try:
            style.theme_use(requested_lc if requested_lc in names else theme)
        except Exception:
            try:
                style.theme_use(theme)
            except Exception:
                pass
        _apply_windows_chrome(root, prefer_dark=None)
        return AppliedTheme(engine="ttk", theme=style.theme_use(), is_dark=None)

    chosen, auto_is_dark = _default_ttkbootstrap_theme()
    if requested_lc and requested_lc != "auto":
        chosen = requested

        if requested_lc in {"darkly", "superhero", "cyborg", "solar", "vapor"}:
            auto_is_dark = True
        elif requested_lc in {
            "flatly",
            "cosmo",
            "minty",
            "litera",
            "lumen",
            "yeti",
            "pulse",
        }:
            auto_is_dark = False

    try:
        import ttkbootstrap as ttkb
    except Exception:
        ttkb = None

    if ttkb is not None:
        try:
            ttkb.Style(theme=chosen)
            _apply_windows_chrome(root, prefer_dark=auto_is_dark)
            return AppliedTheme(
                engine="ttkbootstrap", theme=str(chosen), is_dark=auto_is_dark
            )
        except Exception:
            try:
                fallback, fb_dark = _default_ttkbootstrap_theme()
                ttkb.Style(theme=fallback)
                _apply_windows_chrome(root, prefer_dark=fb_dark)
                return AppliedTheme(
                    engine="ttkbootstrap", theme=str(fallback), is_dark=fb_dark
                )
            except Exception:
                pass

    style = ttk.Style()
    try:
        names = {n.casefold() for n in style.theme_names()}
        if "vista" in names:
            style.theme_use("vista")
        elif "clam" in names:
            style.theme_use("clam")
    except Exception:
        pass
    _apply_windows_chrome(root, prefer_dark=None)
    return AppliedTheme(engine="ttk", theme=style.theme_use(), is_dark=None)


def ttk_text_palette() -> Tuple[str, str]:
    from tkinter import ttk

    style = ttk.Style()
    bg = (
        style.lookup("TFrame", "background")
        or style.lookup(".", "background")
        or "#1f1f1f"
    )
    fg = (
        style.lookup("TLabel", "foreground")
        or style.lookup(".", "foreground")
        or "#e6e6e6"
    )
    bg = str(bg).strip() or "#1f1f1f"
    fg = str(fg).strip() or "#e6e6e6"
    return (bg, fg)


def _env_truthy(name: str) -> bool:
    v = os.environ.get(name)
    if v is None:
        return False
    s = str(v).strip().casefold()
    return s in {"1", "true", "yes", "y", "on"}


def apply_frameless_window(root, *, prefer_dark: Optional[bool] = None) -> bool:
    if os.name != "nt":
        try:
            root.overrideredirect(True)
            return True
        except Exception:
            return False

    try:
        import ctypes
    except Exception:
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_MAXIMIZEBOX = 0x00010000
    WS_MINIMIZEBOX = 0x00020000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    GA_ROOT = 2

    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOOWNERZORDER = 0x0200
    SWP_FRAMECHANGED = 0x0020

    try:
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_int
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
    except Exception:
        pass

    def _top_hwnd(hwnd: int) -> int:
        if not hwnd:
            return 0
        try:
            top = int(user32.GetAncestor(ctypes.c_void_p(hwnd), GA_ROOT) or 0)
        except Exception:
            top = 0
        return top or hwnd

    def _force_overrideredirect_fallback() -> None:
        try:
            root.overrideredirect(True)
        except Exception:
            return

        hwnd0 = 0
        try:
            hwnd0 = _top_hwnd(int(root.winfo_id()))
        except Exception:
            hwnd0 = 0
        if not hwnd0:
            return
        try:
            ctypes.set_last_error(0)
            ex = int(user32.GetWindowLongW(ctypes.c_void_p(hwnd0), GWL_EXSTYLE))
            err = int(ctypes.get_last_error() or 0)
            if ex == 0 and err != 0:
                return
            ex = (ex | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            user32.SetWindowLongW(ctypes.c_void_p(hwnd0), GWL_EXSTYLE, int(ex))
            user32.SetWindowPos(
                ctypes.c_void_p(hwnd0),
                ctypes.c_void_p(0),
                0,
                0,
                0,
                0,
                SWP_NOMOVE
                | SWP_NOSIZE
                | SWP_NOZORDER
                | SWP_NOOWNERZORDER
                | SWP_FRAMECHANGED,
            )
        except Exception:
            return

    def _try_apply_once() -> bool:
        hwnd = 0
        try:
            hwnd = _top_hwnd(int(root.winfo_id()))
        except Exception:
            hwnd = 0
        if not hwnd:
            return False

        try:
            ctypes.set_last_error(0)
            style = int(user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE))
            err = int(ctypes.get_last_error() or 0)
        except Exception:
            return False
        if style == 0 and err != 0:
            return False

        new_style = style
        new_style &= ~WS_CAPTION
        new_style &= ~WS_THICKFRAME
        new_style &= ~WS_MAXIMIZEBOX
        new_style &= ~WS_MINIMIZEBOX

        try:
            if new_style != style:
                user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE, int(new_style))
                user32.SetWindowPos(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_void_p(0),
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE
                    | SWP_NOSIZE
                    | SWP_NOZORDER
                    | SWP_NOOWNERZORDER
                    | SWP_FRAMECHANGED,
                )
        except Exception:
            return False

        try:
            ctypes.set_last_error(0)
            check = int(user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE))
            err2 = int(ctypes.get_last_error() or 0)
        except Exception:
            check = new_style
            err2 = 0
        if check == 0 and err2 != 0:
            return False
        if (check & WS_CAPTION) or (check & WS_THICKFRAME):
            return False

        _apply_windows_chrome(root, prefer_dark=prefer_dark)
        return True

    if _try_apply_once():
        return True

    def _retry(remaining: int) -> None:
        if _try_apply_once():
            return
        if remaining <= 0:
            _force_overrideredirect_fallback()
            return
        try:
            root.after(16, lambda: _retry(remaining - 1))
        except Exception:
            return

    try:
        root.after(0, lambda: _retry(12))
    except Exception:
        pass
    return True


def enable_window_drag(root, widget) -> None:
    state = {"sx": 0, "sy": 0, "wx": 0, "wy": 0, "down": False}

    def _on_down(e) -> None:
        try:
            state["sx"] = int(getattr(e, "x_root", 0))
            state["sy"] = int(getattr(e, "y_root", 0))
            state["wx"] = int(root.winfo_x())
            state["wy"] = int(root.winfo_y())
            state["down"] = True
        except Exception:
            state["down"] = False

    def _on_up(_e) -> None:
        state["down"] = False

    def _on_move(e) -> None:
        if not state.get("down"):
            return
        try:
            x = int(getattr(e, "x_root", 0))
            y = int(getattr(e, "y_root", 0))
            nx = int(state["wx"]) + (x - int(state["sx"]))
            ny = int(state["wy"]) + (y - int(state["sy"]))
            root.geometry(f"{nx:+d}{ny:+d}")
        except Exception:
            return

    try:
        widget.bind("<ButtonPress-1>", _on_down)
        widget.bind("<ButtonRelease-1>", _on_up)
        widget.bind("<B1-Motion>", _on_move)

        for child in getattr(widget, "winfo_children", lambda: [])():
            try:
                child.bind("<ButtonPress-1>", _on_down)
                child.bind("<ButtonRelease-1>", _on_up)
                child.bind("<B1-Motion>", _on_move)
            except Exception:
                continue
    except Exception:
        pass
