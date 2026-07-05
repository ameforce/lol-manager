from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from bs4.element import Tag


@dataclass(frozen=True, slots=True)
class TierInfo:
    label: str
    color: str


TIER_COLORS: Final[dict[str, TierInfo]] = {
    "currentcolor": TierInfo("OP", "red"),
    "#0093ff": TierInfo("1티어", "blue"),
    "#00bba3": TierInfo("2티어", "green"),
    "#ffb900": TierInfo("3티어", "yellow"),
    "#9aa4af": TierInfo("4티어", "gray"),
    "#a88a67": TierInfo("5티어", "brown"),
}
_BADGE_GLYPH_FILLS: Final[frozenset[str]] = frozenset(
    {"#fff", "#ffffff", "white", "rgb(255,255,255)"}
)


def tier_from_fill(fill: str | None) -> tuple[str, str]:
    key = str(fill or "").strip().lower()
    tier = TIER_COLORS.get(key)
    if tier is None:
        return ("unknown", "none")
    return (tier.label, tier.color)


def tier_from_badge_fills(
    fills: Iterable[str | None], *, require_badge_glyph: bool
) -> tuple[str, str]:
    normalized = [str(fill or "").strip().lower() for fill in fills]
    normalized = [fill for fill in normalized if fill]
    if require_badge_glyph and not any(
        fill in _BADGE_GLYPH_FILLS for fill in normalized
    ):
        return ("unknown", "none")
    for fill in normalized:
        tier = tier_from_fill(fill)
        if tier[0] != "unknown":
            return tier
    return ("unknown", "none")


def tier_fill_from_entry_container(container: Tag) -> str | None:
    if str(container.name or "").lower() == "tr":
        cells = [
            cell
            for cell in container.find_all(("td", "th"), recursive=False)
            if isinstance(cell, Tag)
        ]
        if len(cells) < 3:
            return None
        return _tier_fill_from_scope(cells[2], require_badge_glyph=True)
    return _tier_fill_from_scope(container, require_badge_glyph=False)


def _tier_fill_from_scope(scope: Tag, *, require_badge_glyph: bool) -> str | None:
    first_fill: str | None = None
    for svg in scope.select("svg"):
        fills = [
            str(path.get("fill") or "").strip()
            for path in svg.select("path[fill]")
            if str(path.get("fill") or "").strip()
        ]
        if first_fill is None and fills:
            first_fill = fills[0]
        tier_label, _color = tier_from_badge_fills(
            fills, require_badge_glyph=require_badge_glyph
        )
        if tier_label != "unknown":
            return next(
                fill for fill in fills if tier_from_fill(fill)[0] != "unknown"
            )
    return first_fill
