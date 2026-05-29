from __future__ import annotations

from pathlib import Path
from typing import Optional

from lolmanager.core.champion_config import ChampionConfig
from lolmanager.core.opgg_counter_recommendations import (
    AUTO_BAN_LABEL,
    DEFAULT_MAX_AGE_SEC,
    build_auto_ban_label_from_recommendations,
    default_counter_cache_path,
    is_auto_ban_value,
    load_recommendation_cache,
)


def _norm_champ_name(name: object) -> str:
    s = str(name or "").strip()
    if not s:
        return ""
    return " ".join(s.split()).casefold()


def _display_ban_value(
    value: object,
    *,
    role_key: str,
    champion_name: str,
    counter_cache_path: Path,
    max_age_sec: float,
    now: Optional[float],
) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not is_auto_ban_value(raw):
        return raw

    champ = str(champion_name or "").strip()
    if champ:
        try:
            result = load_recommendation_cache(
                counter_cache_path,
                role=role_key,
                configured_pick=champ,
                max_age_sec=max_age_sec,
                now=now,
            )
        except Exception:
            result = None
        if result is not None and result.recommendations:
            return build_auto_ban_label_from_recommendations(result.recommendations)

    return AUTO_BAN_LABEL


def load_role_setting_data(
    config_path: Path,
    role_key: str,
    *,
    counter_cache_path: Optional[Path] = None,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: Optional[float] = None,
) -> dict[str, object]:
    try:
        cfg = ChampionConfig(path=config_path)
    except Exception:
        return {"error": "설정: 로드 실패"}

    info = cfg.get(role_key) if role_key else None
    info = info if isinstance(info, dict) else {}

    champ_raw = info.get("champion")
    if isinstance(champ_raw, (list, tuple)):
        primary_champ = str(champ_raw[0]).strip() if champ_raw else ""
    else:
        primary_champ = str(champ_raw or "").strip()

    resolved_counter_cache_path = (
        counter_cache_path
        if counter_cache_path is not None
        else default_counter_cache_path(config_path)
    )

    ban = _display_ban_value(
        info.get("ban"),
        role_key=role_key,
        champion_name=primary_champ,
        counter_cache_path=resolved_counter_cache_path,
        max_age_sec=max_age_sec,
        now=now,
    )
    reserves = cfg.get_reserve_picks(role_key) if role_key else []
    reserve_pairs: list[tuple[str, str]] = []

    seen_norms: set[str] = set()
    primary_norm = _norm_champ_name(primary_champ)
    if primary_norm:
        seen_norms.add(primary_norm)

    for c, b in reserves:
        reserve_champ = str(c or "").strip()
        if not reserve_champ:
            continue
        reserve_norm = _norm_champ_name(reserve_champ)
        if reserve_norm and reserve_norm in seen_norms:
            continue
        if reserve_norm:
            seen_norms.add(reserve_norm)
        reserve_pairs.append(
            (
                reserve_champ,
                _display_ban_value(
                    b,
                    role_key=role_key,
                    champion_name=reserve_champ,
                    counter_cache_path=resolved_counter_cache_path,
                    max_age_sec=max_age_sec,
                    now=now,
                ),
            )
        )

    coord_val = info.get("pick_coord")
    coord: Optional[tuple[int, int]] = None
    if isinstance(coord_val, (list, tuple)) and len(coord_val) >= 2:
        try:
            coord = (int(coord_val[0]), int(coord_val[1]))
        except Exception:
            coord = None

    return {
        "primary": primary_champ,
        "ban": ban,
        "reserves": reserve_pairs,
        "coord": coord,
    }
