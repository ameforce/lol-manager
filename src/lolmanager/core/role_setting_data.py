from __future__ import annotations

from pathlib import Path
from typing import Optional

from lolmanager.core.champion_config import ChampionConfig
from lolmanager.core.opgg_counter_recommendations import AUTO_BAN_LABEL, is_auto_ban_value


def _norm_champ_name(name: object) -> str:
    s = str(name or "").strip()
    if not s:
        return ""
    return " ".join(s.split()).casefold()


def _display_ban_value(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return AUTO_BAN_LABEL if is_auto_ban_value(raw) else raw


def load_role_setting_data(config_path: Path, role_key: str) -> dict[str, object]:
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

    ban = _display_ban_value(info.get("ban"))
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
        reserve_pairs.append((reserve_champ, _display_ban_value(b)))

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
