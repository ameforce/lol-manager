from __future__ import annotations

import re
from typing import Optional

from lolmanager.core.opgg_counter_recommendations import AUTO_BAN_LABEL


ROLE_LABEL_KO: dict[str, str] = {
    "top": "탑",
    "jungle": "정글",
    "mid": "미드",
    "adc": "원딜",
    "support": "서폿",
}

ROLE_CLEAR_STATES: set[str] = {"UNKNOWN", "LOBBY", "MATCH_FINDING", "MATCH_ACCEPT_WAIT"}


def compact_role_ban_label_for_main_ui(value: object) -> str:
    text = str(value or "").strip()
    if text == AUTO_BAN_LABEL:
        return "자동 추천"

    match = re.fullmatch(
        r"자동 추천 \(현재 최고: (?P<champion>.+?), (?P<tier>.+?), "
        r"(?P<winrate>[^,]+), score (?P<score>[-+]?\d+(?:\.\d+)?)\)",
        text,
    )
    if not match:
        return text

    tier = match.group("tier").strip()
    tier = re.sub(r"^(\d+)티어$", r"\1T", tier)
    return f"{match.group('champion').strip()} {tier} {match.group('winrate').strip()}"


def role_key_from_log_line(line: object) -> Optional[str]:
    text = str(line or "")
    patterns = (
        r"포지션 감지:\s*(?P<role>[A-Za-z_]+)",
        r"LCU 포지션 감지\([^)]*\):\s*(?P<role>[A-Za-z_]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        role_key = match.group("role").strip()
        if role_key in ROLE_LABEL_KO:
            return role_key
    return None
