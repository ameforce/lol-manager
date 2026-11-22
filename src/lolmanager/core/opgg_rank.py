from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from threading import Event
import time
from typing import Callable
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup
import requests

from lolmanager.core.lcu_client import LcuClient
from lolmanager.core.opgg_http import next_page_stream, read_limited_text_response


OPGG_ORIGIN = "https://op.gg"
_PROFILE_MAX_AGE_SEC = 24 * 60 * 60
_SOLO_RANK_TITLES = {"Ranked Solo/Duo", "솔로랭크", "솔로 랭크"}
_TIERS = {
    "Iron": "아이언",
    "Bronze": "브론즈",
    "Silver": "실버",
    "Gold": "골드",
    "Platinum": "플래티넘",
    "Emerald": "에메랄드",
    "Diamond": "다이아몬드",
    "Master": "마스터",
    "Grandmaster": "그랜드마스터",
    "Challenger": "챌린저",
}
_ACTION_REFERENCE = re.compile(
    r'createServerReference\)\("([a-f0-9]{32,64})",[^;\n]{0,250}?"(renewalStatus|renewal)"\)'
)


@dataclass(frozen=True)
class LeagueAccount:
    region: str
    game_name: str
    tagline: str

    @property
    def profile_url(self) -> str:
        riot_id = quote(f"{self.game_name}-{self.tagline}", safe="")
        return f"{OPGG_ORIGIN}/lol/summoners/{self.region}/{riot_id}"


@dataclass(frozen=True)
class OpggRank:
    tier: str
    league_points: int | None
    updated_at: float | None

    def stale(self, *, match_finished_at: float, now: float) -> bool:
        return self.updated_at is None or self.updated_at < max(
            match_finished_at, now - _PROFILE_MAX_AGE_SEC
        )

    def label(self) -> str:
        points = (
            f" · {self.league_points:,} LP" if self.league_points is not None else ""
        )
        return f"솔로 랭크 · {self.tier}{points}"


def _timestamp(value: object) -> float | None:
    if isinstance(value, str):
        try:
            time_value = datetime.fromisoformat(value)
            return time_value.timestamp() if time_value.tzinfo is not None else None
        except ValueError, OverflowError, OSError:
            pass
    return None


def current_league_account(lcu: LcuClient) -> LeagueAccount | None:
    summoner = lcu.request("GET", "/lol-summoner/v1/current-summoner")
    if not summoner.ok or not isinstance(summoner.data, dict):
        return None
    name = str(summoner.data.get("gameName") or "").strip()
    tagline = str(summoner.data.get("tagLine") or "").strip()
    if not name or not tagline:
        return None
    locale = lcu.request("GET", "/riotclient/region-locale")
    if not locale.ok or not isinstance(locale.data, dict):
        return None
    region = (
        str(locale.data.get("webRegion") or locale.data.get("region") or "")
        .strip()
        .lower()
    )
    if not re.fullmatch(r"[a-z0-9]{2,8}", region):
        return None
    return LeagueAccount(region, name, tagline)


def latest_match_finished_at(lcu: LcuClient) -> float:
    response = lcu.request(
        "GET",
        "/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=1",
    )
    try:
        match = response.data["games"]["games"][0]
        return int(match["gameCreation"]) / 1000 + max(0, int(match["gameDuration"]))
    except TypeError, KeyError, IndexError, ValueError, OverflowError:
        return 0.0


def parse_opgg_rank(page: BeautifulSoup, account: LeagueAccount) -> OpggRank:
    # OP.GG publishes the profile identity and refresh time as JSON-LD.
    graph: list[dict] = []
    for script in page.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            graph.extend(
                entity for entity in data["@graph"] if isinstance(entity, dict)
            )
    profile = next(
        (entity for entity in graph if entity.get("@type") == "ProfilePage"), {}
    )
    person = next((entity for entity in graph if entity.get("@type") == "Person"), {})
    name, separator, tag = str(person.get("name") or "").rpartition("#")
    if (
        not separator
        or " ".join(name.casefold().split())
        != " ".join(account.game_name.casefold().split())
        or "".join(tag.casefold().split())
        != "".join(account.tagline.casefold().split())
    ):
        raise ValueError("OP.GG profile does not match the signed-in account")
    updated_at = _timestamp(profile.get("dateModified"))
    for label in page.find_all(
        string=lambda text: bool(text and text.strip() in _SOLO_RANK_TITLES)
    ):
        section = label.parent.find_parent("section")
        if section is None:
            continue
        rank = section.find("strong")
        if rank is None:
            continue
        tier = rank.get_text(" ", strip=True)
        if tier.casefold() in {"unranked", "언랭크"}:
            return OpggRank("언랭크", None, updated_at)
        lp = re.search(r"(\d[\d,]*)\s*LP\b", rank.parent.get_text(" ", strip=True))
        base, _, division = tier.partition(" ")
        if lp and (base in _TIERS or base in _TIERS.values()):
            label = f"{_TIERS.get(base, base)} {division}".strip()
            return OpggRank(label, int(lp.group(1).replace(",", "")), updated_at)
    raise ValueError("OP.GG solo rank could not be read")


class OpggRankClient:
    def __init__(self, session: requests.Session, stop: Event) -> None:
        self.session = session
        self.stop = stop
        self._actions_by_asset: dict[str, dict[str, str]] = {}

    def lookup(self, account: LeagueAccount, match_finished_at: float) -> OpggRank:
        page = self._profile(account)
        rank = parse_opgg_rank(page, account)
        if (
            not rank.stale(match_finished_at=match_finished_at, now=time.time())
            or self.stop.is_set()
        ):
            return rank
        try:
            if self._refresh(account, page):
                return parse_opgg_rank(self._profile(account, fresh=True), account)
        except ValueError, requests.RequestException:
            pass  # The last observed rank remains usable with a stale indicator.
        return rank

    def _profile(self, account: LeagueAccount, *, fresh: bool = False) -> BeautifulSoup:
        with self.session.get(
            account.profile_url,
            headers={"Accept-Language": "en-US", "Cache-Control": "no-cache"},
            params={"_": int(time.time())} if fresh else None,
            timeout=(3, 7),
            stream=True,
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            return BeautifulSoup(
                read_limited_text_response(response, max_bytes=4_000_000), "lxml"
            )

    def _refresh(self, account: LeagueAccount, page: BeautifulSoup) -> bool:
        # Follow the server actions referenced by the page's Update control.
        # The opaque action IDs change on OP.GG deploys.
        stream = next_page_stream(page)
        for match in re.finditer(r'\["\$","\$L([a-f0-9]+)",null,\{"puuid":', stream):
            try:
                props, _ = json.JSONDecoder().raw_decode(
                    stream[match.end() - len('{"puuid":') :]
                )
            except ValueError:
                continue
            if isinstance(props, dict) and "initRenewableAt" in props:
                component = match
                break
        else:
            return False

        public_id = props.get("puuid")
        if not isinstance(public_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{10,200}", public_id
        ):
            return False
        renewable_at = _timestamp(props.get("initRenewableAt"))
        if renewable_at is not None and renewable_at > time.time():
            return False

        reference = re.search(rf"^{component[1]}:I(.+)$", stream, re.MULTILINE)
        if reference is None:
            return False
        module = json.loads(reference[1])
        if (
            not isinstance(module, list)
            or len(module) < 2
            or not isinstance(module[1], list)
        ):
            return False
        chunks = tuple(
            f"/{path}"
            for path in module[1]
            if isinstance(path, str) and path.startswith("static/chunks/")
        )
        assets = []
        for script in page.select("script[src]"):
            asset = str(script["src"])
            parsed = urlsplit(asset)
            if (
                parsed.scheme == "https"
                and parsed.netloc == "c-lol-web.op.gg"
                and parsed.path.endswith(chunks)
            ):
                assets.append(asset)
        actions = self._refresh_actions(assets)
        if not actions or self.stop.is_set():
            return False
        payload = {"region": account.region, "puuid": public_id}
        result = self._run_action(
            account, actions["renewal"], {**payload, "isPremiumPrimary": False}
        )
        deadline = time.monotonic() + 18
        while result.get("status") == "RENEWING" and time.monotonic() < deadline:
            delay = result.get("delay")
            pause = (
                min(3.0, max(0.5, delay / 1000))
                if isinstance(delay, (int, float))
                else 1.0
            )
            if self.stop.wait(pause):
                return False
            result = self._run_action(account, actions["renewalStatus"], payload)
        return result.get("status") == "RENEWAL_FINISH"

    def _refresh_actions(self, assets: list[str]) -> dict[str, str]:
        # Try the known Update component's chunk first, then its other assets if the build changes.
        assets.sort(key=lambda url: "/1874-" not in url)
        deadline = time.monotonic() + 8
        for url in assets:
            if self.stop.is_set() or time.monotonic() >= deadline:
                break
            if url not in self._actions_by_asset:
                try:
                    with self.session.get(
                        url, timeout=(2, 3), stream=True, allow_redirects=False
                    ) as response:
                        response.raise_for_status()
                        body = read_limited_text_response(response, max_bytes=1_500_000)
                except ValueError, requests.RequestException:
                    continue
                actions = {
                    name: action_id
                    for action_id, name in _ACTION_REFERENCE.findall(body)
                }
                self._actions_by_asset[url] = actions
            actions = self._actions_by_asset[url]
            if {"renewal", "renewalStatus"} <= actions.keys():
                return actions
        return {}

    def _run_action(
        self, account: LeagueAccount, action_id: str, payload: dict
    ) -> dict:
        with self.session.post(
            account.profile_url,
            data=json.dumps([payload]),
            timeout=(3, 6),
            stream=True,
            allow_redirects=False,
            headers={
                "Next-Action": action_id,
                "Accept": "text/x-component",
                "Origin": OPGG_ORIGIN,
                "Referer": account.profile_url,
                "Content-Type": "text/plain;charset=UTF-8",
            },
        ) as response:
            response.raise_for_status()
            reply = read_limited_text_response(response, max_bytes=250_000)
        for row in reply.splitlines():
            _key, _, body = row.partition(":")
            if body.startswith("{"):
                try:
                    value = json.loads(body)
                except ValueError:
                    continue
                if isinstance(value, dict) and "status" in value:
                    return value
        return {}


def monitor_opgg_rank(stop: Event, publish: Callable[[str], None]) -> None:
    lcu = LcuClient()
    with lcu.session, requests.Session() as session:
        opgg = OpggRankClient(session, stop)
        account = None
        rank = None
        next_lookup = 0.0
        last_match = 0.0
        while not stop.is_set():
            current = current_league_account(lcu)
            if current != account:
                account, rank, next_lookup, last_match = current, None, 0.0, 0.0
                publish(
                    "솔로 랭크 · 조회 중…" if current else "솔로 랭크 · LoL 로그인 대기"
                )
            if account is not None:
                match_finished_at = latest_match_finished_at(lcu)
                new_match = match_finished_at > last_match
                last_match = max(last_match, match_finished_at)
                if new_match or time.monotonic() >= next_lookup:
                    try:
                        rank = opgg.lookup(account, last_match)
                        stale = rank.stale(
                            match_finished_at=last_match, now=time.time()
                        )
                        text = rank.label() + (" · 갱신 대기" if stale else "")
                        delay = 60 if stale else 300
                    except ValueError, requests.RequestException:
                        text = (
                            rank.label() + " · 갱신 실패"
                            if rank
                            else "솔로 랭크 · OP.GG 조회 실패"
                        )
                        delay = 60
                    if current_league_account(lcu) == account:
                        publish(text)
                    next_lookup = time.monotonic() + delay
            stop.wait(5)
