from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

from lolmanager.core.champion_names import normalize_name


DISPLAY_SEPARATOR_PREFIX: Final = "────────"
_TIER_DISPLAY_ORDER: Final[dict[str, int]] = {
    "OP": 0,
    "1티어": 1,
    "2티어": 2,
    "3티어": 3,
    "4티어": 4,
    "5티어": 5,
}


def build_champion_candidate_values(
    *,
    ranked_entries: Iterable[tuple[str, str, str]],
    all_champion_values: Sequence[str],
) -> list[str]:
    ranked_by_tier: dict[str, list[str]] = {}
    tier_source_order: dict[str, int] = {}
    ranked_keys: set[str] = set()

    for source_order, entry in enumerate(ranked_entries):
        name, tier_label, _href = entry
        champion_name = str(name or "").strip()
        if not champion_name:
            continue
        key = normalize_name(champion_name)
        if key in ranked_keys:
            continue
        ranked_keys.add(key)

        tier = str(tier_label or "").strip() or "unknown"
        tier_source_order.setdefault(tier, source_order)
        ranked_by_tier.setdefault(tier, []).append(champion_name)

    if not ranked_by_tier:
        return [str(value or "").strip() for value in all_champion_values if str(value or "").strip()]

    values: list[str] = []
    rank = 1
    for tier in sorted(
        ranked_by_tier,
        key=lambda label: (
            _TIER_DISPLAY_ORDER.get(label, len(_TIER_DISPLAY_ORDER)),
            tier_source_order[label],
        ),
    ):
        values.append(f"{DISPLAY_SEPARATOR_PREFIX} {tier} {DISPLAY_SEPARATOR_PREFIX}")
        for champion_name in ranked_by_tier[tier]:
            values.append(f"{rank:>3}. {champion_name}")
            rank += 1

    tail = [
        str(value or "").strip()
        for value in all_champion_values
        if str(value or "").strip()
        and normalize_name(str(value or "").strip()) not in ranked_keys
    ]
    if tail:
        values.append(f"{DISPLAY_SEPARATOR_PREFIX} 기타 {DISPLAY_SEPARATOR_PREFIX}")
        values.extend(tail)
    return values
