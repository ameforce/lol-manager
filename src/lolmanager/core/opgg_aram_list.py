from __future__ import annotations

import json
from pathlib import Path
import re
import time

from bs4 import BeautifulSoup
import requests

from lolmanager.core.champion_names import normalize_name
from lolmanager.core.opgg_champion_list import (
    load_champion_list_cache,
    save_champion_list_cache,
)
from lolmanager.core.opgg_http import MAX_OPGG_CHAMPION_ROWS, read_limited_text_response


OPGG_ARAM_URL_KO = "https://op.gg/ko/lol/modes/aram"
ARAM_CACHE_MAX_AGE_SEC = 24 * 60 * 60
_MIN_EXPECTED_CHAMPIONS = 120
ChampionEntry = tuple[str, str, str]


def default_aram_cache_path(config_path: Path) -> Path:
    return config_path.with_name("opgg_aram_champion_cache.json")


def parse_aram_entries_from_opgg_html(html: str) -> list[ChampionEntry]:
    """Read the page's streamed JSON, never executing its JavaScript.

    The HTML table initially contains placeholders. The Korean champion names,
    ARAM tiers and ranks arrive in the `champions` prop of the streamed payload;
    its source order is alphabetical, so it must be sorted by rank explicitly.
    """
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    for script in BeautifulSoup(html, "lxml").select("script:not([src])"):
        text = script.get_text()
        for match in re.finditer(r"self\.__next_f\.push\(", text):
            try:
                value, _ = decoder.raw_decode(text[match.end():].lstrip())
            except ValueError:
                continue
            if (
                isinstance(value, list) and len(value) == 2
                and value[0] == 1 and isinstance(value[1], str)
            ):
                chunks.append(value[1])

    stream = "".join(chunks)
    for match in re.finditer(r'"champions"\s*:\s*', stream):
        try:
            champions, _ = decoder.raw_decode(stream[match.end():])
        except ValueError:
            continue
        if not isinstance(champions, list) or len(champions) > MAX_OPGG_CHAMPION_ROWS:
            continue
        ranked: list[tuple[int, ChampionEntry]] = []
        for champion in champions:
            if not isinstance(champion, dict):
                continue
            name = champion.get("name")
            slug = champion.get("key")
            tier, rank = champion.get("tier"), champion.get("rank")
            if (
                not isinstance(name, str) or not name.strip()
                or not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+", slug)
                or type(tier) is not int or not 0 <= tier <= 5
                or type(rank) is not int or rank < 1
            ):
                continue
            ranked.append((rank, (
                name.strip(), "OP" if tier == 0 else f"{tier}티어",
                f"/ko/lol/modes/aram/{slug}/build",
            )))
        if ranked:
            entries: list[ChampionEntry] = []
            seen: set[str] = set()
            for _rank, entry in sorted(ranked, key=lambda item: item[0]):
                key = normalize_name(entry[0])
                if key not in seen:
                    seen.add(key)
                    entries.append(entry)
            return entries
    return []


def fetch_opgg_aram_entries(timeout_sec: float = 10.0) -> list[ChampionEntry]:
    with requests.get(
        OPGG_ARAM_URL_KO,
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9"},
        timeout=timeout_sec, stream=True,
    ) as response:
        response.raise_for_status()
        entries = parse_aram_entries_from_opgg_html(read_limited_text_response(response))
    if len(entries) < _MIN_EXPECTED_CHAMPIONS:
        raise ValueError(f"OP.GG 칼바람 티어 데이터가 부족합니다: {len(entries)}개")
    return entries


def get_aram_champion_entries(
    cache_path: Path,
    *,
    max_age_sec: float = ARAM_CACHE_MAX_AGE_SEC,
    timeout_sec: float = 10.0,
) -> tuple[list[ChampionEntry], str]:
    # Keep ARAM independent of the five ranked-role requests and their cache.
    cache = load_champion_list_cache(cache_path)
    cached = list(cache.by_position.get("aram", ())) if cache else []
    now = time.time()
    if cached and cache and 0 <= now - cache.fetched_at_unix <= max_age_sec:
        return cached, "cache_fresh"
    try:
        entries = fetch_opgg_aram_entries(timeout_sec)
    except Exception:
        return (cached, "cache_stale") if cached else ([], "unavailable")
    try:
        save_champion_list_cache(
            cache_path, (entry[0] for entry in entries), fetched_at_unix=now,
            by_position={"aram": entries},
        )
    except OSError:
        pass  # A read-only cache must not hide successfully fetched rankings.
    return entries, "opgg"
