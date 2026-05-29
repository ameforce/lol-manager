from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from lolmanager.core.champion_config import ChampionConfig
from lolmanager.core.champion_fetcher import fetch_champion_slug
from lolmanager.core.opgg_champion_list import (
    default_cache_path as default_champion_cache_path,
    get_champion_dataset,
)
from lolmanager.core.opgg_counter_recommendations import (
    DEFAULT_MAX_AGE_SEC,
    RecommendationCacheResult,
    default_counter_cache_path,
    get_counter_recommendations,
    is_auto_ban_value,
    normalize_name,
)


ROLE_ORDER: tuple[str, ...] = ("top", "jungle", "mid", "adc", "support")
AUTO_BAN_REFRESH_SUCCESS_MS = DEFAULT_MAX_AGE_SEC * 1000
AUTO_BAN_REFRESH_RETRY_MS = 30 * 1000


@dataclass(frozen=True)
class AutoBanRefreshTarget:
    role: str
    champion_name: str


class _WarningLogger(Protocol):
    def warning(self, msg: object, *args: object) -> None:
        ...


def collect_auto_ban_targets(config_path: Path) -> list[AutoBanRefreshTarget]:
    try:
        cfg = ChampionConfig(path=config_path)
    except Exception:
        return []

    targets: list[AutoBanRefreshTarget] = []
    seen: set[tuple[str, str]] = set()

    def add(role: str, champion_name: object, ban_value: object) -> None:
        champ = str(champion_name or "").strip()
        if not champ or not is_auto_ban_value(ban_value):
            return
        key = (role, normalize_name(champ))
        if key in seen:
            return
        seen.add(key)
        targets.append(AutoBanRefreshTarget(role=role, champion_name=champ))

    for role in ROLE_ORDER:
        info = cfg.get(role)
        info = info if isinstance(info, dict) else {}

        champ_raw = info.get("champion")
        if isinstance(champ_raw, (list, tuple)):
            primary_champ = champ_raw[0] if champ_raw else ""
        else:
            primary_champ = champ_raw
        add(role, primary_champ, info.get("ban"))

        for reserve_champ, reserve_ban in cfg.get_reserve_picks(role):
            add(role, reserve_champ, reserve_ban)

    return targets


def _entry_parts(entry: object) -> tuple[str, str, str]:
    if isinstance(entry, dict):
        return (
            str(entry.get("name") or entry.get("champion") or "").strip(),
            str(entry.get("tier") or "unknown").strip() or "unknown",
            str(entry.get("href") or "").strip(),
    )
    if isinstance(entry, (list, tuple)):
        name = str(entry[0] or "").strip() if len(entry) >= 1 else ""
        tier_obj = entry[1] if len(entry) >= 2 else "unknown"
        if isinstance(tier_obj, (list, tuple)):
            tier_obj = tier_obj[0] if tier_obj else "unknown"
        tier = str(tier_obj or "unknown").strip()
        href = str(entry[2] or "").strip() if len(entry) >= 3 else ""
        return (name, tier or "unknown", href)
    return ("", "unknown", "")


def _ranked_entries_for_role(
    by_position: object,
    role: str,
) -> list[tuple[str, str, str]]:
    if not isinstance(by_position, dict):
        return []
    entries = by_position.get(role)
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes)):
        return []

    ranked: list[tuple[str, str, str]] = []
    for entry in entries:
        name, tier, href = _entry_parts(entry)
        if name:
            ranked.append((name, tier, href))
    return ranked


def _href_for_target(
    ranked_entries: Iterable[tuple[str, str, str]],
    target: AutoBanRefreshTarget,
) -> Optional[str]:
    target_key = normalize_name(target.champion_name)
    for name, _tier, href in ranked_entries:
        if normalize_name(name) == target_key and href:
            return href
    return None


class AutoBanRefreshCoordinator:
    def __init__(
        self,
        *,
        config_path: Path,
        schedule_after_ms: Callable[[int, Callable[[], None]], object],
        run_background: Callable[[Callable[[], None]], object],
        on_updated: Callable[[], None],
        get_champion_dataset: Callable[..., tuple[object, object, str]] = get_champion_dataset,
        get_counter_recommendations: Callable[..., RecommendationCacheResult] = get_counter_recommendations,
        fetch_champion_slug: Optional[Callable[[str, str], Optional[str]]] = fetch_champion_slug,
        logger: Optional[_WarningLogger] = None,
        champion_dataset_cache_path: Optional[Path] = None,
        counter_cache_path: Optional[Path] = None,
    ) -> None:
        self.config_path = Path(config_path)
        self._schedule_after_ms = schedule_after_ms
        self._run_background = run_background
        self._on_updated = on_updated
        self._get_champion_dataset = get_champion_dataset
        self._get_counter_recommendations = get_counter_recommendations
        self._fetch_champion_slug = fetch_champion_slug
        self._logger = logger or logging.getLogger(__name__)
        self._champion_dataset_cache_path = (
            Path(champion_dataset_cache_path)
            if champion_dataset_cache_path is not None
            else default_champion_cache_path(self.config_path)
        )
        self._counter_cache_path = (
            Path(counter_cache_path)
            if counter_cache_path is not None
            else default_counter_cache_path(self.config_path)
        )
        self._closed = False
        self._generation = 0
        self._in_flight: set[tuple[str, str]] = set()

    def start(self) -> None:
        if self._closed:
            return
        self._generation += 1
        generation = self._generation
        for target in collect_auto_ban_targets(self.config_path):
            self._schedule_target(target, 0, generation)

    def refresh_configured_targets(self) -> None:
        self.start()

    def close(self) -> None:
        self._closed = True
        self._generation += 1

    def _target_key(self, target: AutoBanRefreshTarget) -> tuple[str, str]:
        return (target.role, normalize_name(target.champion_name))

    def _schedule_target(
        self,
        target: AutoBanRefreshTarget,
        delay_ms: int,
        generation: int,
    ) -> None:
        if self._closed:
            return

        def callback() -> None:
            if self._closed or generation != self._generation:
                return
            self._begin_refresh(target, generation)

        self._schedule_after_ms(int(delay_ms), callback)

    def _begin_refresh(
        self,
        target: AutoBanRefreshTarget,
        generation: int,
    ) -> None:
        key = self._target_key(target)
        if key in self._in_flight:
            return
        self._in_flight.add(key)

        def worker() -> None:
            success = False
            try:
                success = self._refresh_target(target)
            except Exception as exc:
                self._logger.warning(
                    "auto ban refresh failed for %s/%s: %s",
                    target.role,
                    target.champion_name,
                    exc,
                )
            finally:
                self._in_flight.discard(key)

            if self._closed or generation != self._generation:
                return

            if success:
                try:
                    self._on_updated()
                except Exception as exc:
                    self._logger.warning("auto ban refresh update hook failed: %s", exc)
                self._schedule_target(target, AUTO_BAN_REFRESH_SUCCESS_MS, generation)
                return

            self._logger.warning(
                "auto ban refresh will retry in 30 seconds for %s/%s",
                target.role,
                target.champion_name,
            )
            self._schedule_target(target, AUTO_BAN_REFRESH_RETRY_MS, generation)

        try:
            self._run_background(worker)
        except Exception as exc:
            self._in_flight.discard(key)
            self._logger.warning(
                "auto ban refresh worker failed for %s/%s: %s",
                target.role,
                target.champion_name,
                exc,
            )
            if not self._closed and generation == self._generation:
                self._schedule_target(target, AUTO_BAN_REFRESH_RETRY_MS, generation)

    def _refresh_target(self, target: AutoBanRefreshTarget) -> bool:
        _names, by_position, _source = self._get_champion_dataset(
            self._champion_dataset_cache_path,
            max_age_sec=0,
            timeout_sec=10.0,
        )
        ranked_entries = _ranked_entries_for_role(by_position, target.role)
        detail_href = _href_for_target(ranked_entries, target)
        if not detail_href and self._fetch_champion_slug is not None:
            detail_href = self._fetch_champion_slug(target.role, target.champion_name)
        if not detail_href:
            return False

        result = self._get_counter_recommendations(
            self._counter_cache_path,
            role=target.role,
            configured_pick=target.champion_name,
            ranked_entries=ranked_entries,
            detail_href=detail_href,
            max_age_sec=0,
        )
        return result.status == "opgg" and bool(result.recommendations)
