from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def tk_interpreter():
    # ttkbootstrap owns a process-wide Style singleton tied to one interpreter.
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass
