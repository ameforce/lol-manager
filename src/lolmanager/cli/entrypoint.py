from __future__ import annotations

import argparse
import logging
import sys
import time
from enum import IntEnum, unique
from pathlib import Path
from typing import Iterable, Optional, Sequence

if __package__ in (None, ""):
    try:
        _src = Path(__file__).resolve().parents[2]  # .../src
        if (_src / "lolmanager" / "__init__.py").exists():
            sys.path.insert(0, str(_src))
    except Exception:
        pass

from lolmanager.platform.external_apps import (
    LeagueClientExitGuard,
    ensure_external_apps_running_once,
    league_client_exe_path,
)
from lolmanager.core.match_timing import append_match_duration, format_duration_mmss
from lolmanager.core.lcu_client import (
    LcuClient,
    PHASE_CHAMP_SELECT,
    PHASE_END_OF_GAME,
    PHASE_IN_PROGRESS,
    PHASE_LOBBY,
    PHASE_MATCHMAKING,
    PHASE_PRE_END_OF_GAME,
    PHASE_READY_CHECK,
    PHASE_WAITING_FOR_STATS,
)
from lolmanager.core.runtime_logging import (
    configure_runtime_logging,
    install_exception_logger,
)
from lolmanager.core.image_search import (
    click_relative,
    click_screen,
    find_best_template,
    find_template_matches_once,
    is_probably_disabled_gray_button,
    search_and_act,
)
from lolmanager.core.champion_config import ChampionConfig
from lolmanager.core.champion_fetcher import (
    fetch_top_champions,
    fetch_champion_slug,
    fetch_counters_from_detail,
)
from lolmanager.platform.resolution_detector import (
    select_image_set,
    window_size_from_rect,
    is_rect_minimized,
    find_league_window_rect,
    is_game_client_active,
)


_LEAGUE_EXIT_GUARD: LeagueClientExitGuard | None = None


ANSI_COLORS = {
    "red": "\033[31m",
    "blue": "\033[34m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "gray": "\033[90m",
    "brown": "\033[38;5;94m",
    "none": "",
}
ANSI_RESET = "\033[0m"

DEFAULT_PICK_COORD = (386, 163)

ROLE_ORDER: tuple[str, ...] = ("top", "jungle", "mid", "adc", "support")


@unique
class ClientState(IntEnum):
    UNKNOWN = 0
    LOBBY = 10
    MATCH_FINDING = 20
    MATCH_ACCEPT_WAIT = 30
    PREPICK = 40
    BANPICK = 50
    PICK = 60
    WAIT_GAME_START = 70
    INGAME = 80
    POSTGAME_SCORE = 90


_last_finding_logged: dict[str, bool] = {}
_last_accept_click_at: dict[str, float] = {}
_last_lcu_ready_accept_at: dict[str, float] = {}
ACCEPT_CLICK_COOLDOWN_SEC = 0.8
LCU_READY_ACCEPT_COOLDOWN_SEC = 0.8


RUNTIME_STATE: dict[str, object] = {
    "client_state": ClientState.UNKNOWN,
    "client_state_updated_at": 0.0,
    "is_my_pick_turn": False,
    "my_pick_turn_updated_at": 0.0,
}
_my_pick_turn_miss_streak: int = 0
MY_PICK_TURN_CLEAR_MISS_STREAK = 3


_MATCH_STARTED_AT_MONO: Optional[float] = None


def _set_client_state(value: ClientState, now: float, logger: logging.Logger) -> None:
    prev = RUNTIME_STATE.get("client_state", ClientState.UNKNOWN)
    if prev == value:
        return
    RUNTIME_STATE["client_state"] = value
    RUNTIME_STATE["client_state_updated_at"] = float(now)
    logger.info("현재 상태 업데이트: client_state=%s", value.name)
    _on_client_state_changed_for_timing(prev, value, float(now), logger)


def _client_state_from_lcu_phase(phase: Optional[str]) -> Optional[ClientState]:
    if phase == PHASE_LOBBY:
        return ClientState.LOBBY
    if phase == PHASE_MATCHMAKING:
        return ClientState.MATCH_FINDING
    if phase == PHASE_READY_CHECK:
        return ClientState.MATCH_ACCEPT_WAIT
    if phase == PHASE_CHAMP_SELECT:
        return ClientState.PREPICK
    if phase == PHASE_IN_PROGRESS:
        return ClientState.INGAME
    if phase in {PHASE_WAITING_FOR_STATS, PHASE_PRE_END_OF_GAME, PHASE_END_OF_GAME}:
        return ClientState.POSTGAME_SCORE
    return None


def _poll_lcu_phase(
    lcu: Optional[LcuClient],
    logger: logging.Logger,
    stage: str,
    *,
    max_age_sec: float = 0.25,
) -> Optional[str]:
    if lcu is None:
        return None

    phase = lcu.get_gameflow_phase(max_age_sec=max_age_sec)
    transition = lcu.consume_phase_transition(phase)
    if transition is not None:
        prev, curr = transition
        logger.info("LCU 상태 전이(%s): %s -> %s", stage, prev or "UNKNOWN", curr)

    state = _client_state_from_lcu_phase(phase)
    if state is not None:
        _set_client_state(state, time.monotonic(), logger)
    return phase


def _accept_ready_check_via_lcu(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> bool:
    if lcu is None:
        return False

    now = time.monotonic()
    last = _last_lcu_ready_accept_at.get(stage, 0.0)
    if now - last < LCU_READY_ACCEPT_COOLDOWN_SEC:
        logger.debug("LCU ReadyCheck 수락 쿨다운 중(%s).", stage)
        return True

    if lcu.accept_ready_check():
        _last_lcu_ready_accept_at[stage] = now
        logger.info("LCU ReadyCheck 수락 요청 완료(%s).", stage)
        return True

    logger.debug("LCU ReadyCheck 수락 요청 실패(%s). 이미지 fallback 진행.", stage)
    return False


def _on_client_state_changed_for_timing(
    prev: object,
    curr: ClientState,
    now: float,
    logger: logging.Logger,
) -> None:
    global _MATCH_STARTED_AT_MONO

    if curr == ClientState.MATCH_FINDING:
        if _MATCH_STARTED_AT_MONO is None:
            _MATCH_STARTED_AT_MONO = float(now)
            logger.info("매칭 타이머 시작(MATCH_FINDING).")
        return

    if curr in {ClientState.LOBBY, ClientState.UNKNOWN}:
        if _MATCH_STARTED_AT_MONO is not None:
            logger.info(
                "매칭 타이머 리셋(%s → %s).",
                getattr(prev, "name", str(prev)),
                curr.name,
            )
            _MATCH_STARTED_AT_MONO = None
        return

    if curr == ClientState.INGAME:
        if _MATCH_STARTED_AT_MONO is None:
            return
        duration_sec = float(now) - float(_MATCH_STARTED_AT_MONO)
        _MATCH_STARTED_AT_MONO = None

        durations, avg = append_match_duration(duration_sec)
        if avg is None:
            logger.info(
                "매칭→인게임 소요: %s (%.1fs)",
                format_duration_mmss(duration_sec),
                duration_sec,
            )
        else:
            logger.info(
                "매칭→인게임 소요: %s (%.1fs) | 평균: %s (%.1fs) | n=%d",
                format_duration_mmss(duration_sec),
                duration_sec,
                format_duration_mmss(avg),
                avg,
                len(durations),
            )


def _reset_match_timer_by_find_match_click(now: float, logger: logging.Logger) -> None:
    _set_client_state(ClientState.LOBBY, float(now), logger)
    _set_client_state(ClientState.MATCH_FINDING, float(now), logger)


def _set_my_pick_turn(value: bool, now: float, logger: logging.Logger) -> None:
    prev = bool(RUNTIME_STATE.get("is_my_pick_turn", False))
    if prev == value:
        return
    RUNTIME_STATE["is_my_pick_turn"] = value
    RUNTIME_STATE["my_pick_turn_updated_at"] = float(now)
    logger.info("현재 상태 업데이트: is_my_pick_turn=%s", "True" if value else "False")


_last_popup_click_at: dict[str, float] = {}
POPUP_CLICK_COOLDOWN_SEC = 0.6


def _popup_button_search_roi(rect) -> tuple[int, int, int, int]:
    left, top, right, bottom = rect
    w = int(right) - int(left)
    h = int(bottom) - int(top)
    if w <= 0 or h <= 0:
        return (0, 0, 0, 0)

    x1 = int(w * 0.15)
    y1 = int(h * 0.18)
    x2 = int(w * 0.85)
    y2 = int(h * 0.98)

    if x2 <= x1 or y2 <= y1:
        return (0, 0, w, h)
    return (x1, y1, x2, y2)


def ensure_active_rect(logger: logging.Logger, poll: float = 0.5):
    last_state = None
    while True:
        if _LEAGUE_EXIT_GUARD is not None and _LEAGUE_EXIT_GUARD.should_exit():
            logger.info("LeagueClient.exe 종료 감지. lolmanager를 종료합니다.")
            raise SystemExit(0)

        rect = find_league_window_rect()
        if rect:
            if is_rect_minimized(rect):
                if last_state != "minimized":
                    logger.info("LoL 창이 최소화/비가시 상태입니다. 복원 대기 중...")
                    last_state = "minimized"
                time.sleep(poll)
                continue
            return rect
        if last_state != "missing":
            logger.info("LoL 창 좌표를 찾지 못했습니다. 재시도...")
            last_state = "missing"
        time.sleep(poll)


def prompt_ban_selection(
    role: str,
    selected_name: str,
    href: Optional[str],
    logger: logging.Logger,
) -> Optional[str]:
    slug = href or fetch_champion_slug(role, selected_name)
    ban_candidates: list[str] = []
    if slug:
        ban_candidates = fetch_counters_from_detail(slug, limit=10)
    if ban_candidates:
        print(f"[{role}] 밴할 챔피언을 번호로 선택하세요 (기본 1):")
        for idx, name in enumerate(ban_candidates, start=1):
            print(f"  {idx}. {name}")
        ban_choice = input("번호 입력 (기본 1): ").strip()
        try:
            ban_idx = int(ban_choice) - 1 if ban_choice else 0
        except ValueError:
            ban_idx = 0
        ban_idx = max(0, min(ban_idx, len(ban_candidates) - 1))
        return ban_candidates[ban_idx]
    logger.warning("밴 후보를 가져오지 못했습니다. 밴 챔피언이 설정되지 않습니다.")
    return None


def ensure_champion_for_role(
    role: str,
    config: ChampionConfig,
    logger: logging.Logger,
) -> dict | None:
    champ_info = config.get(role)
    if champ_info:
        champ = champ_info.get("champion")
        if isinstance(champ, (list, tuple)):
            champ = champ[0] if champ else ""
            champ_info["champion"] = champ
        ban = champ_info.get("ban")
        if isinstance(ban, (list, tuple)):
            ban = ban[0] if ban else ""
            champ_info["ban"] = ban
        return champ_info

    logger.info(
        "포지션 %s의 챔피언이 설정되지 않았습니다. 상위 챔피언을 불러옵니다.", role
    )
    try:
        candidates = fetch_top_champions(role, limit=None)
    except Exception as e:
        logger.error("챔피언 목록을 불러오지 못했습니다: %s", e)
        return None
    if not candidates:
        logger.error("챔피언 후보가 없습니다.")
        return None

    print(
        f"[{role}] 사용할 챔피언을 번호로 선택하세요 (아래쪽일수록 고티어, 번호가 작음):"
    )
    printable = list(reversed(candidates))
    prev_tier = None
    total = len(printable)
    for idx, (name, tier_info, href) in enumerate(printable, start=1):
        tier_label, color_name = tier_info
        color_prefix = ANSI_COLORS.get(color_name, "")
        if tier_label != prev_tier:
            print(f"{color_prefix}# {tier_label}{ANSI_RESET}")
            prev_tier = tier_label
        display_idx = total - idx + 1
        print(f"{color_prefix}  {display_idx}. {name}{ANSI_RESET}")
    choice = input("번호 입력 (기본 1): ").strip()
    try:
        sel_idx = int(choice) - 1 if choice else 0
    except ValueError:
        sel_idx = 0
    sel_idx = max(0, min(sel_idx, len(candidates) - 1))
    selected_name, _tier_info, href = candidates[sel_idx]

    selected_ban = prompt_ban_selection(role, selected_name, href, logger)
    config.set(role, selected_name, None, ban_champion=selected_ban)
    logger.info(
        "포지션 %s 챔피언 설정 완료: %s, 밴: %s", role, selected_name, selected_ban
    )
    return config.get(role)


def _prompt_ban_required(
    role: str,
    champion_name: str,
    href: Optional[str],
    logger: logging.Logger,
) -> str:
    ban = prompt_ban_selection(role, champion_name, href, logger)
    if ban:
        return ban
    manual = input(
        f"[{role}] ({champion_name}) 밴 챔피언을 직접 입력하세요(필수): "
    ).strip()
    while not manual:
        print("밴 챔피언은 비울 수 없습니다.")
        manual = input(
            f"[{role}] ({champion_name}) 밴 챔피언을 직접 입력하세요(필수): "
        ).strip()
    return manual


def _prompt_champion_and_ban_from_opgg(
    role: str,
    label: str,
    logger: logging.Logger,
    exclude_champions: Optional[Iterable[str]] = None,
) -> tuple[str, str]:
    logger.info("[%s] %s 후보 목록을 불러옵니다(op.gg).", role, label)
    try:
        candidates = fetch_top_champions(role, limit=None)
    except Exception as e:
        logger.error("챔피언 목록을 불러오지 못했습니다(%s): %s", role, e)
        return ("", "")
    if not candidates:
        logger.error("챔피언 후보가 없습니다(%s).", role)
        return ("", "")

    if exclude_champions:
        excluded = {
            str(x).strip().casefold()
            for x in exclude_champions
            if x is not None and str(x).strip()
        }
        if excluded:
            candidates = [
                (name, tier_info, href)
                for (name, tier_info, href) in candidates
                if str(name).strip().casefold() not in excluded
            ]
            if not candidates:
                logger.warning(
                    "[%s] %s 후보가 없습니다(이미 선택된 챔피언을 제외한 결과).",
                    role,
                    label,
                )
                return ("", "")

    print(
        f"[{role}] {label}로 사용할 챔피언을 번호로 선택하세요 (아래쪽일수록 고티어, 번호가 작음):"
    )
    printable = list(reversed(candidates))
    prev_tier = None
    total = len(printable)
    for idx, (name, tier_info, _href) in enumerate(printable, start=1):
        tier_label, color_name = tier_info
        color_prefix = ANSI_COLORS.get(color_name, "")
        if tier_label != prev_tier:
            print(f"{color_prefix}# {tier_label}{ANSI_RESET}")
            prev_tier = tier_label
        display_idx = total - idx + 1
        print(f"{color_prefix}  {display_idx}. {name}{ANSI_RESET}")

    choice = input("번호 입력 (Enter=취소): ").strip()
    if not choice:
        return ("", "")
    try:
        sel_idx = int(choice) - 1
    except ValueError:
        sel_idx = 0
    sel_idx = max(0, min(sel_idx, len(candidates) - 1))
    selected_name, _tier_info, href = candidates[sel_idx]
    selected_ban = _prompt_ban_required(role, selected_name, href, logger)
    return (selected_name, selected_ban)


def prompt_reserve_picks_for_role(
    role: str,
    primary: str,
    primary_ban: str,
    logger: logging.Logger,
) -> list[tuple[str, str]]:
    reserves: list[tuple[str, str]] = []

    for idx in (1, 2):
        ban_label = primary_ban if primary_ban else "미설정"
        yn = (
            input(
                f"[{role}] 현재 픽: {primary} (ban={ban_label}) | 예비 챔피언 {idx} 설정? (y/N): "
            )
            .strip()
            .lower()
        )
        if yn not in ("y", "yes"):
            if idx == 1:
                return reserves
            break
        exclude = {primary, *(c for (c, _b) in reserves)}
        champ, ban = _prompt_champion_and_ban_from_opgg(
            role,
            f"예비 챔피언 {idx}",
            logger,
            exclude_champions=exclude,
        )
        if not champ:
            if idx == 1:
                return reserves
            break
        reserves.append((champ, ban))
    return reserves


def _print_pick_pool_summary(
    role: str,
    primary: str,
    primary_ban: str,
    reserves: list[tuple[str, str]],
) -> None:
    ban_label = primary_ban if primary_ban else "미설정"
    print(f"[{role}] 기본: {primary} (ban={ban_label})")
    for idx in (1, 2):
        if idx <= len(reserves):
            champ, ban = reserves[idx - 1]
            champ = str(champ or "").strip()
            ban = str(ban or "").strip()
            ban_label = ban if ban else "미설정"
            print(f"  - 예비{idx}: {champ} (ban={ban_label})")
        else:
            print(f"  - 예비{idx}: 미설정")


def poll_match_state(
    rect,
    stage: str,
    tpl_finding_match: Path,
    tpl_accept: Path,
    threshold: float,
    confirm_check_interval: float,
    logger: logging.Logger,
    tpl_confirm_templates: Optional[Sequence[Path]] = None,
    lcu: Optional[LcuClient] = None,
) -> tuple[bool, bool]:
    phase = _poll_lcu_phase(lcu, logger, stage)
    if phase == PHASE_READY_CHECK:
        if _accept_ready_check_via_lcu(lcu, stage, logger):
            time.sleep(confirm_check_interval)
            return True, True
    elif phase == PHASE_MATCHMAKING:
        already_logged = _last_finding_logged.get(stage, False)
        if not already_logged:
            logger.info("LCU 매칭 상태 감지(%s). 이미지 매칭을 건너뜁니다.", stage)
            _last_finding_logged[stage] = True
        time.sleep(confirm_check_interval)
        return False, True
    elif phase == PHASE_CHAMP_SELECT:
        logger.debug("LCU 챔피언 선택 진입 감지(%s).", stage)
        return False, False
    elif phase == PHASE_IN_PROGRESS:
        logger.debug("LCU 인게임 진입 감지(%s).", stage)
        return False, False

    if rect is None:
        return False, False

    templates: list[tuple[str, Path]] = [
        ("finding", tpl_finding_match),
        ("accept", tpl_accept),
    ]
    confirm_names: list[str] = []
    confirm_name_to_path: dict[str, Path] = {}

    rois: dict[str, tuple[int, int, int, int]] = {}
    if tpl_confirm_templates:
        popup_roi = _popup_button_search_roi(rect)
        for idx, tpl_confirm in enumerate(tpl_confirm_templates):
            if tpl_confirm is None or not tpl_confirm.exists():
                continue
            name = f"confirm#{idx}"
            templates.append((name, tpl_confirm))
            confirm_names.append(name)
            confirm_name_to_path[name] = tpl_confirm
            rois[name] = popup_roi

    matches = find_template_matches_once(
        rect, templates, threshold=threshold, search_rois=(rois if rois else None)
    )

    finding_match = matches.get("finding")
    accept_match = matches.get("accept")
    best_confirm_name: Optional[str] = None
    best_confirm_match: Optional[tuple[tuple[int, int], object, float]] = None
    best_confirm_score = -1.0
    for name in confirm_names:
        hit = matches.get(name)
        if hit is None:
            continue
        score = float(hit[2])
        if score > best_confirm_score:
            best_confirm_score = score
            best_confirm_name = name
            best_confirm_match = hit

    if best_confirm_match is not None:
        center, _roi_bgr, _score = best_confirm_match
        now = time.monotonic()
        key = f"popup_confirm:{stage}"
        last = _last_popup_click_at.get(key, 0.0)
        if now - last >= POPUP_CLICK_COOLDOWN_SEC:
            try:
                click_screen(center)
                _last_popup_click_at[key] = now
                tpl = confirm_name_to_path.get(best_confirm_name) if best_confirm_name else None
                logger.info(
                    "확인 팝업 클릭 처리(%s, tpl=%s).",
                    stage,
                    (tpl.name if tpl else "unknown"),
                )
            except Exception as exc:
                logger.warning("확인 팝업 클릭 실패(%s): %s", stage, exc)

    finding = (finding_match is not None) or (accept_match is not None)

    if accept_match is not None:
        center, roi_bgr, _score = accept_match
        now = time.monotonic()
        _set_client_state(ClientState.MATCH_ACCEPT_WAIT, now, logger)

        if is_probably_disabled_gray_button(roi_bgr):
            logger.debug(
                "수락 버튼이 비활성(회색) 상태로 감지되어 클릭을 생략합니다(%s).", stage
            )
            _last_finding_logged[stage] = True
            return True, True

        last = _last_accept_click_at.get(stage, 0.0)
        if now - last >= ACCEPT_CLICK_COOLDOWN_SEC:
            try:
                click_screen(center)
            except Exception as exc:
                logger.warning("수락 버튼 클릭 실패(%s): %s", stage, exc)
                time.sleep(confirm_check_interval)
                return False, True
            _last_accept_click_at[stage] = now
            logger.info("수락 버튼 클릭 완료(%s).", stage)
        else:
            logger.debug("수락 버튼 클릭 쿨다운 중(%s).", stage)
        _last_finding_logged[stage] = True
        return True, True

    if finding:
        _set_client_state(ClientState.MATCH_FINDING, time.monotonic(), logger)
        already_logged = _last_finding_logged.get(stage, False)
        if not already_logged:
            logger.info("매칭 상태 감지(%s). 수락 버튼 확인.", stage)
            _last_finding_logged[stage] = True
        else:
            logger.debug("매칭 상태 감지(%s). 수락 버튼 확인.", stage)

        logger.debug(
            "수락 버튼 미검출(%s). %.1fs 후 재시도...", stage, confirm_check_interval
        )
        time.sleep(confirm_check_interval)
        return False, True

    _last_finding_logged[stage] = False
    return False, False


def detect_match_reset(
    rect,
    stage: str,
    tpl_find_match: Path,
    tpl_finding_match: Path,
    tpl_accept: Path,
    threshold: float,
    confirm_check_interval: float,
    logger: logging.Logger,
) -> bool:
    accepted, finding = poll_match_state(
        rect,
        stage,
        tpl_finding_match,
        tpl_accept,
        threshold,
        confirm_check_interval,
        logger,
    )
    if accepted or finding:
        logger.info("매칭 상태 재감지(%s). 현재 단계를 중단합니다.", stage)
        return True
    found_find_match = search_and_act(
        rect, tpl_find_match, threshold=threshold, click=False
    )
    if found_find_match:
        _set_client_state(ClientState.LOBBY, time.monotonic(), logger)
        logger.info("대전 찾기 화면 복귀 감지(%s). 현재 단계를 중단합니다.", stage)
        return True
    return False


def detect_champion_select(
    rect,
    stage: str,
    tpl_prepick: Path,
    available_roles: list[tuple[str, Path]],
    threshold: float,
    logger: logging.Logger,
    lcu: Optional[LcuClient] = None,
) -> bool:
    phase = _poll_lcu_phase(lcu, logger, stage)
    if phase == PHASE_CHAMP_SELECT:
        logger.info("LCU 챔피언 선택 상태 감지(%s).", stage)
        return True

    templates: list[tuple[str, Path]] = []
    if tpl_prepick.exists():
        templates.append(("prepick", tpl_prepick))
    for role, tpl in available_roles:
        if tpl.exists():
            templates.append((f"role:{role}", tpl))
    if not templates:
        return False

    matches = find_template_matches_once(rect, templates, threshold=threshold)
    if not matches:
        return False

    if matches.get("prepick") is not None:
        _set_client_state(ClientState.PREPICK, time.monotonic(), logger)
        logger.info("챔피언 선택 화면 감지(%s).", stage)
        return True

    for role, _tpl in available_roles:
        if matches.get(f"role:{role}") is not None:
            _set_client_state(ClientState.PREPICK, time.monotonic(), logger)
            logger.info("챔피언 선택 화면 감지(%s, role=%s).", stage, role)
            return True
    return False


def try_confirm(
    rect, tpl_confirm: Path, threshold: float, logger: logging.Logger
) -> bool:
    clicked = search_and_act(rect, tpl_confirm, threshold=threshold, click=True)
    if clicked:
        logger.info("확인 팝업 클릭 처리.")
    return clicked


def try_pick_popups(
    rect,
    tpl_confirm_templates: Sequence[Path],
    tpl_decline: Optional[Path],
    threshold: float,
    logger: logging.Logger,
    tpl_pick_myturn: Optional[Path] = None,
) -> bool:
    global _my_pick_turn_miss_streak

    now = time.monotonic()
    templates: list[tuple[str, Path]] = []
    confirm_names: list[str] = []
    confirm_name_to_path: dict[str, Path] = {}

    for idx, tpl_confirm in enumerate(tpl_confirm_templates or ()):
        if tpl_confirm is None or not tpl_confirm.exists():
            continue
        name = f"confirm#{idx}"
        templates.append((name, tpl_confirm))
        confirm_names.append(name)
        confirm_name_to_path[name] = tpl_confirm

    if tpl_decline:
        templates.append(("decline", tpl_decline))

    enable_myturn = bool(tpl_pick_myturn) and (
        RUNTIME_STATE.get("client_state") == ClientState.PICK
    )
    if enable_myturn and tpl_pick_myturn:
        templates.append(("myturn", tpl_pick_myturn))

    popup_roi = _popup_button_search_roi(rect)
    rois: dict[str, tuple[int, int, int, int]] = {}
    for name in confirm_names:
        rois[name] = popup_roi
    if tpl_decline:
        rois["decline"] = popup_roi

    matches = find_template_matches_once(
        rect, templates, threshold=threshold, search_rois=rois
    )
    if enable_myturn:
        myturn_detected = matches.get("myturn") is not None if matches else False
        if myturn_detected:
            _my_pick_turn_miss_streak = 0
            _set_my_pick_turn(True, now, logger)
        elif bool(RUNTIME_STATE.get("is_my_pick_turn", False)):
            _my_pick_turn_miss_streak += 1
            if _my_pick_turn_miss_streak >= MY_PICK_TURN_CLEAR_MISS_STREAK:
                _my_pick_turn_miss_streak = 0
                _set_my_pick_turn(False, now, logger)

    if not matches:
        return False

    clicked_any = False

    best_confirm_name: Optional[str] = None
    best_confirm_hit: Optional[tuple[tuple[int, int], object, float]] = None
    best_confirm_score = -1.0
    for name in confirm_names:
        hit = matches.get(name)
        if hit is None:
            continue
        score = float(hit[2])
        if score > best_confirm_score:
            best_confirm_score = score
            best_confirm_name = name
            best_confirm_hit = hit

    for name, hit in (("decline", matches.get("decline")), ("confirm", best_confirm_hit)):
        if hit is None:
            continue

        center, _roi_bgr, _score = hit
        key = f"pick_popup:{name}"
        last = _last_popup_click_at.get(key, 0.0)
        if now - last < POPUP_CLICK_COOLDOWN_SEC:
            continue

        try:
            click_screen(center)
        except Exception as exc:
            logger.warning("팝업 버튼 클릭 실패(%s): %s", name, exc)
            continue

        _last_popup_click_at[key] = now
        clicked_any = True
        if name == "confirm":
            tpl = confirm_name_to_path.get(best_confirm_name) if best_confirm_name else None
            logger.info("확인 팝업 클릭 처리(tpl=%s).", (tpl.name if tpl else "unknown"))
        else:
            logger.info("거절 버튼 클릭 처리.")

    return clicked_any


def process_postgame(
    tpl_end_next: Path,
    tpl_end_one_more: Path,
    tpl_find_match: Path,
    tpl_finding_match: Path,
    tpl_accept: Path,
    tpl_confirm_templates: Sequence[Path],
    tpl_prepick: Path,
    available_roles: list[tuple[str, Path]],
    threshold: float,
    confirm_check_interval: float,
    interval_sec: float,
    logger: logging.Logger,
    lcu: Optional[LcuClient] = None,
) -> None:
    _set_client_state(ClientState.POSTGAME_SCORE, time.monotonic(), logger)
    while True:
        phase = _poll_lcu_phase(lcu, logger, "엔드 이후", max_age_sec=0.5)
        if phase in {PHASE_LOBBY, PHASE_MATCHMAKING, PHASE_CHAMP_SELECT}:
            logger.info("LCU postgame 종료 감지: phase=%s", phase)
            return
        if phase == PHASE_READY_CHECK:
            _accept_ready_check_via_lcu(lcu, "엔드 이후", logger)
            logger.info("LCU ReadyCheck 감지로 postgame 처리를 종료합니다.")
            return
        if phase in {PHASE_WAITING_FOR_STATS, PHASE_PRE_END_OF_GAME, PHASE_END_OF_GAME}:
            if lcu is not None and lcu.is_end_of_game_stats_available():
                logger.debug("LCU 엔드 통계 사용 가능(%s). 엔드 버튼 탐색을 계속합니다.", phase)

        rect = ensure_active_rect(logger)
        if detect_champion_select(
            rect, "엔드 이후", tpl_prepick, available_roles, threshold, logger, lcu=lcu
        ):
            return

        clicked_any = False
        if tpl_end_next.exists():
            clicked_any = clicked_any or search_and_act(
                rect, tpl_end_next, threshold=threshold, click=True
            )
        if tpl_end_one_more.exists():
            clicked_any = clicked_any or search_and_act(
                rect, tpl_end_one_more, threshold=threshold, click=True
            )

        accepted, finding = poll_match_state(
            rect,
            "엔드 이후",
            tpl_finding_match,
            tpl_accept,
            threshold,
            confirm_check_interval,
            logger,
            tpl_confirm_templates=tpl_confirm_templates,
            lcu=lcu,
        )

        found_find_match = search_and_act(
            rect, tpl_find_match, threshold=threshold, click=True
        )
        if found_find_match:
            logger.info("엔드 이후 대전 찾기 버튼 클릭 완료.")
            return

        if clicked_any or finding or accepted:
            time.sleep(interval_sec)
            continue

        logger.debug("엔드/대전 버튼 미검출. %.1fs 후 재시도...", interval_sec)
        time.sleep(interval_sec)


def cli_main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="lolmanager-cli", add_help=True)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="DEBUG 로그(템플릿 매칭/ROI 등)를 출력합니다.",
    )
    parser.add_argument(
        "--config-gui",
        action="store_true",
        help="설정(champion/ban/pick_coord/reserve_picks)을 GUI로 편집하고 종료합니다.",
    )
    args, _unknown = parser.parse_known_args(
        (argv if argv is not None else sys.argv[1:])
    )
    log_path = configure_runtime_logging(debug=bool(args.debug))
    install_exception_logger()
    logger = logging.getLogger("lolmanager")
    logger.info("런타임 로그 파일: %s", log_path)
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.debug("DEBUG 로그 활성화.")
    if args.config_gui:
        try:
            from lolmanager.gui.config_gui import run_config_gui
        except Exception as exc:
            logger.error("GUI 실행에 실패했습니다: %s", exc)
            return
        run_config_gui()
        return

    ensure_external_apps_running_once(logger=logger)
    global _LEAGUE_EXIT_GUARD
    _LEAGUE_EXIT_GUARD = LeagueClientExitGuard(league_client_exe_path())
    lcu = LcuClient()

    config = ChampionConfig()

    print("\n=== 현재 챔피언 설정 ===")
    for role in ROLE_ORDER:
        info = config.get(role) or {}
        champ = str(info.get("champion") or "").strip()
        if not champ:
            continue
        ban = str(info.get("ban") or "").strip()
        if ban:
            print(f"[{role}] {champ} (ban: {ban})")
        else:
            print(f"[{role}] {champ}")

    reserve_pick_pools: dict[str, list[tuple[str, str]]] = {}
    print("\n=== 예비용 챔피언(저장값) ===")
    print("예비 챔피언 변경은 콘솔이 아니라 GUI에서만 가능합니다:")
    print("  - uv run lolmanager-config")
    print("  - 또는: uv run python -m lolmanager.gui.config_gui\n")
    for role in ROLE_ORDER:
        info = config.get(role) or {}
        primary = str(info.get("champion") or "").strip()
        primary_ban = str(info.get("ban") or "").strip()
        if not primary:
            continue
        saved_reserves = [
            (c, b)
            for (c, b) in config.get_reserve_picks(role)
            if str(c or "").strip() and str(c or "").strip() != primary
        ][:2]

        _print_pick_pool_summary(role, primary, primary_ban, saved_reserves)

        pool: list[tuple[str, str]] = [(primary, primary_ban), *saved_reserves]
        reserve_pick_pools[role] = pool

    print("\n=== 예비용 챔피언 요약 ===")
    for role in ROLE_ORDER:
        pool = reserve_pick_pools.get(role)
        if not pool:
            continue
        primary, primary_ban = pool[0]
        reserves = pool[1:]
        _print_pick_pool_summary(role, primary, primary_ban, reserves)

    rect = ensure_active_rect(logger)

    width, height = window_size_from_rect(rect)
    selected = select_image_set(width)

    logger.info("클라이언트 크기: %dx%d", width, height)
    if not selected:
        logger.error("images 하위에 사용할 해상도 폴더가 없습니다.")
        return

    tpl_find_match = selected / "lobby_find-match-button.png"
    tpl_finding_match = selected / "lobby_finding-match-text.png"
    tpl_accept = selected / "lobby_accept-button.png"
    tpl_confirm_candidates = (
        selected / "client_confirm-button.png",
        selected / "client_confirm-button-2.png",
    )
    tpl_confirm_templates = [p for p in tpl_confirm_candidates if p.exists()]
    if not tpl_confirm_templates:
        logger.error(
            "확인 버튼 템플릿 파일이 없습니다: %s, %s",
            tpl_confirm_candidates[0],
            tpl_confirm_candidates[1],
        )
        return
    logger.info(
        "확인 버튼 템플릿 %d개 감시 활성화: %s",
        len(tpl_confirm_templates),
        ", ".join(p.name for p in tpl_confirm_templates),
    )
    tpl_prepick = selected / "prepick_search-text.png"
    tpl_banpick_search = selected / "banpick_search-text.png"
    tpl_banpick_wait = selected / "banpick_wait-text.png"
    tpl_ban_button = selected / "banpick_ban-button.png"
    tpl_pick_ready = selected / "pick_ready-button.png"
    tpl_pick_decline = selected / "pick_decline-button.png"
    tpl_pick_myturn = selected / "pick_myturn-text.png"
    tpl_pick_disable_ready = selected / "pick_disable-reday-button.png"
    tpl_end_next = selected / "end_next-button.png"
    tpl_end_one_more = selected / "end_one-more-button.png"
    role_templates = [
        ("top", selected / "pick_position_top_text.png"),
        ("jungle", selected / "pick_position_jungle_text.png"),
        ("mid", selected / "pick_position_mid_text.png"),
        ("adc", selected / "pick_position_adc_text.png"),
        ("support", selected / "pick_position_support_text.png"),
    ]

    for tpl in (
        tpl_find_match,
        tpl_finding_match,
        tpl_accept,
        tpl_prepick,
        tpl_banpick_search,
        tpl_ban_button,
        tpl_pick_ready,
    ):
        if not tpl.exists():
            logger.error("템플릿 파일이 없습니다: %s", tpl)
            return

    tpl_pick_decline_opt: Optional[Path] = (
        tpl_pick_decline if tpl_pick_decline.exists() else None
    )
    if tpl_pick_decline_opt:
        logger.info("픽 단계 '거절' 버튼 감시 활성화: %s", tpl_pick_decline_opt.name)
    else:
        logger.warning(
            "픽 단계 '거절' 템플릿이 없습니다: %s (자동 클릭 비활성)",
            tpl_pick_decline,
        )

    tpl_pick_myturn_opt: Optional[Path] = (
        tpl_pick_myturn if tpl_pick_myturn.exists() else None
    )
    if tpl_pick_myturn_opt:
        logger.info("내 픽 차례 텍스트 감시 활성화: %s", tpl_pick_myturn_opt.name)
    else:
        logger.warning(
            "내 픽 차례 템플릿이 없습니다(상태 감지 비활성): %s", tpl_pick_myturn
        )

    tpl_pick_disable_ready_opt: Optional[Path] = (
        tpl_pick_disable_ready if tpl_pick_disable_ready.exists() else None
    )
    if tpl_pick_disable_ready_opt:
        logger.info(
            "픽 준비 비활성 버튼 감시 활성화: %s", tpl_pick_disable_ready_opt.name
        )
    else:
        logger.warning(
            "픽 준비 비활성 템플릿이 없습니다(밴 감지/예비 전환 비활성): %s",
            tpl_pick_disable_ready,
        )

    tpl_banpick_wait_opt: Optional[Path] = (
        tpl_banpick_wait if tpl_banpick_wait.exists() else None
    )
    if tpl_banpick_wait_opt:
        logger.info("밴픽 대기 텍스트 감시 활성화: %s", tpl_banpick_wait_opt.name)
    else:
        logger.warning(
            "밴픽 대기 텍스트 템플릿이 없습니다(밴픽→픽 상태 전이 개선 비활성): %s",
            tpl_banpick_wait,
        )

    available_roles = [(role, path) for role, path in role_templates if path.exists()]
    if not available_roles:
        logger.warning("포지션 템플릿이 없습니다. (role detection skipped)")

    interval_sec = 1.0
    threshold = 0.85
    confirm_check_interval = 0.2

    while True:
        _set_client_state(ClientState.LOBBY, time.monotonic(), logger)
        accepted_early = False
        finding_early = False

        logger.info("대전 찾기 버튼 탐색 시작.")
        while True:
            accepted, finding = poll_match_state(
                None,
                "사이클 진입",
                tpl_finding_match,
                tpl_accept,
                threshold,
                confirm_check_interval,
                logger,
                tpl_confirm_templates=tpl_confirm_templates,
                lcu=lcu,
            )
            if accepted:
                logger.info("LCU 매칭 수락 처리 완료(사이클 진입). 수락 단계로 이동합니다.")
                accepted_early = True
                break
            if finding:
                logger.info("LCU 매칭 중 감지(사이클 진입). 화면 캡처를 건너뜁니다.")
                finding_early = True
                break

            rect = ensure_active_rect(logger)
            if detect_champion_select(
                rect,
                "사이클 진입",
                tpl_prepick,
                available_roles,
                threshold,
                logger,
                lcu=lcu,
            ):
                accepted_early = True
                break

            accepted, finding = poll_match_state(
                rect,
                "사이클 진입",
                tpl_finding_match,
                tpl_accept,
                threshold,
                confirm_check_interval,
                logger,
                tpl_confirm_templates=tpl_confirm_templates,
                lcu=lcu,
            )
            if accepted:
                logger.info("이미 매칭 수락 감지(사이클 진입). 수락 단계로 이동합니다.")
                accepted_early = True
                break
            if finding:
                logger.info("이미 매칭 중 감지(사이클 진입). 수락 감시로 전환합니다.")
                finding_early = True
                break

            success = search_and_act(
                rect, tpl_find_match, threshold=threshold, click=True
            )
            if success:
                now = time.monotonic()
                _reset_match_timer_by_find_match_click(now, logger)
                logger.info("대전 찾기 버튼 클릭 완료.")
                break
            logger.debug("대전 찾기 버튼 미검출. %.1fs 후 재시도...", interval_sec)
            time.sleep(interval_sec)

        if not accepted_early:
            last_finding_state = finding_early if finding_early else None
            while True:
                accepted, finding = poll_match_state(
                    None,
                    "매칭 단계",
                    tpl_finding_match,
                    tpl_accept,
                    threshold,
                    confirm_check_interval,
                    logger,
                    tpl_confirm_templates=tpl_confirm_templates,
                    lcu=lcu,
                )
                if accepted:
                    break
                if finding:
                    if finding != last_finding_state:
                        logger.info("매칭 상태 텍스트: 감지")
                        last_finding_state = finding
                    continue

                rect = ensure_active_rect(logger)
                accepted, finding = poll_match_state(
                    rect,
                    "매칭 단계",
                    tpl_finding_match,
                    tpl_accept,
                    threshold,
                    confirm_check_interval,
                    logger,
                    tpl_confirm_templates=tpl_confirm_templates,
                    lcu=lcu,
                )
                if finding != last_finding_state:
                    logger.info("매칭 상태 텍스트: %s", "감지" if finding else "미감지")
                    last_finding_state = finding
                if accepted:
                    break
                if finding:
                    continue
                if detect_champion_select(
                    rect,
                    "매칭 단계",
                    tpl_prepick,
                    available_roles,
                    threshold,
                    logger,
                    lcu=lcu,
                ):
                    break
                clicked = search_and_act(
                    rect, tpl_find_match, threshold=threshold, click=True
                )
                if clicked:
                    now = time.monotonic()
                    _reset_match_timer_by_find_match_click(now, logger)
                    logger.info("대전 찾기 버튼 재클릭.")
                else:
                    logger.debug(
                        "대전 찾기 버튼 미검출. %.1fs 후 재시도...", interval_sec
                    )
                time.sleep(interval_sec)

        if available_roles:
            _set_client_state(ClientState.PREPICK, time.monotonic(), logger)
            logger.info("포지션 텍스트 탐색 시작.")
            role_found = None
            last_role = None
            stable_hits = 0
            while not role_found:
                rect = ensure_active_rect(logger)
                try_pick_popups(
                    rect,
                    tpl_confirm_templates,
                    tpl_pick_decline_opt,
                    threshold,
                    logger,
                    tpl_pick_myturn=tpl_pick_myturn_opt,
                )
                accepted, finding = poll_match_state(
                    rect,
                    "포지션 탐색 중",
                    tpl_finding_match,
                    tpl_accept,
                    threshold,
                    confirm_check_interval,
                    logger,
                    lcu=lcu,
                )
                if accepted:
                    time.sleep(confirm_check_interval)
                    continue
                if finding:
                    continue

                best = find_best_template(rect, available_roles, threshold=threshold)
                if best:
                    detected_role, best_score, second_score = best
                    if detected_role == last_role:
                        stable_hits += 1
                    else:
                        last_role = detected_role
                        stable_hits = 1

                    logger.debug(
                        "포지션 후보: %s (score=%.3f, second=%.3f, hit=%d)",
                        detected_role,
                        best_score,
                        second_score,
                        stable_hits,
                    )

                    if stable_hits >= 2:
                        role_found = detected_role
                        logger.info(
                            "포지션 감지: %s (score=%.3f)", role_found, best_score
                        )
                        break
                else:
                    last_role = None
                    stable_hits = 0

                logger.debug("포지션 미검출. %.1fs 후 재시도...", interval_sec)
                time.sleep(interval_sec)

        if available_roles and role_found:
            role = role_found
        else:
            role = None

        champ_info = None
        if role:
            champ_info = config.get(role)
            if not champ_info:
                logger.warning(
                    "포지션 %s의 챔피언 설정이 없습니다. GUI에서 설정 후 다시 실행하세요(lolmanager-config).",
                    role,
                )
        else:
            logger.warning("포지션을 알 수 없어 챔피언 설정을 건너뜁니다.")

        if champ_info:
            champion_raw = champ_info.get("champion")
            if isinstance(champion_raw, (list, tuple)):
                champion_name = str(champion_raw[0]) if champion_raw else ""
            else:
                champion_name = str(champion_raw)
            pick_coord = champ_info.get("pick_coord") or DEFAULT_PICK_COORD
            ban_name = champ_info.get("ban")

            pick_pool = reserve_pick_pools.get(role, []) if role else []
            if not pick_pool:
                pick_pool = [(champion_name, str(ban_name or "").strip())]

            pick_index = 0
            champion_name = pick_pool[0][0]
            ban_name = pick_pool[0][1]

            restart_cycle = False

            while True:
                rect = ensure_active_rect(logger)
                try_pick_popups(
                    rect,
                    tpl_confirm_templates,
                    tpl_pick_decline_opt,
                    threshold,
                    logger,
                    tpl_pick_myturn=tpl_pick_myturn_opt,
                )
                if detect_match_reset(
                    rect,
                    "챔피언 선택",
                    tpl_find_match,
                    tpl_finding_match,
                    tpl_accept,
                    threshold,
                    confirm_check_interval,
                    logger,
                ):
                    restart_cycle = True
                    break
                clicked = search_and_act(
                    rect, tpl_prepick, threshold=threshold, click=True
                )
                if clicked:
                    _set_client_state(ClientState.PREPICK, time.monotonic(), logger)
                    logger.info("프리픽 검색창 클릭 완료.")
                    break
                logger.debug("프리픽 검색창 미검출. %.1fs 후 재시도...", interval_sec)
                time.sleep(interval_sec)

            if restart_cycle:
                continue

            rect = ensure_active_rect(logger)
            typed = search_and_act(
                rect,
                tpl_prepick,
                threshold=threshold,
                click=False,
                keys=champion_name,
                post_input_sleep=0.2,
            )
            if not typed:
                logger.warning(
                    "챔피언 검색 입력 실패(검색창/입력 문제). 사이클을 재시도합니다."
                )
                continue
            _set_client_state(ClientState.PREPICK, time.monotonic(), logger)
            logger.info("챔피언 검색 입력: %s", champion_name)

            if pick_coord:
                time.sleep(0.1)
                try:
                    if RUNTIME_STATE.get("client_state") != ClientState.PREPICK:
                        logger.warning(
                            "프리픽 상태가 아니므로 좌표 클릭을 스킵합니다: client_state=%s",
                            RUNTIME_STATE.get("client_state"),
                        )
                        restart_cycle = True
                    else:
                        click_relative(rect, (pick_coord[0], pick_coord[1]))
                except Exception as exc:
                    logger.warning("챔피언 선택 좌표 클릭 실패: %s", exc)
                    continue
                if restart_cycle:
                    continue
                logger.info("챔피언 선택 좌표 클릭: %s", pick_coord)
            else:
                logger.info("좌표가 설정되지 않았습니다. 수동으로 챔피언을 선택하세요.")

            tpl_banpick_search = selected / "banpick_search-text.png"
            tpl_ban_button = selected / "banpick_ban-button.png"

            if not tpl_banpick_search.exists():
                logger.warning("밴 검색 템플릿이 없습니다: %s", tpl_banpick_search)
                return

            if not ban_name:
                logger.warning("밴 챔피언이 설정되지 않았습니다. 밴 단계 건너뜀.")
                return

            logger.info("밴 챔피언 검색 시작: %s", ban_name)
            while True:
                rect = ensure_active_rect(logger)
                try_pick_popups(
                    rect,
                    tpl_confirm_templates,
                    tpl_pick_decline_opt,
                    threshold,
                    logger,
                    tpl_pick_myturn=tpl_pick_myturn_opt,
                )
                if detect_match_reset(
                    rect,
                    "밴 검색",
                    tpl_find_match,
                    tpl_finding_match,
                    tpl_accept,
                    threshold,
                    confirm_check_interval,
                    logger,
                ):
                    restart_cycle = True
                    break
                found = search_and_act(
                    rect, tpl_banpick_search, threshold=threshold, click=True
                )
                if found:
                    _set_client_state(ClientState.BANPICK, time.monotonic(), logger)
                    logger.info("밴 검색창 클릭 완료.")
                    break
                logger.debug("밴 검색창 미검출. %.1fs 후 재시도...", interval_sec)
                time.sleep(interval_sec)

            if restart_cycle:
                continue

            rect = ensure_active_rect(logger)
            typed = search_and_act(
                rect,
                tpl_banpick_search,
                threshold=threshold,
                click=False,
                keys=ban_name,
                post_input_sleep=0.2,
            )
            if not typed:
                logger.warning(
                    "밴 챔피언 입력 실패(검색창/입력 문제). 사이클을 재시도합니다."
                )
                continue
            _set_client_state(ClientState.BANPICK, time.monotonic(), logger)
            logger.info("밴 챔피언 입력: %s", ban_name)

            time.sleep(0.1)
            try:
                if RUNTIME_STATE.get("client_state") != ClientState.BANPICK:
                    logger.warning(
                        "밴픽 상태가 아니므로 좌표 클릭을 스킵합니다: client_state=%s",
                        RUNTIME_STATE.get("client_state"),
                    )
                    restart_cycle = True
                else:
                    click_relative(rect, (pick_coord[0], pick_coord[1]))
            except Exception as exc:
                logger.warning("밴 챔피언 선택 좌표 클릭 실패: %s", exc)
                continue
            if restart_cycle:
                continue
            logger.info("밴 챔피언 선택 좌표 클릭: %s", pick_coord)

            while tpl_ban_button.exists():
                rect = ensure_active_rect(logger)
                try_pick_popups(
                    rect,
                    tpl_confirm_templates,
                    tpl_pick_decline_opt,
                    threshold,
                    logger,
                    tpl_pick_myturn=tpl_pick_myturn_opt,
                )
                if detect_match_reset(
                    rect,
                    "밴 버튼 대기",
                    tpl_find_match,
                    tpl_finding_match,
                    tpl_accept,
                    threshold,
                    confirm_check_interval,
                    logger,
                ):
                    restart_cycle = True
                    break
                clicked = search_and_act(
                    rect, tpl_ban_button, threshold=threshold, click=True
                )
                if clicked:
                    logger.info("밴 버튼 클릭 완료.")
                    break
                logger.debug("밴 버튼 미검출. %.1fs 후 재시도...", interval_sec)
                time.sleep(interval_sec)

            if restart_cycle:
                continue

            if tpl_banpick_wait_opt is None:
                _set_client_state(ClientState.PICK, time.monotonic(), logger)

            disabled_ready_signal_streak = 0
            while True:
                rect = ensure_active_rect(logger)
                try_pick_popups(
                    rect,
                    tpl_confirm_templates,
                    tpl_pick_decline_opt,
                    threshold,
                    logger,
                    tpl_pick_myturn=tpl_pick_myturn_opt,
                )
                if detect_match_reset(
                    rect,
                    "픽 준비",
                    tpl_find_match,
                    tpl_finding_match,
                    tpl_accept,
                    threshold,
                    confirm_check_interval,
                    logger,
                ):
                    restart_cycle = True
                    break

                is_my_turn = bool(RUNTIME_STATE.get("is_my_pick_turn", False))
                now = time.monotonic()

                myturn_known = tpl_pick_myturn_opt is not None

                state_templates: list[tuple[str, Path]] = [
                    ("ready", tpl_pick_ready),
                    ("disabled_ready", tpl_pick_disable_ready),
                ]
                if tpl_banpick_wait_opt is not None:
                    state_templates.append(("banpick_wait", tpl_banpick_wait_opt))

                matches = find_template_matches_once(
                    rect, state_templates, threshold=threshold
                )
                disabled_hit = matches.get("disabled_ready") if matches else None
                ready_hit = matches.get("ready") if matches else None
                wait_hit = (
                    matches.get("banpick_wait")
                    if (tpl_banpick_wait_opt is not None and matches)
                    else None
                )

                if tpl_banpick_wait_opt is not None:
                    if wait_hit is not None and ready_hit is None and disabled_hit is None:
                        _set_client_state(ClientState.BANPICK, now, logger)
                        _set_my_pick_turn(False, now, logger)
                        time.sleep(interval_sec)
                        continue
                    _set_client_state(ClientState.PICK, now, logger)

                if myturn_known and not is_my_turn:
                    logger.debug(
                        "내 픽 차례가 아니므로 픽 준비 버튼 클릭을 스킵합니다."
                    )
                    time.sleep(interval_sec)
                    continue

                READY_DISABLED_STRONG_SCORE = 0.90
                MYTURN_DISABLE_GRACE_SEC = 0.80
                REQUIRED_DISABLED_STREAK = 2

                ready_is_gray = False
                ready_score = 0.0
                if ready_hit is not None:
                    _center, roi_bgr, _score = ready_hit
                    ready_score = float(_score)
                    ready_is_gray = is_probably_disabled_gray_button(roi_bgr)

                disabled_is_gray = False
                disabled_score = 0.0
                if disabled_hit is not None:
                    _dcenter, droi_bgr, _dscore = disabled_hit
                    disabled_score = float(_dscore)

                    disabled_is_gray = is_probably_disabled_gray_button(droi_bgr)

                ready_click_hit = None
                if ready_hit is not None and not ready_is_gray:
                    ready_click_hit = ready_hit
                elif (
                    ready_hit is None
                    and disabled_hit is not None
                    and not disabled_is_gray
                    and disabled_score
                    >= max(READY_DISABLED_STRONG_SCORE, threshold + 0.05)
                ):
                    ready_click_hit = disabled_hit

                disabled_strong = bool(
                    disabled_hit is not None
                    and disabled_is_gray
                    and disabled_score
                    >= max(READY_DISABLED_STRONG_SCORE, threshold + 0.03)
                )
                disabled_weak = bool(ready_hit is not None and ready_is_gray)

                disabled_signal = bool(
                    is_my_turn
                    and ready_click_hit is None
                    and (disabled_strong or disabled_weak)
                )
                if disabled_signal:
                    disabled_ready_signal_streak += 1
                    myturn_age = now - float(
                        RUNTIME_STATE.get("my_pick_turn_updated_at", 0.0) or 0.0
                    )

                    if myturn_age < MYTURN_DISABLE_GRACE_SEC:
                        logger.debug(
                            "내 픽 차례 직후(%.2fs) Ready 비활성 신호 감지(강=%s,score=%.3f/%.3f). 안정화 대기.",
                            myturn_age,
                            "Y" if disabled_strong else "N",
                            ready_score,
                            disabled_score,
                        )
                        time.sleep(interval_sec)
                        continue

                    required = 1 if disabled_strong else REQUIRED_DISABLED_STREAK
                    if disabled_ready_signal_streak < required:
                        logger.debug(
                            "Ready 비활성 신호 연속 감지 중(%d/%d, 강=%s,score=%.3f/%.3f).",
                            disabled_ready_signal_streak,
                            required,
                            "Y" if disabled_strong else "N",
                            ready_score,
                            disabled_score,
                        )
                        time.sleep(interval_sec)
                        continue

                    if disabled_strong:
                        logger.info(
                            "내 픽 차례인데 픽 준비 버튼이 비활성(회색)으로 감지되었습니다(score=%.3f). "
                            "미리 픽한 챔피언이 밴된 것으로 판단하고 예비 픽을 시도합니다.",
                            disabled_score,
                        )
                    else:
                        logger.info(
                            "내 픽 차례인데 픽 준비 버튼이 회색으로 감지되었습니다. "
                            "챔피언 미선택/밴 상태로 판단하고 예비 픽을 시도합니다."
                        )

                    switched = False
                    for next_idx in range(pick_index + 1, len(pick_pool)):
                        next_champ, next_ban = pick_pool[next_idx]
                        logger.info(
                            "예비 챔피언 전환 시도: #%d %s", next_idx + 1, next_champ
                        )

                        rect2 = ensure_active_rect(logger)
                        try_pick_popups(
                            rect2,
                            tpl_confirm_templates,
                            tpl_pick_decline_opt,
                            threshold,
                            logger,
                            tpl_pick_myturn=tpl_pick_myturn_opt,
                        )
                        typed_ok = search_and_act(
                            rect2,
                            tpl_prepick,
                            threshold=threshold,
                            click=True,
                            keys=next_champ,
                            post_input_sleep=0.2,
                        )
                        if not typed_ok:
                            logger.warning("예비 챔피언 검색 입력 실패: %s", next_champ)
                            continue

                        time.sleep(0.1)
                        try:
                            click_relative(rect2, (pick_coord[0], pick_coord[1]))
                        except Exception as exc:
                            logger.warning("예비 챔피언 선택 좌표 클릭 실패: %s", exc)
                            continue

                        time.sleep(0.15)
                        rect3 = ensure_active_rect(logger)
                        matches2 = find_template_matches_once(
                            rect3,
                            [
                                ("ready", tpl_pick_ready),
                                ("disabled_ready", tpl_pick_disable_ready),
                            ],
                            threshold=threshold,
                        )
                        disabled2 = matches2.get("disabled_ready") if matches2 else None
                        ready2 = matches2.get("ready") if matches2 else None

                        ready2_is_gray = False
                        if ready2 is not None:
                            roi2 = ready2[1]
                            ready2_is_gray = is_probably_disabled_gray_button(roi2)

                        disabled2_is_gray = False
                        disabled2_score = 0.0
                        if disabled2 is not None:
                            _dc2, droi2, _ds2 = disabled2
                            disabled2_score = float(_ds2)
                            disabled2_is_gray = is_probably_disabled_gray_button(droi2)

                        ready2_click = None
                        if ready2 is not None and not ready2_is_gray:
                            ready2_click = ready2
                        elif (
                            ready2 is None
                            and disabled2 is not None
                            and not disabled2_is_gray
                            and disabled2_score
                            >= max(READY_DISABLED_STRONG_SCORE, threshold + 0.05)
                        ):
                            ready2_click = disabled2

                        if ready2_click is None:
                            if (
                                disabled2 is not None
                                and disabled2_is_gray
                                and disabled2_score
                                >= max(READY_DISABLED_STRONG_SCORE, threshold + 0.03)
                            ):
                                logger.info(
                                    "예비 챔피언도 비활성 Ready로 감지되었습니다(회색,score=%.3f, 밴/미선택으로 판단): %s",
                                    disabled2_score,
                                    next_champ,
                                )
                            elif ready2 is not None and ready2_is_gray:
                                logger.info(
                                    "예비 전환 후 Ready가 회색입니다(밴/미선택으로 판단): %s",
                                    next_champ,
                                )
                            else:
                                logger.debug(
                                    "예비 전환 후 Ready 미확정(미검출/저신뢰). 다음 후보로 진행: %s",
                                    next_champ,
                                )
                            continue

                        center2, _roi2, _score2 = ready2_click
                        try:
                            click_screen(center2)
                        except Exception as exc:
                            logger.warning("픽 준비 버튼 클릭 실패(예비 전환): %s", exc)
                            continue

                        pick_index = next_idx
                        champion_name = next_champ
                        ban_name = next_ban
                        logger.info(
                            "픽 준비 버튼 클릭 완료(예비 전환): %s", champion_name
                        )
                        _set_my_pick_turn(False, time.monotonic(), logger)
                        switched = True
                        break

                    if switched:
                        break

                    logger.warning(
                        "예비 챔피언 전환에 실패했습니다. 수동 픽이 필요합니다."
                    )
                    time.sleep(interval_sec)
                    continue

                disabled_ready_signal_streak = 0

                if ready_click_hit is not None:
                    center, _roi_bgr, _score = ready_click_hit
                    try:
                        click_screen(center)
                    except Exception as exc:
                        logger.warning("픽 준비 버튼 클릭 실패: %s", exc)
                    else:
                        logger.info("픽 준비 버튼 클릭 완료.")
                        _set_my_pick_turn(False, time.monotonic(), logger)
                        break
                else:
                    if disabled_hit is not None and not disabled_is_gray:
                        logger.debug(
                            "disabled_ready 템플릿 매칭은 있었지만 ROI가 회색이 아니어서 비활성으로 확정하지 않음(score=%.3f).",
                            disabled_score,
                        )
                    logger.debug(
                        "픽 준비 버튼 미검출. %.1fs 후 재시도...", interval_sec
                    )

                time.sleep(interval_sec)

            if restart_cycle:
                _set_my_pick_turn(False, time.monotonic(), logger)
                continue

            logger.info("인게임 시작 대기 중(픽 팝업/거절 감시 포함).")
            _set_client_state(ClientState.WAIT_GAME_START, time.monotonic(), logger)
            wait_iter = 0
            last_wait_state = None
            while True:
                if _LEAGUE_EXIT_GUARD is not None and _LEAGUE_EXIT_GUARD.should_exit():
                    logger.info("LeagueClient.exe 종료 감지. lolmanager를 종료합니다.")
                    raise SystemExit(0)
                phase = _poll_lcu_phase(lcu, logger, "인게임 시작 대기", max_age_sec=0.5)
                if phase == PHASE_IN_PROGRESS:
                    logger.info("LCU 인게임 시작 감지. 게임 종료 감시로 전환합니다.")
                    _set_client_state(ClientState.INGAME, time.monotonic(), logger)
                    break
                if phase in {PHASE_LOBBY, PHASE_MATCHMAKING, PHASE_READY_CHECK}:
                    if phase == PHASE_READY_CHECK:
                        _accept_ready_check_via_lcu(lcu, "인게임 시작 대기", logger)
                    logger.info(
                        "LCU 상태가 인게임 대기 밖으로 전환되었습니다(phase=%s). 사이클을 재시작합니다.",
                        phase,
                    )
                    restart_cycle = True
                    break
                if is_game_client_active():
                    _set_client_state(ClientState.INGAME, time.monotonic(), logger)
                    break
                rect = find_league_window_rect()
                if rect and not is_rect_minimized(rect):
                    last_wait_state = "visible"
                    try_pick_popups(
                        rect,
                        tpl_confirm_templates,
                        tpl_pick_decline_opt,
                        threshold,
                        logger,
                        tpl_pick_myturn=tpl_pick_myturn_opt,
                    )

                    wait_iter += 1
                    if wait_iter % 10 == 0:
                        if search_and_act(
                            rect, tpl_find_match, threshold=threshold, click=False
                        ):
                            _set_client_state(ClientState.LOBBY, time.monotonic(), logger)
                            logger.info(
                                "대전 찾기 화면 복귀 감지(인게임 시작 대기). 사이클을 재시작합니다."
                            )
                            restart_cycle = True
                            break
                else:
                    state = "missing" if not rect else "minimized"
                    if last_wait_state != state:
                        if state == "minimized":
                            logger.info(
                                "LoL 창이 최소화/비가시 상태입니다(인게임 시작 대기). 인게임 감지를 계속 진행합니다."
                            )
                        else:
                            logger.info(
                                "LoL 창 좌표를 찾지 못했습니다(인게임 시작 대기). 인게임 감지를 계속 진행합니다."
                            )
                        last_wait_state = state
                time.sleep(0.3)

            if restart_cycle:
                continue

            logger.info("인게임 상태 감시 시작. 게임 종료까지 대기합니다.")
            skip_postgame = False
            while True:
                phase = _poll_lcu_phase(lcu, logger, "인게임 감시", max_age_sec=1.0)
                if phase in {
                    PHASE_WAITING_FOR_STATS,
                    PHASE_PRE_END_OF_GAME,
                    PHASE_END_OF_GAME,
                }:
                    logger.info("LCU 게임 종료 단계 감지: phase=%s", phase)
                    break
                if phase in {PHASE_LOBBY, PHASE_MATCHMAKING, PHASE_READY_CHECK, PHASE_CHAMP_SELECT}:
                    logger.info(
                        "LCU 상태가 인게임 이후 단계로 전환되었습니다(phase=%s). postgame 화면 처리를 건너뜁니다.",
                        phase,
                    )
                    if phase == PHASE_READY_CHECK:
                        _accept_ready_check_via_lcu(lcu, "인게임 감시", logger)
                    skip_postgame = True
                    break
                if is_game_client_active():
                    _set_client_state(ClientState.INGAME, time.monotonic(), logger)
                    time.sleep(1.0)
                    continue
                break

            if skip_postgame:
                continue

            logger.info("엔드 화면 처리 시작.")
            for tpl in (tpl_end_next, tpl_end_one_more):
                if not tpl.exists():
                    logger.warning("엔드 버튼 템플릿이 없습니다: %s", tpl)
            process_postgame(
                tpl_end_next,
                tpl_end_one_more,
                tpl_find_match,
                tpl_finding_match,
                tpl_accept,
                tpl_confirm_templates,
                tpl_prepick,
                available_roles,
                threshold,
                confirm_check_interval,
                interval_sec,
                logger,
                lcu=lcu,
            )

        logger.info("다음 매칭 사이클을 시작합니다.")


def main(argv: Optional[list[str]] = None) -> None:
    argv_list = list(argv) if argv is not None else sys.argv[1:]
    if argv_list:
        cli_argv = [a for a in argv_list if a != "--cli"]
        cli_main(cli_argv)
        return

    try:
        ensure_external_apps_running_once(logger=logging.getLogger("lolmanager"))
    except Exception:
        pass

    from lolmanager.gui.app_gui import main as run_app_gui

    run_app_gui()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.getLogger("lolmanager").info("인터럽트 감지, 스크립트 종료.")
