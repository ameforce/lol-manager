from __future__ import annotations

import sys
from typing import TextIO


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def restore_inherited_output_streams() -> None:
    """Expose the GUI's inherited pipes in a PyInstaller windowed CLI child.

    Windowed Python sets sys.stdout/stderr to None even when its parent supplied
    pipes. Duplicate the inherited handles so closing Python streams cannot
    close handles owned by the bootloader. A standalone GUI has no such pipes.
    """
    if sys.platform != "win32" or not is_frozen():
        return
    import ctypes
    import msvcrt
    import os
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    opened: dict[int, TextIO] = {}
    for name, identifier in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is not None:
            continue
        handle = kernel.GetStdHandle(identifier & 0xFFFFFFFF)
        if not handle or kernel.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
            continue
        if handle in opened:
            setattr(sys, name, opened[handle])
            continue
        process = kernel.GetCurrentProcess()
        duplicate = wintypes.HANDLE()
        if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
            continue
        try:
            fd = msvcrt.open_osfhandle(duplicate.value, os.O_WRONLY | os.O_BINARY)
        except OSError:
            kernel.CloseHandle(duplicate)
            raise
        stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        opened[handle] = stream
        setattr(sys, name, stream)
