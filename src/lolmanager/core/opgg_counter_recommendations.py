from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

from lolmanager.core.champion_names import normalize_name


CACHE_VERSION = 3
DEFAULT_MAX_AGE_SEC = 12 * 60 * 60
DEFAULT_DISPLAY_SEPARATOR_PREFIX = "────────"
AUTO_BAN_VALUE = "__auto__"
AUTO_BAN_LABEL = "자동 추천 (최고 score)"
_AUTO_BAN_ALIASES = {
    AUTO_BAN_VALUE.casefold(),
    AUTO_BAN_LABEL.casefold(),
    "auto",
    "자동",
    "자동 추천",
}


@dataclass(frozen=True)
class CounterRecommendation:
    role: str
    configured_pick: str
    champion: str
    tier: str
    matchup_winrate: Optional[float]
    pick_winrate: Optional[float]
    tier_score_value: float
    matchup_score: float
    total_score: float
    source_order: int
    games: Optional[int] = None
    href: Optional[str] = None
    source_url: Optional[str] = None
    raw_text: str = ""


@dataclass(frozen=True)
class RecommendationCacheResult:
    status: str
    recommendations: Tuple[CounterRecommendation, ...]
    fetched_at_unix: Optional[float] = None


def tier_score(tier: object) -> float:
    text = str(tier or "").strip().casefold()
    if text == "op":
        return 50
    if text.startswith("1"):
        return 50
    if text.startswith("2"):
        return 40
    if text.startswith("3"):
        return 30
    if text.startswith("4"):
        return 20
    if text.startswith("5"):
        return 10
    return 0


def candidate_winrate_from_pick_winrate(pick_winrate: float) -> float:
    return round(100.0 - float(pick_winrate), 2)


def matchup_winrate_score(matchup_winrate: Optional[float]) -> float:
    if matchup_winrate is None:
        return 0
    raw = (50.0 - float(matchup_winrate)) * 5.0
    return round(max(0.0, min(50.0, raw)), 1)


def is_auto_ban_value(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = text.casefold()
    return normalized in _AUTO_BAN_ALIASES or normalized.startswith("자동 추천 (")


def display_value_to_champion_name(
    value: object,
    *,
    label_to_name: Optional[dict[str, str]] = None,
    separator_prefix: str = DEFAULT_DISPLAY_SEPARATOR_PREFIX,
) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if is_auto_ban_value(text):
        return AUTO_BAN_VALUE
    if label_to_name and text in label_to_name:
        return str(label_to_name[text] or "").strip()
    if separator_prefix and text.startswith(separator_prefix):
        return ""

    dot = text.find(". ")
    if dot > 0 and text[:dot].strip().isdigit():
        return text[dot + 2 :].strip()

    match = re.match(r"^(?P<name>.+?)\s+\([^()]*\bscore\s+[-+]?\d+(?:\.\d+)?\)$", text)
    if match:
        return match.group("name").strip()
    return text


def _entry_name(entry: object) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("champion") or "").strip()
    if isinstance(entry, (list, tuple)) and entry:
        return str(entry[0] or "").strip()
    return str(entry or "").strip()


def _entry_tier(entry: object) -> str:
    if isinstance(entry, dict):
        return str(entry.get("tier") or "").strip() or "unknown"
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        tier = entry[1]
        if isinstance(tier, (list, tuple)) and tier:
            return str(tier[0] or "").strip() or "unknown"
        return str(tier or "").strip() or "unknown"
    return "unknown"


def _entry_href(entry: object) -> Optional[str]:
    if isinstance(entry, dict):
        href = str(entry.get("href") or "").strip()
        return href or None
    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
        href = str(entry[2] or "").strip()
        return href or None
    return None


def _ranked_entry_map(
    ranked_entries: Iterable[object],
) -> dict[str, tuple[str, Optional[str]]]:
    out: dict[str, tuple[str, Optional[str]]] = {}
    for entry in ranked_entries:
        name = _entry_name(entry)
        if not name:
            continue
        out.setdefault(normalize_name(name), (_entry_tier(entry), _entry_href(entry)))
    return out


def build_recommendations(
    *,
    role: str,
    configured_pick: str,
    matchups: Iterable[object],
    ranked_entries: Iterable[object],
    source_url: Optional[str] = None,
) -> list[CounterRecommendation]:
    ranked_by_name = _ranked_entry_map(ranked_entries)
    recommendations: list[CounterRecommendation] = []
    seen: set[str] = set()

    for fallback_order, matchup in enumerate(matchups):
        champion = str(getattr(matchup, "champion", "") or "").strip()
        if not champion:
            continue
        key = normalize_name(champion)
        if key in seen:
            continue
        seen.add(key)

        tier, ranked_href = ranked_by_name.get(key, ("unknown", None))
        pick_winrate = getattr(matchup, "pick_winrate", None)
        if pick_winrate is None:
            matchup_winrate = None
        else:
            matchup_winrate = round(float(pick_winrate), 2)
        tier_points = tier_score(tier)
        matchup_points = matchup_winrate_score(matchup_winrate)
        total_points = round(tier_points + matchup_points, 1)

        recommendations.append(
            CounterRecommendation(
                role=str(role or "").strip(),
                configured_pick=str(configured_pick or "").strip(),
                champion=champion,
                tier=tier,
                matchup_winrate=matchup_winrate,
                pick_winrate=float(pick_winrate) if pick_winrate is not None else None,
                tier_score_value=tier_points,
                matchup_score=matchup_points,
                total_score=total_points,
                source_order=int(
                    getattr(matchup, "source_order", fallback_order) or fallback_order
                ),
                games=getattr(matchup, "games", None),
                href=getattr(matchup, "href", None) or ranked_href,
                source_url=source_url or getattr(matchup, "source_url", None),
                raw_text=str(getattr(matchup, "raw_text", "") or ""),
            )
        )

    return sorted(
        recommendations,
        key=lambda r: (
            -r.total_score,
            -r.tier_score_value,
            r.matchup_winrate if r.matchup_winrate is not None else 101.0,
            r.source_order,
            r.champion,
        ),
    )


def format_recommendation_label(recommendation: CounterRecommendation) -> str:
    tier = recommendation.tier or "unknown"
    if recommendation.matchup_winrate is None:
        winrate = "-"
    else:
        winrate = f"{recommendation.matchup_winrate:.1f}%"
    return (
        f"{recommendation.champion} "
        f"({tier}, {winrate}, score {recommendation.total_score:.1f})"
    )


def build_auto_ban_label(labels: Iterable[str]) -> str:
    top_label = next(
        (str(label or "").strip() for label in labels if str(label or "").strip()),
        "",
    )
    if not top_label:
        return AUTO_BAN_LABEL

    top_name = display_value_to_champion_name(top_label)
    if not top_name:
        return AUTO_BAN_LABEL

    prefix = f"{top_name} ("
    if top_label.startswith(prefix) and top_label.endswith(")"):
        detail = top_label[len(prefix) : -1].strip()
        if detail:
            return f"자동 추천 (현재 최고: {top_name}, {detail})"
    return f"자동 추천 (현재 최고: {top_name})"


def build_auto_ban_label_from_recommendations(
    recommendations: Iterable[CounterRecommendation],
) -> str:
    return build_auto_ban_label(
        format_recommendation_label(recommendation)
        for recommendation in recommendations
    )


def build_label_name_map(
    recommendations: Iterable[CounterRecommendation],
) -> tuple[list[str], dict[str, str]]:
    labels: list[str] = []
    label_to_name: dict[str, str] = {}
    for recommendation in recommendations:
        label = format_recommendation_label(recommendation)
        labels.append(label)
        label_to_name[label] = recommendation.champion
    return labels, label_to_name


def default_counter_cache_path(config_path: Path) -> Path:
    return config_path.with_name("opgg_counter_recommendation_cache.json")


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name, suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp_path, path)
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass


def _entry_key(role: str, configured_pick: str) -> str:
    return f"{str(role or '').strip()}::{normalize_name(configured_pick)}"


def _read_cache_payload(path: Path) -> Optional[dict[str, object]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("v") != CACHE_VERSION:
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    return payload


def _recommendation_to_dict(recommendation: CounterRecommendation) -> dict[str, object]:
    return asdict(recommendation)


def _recommendation_from_dict(obj: object) -> Optional[CounterRecommendation]:
    if not isinstance(obj, dict):
        return None
    champion = str(obj.get("champion") or "").strip()
    if not champion:
        return None
    try:
        return CounterRecommendation(
            role=str(obj.get("role") or "").strip(),
            configured_pick=str(obj.get("configured_pick") or "").strip(),
            champion=champion,
            tier=str(obj.get("tier") or "unknown").strip() or "unknown",
            matchup_winrate=float(obj["matchup_winrate"])
            if obj.get("matchup_winrate") is not None
            else None,
            pick_winrate=float(obj["pick_winrate"])
            if obj.get("pick_winrate") is not None
            else None,
            tier_score_value=float(obj.get("tier_score_value") or 0),
            matchup_score=float(obj.get("matchup_score") or 0),
            total_score=float(obj.get("total_score") or 0),
            source_order=int(obj.get("source_order") or 0),
            games=int(obj["games"]) if obj.get("games") is not None else None,
            href=str(obj.get("href") or "").strip() or None,
            source_url=str(obj.get("source_url") or "").strip() or None,
            raw_text=str(obj.get("raw_text") or ""),
        )
    except (TypeError, ValueError):
        return None


def save_recommendation_cache(
    path: Path,
    *,
    role: str,
    configured_pick: str,
    recommendations: Iterable[CounterRecommendation],
    fetched_at_unix: Optional[float] = None,
) -> None:
    fetched_at = time.time() if fetched_at_unix is None else float(fetched_at_unix)
    payload = _read_cache_payload(path) or {"v": CACHE_VERSION, "entries": []}
    entries_obj = payload.get("entries")
    entries = entries_obj if isinstance(entries_obj, list) else []
    key = _entry_key(role, configured_pick)

    next_entry = {
        "key": key,
        "role": str(role or "").strip(),
        "configured_pick": str(configured_pick or "").strip(),
        "fetched_at_unix": fetched_at,
        "recommendations": [
            _recommendation_to_dict(recommendation)
            for recommendation in recommendations
            if recommendation.champion
        ],
    }

    replaced = False
    next_entries: list[object] = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("key") == key:
            next_entries.append(next_entry)
            replaced = True
        else:
            next_entries.append(entry)
    if not replaced:
        next_entries.append(next_entry)

    payload = {"v": CACHE_VERSION, "entries": next_entries}
    _atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_recommendation_cache(
    path: Path,
    *,
    role: str,
    configured_pick: str,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: Optional[float] = None,
) -> RecommendationCacheResult:
    payload = _read_cache_payload(path)
    if payload is None:
        return RecommendationCacheResult("cache_miss", tuple(), None)

    key = _entry_key(role, configured_pick)
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return RecommendationCacheResult("cache_miss", tuple(), None)

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("key") != key:
            continue
        fetched_at = entry.get("fetched_at_unix")
        if not isinstance(fetched_at, (int, float)):
            return RecommendationCacheResult("cache_miss", tuple(), None)
        raw_recommendations = entry.get("recommendations")
        if not isinstance(raw_recommendations, list):
            return RecommendationCacheResult("cache_miss", tuple(), None)
        recommendations = tuple(
            recommendation
            for recommendation in (
                _recommendation_from_dict(obj) for obj in raw_recommendations
            )
            if recommendation is not None
        )
        status = (
            "cache_fresh"
            if ((time.time() if now is None else float(now)) - float(fetched_at))
            <= float(max_age_sec)
            else "cache_stale"
        )
        return RecommendationCacheResult(status, recommendations, float(fetched_at))

    return RecommendationCacheResult("cache_miss", tuple(), None)


def get_counter_recommendations(
    cache_path: Path,
    *,
    role: str,
    configured_pick: str,
    ranked_entries: Iterable[object],
    detail_href: Optional[str],
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: Optional[float] = None,
    limit: int = 10,
    fetch_matchups: Optional[Callable[[str, int], Iterable[object]]] = None,
) -> RecommendationCacheResult:
    cached = load_recommendation_cache(
        cache_path,
        role=role,
        configured_pick=configured_pick,
        max_age_sec=max_age_sec,
        now=now,
    )
    if cached.status == "cache_fresh":
        return cached

    if not detail_href:
        return (
            cached
            if cached.status == "cache_stale"
            else RecommendationCacheResult("cache_miss", tuple(), None)
        )

    try:
        if fetch_matchups is None:
            from lolmanager.core.champion_fetcher import (
                fetch_counter_matchups_from_detail,
            )

            fetch_matchups = fetch_counter_matchups_from_detail

        matchups = fetch_matchups(detail_href, limit)
        recommendations = build_recommendations(
            role=role,
            configured_pick=configured_pick,
            matchups=matchups,
            ranked_entries=ranked_entries,
            source_url=str(detail_href),
        )
        if not recommendations:
            return (
                cached
                if cached.status == "cache_stale"
                else RecommendationCacheResult("cache_miss", tuple(), None)
            )
        fetched_at = time.time() if now is None else float(now)
        save_recommendation_cache(
            cache_path,
            role=role,
            configured_pick=configured_pick,
            recommendations=recommendations,
            fetched_at_unix=fetched_at,
        )
        return RecommendationCacheResult("opgg", tuple(recommendations), fetched_at)
    except Exception:
        if cached.status == "cache_stale":
            return cached
        return RecommendationCacheResult("cache_miss", tuple(), None)
