from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional


CONTINUE_AFTER_GAME_KEY = "continue_after_game"


def load_continue_after_game_preference(path: Path) -> Optional[bool]:
    """Return the saved choice, or None until the user has made one."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(CONTINUE_AFTER_GAME_KEY)
    return value if isinstance(value, bool) else None


def save_continue_after_game_preference(path: Path, value: object) -> None:
    """Atomically persist the explicit GUI continuation choice."""
    target = Path(path)
    payload: dict[str, object] = {}
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(current, dict):
            payload.update(current)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    payload[CONTINUE_AFTER_GAME_KEY] = bool(value)

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.name,
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp_path, target)
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
