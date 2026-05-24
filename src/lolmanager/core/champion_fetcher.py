from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError

logger = logging.getLogger(__name__)


POSITION_URLS = {
    "top": "https://op.gg/ko/lol/champions?position=top",
    "jungle": "https://op.gg/ko/lol/champions?position=jungle",
    "mid": "https://op.gg/ko/lol/champions?position=mid",
    "adc": "https://op.gg/ko/lol/champions?position=adc",
    "support": "https://op.gg/ko/lol/champions?position=support",
}


TIER_COLORS: dict[str, Tuple[str, str]] = {
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
    key = fill.strip().lower()
    return TIER_COLORS.get(key, ("unknown", "none"))


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
            logger.warning("기본 셀렉터 대기 실패, XPath fallback 시도: %s", position)

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
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        rows = soup.select("table tbody tr")
        champs_http: List[Tuple[str, Tuple[str, str], Optional[str]]] = []
        slice_rows = rows if limit is None else rows[:limit]
        for row in slice_rows:
            strong = row.select_one("td:nth-of-type(2) div a strong")
            tier_path = row.select_one("td:nth-of-type(3) svg g path")
            link = row.select_one("td:nth-of-type(2) div a")
            href = link.get("href") if link else None
            if strong and strong.text:
                fill_val = tier_path.get("fill") if tier_path else None
                champs_http.append(
                    (strong.text.strip(), _tier_from_fill(fill_val), href)
                )
        return champs_http

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


def fetch_counters_from_detail(detail_href: str, limit: int = 10) -> List[str]:
    url = detail_href
    if url.startswith("/"):
        url = "https://op.gg" + url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code != 200:
        logger.error("상세 페이지 요청 실패: %s (%s)", url, resp.status_code)
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    imgs = soup.select("section:nth-of-type(1) div:nth-of-type(3) ul li a img")
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
