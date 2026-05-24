from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from lolmanager.core.opgg_counter_recommendations import display_value_to_champion_name
from lolmanager.platform.paths import champion_config_path, project_root, resource_path
from lolmanager.platform.runtime import is_frozen


DEFAULT_CONFIG_PATH = champion_config_path()
CONFIG_PATH = DEFAULT_CONFIG_PATH


_MIGRATION_ATTEMPTED = False


def _legacy_config_candidates() -> List[Path]:
    out: List[Path] = []
    out.append(Path.cwd() / "champion_config.json")

    try:
        exe_dir = Path(sys.executable).resolve().parent
        out.append(exe_dir / "champion_config.json")
        out.append(exe_dir / "_internal" / "champion_config.json")
    except Exception:
        pass

    if not is_frozen():
        try:
            proj_root = project_root()
            out.append(proj_root / "champion_config.json")
        except Exception:
            pass

    seen: set[str] = set()
    deduped: List[Path] = []
    for p in out:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def _maybe_migrate_legacy_config(dst: Path) -> None:
    global _MIGRATION_ATTEMPTED
    if _MIGRATION_ATTEMPTED:
        return
    _MIGRATION_ATTEMPTED = True

    if dst != DEFAULT_CONFIG_PATH:
        return
    if dst.exists():
        return

    for src in _legacy_config_candidates():
        if src == dst:
            continue
        try:
            if src.exists() and src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                return
        except Exception:
            continue

    try:
        default_src = resource_path("defaults", "champion_config.json")
        if default_src.exists() and default_src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(default_src, dst)
            return
    except Exception:
        return


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


def _normalize_reserve_picks(value: Any) -> List[Dict[str, str]]:
    if not value:
        return []
    if not isinstance(value, list):
        return []

    out: List[Dict[str, str]] = []
    seen: set[str] = set()

    for item in value:
        champ = ""
        ban = ""
        if isinstance(item, dict):
            champ = display_value_to_champion_name(item.get("champion"))
            ban = display_value_to_champion_name(item.get("ban"))
        elif isinstance(item, (list, tuple)):
            if len(item) >= 1:
                champ = display_value_to_champion_name(item[0])
            if len(item) >= 2:
                ban = display_value_to_champion_name(item[1])
        else:
            continue

        if not champ or champ in seen:
            continue
        seen.add(champ)
        out.append({"champion": champ, "ban": ban})

    return out


class ChampionConfig:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self.data: Dict[str, Dict] = {}
        self._load()

    def _load(self) -> None:
        _maybe_migrate_legacy_config(self.path)
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))

                changed = False
                for role, entry in list(self.data.items()):
                    if not isinstance(entry, dict):
                        continue
                    champ = entry.get("champion")
                    if isinstance(champ, (list, tuple)):
                        champ = champ[0] if champ else ""
                        entry["champion"] = champ
                        changed = True
                    normalized_champ = display_value_to_champion_name(champ)
                    if normalized_champ != str(champ or "").strip():
                        entry["champion"] = normalized_champ
                        changed = True
                    ban = entry.get("ban")
                    if isinstance(ban, (list, tuple)):
                        ban = ban[0] if ban else ""
                        entry["ban"] = ban
                        changed = True
                    normalized_ban = display_value_to_champion_name(ban)
                    if normalized_ban != str(ban or "").strip():
                        entry["ban"] = normalized_ban
                        changed = True

                    if "reserve_picks" in entry:
                        raw_reserves = entry.get("reserve_picks")
                        norm_reserves = _normalize_reserve_picks(raw_reserves)
                        if norm_reserves != raw_reserves:
                            entry["reserve_picks"] = norm_reserves
                            changed = True

                    self.data[role] = entry
                if changed:
                    self.save()
            except json.JSONDecodeError:
                self.data = {}
        else:
            self.data = {}

    def save(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        _atomic_write_text(self.path, payload, encoding="utf-8")

    def get(self, role: str) -> Optional[Dict]:
        return self.data.get(role)

    def set(
        self,
        role: str,
        champion: str,
        pick_coord: Optional[Tuple[int, int]] = None,
        ban_champion: Optional[str] = None,
    ) -> None:
        payload = self.data.get(role) if isinstance(self.data.get(role), dict) else {}
        payload["champion"] = champion
        if pick_coord is not None:
            payload["pick_coord"] = [int(pick_coord[0]), int(pick_coord[1])]
        if ban_champion:
            payload["ban"] = ban_champion
        self.data[role] = payload
        self.save()

    def get_reserve_picks(self, role: str) -> List[Tuple[str, str]]:
        entry = self.data.get(role)
        if not isinstance(entry, dict):
            return []
        reserves = _normalize_reserve_picks(entry.get("reserve_picks"))
        return [(r.get("champion", ""), r.get("ban", "")) for r in reserves]

    def set_reserve_picks(
        self, role: str, reserve_picks: List[Tuple[str, str]]
    ) -> None:
        entry = self.data.get(role) if isinstance(self.data.get(role), dict) else {}
        entry["reserve_picks"] = _normalize_reserve_picks(list(reserve_picks))
        self.data[role] = entry
        self.save()
