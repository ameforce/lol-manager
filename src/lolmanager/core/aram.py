from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from lolmanager.core.champion_names import normalize_name


@dataclass(frozen=True)
class AramPreferences:
    enabled: bool = False
    preferred_champions: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, value: object) -> AramPreferences:
        if not isinstance(value, Mapping):
            return cls()
        raw = value.get("preferred_champions")
        names: list[str] = []
        seen: set[str] = set()
        for item in raw if isinstance(raw, (list, tuple)) else ():
            if not isinstance(item, str):
                continue
            name = " ".join(item.split())
            key = normalize_name(name)
            if key and key not in seen:
                seen.add(key)
                names.append(name)
        return cls(value.get("enabled") is True, tuple(names))

    def as_config(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "preferred_champions": list(self.preferred_champions),
        }


def choose_aram_bench_champion(
    preferred_ids: Sequence[int],
    current_id: int,
    available_bench_ids: Iterable[int],
) -> int | None:
    """Only trade up; holding a preferred champion beats every lower priority."""
    available = set(available_bench_ids)
    for champion_id in preferred_ids:
        if champion_id == current_id:
            return None
        if champion_id in available:
            return champion_id
    return None


def choose_aram_initial_champion(
    preferred_ids: Sequence[int],
    offered_ids: Iterable[int],
) -> int | None:
    """Choose the highest-priority offered card, or the first offer as fallback."""
    offered = tuple(
        dict.fromkeys(champion_id for champion_id in offered_ids if champion_id > 0)
    )
    if not offered:
        return None
    offered_set = set(offered)
    for champion_id in preferred_ids:
        if champion_id in offered_set:
            return champion_id
    return offered[0]
