from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

OPGG_CHAMPIONS_URL_KO = "https://op.gg/ko/lol/champions"
OPGG_CHAMPIONS_BY_POSITION_URLS_KO: Tuple[str, ...] = (
    "https://op.gg/ko/lol/champions?position=top",
    "https://op.gg/ko/lol/champions?position=jungle",
    "https://op.gg/ko/lol/champions?position=mid",
    "https://op.gg/ko/lol/champions?position=adc",
    "https://op.gg/ko/lol/champions?position=support",
)


_CACHE_VERSION = 3


_MIN_EXPECTED_CHAMPION_COUNT = 120


def normalize_name(value: str) -> str:
    return "".join(str(value or "").split()).casefold()


@dataclass(frozen=True)
class ChampionListCache:
    champions: Tuple[str, ...]
    fetched_at_unix: float

    by_position: Dict[str, Tuple[Tuple[str, str, str], ...]] = field(
        default_factory=dict
    )


_TIER_COLORS: Dict[str, Tuple[str, str]] = {
    "currentcolor": ("OP", "red"),
    "#0093ff": ("1티어", "blue"),
    "#00bba3": ("2티어", "green"),
    "#ffb900": ("3티어", "yellow"),
    "#9aa4af": ("4티어", "gray"),
    "#a88a67": ("5티어", "brown"),
}


def _tier_from_fill(fill: Optional[str]) -> Tuple[str, str]:
    if not fill:
        return ("unknown", "none")
    key = str(fill).strip().lower()
    return _TIER_COLORS.get(key, ("unknown", "none"))


def default_cache_path(config_path: Path) -> Path:
    return config_path.with_name("opgg_champion_list_cache.json")


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


def _dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for it in items:
        s = str(it or "").strip()
        if not s:
            continue
        key = normalize_name(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def parse_position_entries_from_opgg_html(
    html_text: str, position: str
) -> List[Tuple[str, str, str]]:
    text = html_text or ""
    needle = 'href="/ko/lol/champions/'
    i = 0
    entries: List[Tuple[str, str, str]] = []

    max_anchor_scan = 4000
    href_prefix_len = len('href="')
    build_suffix = f"/build/{position}"

    while True:
        idx = text.find(needle, i)
        if idx == -1:
            break

        url_start = idx + href_prefix_len
        url_end = text.find('"', url_start)
        if url_end == -1:
            break
        url = text[url_start:url_end]

        if build_suffix not in url:
            i = idx + len(needle)
            continue

        anchor_end = text.find("</a>", url_end, url_end + max_anchor_scan)
        if anchor_end == -1:
            i = url_end
            continue

        anchor = text[url_end:anchor_end]
        strong_idx = anchor.find("<strong")
        if strong_idx != -1:
            gt = anchor.find(">", strong_idx)
            end = anchor.find("</strong>", gt + 1 if gt != -1 else 0)
            if gt != -1 and end != -1 and end > gt:
                raw = anchor[gt + 1 : end]
                name = unescape(raw).strip()
                if name:
                    tr_start = text.rfind("<tr", 0, idx)
                    tr_end = text.find("</tr>", idx)
                    fill_val: Optional[str] = None
                    if tr_start != -1 and tr_end != -1 and tr_end > tr_start:
                        row = text[tr_start:tr_end]
                        badge_pos = row.find("M2 0h20v18.056L12 23 2 18.056z")
                        if badge_pos != -1:
                            fpos = row.rfind('fill="', 0, badge_pos)
                            if fpos != -1:
                                vend = row.find('"', fpos + 6)
                                if vend != -1:
                                    fill_val = row[fpos + 6 : vend].strip()
                    tier_label, _color = _tier_from_fill(fill_val)
                    entries.append((name, tier_label, url))

        i = anchor_end + 4

    out: List[Tuple[str, str, str]] = []
    seen: set[str] = set()
    for name, tier_label, href in entries:
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        out.append((name, tier_label, href))
    return out


def fetch_opgg_champion_dataset(
    timeout_sec: float = 10.0,
) -> Tuple[List[str], Dict[str, List[Tuple[str, str, str]]]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    role_urls = {
        "top": OPGG_CHAMPIONS_BY_POSITION_URLS_KO[0],
        "jungle": OPGG_CHAMPIONS_BY_POSITION_URLS_KO[1],
        "mid": OPGG_CHAMPIONS_BY_POSITION_URLS_KO[2],
        "adc": OPGG_CHAMPIONS_BY_POSITION_URLS_KO[3],
        "support": OPGG_CHAMPIONS_BY_POSITION_URLS_KO[4],
    }
    by_position: Dict[str, List[Tuple[str, str, str]]] = {}
    all_names: List[str] = []
    for role, url in role_urls.items():
        resp = requests.get(url, headers=headers, timeout=timeout_sec)
        resp.raise_for_status()
        entries = parse_position_entries_from_opgg_html(resp.text, role)
        by_position[role] = entries
        all_names.extend([name for (name, _tier, _href) in entries])

    champions = _dedupe_preserve_order(all_names)
    if len(champions) < _MIN_EXPECTED_CHAMPION_COUNT:
        raise RuntimeError(
            f"op.gg 챔피언 파싱 결과가 너무 적습니다: {len(champions)}개"
        )
    return (champions, by_position)


def fetch_opgg_champion_names(timeout_sec: float = 10.0) -> List[str]:
    champions, _by_position = fetch_opgg_champion_dataset(timeout_sec=timeout_sec)
    return champions


def fetch_riot_ddragon_champion_names(timeout_sec: float = 10.0) -> List[str]:
    versions = requests.get(
        "https://ddragon.leagueoflegends.com/api/versions.json", timeout=timeout_sec
    )
    versions.raise_for_status()
    version_list = versions.json()
    if not isinstance(version_list, list) or not version_list:
        raise RuntimeError("Data Dragon versions 응답이 비정상입니다.")
    ver = str(version_list[0])

    champ_url = (
        f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/ko_KR/champion.json"
    )
    champs = requests.get(champ_url, timeout=timeout_sec)
    champs.raise_for_status()
    payload = champs.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Data Dragon champion.json 구조가 비정상입니다.")

    names = []
    for _k, v in data.items():
        if isinstance(v, dict):
            nm = str(v.get("name") or "").strip()
            if nm:
                names.append(nm)
    names = _dedupe_preserve_order(names)
    if len(names) < _MIN_EXPECTED_CHAMPION_COUNT:
        raise RuntimeError(
            f"Data Dragon 챔피언 파싱 결과가 너무 적습니다: {len(names)}개"
        )
    return names


def load_champion_list_cache(path: Path) -> Optional[ChampionListCache]:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("v") != _CACHE_VERSION:
        return None
    champs = obj.get("champions")
    fetched_at = obj.get("fetched_at_unix")
    by_position_raw = obj.get("by_position")
    if not isinstance(champs, list) or not isinstance(fetched_at, (int, float)):
        return None
    champions = tuple(_dedupe_preserve_order(champs))
    if len(champions) < _MIN_EXPECTED_CHAMPION_COUNT:
        return None
    by_position: Dict[str, Tuple[Tuple[str, str, str], ...]] = {}
    if isinstance(by_position_raw, dict):
        for role, items in by_position_raw.items():
            if not isinstance(role, str) or not isinstance(items, list):
                continue
            parsed: List[Tuple[str, str, str]] = []
            for it in items:
                if isinstance(it, dict):
                    name = str(it.get("name") or "").strip()
                    tier_label = str(it.get("tier") or "").strip() or "unknown"
                    href = str(it.get("href") or "").strip()
                elif isinstance(it, (list, tuple)) and len(it) >= 3:
                    name = str(it[0] or "").strip()
                    tier_label = str(it[1] or "").strip() or "unknown"
                    href = str(it[2] or "").strip()
                else:
                    continue
                if name and href:
                    parsed.append((name, tier_label, href))
            if parsed:
                by_position[role] = tuple(parsed)
    return ChampionListCache(
        champions=champions, fetched_at_unix=float(fetched_at), by_position=by_position
    )


def save_champion_list_cache(
    path: Path,
    champions: Iterable[str],
    fetched_at_unix: Optional[float] = None,
    *,
    by_position: Optional[Dict[str, Iterable[Tuple[str, str, str]]]] = None,
) -> None:
    fetched_at = time.time() if fetched_at_unix is None else float(fetched_at_unix)
    champs = _dedupe_preserve_order(champions)
    payload = {"v": _CACHE_VERSION, "fetched_at_unix": fetched_at, "champions": champs}
    if by_position:
        out_pos: Dict[str, List[Dict[str, str]]] = {}
        for role, items in by_position.items():
            if not isinstance(role, str):
                continue
            lst: List[Dict[str, str]] = []
            for name, tier_label, href in items:
                n = str(name or "").strip()
                h = str(href or "").strip()
                if n and h:
                    t = str(tier_label or "").strip() or "unknown"
                    lst.append({"name": n, "tier": t, "href": h})
            if lst:
                out_pos[role] = lst
        if out_pos:
            payload["by_position"] = out_pos
    _atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def get_champion_dataset(
    cache_path: Path,
    *,
    max_age_sec: float = 7 * 24 * 60 * 60,
    timeout_sec: float = 10.0,
) -> Tuple[List[str], Dict[str, List[Tuple[str, str, str]]], str]:
    now = time.time()
    cache = load_champion_list_cache(cache_path)
    if cache is not None and (now - cache.fetched_at_unix) <= max_age_sec:
        by_position = {k: list(v) for k, v in (cache.by_position or {}).items()}
        return (list(cache.champions), by_position, "cache_fresh")

    try:
        champs, by_position = fetch_opgg_champion_dataset(timeout_sec=timeout_sec)
        save_champion_list_cache(
            cache_path, champs, fetched_at_unix=now, by_position=by_position
        )
        return (champs, by_position, "opgg")
    except Exception:
        if cache is not None:
            by_position = {k: list(v) for k, v in (cache.by_position or {}).items()}
            return (list(cache.champions), by_position, "cache_stale")

    champs = fetch_riot_ddragon_champion_names(timeout_sec=timeout_sec)
    save_champion_list_cache(cache_path, champs, fetched_at_unix=now, by_position=None)
    return (champs, {}, "ddragon")


def get_champion_names(
    cache_path: Path,
    *,
    max_age_sec: float = 7 * 24 * 60 * 60,
    timeout_sec: float = 10.0,
) -> Tuple[List[str], str]:
    champs, _by_position, source = get_champion_dataset(
        cache_path,
        max_age_sec=max_age_sec,
        timeout_sec=timeout_sec,
    )
    return (champs, source)


def build_normalized_index(champions: Iterable[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for c in champions:
        cc = str(c or "").strip()
        if not cc:
            continue
        out.setdefault(normalize_name(cc), cc)
    return out
