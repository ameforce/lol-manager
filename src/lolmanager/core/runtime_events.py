from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TextIO

from lolmanager.core.client_state import ClientState


EVENT_PREFIX = "@LOLManagerEvent:"
EVENT_VERSION = 1
_EVENT_LOGGER = logging.getLogger("lolmanager.runtime_events")
_EVENT_LOGGER.propagate = False
_EVENT_LOGGER.addHandler(logging.NullHandler())


@dataclass(frozen=True)
class RuntimeEvent:
    kind: str
    value: str | bool

    def to_line(self) -> str:
        return EVENT_PREFIX + json.dumps(
            {"v": EVENT_VERSION, "kind": self.kind, "value": self.value},
            ensure_ascii=True,
            separators=(",", ":"),
        )


def parse_runtime_event(line: str) -> RuntimeEvent | None:
    if not line.startswith(EVENT_PREFIX):
        return None
    try:
        payload = json.loads(line[len(EVENT_PREFIX):])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != EVENT_VERSION:
        return None
    kind, value = payload.get("kind"), payload.get("value")
    if not isinstance(kind, str):
        return None
    if kind == "pick_turn":
        return RuntimeEvent(kind, value) if isinstance(value, bool) else None
    if not isinstance(value, str) or not value.strip():
        return None
    if kind == "client_state" and value in ClientState.__members__:
        return RuntimeEvent(kind, value)
    if kind == "role" and value in {"top", "jungle", "mid", "adc", "support"}:
        return RuntimeEvent(kind, value)
    if kind in {"pick_target", "pick_blocked"}:
        return RuntimeEvent(kind, value)
    return None


def configure_event_stream(stream: TextIO | None) -> None:
    """Enable the GUI protocol only for a CLI child started by the GUI."""
    for handler in list(_EVENT_LOGGER.handlers):
        _EVENT_LOGGER.removeHandler(handler)
        handler.close()
    _EVENT_LOGGER.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream) if stream is not None else logging.NullHandler()
    _EVENT_LOGGER.addHandler(handler)


def emit_runtime_event(kind: str, value: str | bool) -> None:
    _EVENT_LOGGER.info(RuntimeEvent(kind, value).to_line())
