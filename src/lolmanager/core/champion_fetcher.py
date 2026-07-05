from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError

from lolmanager.core.champion_names import normalize_name as _normalize_name
from lolmanager.core.opgg_http import (
    MAX_OPGG_CHAMPION_ROWS,
    MAX_OPGG_COUNTER_LINK_SCAN,
    read_limited_text_response,
)
from lolmanager.core.opgg_champion_list import parse_position_entries_from_opgg_html
from lolmanager.core.opgg_tiers import (
    tier_from_fill as _tier_from_fill,
    tier_from_label as _tier_from_label,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CounterMatchup:
    champion: str
    pick_winrate: Optional[float]
    games: Optional[int]
    source_order: int
    raw_text: str
    href: Optional[str] = None
    source_url: str = ""


POSITION_URLS = {
    "top": "https://op.gg/ko/lol/champions?position=top",
    "jungle": "https://op.gg/ko/lol/champions?position=jungle",
    "mid": "https://op.gg/ko/lol/champions?position=mid",
    "adc": "https://op.gg/ko/lol/champions?position=adc",
    "support": "https://op.gg/ko/lol/champions?position=support",
}


def _ranked_entry_name(entry: object) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("champion") or "").strip()
    if isinstance(entry, (list, tuple)) and entry:
        return str(entry[0] or "").strip()
    return str(entry or "").strip()


def sort_counter_candidates_by_role_rank(
    counter_candidates: Iterable[str],
    ranked_entries: Iterable[object],
) -> List[str]:
    rank_by_name: dict[str, int] = {}
    for rank, entry in enumerate(ranked_entries):
        name = _ranked_entry_name(entry)
        if not name:
            continue
        rank_by_name.setdefault(_normalize_name(name), rank)

    deduped: list[tuple[bool, int, int, str]] = []
    seen: set[str] = set()
    for counter_order, candidate in enumerate(counter_candidates):
        name = str(candidate or "").strip()
        if not name:
            continue
        key = _normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        rank = rank_by_name.get(key)
        rank_order = rank if rank is not None else 1_000_000
        deduped.append((rank is None, rank_order, counter_order, name))

    return [name for _unknown, _rank, _counter_order, name in sorted(deduped)]


def _scrape(
    position: str, limit: int, headless: bool
) -> List[Tuple[str, Tuple[str, str], Optional[str]]]:
    url = POSITION_URLS.get(position)
    if not url:
        raise ValueError(f"지원하지 않는 포지션: {position}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        )
        context = browser.new_context(
            locale="ko-KR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()

        block_types = {"image", "media", "font"}
        page.route(
            "**/*",
            lambda route, request: route.abort()
            if request.resource_type in block_types
            else route.continue_(),
        )

        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except TimeoutError:
            pass

        try:
            page.wait_for_selector(
                "table tbody tr td:nth-child(2) div a strong", timeout=8000
            )
        except TimeoutError:
            logger.warning("기본 셀렉터 대기 실패, 구조화 파서 fallback 시도: %s", position)

        parsed_entries = parse_position_entries_from_opgg_html(
            page.content(), position, max_entries=limit
        )
        if parsed_entries:
            browser.close()
            return [
                (name, _tier_from_label(tier_label), href)
                for name, tier_label, href in parsed_entries
            ]

        champs: List[Tuple[str, Tuple[str, str], Optional[str]]] = []
        rows = page.query_selector_all("table tbody tr")
        if not rows:
            for idx in range(1, limit + 1):
                xpath = f"/html/body/div[11]/div/div[3]/main/div[2]/table/tbody/tr[{idx}]/td[2]/div/a/strong"
                strong = page.query_selector(f"xpath={xpath}")
                if strong:
                    name = strong.inner_text().strip()
                    link = page.query_selector(
                        f"xpath=/html/body/div[11]/div/div[3]/main/div[2]/table/tbody/tr[{idx}]/td[2]/div/a"
                    )
                    href = link.get_attribute("href") if link else None
                    tier_fill = page.query_selector(
                        f"xpath=/html/body/div[11]/div/div[3]/main/div[2]/table/tbody/tr[{idx}]/td[3]/svg/g/path[1]"
                    )
                    fill_val = tier_fill.get_attribute("fill") if tier_fill else None
                    champs.append((name, _tier_from_fill(fill_val), href))
        else:
            for row in rows[:limit]:
                strong = row.query_selector("td:nth-child(2) div a strong")
                tier_path = row.query_selector("td:nth-child(3) svg g path")
                link = row.query_selector("td:nth-child(2) div a")
                href = link.get_attribute("href") if link else None
                if strong:
                    name = strong.inner_text().strip()
                    fill_val = tier_path.get_attribute("fill") if tier_path else None
                    champs.append((name, _tier_from_fill(fill_val), href))

        browser.close()
        return champs


def fetch_top_champions(
    position: str, limit: Optional[int] = None, headless: bool = True
) -> List[Tuple[str, Tuple[str, str], Optional[str]]]:
    def _scrape_http() -> List[Tuple[str, Tuple[str, str], Optional[str]]]:
        url = POSITION_URLS.get(position)
        if not url:
            raise ValueError(f"지원하지 않는 포지션: {position}")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        resp = requests.get(url, headers=headers, timeout=10, stream=True)
        if resp.status_code != 200:
            return []
        row_budget = MAX_OPGG_CHAMPION_ROWS if limit is None else min(
            int(limit), MAX_OPGG_CHAMPION_ROWS
        )
        entries = parse_position_entries_from_opgg_html(
            read_limited_text_response(resp), position, max_entries=row_budget
        )
        return [
            (name, _tier_from_label(tier_label), href)
            for name, tier_label, href in entries
        ]

    champs: List[Tuple[str, Tuple[str, str], Optional[str]]] = []

    try:
        champs = _scrape_http()
    except Exception as e:
        logger.info("HTTP 파싱 실패: %s", e)

    if not champs:
        try:
            use_limit = 9999 if limit is None else limit
            champs = _scrape(position, use_limit, headless=headless)
        except Exception as e:
            logger.warning("headless 모드 실패: %s", e)

    if not champs and headless:
        logger.info("headless 실패, headed 재시도: %s", position)
        try:
            use_limit = 9999 if limit is None else limit
            champs = _scrape(position, use_limit, headless=False)
        except Exception as e:
            logger.error("headed 재시도 실패: %s", e)

    if not champs:
        logger.error("챔피언 목록을 가져오지 못했습니다: %s", position)
    else:
        logger.info("챔피언 목록(%s) %d개 수집", position, len(champs))
        logger.debug("챔피언 목록(%s) 상세: %s", position, champs)
    return champs


def fetch_champion_slug(position: str, champion_name: str) -> Optional[str]:
    for name, _tier, href in fetch_top_champions(position, limit=None):
        if name == champion_name and href:
            return href
    return None


def _absolute_opgg_url(detail_href: str) -> str:
    url = str(detail_href or "").strip()
    if not url:
        return ""

    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        if parts.scheme != "https" or parts.netloc.casefold() != "op.gg":
            return ""
    elif not url.startswith("/"):
        return ""

    if not parts.path.startswith("/ko/lol/champions/") or "/build/" not in parts.path:
        return ""

    return urlunsplit(("https", "op.gg", parts.path, parts.query, ""))


def _parse_percent(value: str) -> Optional[float]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", value or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_games(value: str) -> Optional[int]:
    match = re.search(r"([\d,]+)\s*게임", value or "")
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _compact_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _counter_links_from_difficult_section(
    soup: BeautifulSoup, *, max_links: int
) -> List[object]:
    header_text = soup.find(
        string=lambda text: bool(text and "상대하기 어려운" in str(text))
    )
    if header_text is not None:
        header = getattr(header_text, "parent", None)
        if header is not None:
            ul = header.find_next("ul")
            if ul is not None:
                links = ul.select("li a", limit=max_links)
                if links:
                    return list(links)

    links = soup.select(
        "section:nth-of-type(1) div:nth-of-type(3) ul li a", limit=max_links
    )
    if links:
        return list(links)
    return list(soup.select("section ul li a", limit=max_links))


def parse_counter_matchups_from_html(
    html_text: str,
    *,
    source_url: str = "",
    limit: int = 10,
) -> List[CounterMatchup]:
    if limit <= 0:
        return []
    soup = BeautifulSoup(html_text or "", "lxml")
    scan_limit = min(MAX_OPGG_COUNTER_LINK_SCAN, max(1, int(limit)) * 4)
    links = _counter_links_from_difficult_section(soup, max_links=scan_limit)
    matchups: List[CounterMatchup] = []
    seen: set[str] = set()

    for link in links:
        img = link.select_one("img")
        champion = ""
        if img is not None:
            champion = str(img.get("alt") or "").strip()
        if not champion:
            text = _compact_text(link.get_text(" ", strip=True))
            champion = re.sub(r"\d+(?:\.\d+)?\s*%.*$", "", text).strip()
        if not champion:
            continue

        key = _normalize_name(champion)
        if key in seen:
            continue
        seen.add(key)

        raw_text = _compact_text(link.get_text(" ", strip=True))
        if not raw_text and getattr(link, "parent", None) is not None:
            raw_text = _compact_text(link.parent.get_text(" ", strip=True))
        matchups.append(
            CounterMatchup(
                champion=champion,
                pick_winrate=_parse_percent(raw_text),
                games=_parse_games(raw_text),
                source_order=len(matchups),
                raw_text=raw_text,
                href=str(link.get("href") or "").strip() or None,
                source_url=source_url,
            )
        )
        if len(matchups) >= limit:
            break

    return matchups


def fetch_counter_matchups_from_detail(
    detail_href: str,
    limit: int = 10,
) -> List[CounterMatchup]:
    url = _absolute_opgg_url(detail_href)
    if not url:
        logger.warning("허용되지 않는 OP.GG 상세 href를 차단했습니다.")
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(
        url, headers=headers, timeout=10, allow_redirects=False, stream=True
    )
    if resp.status_code != 200:
        logger.error("상세 페이지 요청 실패: %s (%s)", url, resp.status_code)
        return []
    try:
        html_text = read_limited_text_response(resp)
    except ValueError as exc:
        logger.error("OP.GG 상세 페이지 응답 거부: %s", exc)
        return []
    return parse_counter_matchups_from_html(html_text, source_url=url, limit=limit)


def fetch_counters_from_detail(detail_href: str, limit: int = 10) -> List[str]:
    try:
        matchups = fetch_counter_matchups_from_detail(detail_href, limit=limit)
    except Exception as exc:
        logger.info("상세 카운터 구조화 파싱 실패: %s", exc)
        matchups = []
    if matchups:
        return [matchup.champion for matchup in matchups[:limit]]

    url = _absolute_opgg_url(detail_href)
    if not url:
        logger.warning("허용되지 않는 OP.GG 상세 href를 차단했습니다.")
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(
        url, headers=headers, timeout=10, allow_redirects=False, stream=True
    )
    if resp.status_code != 200:
        logger.error("상세 페이지 요청 실패: %s (%s)", url, resp.status_code)
        return []
    try:
        html_text = read_limited_text_response(resp)
    except ValueError as exc:
        logger.error("OP.GG 상세 페이지 응답 거부: %s", exc)
        return []
    soup = BeautifulSoup(html_text, "lxml")
    imgs = soup.select(
        "section:nth-of-type(1) div:nth-of-type(3) ul li a img",
        limit=min(MAX_OPGG_COUNTER_LINK_SCAN, max(1, int(limit)) * 4),
    )
    counters: List[str] = []
    for img in imgs[:limit]:
        alt = img.get("alt")
        if alt:
            counters.append(alt.strip())
        else:
            src = img.get("src", "")
            if "/" in src:
                name_part = src.split("/")[-1].split(".")[0]
                counters.append(name_part)
    return counters
