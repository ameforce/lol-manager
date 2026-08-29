from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence, cast

from requests import RequestException

if __package__ in (None, ""):
    try:
        _src = Path(__file__).resolve().parents[2]  # .../src
        if (_src / "lolmanager" / "__init__.py").exists():
            sys.path.insert(0, str(_src))
    except Exception:
        pass

from lolmanager.platform.external_apps import (
    LeagueClientExitGuard,
    close_running_opgg,
    ensure_external_apps_running_once,
    league_client_exe_path,
)
from lolmanager.core.match_timing import append_match_duration, format_duration_mmss
from lolmanager.core.gui_preferences import load_continue_after_game_preference
from lolmanager.core.lcu_client import (
    LcuClient,
    LcuLoopAction,
    PHASE_CHAMP_SELECT,
    PHASE_END_OF_GAME,
    PHASE_IN_PROGRESS,
    PHASE_LOBBY,
    PHASE_MATCHMAKING,
    PHASE_NONE,
    PHASE_PRE_END_OF_GAME,
    PHASE_READY_CHECK,
    PHASE_RECONNECT,
    PHASE_WAITING_FOR_STATS,
    PHASE_WATCH_IN_PROGRESS,
    champ_select_session_identity,
    champ_select_time_left_seconds,
    completed_champ_select_champion_ids,
    is_known_gameflow_phase,
    lcu_loop_action_for,
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
    fetch_counter_matchups_from_detail,
    fetch_counters_from_detail,
    sort_counter_candidates_by_role_rank,
)
from lolmanager.core.opgg_counter_recommendations import (
    AUTO_BAN_LABEL,
    DEFAULT_MAX_AGE_SEC as COUNTER_RECOMMENDATION_MAX_AGE_SEC,
    build_label_name_map,
    build_recommendations,
    default_counter_cache_path,
    is_auto_ban_value,
    load_recommendation_cache,
)
from lolmanager.cli.runtime_state import (
    ClientState,
    LCU_UI_ACTION_CLASSIFICATION as LCU_UI_ACTION_CLASSIFICATION,
    POSTGAME_PHASES,
    client_state_from_lcu_phase,
    should_preserve_champ_select_state,
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
DEFAULT_LEAGUE_WINDOW_LOOKUP_TIMEOUT_SEC = 30.0
DEFAULT_IMAGE_SET_WIDTH = 1280
DEFAULT_IMAGE_SET_HEIGHT = 720
DEFAULT_CONTINUE_AFTER_GAME = False
BAN_COMPLETION_TARGET_SEC = 10.0
# Two normal 2-second LCU confirmation waits plus request latency must finish
# before the target. Start at 15 seconds remaining instead of gambling at 4.
BAN_COMMIT_WINDOW_SEC = 15.0

ROLE_ORDER: tuple[str, ...] = ("top", "jungle", "mid", "adc", "support")


def should_continue_after_game(value: object) -> bool:
    """The explicit UI/CLI opt-in is the only way to requeue after a match."""
    if callable(value):
        value = value()
    return bool(value)


class ContinueAfterGamePolicy:
    """Read the GUI preference at each game-boundary decision."""

    def __init__(
        self,
        *,
        initial_value: object,
        preference_path: Optional[Path] = None,
    ) -> None:
        self.preference_path = Path(preference_path) if preference_path else None
        self._last_value = bool(initial_value)

    def current(self, logger: Optional[logging.Logger] = None) -> bool:
        if self.preference_path is None:
            return self._last_value
        saved = load_continue_after_game_preference(self.preference_path)
        if saved is None:
            return self._last_value
        if saved != self._last_value and logger is not None:
            logger.info(
                "다음 게임 자동 진행 설정 변경 감지: %s",
                "활성" if saved else "비활성(한 게임 모드)",
            )
        self._last_value = saved
        return self._last_value


class LeagueWindowLookupTimeout(RuntimeError):
    def __init__(self, state: str, timeout_sec: float) -> None:
        self.state = state
        self.timeout_sec = timeout_sec
        super().__init__(
            "League window lookup timed out "
            f"after {timeout_sec:.1f}s while window was {state}"
        )


class LeagueWindowVisibility(Enum):
    VISIBLE = "visible"
    MINIMIZED = "minimized"
    MISSING = "missing"


@dataclass(frozen=True)
class LeagueWindowVisibilitySnapshot:
    state: LeagueWindowVisibility
    rect: Optional[tuple[int, int, int, int]] = None


_last_restore_wait_log_state: dict[str, LeagueWindowVisibility] = {}


def _exit_after_league_client_closed(logger: logging.Logger) -> None:
    logger.info("LeagueClient.exe 종료 감지. lolmanager를 종료합니다.")
    close_running_opgg(logger=logger)
    raise SystemExit(0)


def get_league_window_visibility() -> LeagueWindowVisibilitySnapshot:
    rect = find_league_window_rect()
    if rect is None:
        return LeagueWindowVisibilitySnapshot(LeagueWindowVisibility.MISSING)
    if is_rect_minimized(rect):
        return LeagueWindowVisibilitySnapshot(LeagueWindowVisibility.MINIMIZED)
    return LeagueWindowVisibilitySnapshot(LeagueWindowVisibility.VISIBLE, rect)


def _log_client_restore_wait(
    logger: logging.Logger,
    stage: str,
    snapshot: LeagueWindowVisibilitySnapshot,
) -> None:
    previous = _last_restore_wait_log_state.get(stage)
    if previous == snapshot.state:
        return
    _last_restore_wait_log_state[stage] = snapshot.state
    if snapshot.state == LeagueWindowVisibility.MINIMIZED:
        logger.info(
            "LoL 창이 최소화/비가시 상태입니다(%s). 클라이언트 복원 대기 중...",
            stage,
        )
    elif snapshot.state == LeagueWindowVisibility.MISSING:
        logger.info(
            "LoL 창 좌표를 찾지 못했습니다(%s). 클라이언트 복원 대기 중...",
            stage,
        )


def _visible_rect_or_wait(
    logger: logging.Logger,
    stage: str,
    interval_sec: float,
) -> Optional[tuple[int, int, int, int]]:
    if _LEAGUE_EXIT_GUARD is not None and _LEAGUE_EXIT_GUARD.should_exit():
        _exit_after_league_client_closed(logger)

    rect = _visible_rect_for_image_scan(logger, stage)
    if rect is not None:
        return rect

    time.sleep(max(0.0, float(interval_sec)))
    return None


def _visible_rect_for_image_scan(
    logger: logging.Logger,
    stage: str,
) -> Optional[tuple[int, int, int, int]]:
    snapshot = get_league_window_visibility()
    if snapshot.state == LeagueWindowVisibility.VISIBLE:
        _last_restore_wait_log_state.pop(stage, None)
        return snapshot.rect

    _log_client_restore_wait(logger, stage, snapshot)
    return None


def display_ban_name_for_summary(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return AUTO_BAN_LABEL if is_auto_ban_value(raw) else raw


def _should_process_postgame_at_cycle(phase: object) -> bool:
    return phase in POSTGAME_PHASES


def resolve_ban_name_for_runtime(
    cache_path: Path,
    *,
    role: str,
    champion_name: str,
    configured_ban: object,
    logger: logging.Logger,
    max_age_sec: float = COUNTER_RECOMMENDATION_MAX_AGE_SEC,
    now: Optional[float] = None,
) -> str:
    ban_name = str(configured_ban or "").strip()
    if not is_auto_ban_value(ban_name):
        return ban_name

    result = load_recommendation_cache(
        cache_path,
        role=role,
        configured_pick=champion_name,
        max_age_sec=max_age_sec,
        now=now,
    )
    if result.recommendations:
        selected = str(result.recommendations[0].champion or "").strip()
        if selected:
            logger.info(
                "자동 추천 밴 적용: %s -> %s (%s)",
                champion_name,
                selected,
                result.status,
            )
            return selected

    logger.warning(
        "자동 추천 밴 후보가 없습니다: role=%s champion=%s status=%s",
        role,
        champion_name,
        result.status,
    )
    return ""


_last_finding_logged: dict[str, bool] = {}
_last_accept_click_at: dict[str, float] = {}
_last_lcu_ready_accept_at: dict[str, float] = {}
ACCEPT_CLICK_COOLDOWN_SEC = 0.8
LCU_READY_ACCEPT_COOLDOWN_SEC = 0.8
MATCHMAKING_START_CONFIRM_GRACE_SEC = 1.0


RUNTIME_STATE: dict[str, object] = {
    "client_state": ClientState.UNKNOWN,
    "client_state_updated_at": 0.0,
    "is_my_pick_turn": False,
    "my_pick_turn_updated_at": 0.0,
    "matchmaking_start_pending_at": None,
}
_my_pick_turn_miss_streak: int = 0
MY_PICK_TURN_CLEAR_MISS_STREAK = 3


_MATCH_STARTED_AT_MONO: Optional[float] = None


@dataclass(frozen=True)
class ChampSelectLcuAttempt:
    completed: bool
    loop_action: LcuLoopAction
    outcome: str


@dataclass(frozen=True)
class LcuActionAttempt:
    completed: bool
    loop_action: LcuLoopAction
    outcome: str


@dataclass(frozen=True)
class RoleLcuAttempt:
    role: Optional[str]
    loop_action: LcuLoopAction
    outcome: str


@dataclass(frozen=True)
class PhaseLcuAttempt:
    phase: Optional[str]
    loop_action: LcuLoopAction
    outcome: str


@dataclass(frozen=True)
class MatchPollAttempt:
    accepted: bool
    finding: bool
    loop_action: LcuLoopAction
    outcome: str
    phase: Optional[str] = None

    def __iter__(self) -> Iterator[bool]:
        yield self.accepted
        yield self.finding


@dataclass
class MatchmakingSearchTracker:
    """Track one queue search without losing its observation provenance."""

    last_authoritative_phase: Optional[str] = None
    finding_source: Optional[str] = None
    start_pending_since: Optional[float] = None

    @property
    def start_pending(self) -> bool:
        return self.start_pending_since is not None

    def mark_start_requested(self, now: float) -> None:
        self.start_pending_since = float(now)
        self.finding_source = None

    def observe(
        self,
        attempt: MatchPollAttempt,
        *,
        now: float,
        image_observation_complete: bool = False,
    ) -> bool:
        """Return True when an observed or requested search disappeared."""
        authoritative = attempt.loop_action == LcuLoopAction.ACT_LCU
        authoritative_lobby = authoritative and attempt.phase == PHASE_LOBBY
        fallback_absence = (
            attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE
            and image_observation_complete
            and not attempt.accepted
            and not attempt.finding
        )

        if attempt.accepted or (
            authoritative
            and attempt.phase
            in {
                PHASE_READY_CHECK,
                PHASE_CHAMP_SELECT,
                PHASE_IN_PROGRESS,
                PHASE_RECONNECT,
                PHASE_WATCH_IN_PROGRESS,
            }
        ):
            self.start_pending_since = None
            self.finding_source = None
        elif attempt.finding:
            if authoritative and attempt.phase == PHASE_MATCHMAKING:
                self.finding_source = "lcu"
            elif (
                attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE
                and image_observation_complete
            ):
                self.finding_source = "image"
            if self.finding_source is not None:
                self.start_pending_since = None
        elif self.finding_source is not None and (
            authoritative_lobby or fallback_absence
        ):
            return True
        elif (
            self.start_pending_since is not None
            and (authoritative_lobby or fallback_absence)
            and float(now) - self.start_pending_since
            >= MATCHMAKING_START_CONFIRM_GRACE_SEC
        ):
            return True

        if authoritative and attempt.phase is not None:
            self.last_authoritative_phase = attempt.phase
        return False


def _record_matchmaking_start_requested(now: Optional[float] = None) -> float:
    requested_at = time.monotonic() if now is None else float(now)
    RUNTIME_STATE["matchmaking_start_pending_at"] = requested_at
    return requested_at


def _matchmaking_start_pending_at() -> Optional[float]:
    value = RUNTIME_STATE.get("matchmaking_start_pending_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _set_client_state(value: ClientState, now: float, logger: logging.Logger) -> None:
    prev = RUNTIME_STATE.get("client_state", ClientState.UNKNOWN)
    if prev == value:
        return
    RUNTIME_STATE["client_state"] = value
    RUNTIME_STATE["client_state_updated_at"] = float(now)
    logger.info("현재 상태 업데이트: client_state=%s", value.name)
    _on_client_state_changed_for_timing(prev, value, float(now), logger)


def _client_state_from_lcu_phase(phase: Optional[str]) -> Optional[ClientState]:
    return client_state_from_lcu_phase(phase)


def _apply_lcu_phase_state(
    phase: Optional[str],
    now: float,
    logger: logging.Logger,
) -> None:
    state = _client_state_from_lcu_phase(phase)
    if state is None:
        return

    current = RUNTIME_STATE.get("client_state", ClientState.UNKNOWN)
    if should_preserve_champ_select_state(state, current):
        return

    _set_client_state(state, now, logger)


def _lcu_local_action_in_progress(
    lcu: Optional[LcuClient],
    action_type: str,
    *,
    logger: logging.Logger,
    stage: str,
) -> bool:
    if lcu is None:
        return False
    action_fn = getattr(lcu, "get_local_action_state", None)
    if not callable(action_fn):
        return False

    try:
        result = action_fn(action_type, require_in_progress=True)
    except RequestException as exc:
        logger.debug(
            "LCU 로컬 action 상태 요청 실패(%s,type=%s): %s",
            stage,
            action_type,
            exc,
        )
        return False
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception(
            "LCU 로컬 action 상태 확인 중 예상하지 못한 오류(%s,type=%s).",
            stage,
            action_type,
        )
        raise

    return bool(getattr(result, "ok", False))


def _apply_lcu_champ_select_action_state(
    lcu: Optional[LcuClient],
    now: float,
    logger: logging.Logger,
    stage: str,
) -> Optional[str]:
    current = RUNTIME_STATE.get("client_state", ClientState.UNKNOWN)
    if current in {
        ClientState.WAIT_GAME_START,
        ClientState.INGAME,
        ClientState.POSTGAME_SCORE,
    }:
        return None

    action_to_state: tuple[tuple[str, ClientState], ...] = (
        ("ban", ClientState.BANPICK),
        ("pick", ClientState.PICK),
    )
    for action_type, state in action_to_state:
        if not _lcu_local_action_in_progress(
            lcu, action_type, logger=logger, stage=stage
        ):
            continue

        _set_client_state(state, now, logger)
        if action_type == "pick":
            _set_my_pick_turn(True, now, logger)
        logger.debug("LCU 로컬 action 감지(%s,type=%s).", stage, action_type)
        return action_type

    return None


def _poll_lcu_phase_attempt(
    lcu: Optional[LcuClient],
    logger: logging.Logger,
    stage: str,
    *,
    max_age_sec: float = 0.25,
) -> PhaseLcuAttempt:
    if lcu is None:
        return PhaseLcuAttempt(None, LcuLoopAction.FALLBACK_IMAGE, "not_attempted")

    try:
        decision_fn = getattr(lcu, "get_gameflow_phase_decision", None)
        if callable(decision_fn):
            result = decision_fn(max_age_sec=max_age_sec)
            outcome = _lcu_status_label(result)
            loop_action = lcu_loop_action_for(result, context="phase")
            if not getattr(result, "ok", False):
                logger.debug(
                    "LCU phase 조회 보류(%s,outcome=%s,action=%s).",
                    stage,
                    outcome,
                    loop_action.value,
                )
                return PhaseLcuAttempt(None, loop_action, outcome)
            raw_phase = getattr(result, "value", None)
        else:
            phase_fn = getattr(lcu, "get_gameflow_phase", None)
            if not callable(phase_fn):
                return PhaseLcuAttempt(
                    None, LcuLoopAction.WAIT_AUTHORITATIVE, "not_supported"
                )
            raw_phase = phase_fn(max_age_sec=max_age_sec)
            if raw_phase is None:
                return PhaseLcuAttempt(
                    None, LcuLoopAction.WAIT_AUTHORITATIVE, "legacy_none"
                )
            outcome = "success"
    except RequestException as exc:
        logger.debug("LCU phase 요청 실패(%s): %s", stage, exc)
        return PhaseLcuAttempt(None, LcuLoopAction.FALLBACK_IMAGE, "request_exception")
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception("LCU phase 조회 중 예상하지 못한 오류(%s).", stage)
        raise

    if not isinstance(raw_phase, str) or not is_known_gameflow_phase(raw_phase):
        logger.debug("LCU phase 응답 형식 오류(%s): %r", stage, raw_phase)
        return PhaseLcuAttempt(
            None, LcuLoopAction.WAIT_AUTHORITATIVE, "malformed_response"
        )

    phase = raw_phase.strip()
    transition: Optional[tuple[Optional[str], str]] = None
    transition_fn = getattr(lcu, "consume_phase_transition", None)
    if callable(transition_fn):
        try:
            transition = cast(Optional[tuple[Optional[str], str]], transition_fn(phase))
        except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
            logger.exception(
                "LCU phase 전이 hook 처리 중 예상하지 못한 오류(%s).", stage
            )
            raise
    if transition is not None:
        prev, curr = transition
        logger.info("LCU 상태 전이(%s): %s -> %s", stage, prev or "UNKNOWN", curr)

    _apply_lcu_phase_state(phase, time.monotonic(), logger)
    if phase == PHASE_CHAMP_SELECT:
        _dismiss_blocking_modal_lcu_attempt(lcu, f"{stage} ChampSelect", logger)
        _apply_lcu_champ_select_action_state(lcu, time.monotonic(), logger, stage)
    return PhaseLcuAttempt(phase, LcuLoopAction.ACT_LCU, outcome)


def _poll_lcu_phase(
    lcu: Optional[LcuClient],
    logger: logging.Logger,
    stage: str,
    *,
    max_age_sec: float = 0.25,
) -> Optional[str]:
    return _poll_lcu_phase_attempt(lcu, logger, stage, max_age_sec=max_age_sec).phase


def _accept_ready_check_lcu_attempt(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> LcuActionAttempt:
    if lcu is None:
        return LcuActionAttempt(False, LcuLoopAction.FALLBACK_IMAGE, "not_attempted")

    now = time.monotonic()
    last = _last_lcu_ready_accept_at.get(stage, 0.0)
    if now - last < LCU_READY_ACCEPT_COOLDOWN_SEC:
        logger.debug("LCU ReadyCheck 수락 쿨다운 중(%s).", stage)
        return LcuActionAttempt(True, LcuLoopAction.ACT_LCU, "cooldown")

    try:
        decision_fn = getattr(lcu, "accept_ready_check_decision", None)
        if callable(decision_fn):
            result = decision_fn()
            outcome = _lcu_status_label(result)
            if getattr(result, "ok", False):
                _last_lcu_ready_accept_at[stage] = now
                logger.info("LCU ReadyCheck 수락 요청 완료(%s).", stage)
                return LcuActionAttempt(True, LcuLoopAction.ACT_LCU, outcome)

            loop_action = lcu_loop_action_for(result, context="ready_check")
            if loop_action == LcuLoopAction.FALLBACK_IMAGE:
                logger.debug(
                    "LCU ReadyCheck 수락 요청 실패(%s,outcome=%s). 이미지 fallback 진행.",
                    stage,
                    outcome,
                )
            else:
                logger.debug(
                    "LCU ReadyCheck 수락 보류(%s,outcome=%s,action=%s).",
                    stage,
                    outcome,
                    loop_action.value,
                )
            return LcuActionAttempt(False, loop_action, outcome)

        if lcu.accept_ready_check():
            _last_lcu_ready_accept_at[stage] = now
            logger.info("LCU ReadyCheck 수락 요청 완료(%s).", stage)
            return LcuActionAttempt(True, LcuLoopAction.ACT_LCU, "success")
    except RequestException as exc:
        logger.debug("LCU ReadyCheck 수락 요청 실패(%s): %s", stage, exc)
        return LcuActionAttempt(
            False, LcuLoopAction.FALLBACK_IMAGE, "request_exception"
        )
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception("LCU ReadyCheck 수락 중 예상하지 못한 오류(%s).", stage)
        raise

    logger.debug(
        "LCU ReadyCheck 수락 결과가 원인 미상 false입니다(%s). "
        "연결/요청 장애로 확인되지 않아 이미지 fallback을 스킵합니다.",
        stage,
    )
    return LcuActionAttempt(False, LcuLoopAction.WAIT_AUTHORITATIVE, "legacy_false")


def _accept_ready_check_via_lcu(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> bool:
    return _accept_ready_check_lcu_attempt(lcu, stage, logger).completed


def _phase_blocks_matchmaking_start(phase: Optional[str]) -> bool:
    if phase is None or phase == PHASE_LOBBY:
        return False
    return phase in {
        PHASE_NONE,
        PHASE_MATCHMAKING,
        PHASE_READY_CHECK,
        PHASE_CHAMP_SELECT,
        PHASE_IN_PROGRESS,
        PHASE_RECONNECT,
        PHASE_WATCH_IN_PROGRESS,
        PHASE_WAITING_FOR_STATS,
        PHASE_PRE_END_OF_GAME,
        PHASE_END_OF_GAME,
    }


def _matchmaking_start_blocked_by_lcu_phase(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
    *,
    max_age_sec: float = 0.5,
    allow_none_phase: bool = False,
) -> bool:
    if lcu is None:
        return False
    attempt = _poll_lcu_phase_attempt(lcu, logger, stage, max_age_sec=max_age_sec)
    if attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
        logger.debug(
            "LCU 대전찾기 phase 판단 보류(%s,outcome=%s).",
            stage,
            attempt.outcome,
        )
        return True

    if _phase_blocks_matchmaking_start(attempt.phase):
        if allow_none_phase and attempt.phase == PHASE_NONE:
            logger.debug(
                "LCU phase=None 이지만 postgame 완료 직후이므로 대전찾기 시작을 허용합니다(%s).",
                stage,
            )
            return False
        logger.debug(
            "LCU phase=%s 이므로 대전찾기 시작을 보류합니다(%s).",
            attempt.phase,
            stage,
        )
        return True
    return False


def _start_matchmaking_via_lcu(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> bool:
    return _start_matchmaking_lcu_attempt(lcu, stage, logger).completed


def _start_matchmaking_lcu_attempt(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
    *,
    allow_none_phase: bool = False,
) -> LcuActionAttempt:
    if lcu is None:
        return LcuActionAttempt(False, LcuLoopAction.FALLBACK_IMAGE, "not_attempted")
    if _matchmaking_start_blocked_by_lcu_phase(
        lcu, stage, logger, allow_none_phase=allow_none_phase
    ):
        return LcuActionAttempt(
            False, LcuLoopAction.WAIT_AUTHORITATIVE, "phase_blocked"
        )

    try:
        decision_fn = getattr(lcu, "start_matchmaking_decision", None)
        if callable(decision_fn):
            result = decision_fn()
            outcome = _lcu_status_label(result)
            if getattr(result, "ok", False):
                _record_matchmaking_start_requested()
                logger.info("LCU 대전 찾기 요청 완료(%s).", stage)
                return LcuActionAttempt(True, LcuLoopAction.ACT_LCU, outcome)

            loop_action = lcu_loop_action_for(result, context="matchmaking_start")
            if loop_action == LcuLoopAction.FALLBACK_IMAGE:
                logger.debug(
                    "LCU 대전 찾기 요청 실패(%s,outcome=%s). 이미지 fallback 진행.",
                    stage,
                    outcome,
                )
            else:
                logger.debug(
                    "LCU 대전 찾기 요청 보류(%s,outcome=%s,action=%s).",
                    stage,
                    outcome,
                    loop_action.value,
                )
            return LcuActionAttempt(False, loop_action, outcome)

        if lcu.start_matchmaking():
            _record_matchmaking_start_requested()
            logger.info("LCU 대전 찾기 요청 완료(%s).", stage)
            return LcuActionAttempt(True, LcuLoopAction.ACT_LCU, "success")
    except RequestException as exc:
        logger.debug("LCU 대전 찾기 요청 실패(%s): %s", stage, exc)
        return LcuActionAttempt(
            False, LcuLoopAction.FALLBACK_IMAGE, "request_exception"
        )
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception("LCU 대전 찾기 중 예상하지 못한 오류(%s).", stage)
        raise

    logger.debug(
        "LCU 대전 찾기 결과가 원인 미상 false입니다(%s). "
        "연결/요청 장애로 확인되지 않아 이미지 fallback을 스킵합니다.",
        stage,
    )
    return LcuActionAttempt(False, LcuLoopAction.WAIT_AUTHORITATIVE, "legacy_false")


def _lcu_status_label(result: object) -> str:
    status = getattr(result, "status", None)
    return str(getattr(status, "value", status or "unknown"))


def _honor_vote_lcu_attempt(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> LcuActionAttempt:
    if lcu is None:
        return LcuActionAttempt(False, LcuLoopAction.FALLBACK_IMAGE, "not_attempted")

    try:
        decision_fn = getattr(lcu, "honor_random_eligible_teammate_decision", None)
        if not callable(decision_fn):
            logger.debug("LCU 명예 투표 API가 없습니다(%s).", stage)
            return LcuActionAttempt(True, LcuLoopAction.ABORT_LOG, "not_supported")

        result = decision_fn()
        outcome = _lcu_status_label(result)
        if getattr(result, "ok", False):
            logger.info("LCU 명예 투표 요청 완료(%s,outcome=%s).", stage, outcome)
            return LcuActionAttempt(True, LcuLoopAction.ACT_LCU, outcome)

        loop_action = lcu_loop_action_for(result, context="postgame_honor_vote")
        logger.debug(
            "LCU 명예 투표 처리 종료(%s,outcome=%s,reason=%s). "
            "이미지 fallback 없이 다음 자동화 사이클로 복귀합니다.",
            stage,
            outcome,
            getattr(result, "reason", ""),
        )
        return LcuActionAttempt(True, loop_action, outcome)
    except RequestException as exc:
        logger.debug(
            "LCU 명예 투표 요청 실패(%s): %s. 다음 자동화 사이클로 복귀합니다.",
            stage,
            exc,
        )
        return LcuActionAttempt(True, LcuLoopAction.ABORT_LOG, "request_exception")
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception("LCU 명예 투표 중 예상하지 못한 오류(%s).", stage)
        raise


def _dismiss_blocking_modal_lcu_attempt(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> LcuActionAttempt:
    if lcu is None:
        return LcuActionAttempt(False, LcuLoopAction.FALLBACK_IMAGE, "not_attempted")

    try:
        decision_fn = getattr(lcu, "dismiss_blocking_modal_decision", None)
        if not callable(decision_fn):
            logger.debug("LCU 클라이언트 모달 닫기 API가 없습니다(%s).", stage)
            return LcuActionAttempt(
                False, LcuLoopAction.FALLBACK_IMAGE, "not_supported"
            )

        result = decision_fn()
        outcome = _lcu_status_label(result)
        if getattr(result, "ok", False):
            logger.info("LCU 클라이언트 모달 닫기 완료(%s,outcome=%s).", stage, outcome)
            return LcuActionAttempt(True, LcuLoopAction.ACT_LCU, outcome)

        logger.debug(
            "LCU 클라이언트 모달 닫기 종료(%s,outcome=%s,reason=%s). "
            "LCU로 닫을 항목이 없으면 이미지 fallback을 허용합니다.",
            stage,
            outcome,
            getattr(result, "reason", ""),
        )
        return LcuActionAttempt(False, LcuLoopAction.FALLBACK_IMAGE, outcome)
    except RequestException as exc:
        logger.debug(
            "LCU 클라이언트 모달 닫기 요청 실패(%s): %s. 이미지 fallback 진행.",
            stage,
            exc,
        )
        return LcuActionAttempt(
            False, LcuLoopAction.FALLBACK_IMAGE, "request_exception"
        )
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception("LCU 클라이언트 모달 닫기 중 예상하지 못한 오류(%s).", stage)
        raise


def _dismiss_end_of_game_stats_lcu_attempt(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> LcuActionAttempt:
    if lcu is None:
        return LcuActionAttempt(False, LcuLoopAction.FALLBACK_IMAGE, "not_attempted")

    try:
        decision_fn = getattr(lcu, "dismiss_end_of_game_stats_decision", None)
        if not callable(decision_fn):
            logger.debug(
                "LCU 엔드 계속하기 API가 없습니다(%s). 이미지 fallback 진행.", stage
            )
            return LcuActionAttempt(
                False, LcuLoopAction.FALLBACK_IMAGE, "not_supported"
            )

        result = decision_fn()
        outcome = _lcu_status_label(result)
        if getattr(result, "ok", False):
            logger.info("LCU 엔드 계속하기 요청 완료(%s,outcome=%s).", stage, outcome)
            return LcuActionAttempt(True, LcuLoopAction.ACT_LCU, outcome)

        loop_action = lcu_loop_action_for(result, context="postgame_continue")
        if _lcu_status_label(result) == "unsupported":
            loop_action = LcuLoopAction.FALLBACK_IMAGE

        if loop_action == LcuLoopAction.FALLBACK_IMAGE:
            logger.debug(
                "LCU 엔드 계속하기 요청 실패(%s,outcome=%s). 이미지 fallback 진행.",
                stage,
                outcome,
            )
        else:
            logger.debug(
                "LCU 엔드 계속하기 요청 보류(%s,outcome=%s,action=%s).",
                stage,
                outcome,
                loop_action.value,
            )
        return LcuActionAttempt(False, loop_action, outcome)
    except RequestException as exc:
        logger.debug(
            "LCU 엔드 계속하기 요청 실패(%s): %s. 이미지 fallback 진행.",
            stage,
            exc,
        )
        return LcuActionAttempt(
            False, LcuLoopAction.FALLBACK_IMAGE, "request_exception"
        )
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception("LCU 엔드 계속하기 중 예상하지 못한 오류(%s).", stage)
        raise


def _play_again_lcu_attempt(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> LcuActionAttempt:
    if lcu is None:
        return LcuActionAttempt(False, LcuLoopAction.FALLBACK_IMAGE, "not_attempted")

    try:
        decision_fn = getattr(lcu, "play_again_decision", None)
        if not callable(decision_fn):
            logger.debug(
                "LCU 다음 게임 API가 없습니다(%s). 기존 postgame fallback 진행.",
                stage,
            )
            return LcuActionAttempt(
                False, LcuLoopAction.FALLBACK_IMAGE, "not_supported"
            )

        result = decision_fn()
        outcome = _lcu_status_label(result)
        if getattr(result, "ok", False):
            logger.info("LCU 다음 게임 요청 완료(%s,outcome=%s).", stage, outcome)
            return LcuActionAttempt(True, LcuLoopAction.ACT_LCU, outcome)

        loop_action = lcu_loop_action_for(result, context="postgame_play_again")
        if _lcu_status_label(result) == "unsupported":
            loop_action = LcuLoopAction.FALLBACK_IMAGE
        elif loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
            loop_action = LcuLoopAction.FALLBACK_IMAGE

        if loop_action == LcuLoopAction.FALLBACK_IMAGE:
            logger.debug(
                "LCU 다음 게임 요청 실패(%s,outcome=%s). 기존 postgame fallback 진행.",
                stage,
                outcome,
            )
        else:
            logger.debug(
                "LCU 다음 게임 요청 보류(%s,outcome=%s,action=%s).",
                stage,
                outcome,
                loop_action.value,
            )
        return LcuActionAttempt(False, loop_action, outcome)
    except RequestException as exc:
        logger.debug(
            "LCU 다음 게임 요청 실패(%s): %s. 기존 postgame fallback 진행.",
            stage,
            exc,
        )
        return LcuActionAttempt(
            False, LcuLoopAction.FALLBACK_IMAGE, "request_exception"
        )
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception("LCU 다음 게임 요청 중 예상하지 못한 오류(%s).", stage)
        raise


def _champ_select_action_attempt_via_lcu(
    lcu: Optional[LcuClient],
    champion_name: object,
    *,
    action_type: str,
    complete: bool,
    stage: str,
    logger: logging.Logger,
) -> ChampSelectLcuAttempt:
    fallback = ChampSelectLcuAttempt(
        False, LcuLoopAction.FALLBACK_IMAGE, "not_attempted"
    )
    if lcu is None:
        return fallback
    if not str(champion_name or "").strip():
        return ChampSelectLcuAttempt(
            False, LcuLoopAction.WAIT_AUTHORITATIVE, "missing_champion"
        )

    try:
        decision_fn = getattr(lcu, "select_champ_select_champion_decision", None)
        if callable(decision_fn):
            result = decision_fn(
                champion_name, action_type=action_type, complete=complete
            )
            outcome = _lcu_status_label(result)
            if getattr(result, "ok", False):
                mode = "완료" if complete else "선택"
                logger.info(
                    "LCU 챔피언 %s 요청 완료(%s,type=%s,outcome=%s): %s",
                    mode,
                    stage,
                    action_type,
                    outcome,
                    champion_name,
                )
                return ChampSelectLcuAttempt(True, LcuLoopAction.ACT_LCU, outcome)

            loop_action = lcu_loop_action_for(result, context="write")
            if loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                logger.debug(
                    "LCU 챔피언 action 대기(%s,type=%s,outcome=%s). "
                    "LCU 대상 액션이므로 이미지/마우스 fallback을 스킵합니다.",
                    stage,
                    action_type,
                    outcome,
                )
            else:
                logger.debug(
                    "LCU 챔피언 action 요청 실패(%s,type=%s,outcome=%s). "
                    "LCU 연결/요청 장애로 이미지 fallback을 허용합니다.",
                    stage,
                    action_type,
                    outcome,
                )
            return ChampSelectLcuAttempt(False, loop_action, outcome)

        if lcu.select_champ_select_champion(
            champion_name, action_type=action_type, complete=complete
        ):
            mode = "완료" if complete else "선택"
            logger.info(
                "LCU 챔피언 %s 요청 완료(%s,type=%s): %s",
                mode,
                stage,
                action_type,
                champion_name,
            )
            return ChampSelectLcuAttempt(True, LcuLoopAction.ACT_LCU, "success")
    except RequestException as exc:
        logger.debug(
            "LCU 챔피언 action 요청 실패(%s,type=%s): %s",
            stage,
            action_type,
            exc,
        )
        return ChampSelectLcuAttempt(
            False, LcuLoopAction.FALLBACK_IMAGE, "request_exception"
        )
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception(
            "LCU 챔피언 action 중 예상하지 못한 오류(%s,type=%s).",
            stage,
            action_type,
        )
        raise

    logger.debug(
        "LCU 챔피언 action 요청 실패(%s,type=%s). LCU 대상 액션이므로 이미지/마우스 fallback을 스킵합니다.",
        stage,
        action_type,
    )
    return ChampSelectLcuAttempt(False, LcuLoopAction.WAIT_AUTHORITATIVE, "unknown")


def _guard_champ_select_phase_exit(
    attempt: ChampSelectLcuAttempt,
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> ChampSelectLcuAttempt:
    if attempt.completed or attempt.loop_action != LcuLoopAction.WAIT_AUTHORITATIVE:
        return attempt

    phase_attempt = _poll_lcu_phase_attempt(lcu, logger, f"{stage} phase 확인")
    if phase_attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE:
        logger.debug(
            "LCU %s phase 확인 실패(outcome=%s). 이미지 fallback을 허용합니다.",
            stage,
            phase_attempt.outcome,
        )
        return ChampSelectLcuAttempt(
            False,
            LcuLoopAction.FALLBACK_IMAGE,
            f"phase_probe:{phase_attempt.outcome}",
        )
    if (
        phase_attempt.loop_action == LcuLoopAction.ACT_LCU
        and phase_attempt.phase != PHASE_CHAMP_SELECT
    ):
        logger.info(
            "LCU %s action 대기 중 ChampSelect 이탈 감지(phase=%s).",
            stage,
            phase_attempt.phase,
        )
        return ChampSelectLcuAttempt(
            False,
            LcuLoopAction.ACT_LCU,
            f"phase_exit:{phase_attempt.phase}",
        )
    return attempt


def _wait_champ_select_action_via_lcu(
    lcu: Optional[LcuClient],
    champion_name: object,
    *,
    action_type: str,
    complete: bool,
    stage: str,
    logger: logging.Logger,
    interval_sec: float,
    timeout_sec: float,
    stop_when_action_type_in_progress: str | None = None,
) -> ChampSelectLcuAttempt:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    wait_count = 0

    while True:
        attempt = _champ_select_action_attempt_via_lcu(
            lcu,
            champion_name,
            action_type=action_type,
            complete=complete,
            stage=stage,
            logger=logger,
        )
        if attempt.completed or attempt.loop_action != LcuLoopAction.WAIT_AUTHORITATIVE:
            return attempt

        guarded_attempt = _guard_champ_select_phase_exit(
            attempt, lcu, stage, logger
        )
        if guarded_attempt is not attempt:
            return guarded_attempt

        wait_count += 1
        now = time.monotonic()
        _apply_lcu_champ_select_action_state(lcu, now, logger, f"{stage} 대기")

        if stop_when_action_type_in_progress and _lcu_local_action_in_progress(
            lcu,
            stop_when_action_type_in_progress,
            logger=logger,
            stage=f"{stage} 대기",
        ):
            logger.info(
                "LCU %s action 대기 중 %s action이 시작되어 다음 단계로 전환합니다(outcome=%s).",
                stage,
                stop_when_action_type_in_progress,
                attempt.outcome,
            )
            return ChampSelectLcuAttempt(
                False,
                LcuLoopAction.WAIT_AUTHORITATIVE,
                f"superseded_by_{stop_when_action_type_in_progress}",
            )

        if now >= deadline:
            logger.warning(
                "LCU %s action 대기 시간 초과(type=%s,outcome=%s,%.1fs).",
                stage,
                action_type,
                attempt.outcome,
                timeout_sec,
            )
            return attempt

        if wait_count == 1 or wait_count % 5 == 0:
            logger.info(
                "LCU %s action 대기 중(type=%s,outcome=%s,%d회).",
                stage,
                action_type,
                attempt.outcome,
                wait_count,
            )
        time.sleep(min(max(0.0, interval_sec), max(0.0, deadline - now)))


def _ban_champ_select_attempt_or_skip(
    lcu: Optional[LcuClient],
    ban_name: object,
    *,
    logger: logging.Logger,
    interval_sec: float,
) -> ChampSelectLcuAttempt:
    resolved_ban = str(ban_name or "").strip()
    if not resolved_ban:
        logger.warning(
            "밴 챔피언이 설정되지 않았습니다. 밴 단계만 건너뛰고 픽 단계 감지를 계속합니다."
        )
        return ChampSelectLcuAttempt(
            False,
            LcuLoopAction.WAIT_AUTHORITATIVE,
            "missing_ban",
        )

    return _wait_champ_select_action_via_lcu(
        lcu,
        resolved_ban,
        action_type="ban",
        complete=True,
        stage="밴",
        logger=logger,
        interval_sec=interval_sec,
        timeout_sec=max(20.0, interval_sec * 20.0),
        stop_when_action_type_in_progress="pick",
    )


def _handle_champ_select_phase_exit(
    attempt: ChampSelectLcuAttempt,
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
) -> bool:
    global _my_pick_turn_miss_streak

    session_reset_prefix = "session_reset:"
    prefix = "phase_exit:"
    if attempt.outcome.startswith(session_reset_prefix):
        reason = attempt.outcome.removeprefix(session_reset_prefix)
        now = time.monotonic()
        _my_pick_turn_miss_streak = 0
        _set_my_pick_turn(False, now, logger)
        _set_client_state(ClientState.UNKNOWN, now, logger)
        logger.info(
            "LCU ChampSelect 세션 초기화(%s,reason=%s). 외부 매칭 사이클로 복귀합니다.",
            stage,
            reason,
        )
        return True
    if not attempt.outcome.startswith(prefix):
        return False

    phase = attempt.outcome.removeprefix(prefix)
    now = time.monotonic()
    _my_pick_turn_miss_streak = 0
    _set_my_pick_turn(False, now, logger)
    _apply_lcu_phase_state(phase, now, logger)
    if phase == PHASE_READY_CHECK:
        _accept_ready_check_via_lcu(lcu, stage, logger)
    logger.info(
        "LCU ChampSelect 이탈 처리(%s,phase=%s). 외부 매칭 사이클로 복귀합니다.",
        stage,
        phase,
    )
    return True


def _champ_select_action_via_lcu(
    lcu: Optional[LcuClient],
    champion_name: object,
    *,
    action_type: str,
    complete: bool,
    stage: str,
    logger: logging.Logger,
) -> bool:
    result = _champ_select_action_attempt_via_lcu(
        lcu,
        champion_name,
        action_type=action_type,
        complete=complete,
        stage=stage,
        logger=logger,
    )
    return result.completed


def _detect_role_via_lcu(
    lcu: Optional[LcuClient],
    *,
    stage: str,
    logger: logging.Logger,
) -> RoleLcuAttempt:
    if lcu is None:
        return RoleLcuAttempt(None, LcuLoopAction.FALLBACK_IMAGE, "not_attempted")

    try:
        result = lcu.get_local_player_position()
    except RequestException as exc:
        logger.debug("LCU 포지션 감지 실패(%s): %s. 이미지 fallback 진행.", stage, exc)
        return RoleLcuAttempt(None, LcuLoopAction.FALLBACK_IMAGE, "request_exception")
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception("LCU 포지션 감지 중 예상하지 못한 오류(%s).", stage)
        raise

    if getattr(result, "ok", False):
        role = str(getattr(result, "value", "") or "").strip()
        if role in ROLE_ORDER:
            logger.info("LCU 포지션 감지(%s): %s", stage, role)
            return RoleLcuAttempt(
                role, LcuLoopAction.ACT_LCU, _lcu_status_label(result)
            )
        logger.debug(
            "LCU 포지션 감지 결과가 지원되지 않는 role입니다(%s,outcome=%s,role=%s). "
            "LCU semantic 결과이므로 이미지 fallback을 스킵합니다.",
            stage,
            _lcu_status_label(result),
            role,
        )
        return RoleLcuAttempt(
            None, LcuLoopAction.WAIT_AUTHORITATIVE, _lcu_status_label(result)
        )

    loop_action = lcu_loop_action_for(result, context="role")
    if loop_action == LcuLoopAction.FALLBACK_IMAGE:
        logger.debug(
            "LCU 포지션 감지 실패(%s,outcome=%s). 이미지 fallback 진행.",
            stage,
            _lcu_status_label(result),
        )
    else:
        logger.debug(
            "LCU 포지션 감지 보류(%s,outcome=%s,action=%s).",
            stage,
            _lcu_status_label(result),
            loop_action.value,
        )
    return RoleLcuAttempt(None, loop_action, _lcu_status_label(result))


def _detect_role_lcu_first(
    lcu: Optional[LcuClient],
    *,
    stage: str,
    logger: logging.Logger,
    image_detector: Callable[[], Optional[str]],
) -> Optional[str]:
    return _detect_role_lcu_first_attempt(
        lcu,
        stage=stage,
        logger=logger,
        image_detector=image_detector,
    ).role


def _detect_role_lcu_first_attempt(
    lcu: Optional[LcuClient],
    *,
    stage: str,
    logger: logging.Logger,
    image_detector: Callable[[], Optional[str]],
) -> RoleLcuAttempt:
    attempt = _detect_role_via_lcu(lcu, stage=stage, logger=logger)
    if attempt.role:
        return attempt
    if attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE:
        role = image_detector()
        if role:
            return RoleLcuAttempt(role, LcuLoopAction.FALLBACK_IMAGE, attempt.outcome)
    return attempt


def _detect_role_lcu_first_with_retry(
    lcu: Optional[LcuClient],
    *,
    stage: str,
    logger: logging.Logger,
    image_detector: Callable[[], Optional[str]],
    interval_sec: float,
) -> Optional[str]:
    return _detect_role_lcu_first_with_retry_attempt(
        lcu,
        stage=stage,
        logger=logger,
        image_detector=image_detector,
        interval_sec=interval_sec,
    ).role


def _detect_role_lcu_first_with_retry_attempt(
    lcu: Optional[LcuClient],
    *,
    stage: str,
    logger: logging.Logger,
    image_detector: Callable[[], Optional[str]],
    interval_sec: float,
) -> RoleLcuAttempt:
    while True:
        attempt = _detect_role_lcu_first_attempt(
            lcu,
            stage=stage,
            logger=logger,
            image_detector=image_detector,
        )
        if attempt.role:
            return attempt
        if attempt.loop_action != LcuLoopAction.WAIT_AUTHORITATIVE:
            return attempt

        phase_attempt = _poll_lcu_phase_attempt(lcu, logger, f"{stage} phase 확인")
        if (
            phase_attempt.loop_action == LcuLoopAction.ACT_LCU
            and phase_attempt.phase != PHASE_CHAMP_SELECT
        ):
            logger.info(
                "LCU 포지션 감지 대기 중 ChampSelect 이탈 감지"
                "(%s,phase=%s). 외부 매칭 사이클로 복귀합니다.",
                stage,
                phase_attempt.phase,
            )
            return RoleLcuAttempt(
                None,
                LcuLoopAction.ACT_LCU,
                f"phase_exit:{phase_attempt.phase}",
            )

        logger.info(
            "LCU 포지션 감지 대기 중(%s,outcome=%s). %.1fs 후 재시도합니다.",
            stage,
            attempt.outcome,
            interval_sec,
        )
        time.sleep(interval_sec)


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


def _client_confirm_template_candidates(selected: Path) -> tuple[Path, ...]:
    return (
        selected / "client_confirm-button.png",
        selected / "client_confirm-button-2.png",
        selected / "client_thanks-button.png",
    )


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


def _dismiss_blocking_modal_ui_fallback(
    rect,
    tpl_confirm_templates: Sequence[Path],
    threshold: float,
    stage: str,
    logger: logging.Logger,
) -> bool:
    if rect is None or not tpl_confirm_templates:
        return False
    if is_rect_minimized(rect):
        return False

    popup_roi = _popup_button_search_roi(rect)
    templates: list[tuple[str, Path]] = []
    name_to_path: dict[str, Path] = {}
    rois: dict[str, tuple[int, int, int, int]] = {}
    for idx, tpl_confirm in enumerate(tpl_confirm_templates):
        if tpl_confirm is None or not tpl_confirm.exists():
            continue
        name = f"confirm#{idx}"
        templates.append((name, tpl_confirm))
        name_to_path[name] = tpl_confirm
        rois[name] = popup_roi

    if not templates:
        return False

    matches = find_template_matches_once(
        rect, templates, threshold=threshold, search_rois=rois
    )
    best_name: Optional[str] = None
    best_hit: Optional[tuple[tuple[int, int], object, float]] = None
    best_score = -1.0
    for name, hit in matches.items():
        score = float(hit[2])
        if score > best_score:
            best_score = score
            best_name = name
            best_hit = hit

    if best_hit is None:
        return False

    now = time.monotonic()
    key = f"blocking_modal_ui:{stage}"
    last = _last_popup_click_at.get(key, 0.0)
    if now - last < POPUP_CLICK_COOLDOWN_SEC:
        return False

    center, _roi_bgr, _score = best_hit
    try:
        click_screen(center)
    except Exception as exc:
        logger.warning("클라이언트 모달 UI fallback 클릭 실패(%s): %s", stage, exc)
        return False

    _last_popup_click_at[key] = now
    tpl = name_to_path.get(best_name) if best_name else None
    logger.info(
        "LCU 미노출 클라이언트 모달 UI fallback 클릭 처리(%s,tpl=%s).",
        stage,
        (tpl.name if tpl else "unknown"),
    )
    return True


def _should_scan_popup_confirm_during_match_poll(phase: Optional[str]) -> bool:
    if phase == PHASE_LOBBY:
        return False

    state = RUNTIME_STATE.get("client_state")
    return state not in {
        ClientState.LOBBY,
        ClientState.MATCH_FINDING,
        ClientState.MATCH_ACCEPT_WAIT,
    }


AUTO_LOBBY_CREATE_RETRY_SEC = 10.0
_last_auto_lobby_create_at: float = 0.0


def _should_click_popup_confirm_at_cycle_start(phase: Optional[str]) -> bool:
    return phase == PHASE_LOBBY


def _should_attempt_auto_lobby_create(
    phase: Optional[str],
    seconds_since_last_attempt: float,
) -> bool:
    if phase not in {None, PHASE_NONE}:
        return False
    return seconds_since_last_attempt >= AUTO_LOBBY_CREATE_RETRY_SEC


def _maybe_auto_create_lobby_for_home_screen(
    lcu: Optional[LcuClient],
    phase: Optional[str],
    logger: logging.Logger,
) -> None:
    global _last_auto_lobby_create_at

    now = time.monotonic()
    if not _should_attempt_auto_lobby_create(phase, now - _last_auto_lobby_create_at):
        return

    _last_auto_lobby_create_at = now
    if lcu is None:
        return

    try:
        result = lcu.create_lobby_decision()
    except RequestException as exc:
        logger.debug("LCU 로비 생성 요청 실패: %s", exc)
        return
    except Exception:  # noqa: BLE001 - unexpected failures must stay visible.
        logger.exception("LCU 로비 생성 중 예상하지 못한 오류.")
        raise

    outcome = str(getattr(getattr(result, "status", None), "value", "unknown"))
    if getattr(result, "ok", False):
        logger.info(
            "홈 화면 감지로 로비 생성을 요청했습니다(queue=ranked solo/duo, outcome=%s).",
            outcome,
        )
    else:
        logger.warning(
            "홈 화면에서 로비 생성이 보류되었습니다(outcome=%s). "
            "Riot Client 로그인 상태를 확인하세요.",
            outcome,
        )


def ensure_active_rect(
    logger: logging.Logger,
    poll: float = 0.5,
    timeout_sec: float = DEFAULT_LEAGUE_WINDOW_LOOKUP_TIMEOUT_SEC,
):
    timeout_sec = max(0.0, float(timeout_sec))
    poll = max(0.0, float(poll))
    deadline = time.monotonic() + timeout_sec
    last_state = None
    while True:
        if _LEAGUE_EXIT_GUARD is not None and _LEAGUE_EXIT_GUARD.should_exit():
            _exit_after_league_client_closed(logger)

        rect = find_league_window_rect()
        if rect:
            if is_rect_minimized(rect):
                if last_state != "minimized":
                    logger.info("LoL 창이 최소화/비가시 상태입니다. 복원 대기 중...")
                    last_state = "minimized"
                now = time.monotonic()
                if now >= deadline:
                    logger.warning(
                        "LoL 창 대기 시간 초과: state=minimized %.1fs", timeout_sec
                    )
                    raise LeagueWindowLookupTimeout("minimized", timeout_sec)
                time.sleep(min(poll, deadline - now))
                continue
            return rect
        if last_state != "missing":
            logger.info("LoL 창 좌표를 찾지 못했습니다. 재시도...")
            last_state = "missing"
        now = time.monotonic()
        if now >= deadline:
            logger.warning("LoL 창 대기 시간 초과: state=missing %.1fs", timeout_sec)
            raise LeagueWindowLookupTimeout("missing", timeout_sec)
        time.sleep(min(poll, deadline - now))


def prompt_ban_selection(
    role: str,
    selected_name: str,
    href: Optional[str],
    logger: logging.Logger,
    ranked_entries: Optional[Iterable[object]] = None,
) -> Optional[str]:
    slug = href or fetch_champion_slug(role, selected_name)
    labels: list[str] = []
    label_to_name: dict[str, str] = {}
    if slug:
        try:
            recommendations = build_recommendations(
                role=role,
                configured_pick=selected_name,
                matchups=fetch_counter_matchups_from_detail(slug, limit=10),
                ranked_entries=ranked_entries or (),
                source_url=slug,
            )
            labels, label_to_name = build_label_name_map(recommendations)
        except Exception as exc:
            logger.info("OP.GG 추천 밴 후보 계산 실패: %s", exc)

    if labels:
        print(f"[{role}] 밴할 챔피언을 번호로 선택하세요 (기본 1):")
        for idx, label in enumerate(labels, start=1):
            print(f"  {idx}. {label}")
        ban_choice = input("번호 입력 (기본 1): ").strip()
        try:
            ban_idx = int(ban_choice) - 1 if ban_choice else 0
        except ValueError:
            ban_idx = 0
        ban_idx = max(0, min(ban_idx, len(labels) - 1))
        return label_to_name.get(labels[ban_idx], labels[ban_idx])

    ban_candidates: list[str] = []
    if slug:
        ban_candidates = sort_counter_candidates_by_role_rank(
            fetch_counters_from_detail(slug, limit=10),
            ranked_entries or (),
        )
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

    selected_ban = prompt_ban_selection(
        role, selected_name, href, logger, ranked_entries=candidates
    )
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
    ranked_entries: Optional[Iterable[object]] = None,
) -> str:
    ban = prompt_ban_selection(
        role, champion_name, href, logger, ranked_entries=ranked_entries
    )
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

    ranked_entries = list(candidates)

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
    selected_ban = _prompt_ban_required(
        role, selected_name, href, logger, ranked_entries=ranked_entries
    )
    return (selected_name, selected_ban)


def prompt_reserve_picks_for_role(
    role: str,
    primary: str,
    primary_ban: str,
    logger: logging.Logger,
) -> list[tuple[str, str]]:
    reserves: list[tuple[str, str]] = []

    for idx in (1, 2):
        ban_label = (
            display_ban_name_for_summary(primary_ban) if primary_ban else "미설정"
        )
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
    ban_label = display_ban_name_for_summary(primary_ban) if primary_ban else "미설정"
    print(f"[{role}] 기본: {primary} (ban={ban_label})")
    for idx in (1, 2):
        if idx <= len(reserves):
            champ, ban = reserves[idx - 1]
            champ = str(champ or "").strip()
            ban = str(ban or "").strip()
            ban_label = display_ban_name_for_summary(ban) if ban else "미설정"
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
) -> MatchPollAttempt:
    phase_attempt = _poll_lcu_phase_attempt(lcu, logger, stage)
    phase = phase_attempt.phase
    allow_league_state_images = (
        phase_attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE
    )
    if phase_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
        logger.debug(
            "LCU phase가 authoritative wait 상태입니다(%s,outcome=%s). "
            "매칭 이미지 탐색을 생략합니다.",
            stage,
            phase_attempt.outcome,
        )

    if phase == PHASE_READY_CHECK:
        ready_attempt = _accept_ready_check_lcu_attempt(lcu, stage, logger)
        if ready_attempt.completed:
            time.sleep(confirm_check_interval)
            return MatchPollAttempt(
                True, True, LcuLoopAction.ACT_LCU, ready_attempt.outcome, phase
            )
        if ready_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
            time.sleep(confirm_check_interval)
            return MatchPollAttempt(
                False,
                False,
                LcuLoopAction.WAIT_AUTHORITATIVE,
                ready_attempt.outcome,
                phase,
            )
        allow_league_state_images = True
    elif phase == PHASE_MATCHMAKING:
        already_logged = _last_finding_logged.get(stage, False)
        if not already_logged:
            logger.info("LCU 매칭 상태 감지(%s). 이미지 매칭을 건너뜁니다.", stage)
            _last_finding_logged[stage] = True
        time.sleep(confirm_check_interval)
        return MatchPollAttempt(
            False, True, LcuLoopAction.ACT_LCU, "matchmaking", phase
        )
    elif phase == PHASE_CHAMP_SELECT:
        logger.debug("LCU 챔피언 선택 진입 감지(%s).", stage)
        return MatchPollAttempt(
            False, False, LcuLoopAction.ACT_LCU, "champ_select", phase
        )
    elif phase in {PHASE_IN_PROGRESS, PHASE_RECONNECT, PHASE_WATCH_IN_PROGRESS}:
        logger.debug("LCU 인게임 진입 감지(%s).", stage)
        return MatchPollAttempt(
            False, False, LcuLoopAction.ACT_LCU, "in_progress", phase
        )

    if rect is None:
        return MatchPollAttempt(
            False, False, phase_attempt.loop_action, phase_attempt.outcome, phase
        )

    templates: list[tuple[str, Path]] = []
    if allow_league_state_images:
        templates.extend(
            [
                ("finding", tpl_finding_match),
                ("accept", tpl_accept),
            ]
        )
    confirm_names: list[str] = []
    confirm_name_to_path: dict[str, Path] = {}

    rois: dict[str, tuple[int, int, int, int]] = {}
    if tpl_confirm_templates and _should_scan_popup_confirm_during_match_poll(phase):
        modal_attempt = _dismiss_blocking_modal_lcu_attempt(lcu, stage, logger)
        if modal_attempt.completed:
            time.sleep(confirm_check_interval)
            return MatchPollAttempt(
                False, False, LcuLoopAction.ACT_LCU, modal_attempt.outcome, phase
            )
        popup_roi = _popup_button_search_roi(rect)
        for idx, tpl_confirm in enumerate(tpl_confirm_templates):
            if tpl_confirm is None or not tpl_confirm.exists():
                continue
            name = f"confirm#{idx}"
            templates.append((name, tpl_confirm))
            confirm_names.append(name)
            confirm_name_to_path[name] = tpl_confirm
            rois[name] = popup_roi

    matches = (
        find_template_matches_once(
            rect, templates, threshold=threshold, search_rois=(rois if rois else None)
        )
        if templates
        else {}
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
                tpl = (
                    confirm_name_to_path.get(best_confirm_name)
                    if best_confirm_name
                    else None
                )
                logger.info(
                    "확인 팝업 클릭 처리(%s, tpl=%s).",
                    stage,
                    (tpl.name if tpl else "unknown"),
                )
            except Exception as exc:
                logger.warning("확인 팝업 클릭 실패(%s): %s", stage, exc)

    if phase_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
        time.sleep(confirm_check_interval)
        return MatchPollAttempt(
            False,
            False,
            LcuLoopAction.WAIT_AUTHORITATIVE,
            phase_attempt.outcome,
            phase,
        )

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
            return MatchPollAttempt(
                True, True, LcuLoopAction.FALLBACK_IMAGE, "image_accept", phase
            )

        last = _last_accept_click_at.get(stage, 0.0)
        if now - last >= ACCEPT_CLICK_COOLDOWN_SEC:
            try:
                click_screen(center)
            except Exception as exc:
                logger.warning("수락 버튼 클릭 실패(%s): %s", stage, exc)
                time.sleep(confirm_check_interval)
                return MatchPollAttempt(
                    False,
                    True,
                    LcuLoopAction.FALLBACK_IMAGE,
                    "image_accept_click_failed",
                    phase,
                )
            _last_accept_click_at[stage] = now
            logger.info("수락 버튼 클릭 완료(%s).", stage)
        else:
            logger.debug("수락 버튼 클릭 쿨다운 중(%s).", stage)
        _last_finding_logged[stage] = True
        return MatchPollAttempt(
            True, True, LcuLoopAction.FALLBACK_IMAGE, "image_accept", phase
        )

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
        return MatchPollAttempt(
            False, True, LcuLoopAction.FALLBACK_IMAGE, "image_finding", phase
        )

    _last_finding_logged[stage] = False
    return MatchPollAttempt(
        False, False, phase_attempt.loop_action, phase_attempt.outcome, phase
    )


def _authoritative_champ_select_exit_from_match_poll(
    attempt: MatchPollAttempt,
) -> Optional[str]:
    if (
        attempt.loop_action == LcuLoopAction.ACT_LCU
        and attempt.phase is not None
        and attempt.phase != PHASE_CHAMP_SELECT
    ):
        return attempt.phase
    return None


def _wait_for_champ_select_after_match_accept(
    lcu: Optional[LcuClient],
    stage: str,
    logger: logging.Logger,
    *,
    interval_sec: float,
    timeout_sec: float,
) -> PhaseLcuAttempt:
    if lcu is None:
        return PhaseLcuAttempt(None, LcuLoopAction.FALLBACK_IMAGE, "not_attempted")

    deadline = time.monotonic() + max(0.0, timeout_sec)
    wait_count = 0

    while True:
        phase_attempt = _poll_lcu_phase_attempt(
            lcu, logger, f"{stage} ChampSelect 대기"
        )
        phase = phase_attempt.phase

        if (
            phase_attempt.loop_action == LcuLoopAction.ACT_LCU
            and phase == PHASE_CHAMP_SELECT
        ):
            logger.info("LCU ChampSelect 진입 확인(%s).", stage)
            return phase_attempt

        if phase_attempt.loop_action == LcuLoopAction.ACT_LCU:
            if phase == PHASE_READY_CHECK:
                ready_attempt = _accept_ready_check_lcu_attempt(lcu, stage, logger)
                if ready_attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE:
                    return PhaseLcuAttempt(
                        phase, ready_attempt.loop_action, ready_attempt.outcome
                    )
            elif phase not in {PHASE_MATCHMAKING, PHASE_NONE}:
                logger.info(
                    "LCU 매칭 수락 후 ChampSelect 대신 phase=%s 감지(%s). "
                    "외부 매칭 사이클로 복귀합니다.",
                    phase,
                    stage,
                )
                return phase_attempt
        elif phase_attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE:
            return phase_attempt

        wait_count += 1
        now = time.monotonic()
        if now >= deadline:
            logger.warning(
                "LCU 매칭 수락 후 ChampSelect 대기 시간 초과(%s,%.1fs).",
                stage,
                timeout_sec,
            )
            return PhaseLcuAttempt(None, LcuLoopAction.WAIT_AUTHORITATIVE, "timeout")

        if wait_count == 1 or wait_count % 5 == 0:
            logger.info(
                "LCU 매칭 수락 후 ChampSelect 대기 중(%s,phase=%s,%d회).",
                stage,
                phase or "UNKNOWN",
                wait_count,
            )
        time.sleep(min(max(0.0, interval_sec), max(0.0, deadline - now)))


def detect_match_reset(
    rect,
    stage: str,
    tpl_find_match: Path,
    tpl_finding_match: Path,
    tpl_accept: Path,
    threshold: float,
    confirm_check_interval: float,
    logger: logging.Logger,
    lcu: Optional[LcuClient] = None,
) -> bool:
    phase_attempt = _poll_lcu_phase_attempt(lcu, logger, stage)
    phase = phase_attempt.phase
    if phase_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
        logger.debug(
            "LCU phase가 authoritative wait 상태입니다(%s,outcome=%s). "
            "대전 찾기 이미지 복귀 판단을 생략합니다.",
            stage,
            phase_attempt.outcome,
        )
        return False

    if phase == PHASE_CHAMP_SELECT:
        logger.debug(
            "LCU 챔피언 선택 상태 유지(%s). 대전 찾기 이미지 복귀 판단을 생략합니다.",
            stage,
        )
        return False
    if phase in {PHASE_IN_PROGRESS, PHASE_RECONNECT, PHASE_WATCH_IN_PROGRESS}:
        logger.info(
            "LCU 인게임 시작 감지(%s). 챔피언 선택 단계를 중단합니다.",
            stage,
        )
        return True
    if phase == PHASE_LOBBY:
        _set_client_state(ClientState.LOBBY, time.monotonic(), logger)
        logger.info("LCU 로비 복귀 감지(%s). 현재 단계를 중단합니다.", stage)
        return True

    poll_attempt = poll_match_state(
        rect,
        stage,
        tpl_finding_match,
        tpl_accept,
        threshold,
        confirm_check_interval,
        logger,
        lcu=lcu,
    )
    accepted, finding = poll_attempt
    if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
        return False
    if accepted or finding:
        logger.info("매칭 상태 재감지(%s). 현재 단계를 중단합니다.", stage)
        return True
    recovered_phase = _authoritative_champ_select_exit_from_match_poll(poll_attempt)
    if recovered_phase is not None:
        logger.info(
            "LCU phase authority 회복 감지(%s,phase=%s). 현재 단계를 중단합니다.",
            stage,
            recovered_phase,
        )
        return True
    if poll_attempt.loop_action != LcuLoopAction.FALLBACK_IMAGE:
        authoritative_phase = poll_attempt.phase
        if (
            authoritative_phase is None
            and phase_attempt.loop_action == LcuLoopAction.ACT_LCU
        ):
            authoritative_phase = phase
        if authoritative_phase is not None:
            logger.info(
                "LCU phase 전환 감지(%s,phase=%s). 현재 단계를 중단합니다.",
                stage,
                authoritative_phase,
            )
            return authoritative_phase != PHASE_CHAMP_SELECT
        return False
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
    phase_attempt = _poll_lcu_phase_attempt(lcu, logger, stage)
    phase = phase_attempt.phase
    if phase_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
        logger.debug(
            "LCU phase가 authoritative wait 상태입니다(%s,outcome=%s). "
            "챔피언 선택 이미지 탐색을 생략합니다.",
            stage,
            phase_attempt.outcome,
        )
        return False

    if phase == PHASE_CHAMP_SELECT:
        logger.info("LCU 챔피언 선택 상태 감지(%s).", stage)
        return True
    if phase is not None:
        logger.debug(
            "LCU phase=%s 이므로 챔피언 선택 이미지 탐색을 생략합니다(%s).",
            phase,
            stage,
        )
        return False

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
    lcu: Optional[LcuClient] = None,
) -> bool:
    modal_attempt = _dismiss_blocking_modal_lcu_attempt(lcu, "픽 팝업", logger)
    if modal_attempt.completed:
        return True

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

    popup_roi = _popup_button_search_roi(rect)
    rois: dict[str, tuple[int, int, int, int]] = {}
    for name in confirm_names:
        rois[name] = popup_roi
    if tpl_decline:
        rois["decline"] = popup_roi

    matches = find_template_matches_once(
        rect, templates, threshold=threshold, search_rois=rois
    )
    if not matches:
        return False

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

    for name, hit in (
        ("decline", matches.get("decline")),
        ("confirm", best_confirm_hit),
    ):
        if hit is None:
            continue

        center, _roi_bgr, _score = hit
        key = f"pick_popup:{name}"
        last = _last_popup_click_at.get(key, 0.0)
        if now - last < POPUP_CLICK_COOLDOWN_SEC:
            return False

        try:
            click_screen(center)
        except Exception as exc:
            logger.warning("팝업 버튼 클릭 실패(%s): %s", name, exc)
            return False

        _last_popup_click_at[key] = now
        if name == "confirm":
            tpl = (
                confirm_name_to_path.get(best_confirm_name)
                if best_confirm_name
                else None
            )
            logger.info(
                "확인 팝업 클릭 처리(tpl=%s).", (tpl.name if tpl else "unknown")
            )
        else:
            logger.info("거절 버튼 클릭 처리.")
        return True

    return False


def _update_my_pick_turn_from_image(
    rect,
    tpl_pick_myturn: Optional[Path],
    threshold: float,
    logger: logging.Logger,
) -> None:
    global _my_pick_turn_miss_streak

    if tpl_pick_myturn is None or RUNTIME_STATE.get("client_state") != ClientState.PICK:
        return

    matches = find_template_matches_once(
        rect, [("myturn", tpl_pick_myturn)], threshold=threshold
    )
    now = time.monotonic()
    myturn_detected = bool(matches) and matches.get("myturn") is not None
    if myturn_detected:
        _my_pick_turn_miss_streak = 0
        _set_my_pick_turn(True, now, logger)
    elif bool(RUNTIME_STATE.get("is_my_pick_turn", False)):
        _my_pick_turn_miss_streak += 1
        if _my_pick_turn_miss_streak >= MY_PICK_TURN_CLEAR_MISS_STREAK:
            _my_pick_turn_miss_streak = 0
            _set_my_pick_turn(False, now, logger)


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
    continue_after_game: object = True,
) -> bool:
    def continuation_enabled() -> bool:
        return should_continue_after_game(continue_after_game)

    if not continuation_enabled():
        logger.info("한 게임 모드: 다음 게임 자동 진행을 하지 않고 postgame 화면에서 종료합니다.")
        return False
    _set_client_state(ClientState.POSTGAME_SCORE, time.monotonic(), logger)
    end_stats_dismissed = False
    play_again_attempted = False
    play_again_completed_at: Optional[float] = None
    pre_end_honor_attempted = False
    while True:
        if not continuation_enabled():
            logger.info("다음 게임 계속 해제 감지. postgame 자동 처리를 중단합니다.")
            return False
        phase_attempt = _poll_lcu_phase_attempt(
            lcu, logger, "엔드 이후", max_age_sec=0.5
        )
        phase = phase_attempt.phase
        if phase_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
            logger.debug(
                "LCU phase가 authoritative wait 상태입니다(엔드 이후,outcome=%s).",
                phase_attempt.outcome,
            )
            time.sleep(interval_sec)
            continue
        if not continuation_enabled():
            logger.info("다음 게임 계속 해제 감지. postgame 자동 처리를 중단합니다.")
            return False
        if phase in {
            PHASE_MATCHMAKING,
            PHASE_CHAMP_SELECT,
            PHASE_IN_PROGRESS,
            PHASE_RECONNECT,
            PHASE_WATCH_IN_PROGRESS,
        }:
            logger.info("LCU postgame 종료 감지: phase=%s", phase)
            return True
        if phase == PHASE_LOBBY:
            if play_again_completed_at is not None:
                elapsed = time.monotonic() - play_again_completed_at
                logger.info(
                    "LCU 다음 게임 요청 후 Lobby 전이를 감지했습니다(%.1fs). "
                    "대전 찾기 재시작을 시도합니다.",
                    elapsed,
                )
            else:
                logger.info(
                    "LCU postgame 이후 로비 감지. 대전 찾기 재시작을 시도합니다."
                )
            start_attempt = _start_matchmaking_lcu_attempt(lcu, "엔드 이후", logger)
            if start_attempt.completed:
                return True
            if play_again_completed_at is not None:
                logger.debug(
                    "LCU 다음 게임 요청 후 Lobby 대전찾기 요청 보류"
                    "(outcome=%s,action=%s). LCU 재시도를 계속합니다.",
                    start_attempt.outcome,
                    start_attempt.loop_action.value,
                )
                time.sleep(interval_sec)
                continue
            if start_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                time.sleep(interval_sec)
                continue
        if phase == PHASE_READY_CHECK:
            _accept_ready_check_via_lcu(lcu, "엔드 이후", logger)
            logger.info("LCU ReadyCheck 감지로 postgame 처리를 종료합니다.")
            return True
        if phase == PHASE_NONE:
            if play_again_completed_at is not None:
                elapsed = time.monotonic() - play_again_completed_at
                logger.debug(
                    "LCU 다음 게임 요청 후 phase=None 유지 중입니다(%.1fs). "
                    "로비/큐 전환을 기다립니다.",
                    elapsed,
                )
                time.sleep(interval_sec)
                continue
            if end_stats_dismissed:
                if not play_again_attempted:
                    play_again_attempt = _play_again_lcu_attempt(
                        lcu, "엔드 이후", logger
                    )
                    if play_again_attempt.completed:
                        play_again_attempted = True
                        play_again_completed_at = time.monotonic()
                        logger.info(
                            "LCU 다음 게임 처리 완료(outcome=%s). 로비/큐 상태를 계속 확인합니다.",
                            play_again_attempt.outcome,
                        )
                        time.sleep(interval_sec)
                        continue
                    if (
                        play_again_attempt.loop_action
                        == LcuLoopAction.WAIT_AUTHORITATIVE
                    ):
                        time.sleep(interval_sec)
                        continue
                    play_again_attempted = True

                start_attempt = _start_matchmaking_lcu_attempt(
                    lcu, "엔드 이후", logger, allow_none_phase=True
                )
                if start_attempt.completed:
                    return True
                logger.debug(
                    "LCU phase=None postgame 이후 대전찾기 요청 보류(outcome=%s,action=%s).",
                    start_attempt.outcome,
                    start_attempt.loop_action.value,
                )
                time.sleep(interval_sec)
                continue

            _dismiss_blocking_modal_lcu_attempt(lcu, "엔드 이후", logger)
            logger.debug(
                "LCU phase=None 상태입니다. 엔드 버튼 이미지를 탐색하지 않습니다."
            )
            time.sleep(interval_sec)
            continue
        if phase == PHASE_PRE_END_OF_GAME:
            if not pre_end_honor_attempted:
                honor_attempt = _honor_vote_lcu_attempt(lcu, "엔드 이후", logger)
                pre_end_honor_attempted = True
                logger.info(
                    "LCU 명예 투표 화면 처리 종료(outcome=%s). "
                    "postgame 전환을 계속 확인합니다.",
                    honor_attempt.outcome,
                )
            else:
                logger.debug(
                    "LCU PreEndOfGame 유지 중입니다. postgame 전환을 기다립니다."
                )
            modal_attempt = _dismiss_blocking_modal_lcu_attempt(
                lcu, "엔드 이후", logger
            )
            if modal_attempt.completed:
                logger.info(
                    "LCU PreEndOfGame 알림 처리 완료(outcome=%s). "
                    "postgame 전환을 계속 확인합니다.",
                    modal_attempt.outcome,
                )
            time.sleep(interval_sec)
            continue
        if phase == PHASE_END_OF_GAME:
            if not play_again_attempted:
                play_again_attempt = _play_again_lcu_attempt(lcu, "엔드 이후", logger)
                if play_again_attempt.completed:
                    play_again_attempted = True
                    play_again_completed_at = time.monotonic()
                    logger.info(
                        "LCU 다음 게임 처리 완료(outcome=%s). 로비/큐 상태를 계속 확인합니다.",
                        play_again_attempt.outcome,
                    )
                    time.sleep(interval_sec)
                    continue
                if play_again_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    time.sleep(interval_sec)
                    continue
                play_again_attempted = True

            if not end_stats_dismissed:
                continue_attempt = _dismiss_end_of_game_stats_lcu_attempt(
                    lcu, "엔드 이후", logger
                )
                if continue_attempt.completed:
                    end_stats_dismissed = True
                    logger.info(
                        "LCU 엔드 계속하기 처리 완료(outcome=%s). 로비/큐 상태를 계속 확인합니다.",
                        continue_attempt.outcome,
                    )
                    time.sleep(interval_sec)
                    continue
                if continue_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    time.sleep(interval_sec)
                    continue
        if phase in {PHASE_WAITING_FOR_STATS, PHASE_PRE_END_OF_GAME, PHASE_END_OF_GAME}:
            if lcu is not None and lcu.is_end_of_game_stats_available():
                logger.debug(
                    "LCU 엔드 통계 사용 가능(%s). 엔드 버튼 탐색을 계속합니다.", phase
                )

        if not continuation_enabled():
            logger.info("다음 게임 계속 해제 감지. postgame 자동 처리를 중단합니다.")
            return False
        _dismiss_blocking_modal_lcu_attempt(lcu, "엔드 이후", logger)
        rect = _visible_rect_or_wait(logger, "엔드 이후", interval_sec)
        if rect is None:
            continue
        if detect_champion_select(
            rect, "엔드 이후", tpl_prepick, available_roles, threshold, logger, lcu=lcu
        ):
            return True

        if not continuation_enabled():
            logger.info("다음 게임 계속 해제 감지. postgame 자동 처리를 중단합니다.")
            return False
        clicked_any = False
        allow_end_button_images = (
            phase_attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE
            or phase
            in {PHASE_WAITING_FOR_STATS, PHASE_PRE_END_OF_GAME, PHASE_END_OF_GAME}
        )
        if allow_end_button_images:
            if tpl_end_next.exists():
                clicked_any = clicked_any or search_and_act(
                    rect, tpl_end_next, threshold=threshold, click=True
                )
            if tpl_end_one_more.exists():
                clicked_any = clicked_any or search_and_act(
                    rect, tpl_end_one_more, threshold=threshold, click=True
                )

        if not continuation_enabled():
            logger.info("다음 게임 계속 해제 감지. postgame 자동 처리를 중단합니다.")
            return False
        poll_attempt = poll_match_state(
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
        accepted, finding = poll_attempt
        if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
            time.sleep(interval_sec)
            continue

        if not continuation_enabled():
            logger.info("다음 게임 계속 해제 감지. postgame 자동 처리를 중단합니다.")
            return False
        start_attempt = _start_matchmaking_lcu_attempt(lcu, "엔드 이후", logger)
        if start_attempt.completed:
            return True
        if start_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
            time.sleep(interval_sec)
            continue

        if not continuation_enabled():
            logger.info("다음 게임 계속 해제 감지. postgame 자동 처리를 중단합니다.")
            return False
        found_find_match = search_and_act(
            rect, tpl_find_match, threshold=threshold, click=True
        )
        if found_find_match:
            _record_matchmaking_start_requested()
            logger.info("엔드 이후 대전 찾기 버튼 클릭 완료.")
            return True

        if clicked_any or finding or accepted:
            time.sleep(interval_sec)
            continue

        logger.debug("엔드/대전 버튼 미검출. %.1fs 후 재시도...", interval_sec)
        time.sleep(interval_sec)


def monitor_ingame_and_postgame(
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
    continue_after_game: object = True,
) -> bool:
    logger.info("인게임 상태 감시 시작. 게임 종료까지 대기합니다.")
    skip_postgame = False
    while True:
        phase_attempt = _poll_lcu_phase_attempt(
            lcu, logger, "인게임 감시", max_age_sec=1.0
        )
        phase = phase_attempt.phase
        if phase_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
            logger.debug(
                "LCU phase가 authoritative wait 상태입니다(인게임 감시,outcome=%s).",
                phase_attempt.outcome,
            )
            time.sleep(1.0)
            continue

        if phase in {
            PHASE_WAITING_FOR_STATS,
            PHASE_PRE_END_OF_GAME,
            PHASE_END_OF_GAME,
        }:
            logger.info("LCU 게임 종료 단계 감지: phase=%s", phase)
            break
        if phase in {PHASE_IN_PROGRESS, PHASE_RECONNECT, PHASE_WATCH_IN_PROGRESS}:
            _set_client_state(ClientState.INGAME, time.monotonic(), logger)
            time.sleep(1.0)
            continue
        if phase in {
            PHASE_LOBBY,
            PHASE_MATCHMAKING,
            PHASE_READY_CHECK,
            PHASE_CHAMP_SELECT,
        }:
            logger.info(
                "LCU 상태가 인게임 이후 단계로 전환되었습니다(phase=%s). postgame 화면 처리를 건너뜁니다.",
                phase,
            )
            if phase == PHASE_READY_CHECK:
                _accept_ready_check_via_lcu(lcu, "인게임 감시", logger)
            skip_postgame = True
            break
        if (
            phase_attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE
            and is_game_client_active()
        ):
            _set_client_state(ClientState.INGAME, time.monotonic(), logger)
            time.sleep(1.0)
            continue
        break

    continue_after_game_now = should_continue_after_game(continue_after_game)
    if skip_postgame:
        if not continue_after_game_now:
            logger.info("한 게임 모드: 인게임 종료 후 다음 매칭을 시작하지 않습니다.")
            return False
        return True

    if not continue_after_game_now:
        logger.info("한 게임 모드: 인게임 종료를 감지했으며 다음 매칭을 시작하지 않습니다.")
        return False

    logger.info("엔드 화면 처리 시작.")
    for tpl in (tpl_end_next, tpl_end_one_more):
        if not tpl.exists():
            logger.warning("엔드 버튼 템플릿이 없습니다: %s", tpl)
    return process_postgame(
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
        continue_after_game=continue_after_game,
    )


def choose_available_pick_index(
    *,
    pick_pool: Sequence[tuple[str, str]],
    candidate_ids: dict[int, int],
    unavailable_ids: frozenset[int],
    current_index: int,
) -> Optional[int]:
    """Prefer the first configured champion not already banned or locked."""
    if not pick_pool or not candidate_ids:
        return current_index if 0 <= current_index < len(pick_pool) else None
    for index, _candidate in enumerate(pick_pool):
        champion_id = candidate_ids.get(index)
        if champion_id is not None and champion_id > 0 and champion_id not in unavailable_ids:
            return index
    return None


def _resolve_pick_pool_champion_ids(
    lcu: Optional[LcuClient],
    pick_pool: Sequence[tuple[str, str]],
    logger: logging.Logger,
) -> dict[int, int]:
    resolver = getattr(lcu, "resolve_champ_select_champion_id_decision", None)
    if not callable(resolver):
        return {}
    resolved: dict[int, int] = {}
    for index, (champion_name, _configured_ban) in enumerate(pick_pool):
        try:
            result = resolver(champion_name)
        except Exception as exc:
            logger.debug("LCU 예비 픽 ID 조회 실패(%s): %s", champion_name, exc)
            return {}
        if not getattr(result, "ok", False):
            logger.debug(
                "LCU 예비 픽 ID 조회 미완료(%s,outcome=%s).",
                champion_name,
                _lcu_status_label(result),
            )
            return {}
        try:
            champion_id = int(getattr(result, "value", 0) or 0)
        except (TypeError, ValueError):
            return {}
        if champion_id <= 0:
            return {}
        resolved[index] = champion_id
    return resolved


def _reconcile_pick_pool_availability(
    lcu: Optional[LcuClient],
    pick_pool: Sequence[tuple[str, str]],
    *,
    current_index: int,
    candidate_ids: Optional[dict[int, int]],
    logger: logging.Logger,
    snapshot: Optional[object] = None,
) -> tuple[int, dict[int, int]]:
    """Read completed bans/picks even before the local pick action begins."""
    snapshot_fn = getattr(lcu, "get_champ_select_snapshot", None)
    if not callable(snapshot_fn):
        return current_index, candidate_ids or {}
    if candidate_ids is None:
        candidate_ids = _resolve_pick_pool_champion_ids(lcu, pick_pool, logger)
    if not candidate_ids:
        return current_index, candidate_ids
    if snapshot is None:
        try:
            result = snapshot_fn()
        except Exception as exc:
            logger.debug("LCU 밴/픽 가용성 조회 실패: %s", exc)
            return current_index, candidate_ids
        if not getattr(result, "ok", False):
            return current_index, candidate_ids
        snapshot = getattr(result, "value", None)
    raw = getattr(snapshot, "raw", None)
    if not isinstance(raw, dict):
        return current_index, candidate_ids
    selected = choose_available_pick_index(
        pick_pool=pick_pool,
        candidate_ids=candidate_ids,
        unavailable_ids=completed_champ_select_champion_ids(raw),
        current_index=current_index,
    )
    if selected is None:
        logger.warning("설정된 모든 픽 후보가 이미 밴 또는 확정 픽 상태입니다.")
        return current_index, candidate_ids
    return selected, candidate_ids


def _champ_select_session_reset_attempt(reason: object) -> ChampSelectLcuAttempt:
    detail = str(reason or "unavailable").strip().casefold().replace(" ", "_")
    return ChampSelectLcuAttempt(
        False,
        LcuLoopAction.ACT_LCU,
        f"session_reset:{detail or 'unavailable'}",
    )


def _commit_ban_with_timing(
    lcu: Optional[LcuClient],
    ban_name: str,
    *,
    logger: logging.Logger,
    interval_sec: float,
    time_left: Optional[float] = None,
    expected_session_identity: Optional[str] = None,
) -> ChampSelectLcuAttempt:
    if expected_session_identity is not None:
        snapshot_fn = getattr(lcu, "get_champ_select_snapshot", None)
        if not callable(snapshot_fn):
            return _champ_select_session_reset_attempt("snapshot_unavailable")
        try:
            latest_result = snapshot_fn()
        except Exception:
            return _champ_select_session_reset_attempt("snapshot_error")
        if not getattr(latest_result, "ok", False):
            outcome = getattr(getattr(latest_result, "status", None), "value", None)
            return _champ_select_session_reset_attempt(outcome or "snapshot_unavailable")
        latest_raw = getattr(getattr(latest_result, "value", None), "raw", None)
        if not isinstance(latest_raw, dict):
            return _champ_select_session_reset_attempt("malformed_snapshot")
        latest_identity = champ_select_session_identity(latest_raw)
        if latest_identity != expected_session_identity:
            return _champ_select_session_reset_attempt("identity_changed")
        latest_time_left = champ_select_time_left_seconds(latest_raw)
        if latest_time_left is not None:
            time_left = latest_time_left

    started_at = time.monotonic()
    logger.info(
        "LCU 밴 실행 시작(time_left=%.1f,target_complete=%.1f,start_window=%.1f,champion=%s)",
        time_left if time_left is not None else -1.0,
        BAN_COMPLETION_TARGET_SEC,
        BAN_COMMIT_WINDOW_SEC,
        ban_name or "NONE",
    )
    attempt = _ban_champ_select_attempt_or_skip(
        lcu, ban_name, logger=logger, interval_sec=interval_sec
    )
    timings = getattr(lcu, "last_champ_select_action_timings", {})
    if not isinstance(timings, dict):
        timings = {}
    logger.info(
        "LCU 밴 실행 결과(completed=%s,outcome=%s,elapsed=%.3f,assignment=%.3f,completion=%.3f,fallback_completion=%.3f)",
        attempt.completed,
        attempt.outcome,
        max(0.0, time.monotonic() - started_at),
        float(timings.get("assignment_elapsed_sec", -1.0)),
        float(timings.get("completion_elapsed_sec", -1.0)),
        float(timings.get("fallback_completion_elapsed_sec", -1.0)),
    )
    return attempt


def _wait_for_late_ban_and_reconcile_pick_pool(
    lcu: Optional[LcuClient],
    pick_pool: Sequence[tuple[str, str]],
    *,
    current_index: int,
    counter_cache_path: Path,
    role: str,
    logger: logging.Logger,
    interval_sec: float,
    candidate_ids: Optional[dict[int, int]] = None,
) -> tuple[int, str, str, ChampSelectLcuAttempt, dict[int, int]]:
    """Keep adapting the prepick, then commit early enough to finish by target."""
    if not pick_pool:
        return (
            current_index,
            "",
            "",
            _commit_ban_with_timing(
                lcu, "", logger=logger, interval_sec=interval_sec
            ),
            {},
        )

    snapshot_fn = getattr(lcu, "get_champ_select_snapshot", None)
    if not callable(snapshot_fn):
        champion_name, configured_ban = pick_pool[current_index]
        ban_name = resolve_ban_name_for_runtime(
            counter_cache_path,
            role=role,
            champion_name=champion_name,
            configured_ban=configured_ban,
            logger=logger,
        )
        return (
            current_index,
            champion_name,
            ban_name,
            _commit_ban_with_timing(
                lcu, ban_name, logger=logger, interval_sec=interval_sec
            ),
            {},
        )

    last_delay_bucket: Optional[int] = None
    session_identity: Optional[str] = None
    while True:
        try:
            snapshot_result = snapshot_fn()
        except Exception as exc:
            logger.debug("LCU 밴 단계 조회 실패: %s", exc)
            return (
                current_index,
                pick_pool[current_index][0],
                "",
                _champ_select_session_reset_attempt("snapshot_error"),
                candidate_ids or {},
            )
        if not getattr(snapshot_result, "ok", False):
            outcome = getattr(getattr(snapshot_result, "status", None), "value", None)
            return (
                current_index,
                pick_pool[current_index][0],
                "",
                _champ_select_session_reset_attempt(outcome or "snapshot_unavailable"),
                candidate_ids or {},
            )
        snapshot = getattr(snapshot_result, "value", None)
        raw = getattr(snapshot, "raw", None)
        if not isinstance(raw, dict):
            return (
                current_index,
                pick_pool[current_index][0],
                "",
                _champ_select_session_reset_attempt("malformed_snapshot"),
                candidate_ids or {},
            )
        current_session_identity = champ_select_session_identity(raw)
        if current_session_identity is None:
            return (
                current_index,
                pick_pool[current_index][0],
                "",
                _champ_select_session_reset_attempt("missing_identity"),
                candidate_ids or {},
            )
        if session_identity is None:
            session_identity = current_session_identity
        elif current_session_identity != session_identity:
            return (
                current_index,
                pick_pool[current_index][0],
                "",
                _champ_select_session_reset_attempt("identity_changed"),
                candidate_ids or {},
            )

        next_index, candidate_ids = _reconcile_pick_pool_availability(
            lcu,
            pick_pool,
            current_index=current_index,
            candidate_ids=candidate_ids,
            logger=logger,
            snapshot=snapshot,
        )
        if next_index != current_index:
            current_index = next_index
            champion_name, _configured_ban = pick_pool[current_index]
            logger.info(
                "내 차례 전 밴/확정 픽을 감지해 예비 픽으로 전환합니다: #%d %s",
                current_index + 1,
                champion_name,
            )
            _champ_select_action_attempt_via_lcu(
                lcu,
                champion_name,
                action_type="pick",
                complete=False,
                stage="사전 예비 픽",
                logger=logger,
            )

        champion_name, configured_ban = pick_pool[current_index]
        ban_name = resolve_ban_name_for_runtime(
            counter_cache_path,
            role=role,
            champion_name=champion_name,
            configured_ban=configured_ban,
            logger=logger,
        )
        actions = getattr(snapshot, "actions", ())
        pick_in_progress = any(
            str(getattr(action, "type", "")).casefold() == "pick"
            and bool(getattr(action, "is_in_progress", False))
            and not bool(getattr(action, "completed", False))
            for action in actions
        )
        if pick_in_progress:
            return (
                current_index,
                champion_name,
                ban_name,
                ChampSelectLcuAttempt(
                    False, LcuLoopAction.WAIT_AUTHORITATIVE, "superseded_by_pick"
                ),
                candidate_ids or {},
            )
        ban_in_progress = any(
            str(getattr(action, "type", "")).casefold() == "ban"
            and bool(getattr(action, "is_in_progress", False))
            and not bool(getattr(action, "completed", False))
            for action in actions
        )
        if not ban_in_progress:
            phase_attempt = _poll_lcu_phase_attempt(lcu, logger, "밴 대기", max_age_sec=0.5)
            if (
                phase_attempt.loop_action == LcuLoopAction.ACT_LCU
                and phase_attempt.phase != PHASE_CHAMP_SELECT
            ):
                return (
                    current_index,
                    champion_name,
                    ban_name,
                    ChampSelectLcuAttempt(
                        False,
                        LcuLoopAction.ACT_LCU,
                        f"phase_exit:{phase_attempt.phase}",
                    ),
                    candidate_ids or {},
                )
            time.sleep(interval_sec)
            continue
        time_left = champ_select_time_left_seconds(raw)
        if time_left is not None and time_left > BAN_COMMIT_WINDOW_SEC:
            bucket = int(time_left)
            if bucket != last_delay_bucket:
                logger.info(
                    "밴 단계 %.1fs 남음. 예비 픽 가용성을 확인하며 %.1fs부터 밴을 시작합니다.",
                    time_left,
                    BAN_COMMIT_WINDOW_SEC,
                )
                last_delay_bucket = bucket
            time.sleep(min(interval_sec, max(0.05, time_left - BAN_COMMIT_WINDOW_SEC)))
            continue
        return (
            current_index,
            champion_name,
            ban_name,
            _commit_ban_with_timing(
                lcu,
                ban_name,
                logger=logger,
                interval_sec=interval_sec,
                time_left=time_left,
                expected_session_identity=session_identity,
            ),
            candidate_ids or {},
        )


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
    parser.add_argument(
        "--continue-after-game",
        action="store_true",
        default=DEFAULT_CONTINUE_AFTER_GAME,
        help="명시적으로 켠 경우에만 게임 종료 뒤 다음 매칭을 계속 진행합니다.",
    )
    parser.add_argument(
        "--continue-after-game-preference-path",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    RUNTIME_STATE["matchmaking_start_pending_at"] = None
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

    continue_after_game_policy = ContinueAfterGamePolicy(
        initial_value=args.continue_after_game,
        preference_path=args.continue_after_game_preference_path,
    )

    def current_continue_after_game() -> bool:
        return continue_after_game_policy.current(logger)

    continue_after_game = current_continue_after_game()
    logger.info(
        "다음 게임 자동 진행: %s",
        "활성" if continue_after_game else "비활성(한 게임 모드)",
    )

    ensure_external_apps_running_once(logger=logger)
    global _LEAGUE_EXIT_GUARD
    _LEAGUE_EXIT_GUARD = LeagueClientExitGuard(league_client_exe_path())
    lcu = LcuClient()

    config = ChampionConfig()
    counter_cache_path = default_counter_cache_path(config.path.resolve())

    print("\n=== 현재 챔피언 설정 ===")
    for role in ROLE_ORDER:
        info = config.get(role) or {}
        champ = str(info.get("champion") or "").strip()
        if not champ:
            continue
        ban = str(info.get("ban") or "").strip()
        if ban:
            print(f"[{role}] {champ} (ban: {display_ban_name_for_summary(ban)})")
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
        pool_for_role = reserve_pick_pools.get(role)
        if not pool_for_role:
            continue
        primary, primary_ban = pool_for_role[0]
        reserves = pool_for_role[1:]
        _print_pick_pool_summary(role, primary, primary_ban, reserves)

    rect: Optional[tuple[int, int, int, int]] = None
    selected: Optional[Path] = None
    width = DEFAULT_IMAGE_SET_WIDTH
    height = DEFAULT_IMAGE_SET_HEIGHT
    while True:
        phase_attempt = _poll_lcu_phase_attempt(lcu, logger, "시작", max_age_sec=0.5)
        phase = phase_attempt.phase
        if phase_attempt.phase == PHASE_READY_CHECK:
            _accept_ready_check_via_lcu(lcu, "시작", logger)

        rect = _visible_rect_for_image_scan(logger, "시작")
        if rect is not None:
            width, height = window_size_from_rect(rect)
            selected = select_image_set(width)
            break

        if phase is not None and phase != PHASE_NONE:
            selected = select_image_set(DEFAULT_IMAGE_SET_WIDTH)
            if selected is not None:
                logger.info(
                    "LCU phase=%s 상태에서 LoL 창이 비가시입니다. "
                    "기본 이미지 세트(%d)를 사용해 LCU 흐름을 계속합니다.",
                    phase,
                    DEFAULT_IMAGE_SET_WIDTH,
                )
                break

        time.sleep(1.0)

    logger.info("클라이언트 크기: %dx%d", width, height)
    if not selected:
        logger.error("images 하위에 사용할 해상도 폴더가 없습니다.")
        return

    tpl_find_match = selected / "lobby_find-match-button.png"
    tpl_finding_match = selected / "lobby_finding-match-text.png"
    tpl_accept = selected / "lobby_accept-button.png"
    tpl_confirm_candidates = _client_confirm_template_candidates(selected)
    tpl_confirm_templates = [p for p in tpl_confirm_candidates if p.exists()]
    if not tpl_confirm_templates:
        logger.error(
            "확인 버튼 템플릿 파일이 없습니다: %s",
            ", ".join(str(p) for p in tpl_confirm_candidates),
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
            logger.warning("LCU 요청 장애 fallback 템플릿 파일이 없습니다: %s", tpl)

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
        logger.info(
            "LCU 요청 장애 fallback용 내 픽 차례 텍스트 감시 활성화: %s",
            tpl_pick_myturn_opt.name,
        )
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
        logger.warning(
            "포지션 템플릿이 없습니다. (LCU 요청 장애 시 이미지 role fallback 비활성)"
        )

    interval_sec = 1.0
    threshold = 0.85
    confirm_check_interval = 0.2

    while True:
        phase_attempt_at_cycle = _poll_lcu_phase_attempt(
            lcu, logger, "사이클 시작", max_age_sec=0.5
        )
        phase_at_cycle = phase_attempt_at_cycle.phase
        if phase_attempt_at_cycle.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
            logger.debug(
                "LCU phase가 authoritative wait 상태입니다(사이클 시작,outcome=%s).",
                phase_attempt_at_cycle.outcome,
            )
            time.sleep(interval_sec)
            continue

        cycle_ingame_active = phase_at_cycle in {
            PHASE_IN_PROGRESS,
            PHASE_RECONNECT,
            PHASE_WATCH_IN_PROGRESS,
        } or (
            phase_attempt_at_cycle.loop_action == LcuLoopAction.FALLBACK_IMAGE
            and is_game_client_active()
        )

        if _should_process_postgame_at_cycle(phase_at_cycle):
            logger.info(
                "LCU postgame 단계 감지(사이클 시작,phase=%s). 엔드 화면 처리로 전환합니다.",
                phase_at_cycle,
            )
            should_continue = process_postgame(
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
                continue_after_game=current_continue_after_game,
            )
            if not should_continue:
                logger.info("한 게임 모드 완료. 자동화를 종료합니다.")
                return
            logger.info("다음 매칭 사이클을 시작합니다.")
            continue

        if cycle_ingame_active:
            logger.info(
                "이미 인게임 상태 감지(사이클 시작). 게임 종료 감시로 전환합니다."
            )
            should_continue = monitor_ingame_and_postgame(
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
                continue_after_game=current_continue_after_game,
            )
            if not should_continue:
                logger.info("한 게임 모드 완료. 자동화를 종료합니다.")
                return
            logger.info("다음 매칭 사이클을 시작합니다.")
            continue

        modal_attempt = _dismiss_blocking_modal_lcu_attempt(lcu, "사이클 시작", logger)
        if (
            not modal_attempt.completed
            and _should_click_popup_confirm_at_cycle_start(phase_at_cycle)
            and tpl_confirm_templates
        ):
            rect_for_modal = _visible_rect_for_image_scan(logger, "사이클 시작")
            if rect_for_modal is not None:
                if _dismiss_blocking_modal_ui_fallback(
                    rect_for_modal,
                    tpl_confirm_templates,
                    threshold,
                    "사이클 시작",
                    logger,
                ):
                    time.sleep(confirm_check_interval)
                    continue

        if phase_at_cycle in {None, PHASE_NONE}:
            _maybe_auto_create_lobby_for_home_screen(
                lcu, phase_at_cycle, logger
            )

        _set_client_state(ClientState.LOBBY, time.monotonic(), logger)
        accepted_early = False
        finding_early = False
        initial_authoritative_phase: Optional[str] = (
            phase_at_cycle
            if phase_attempt_at_cycle.loop_action == LcuLoopAction.ACT_LCU
            else None
        )
        initial_finding_source = (
            "lcu" if initial_authoritative_phase == PHASE_MATCHMAKING else None
        )
        matchmaking_tracker = MatchmakingSearchTracker(
            last_authoritative_phase=initial_authoritative_phase,
            finding_source=initial_finding_source,
            start_pending_since=(
                None
                if initial_finding_source is not None
                else _matchmaking_start_pending_at()
            ),
        )
        RUNTIME_STATE["matchmaking_start_pending_at"] = (
            matchmaking_tracker.start_pending_since
        )
        restart_matching_cycle = False

        def observed_manual_matchmaking_cancel(
            attempt: MatchPollAttempt, *, image_observation_complete: bool = False
        ) -> bool:
            source = (
                matchmaking_tracker.finding_source
                or ("start-pending" if matchmaking_tracker.start_pending else "unknown")
            )
            cancelled = matchmaking_tracker.observe(
                attempt,
                now=time.monotonic(),
                image_observation_complete=image_observation_complete,
            )
            RUNTIME_STATE["matchmaking_start_pending_at"] = (
                matchmaking_tracker.start_pending_since
            )
            if cancelled:
                logger.info(
                    "매칭 검색 종료를 감지했습니다(source=%s). "
                    "사용자 매칭 취소로 판단해 자동화를 종료합니다.",
                    source,
                )
                return True
            return False

        def confirm_champ_select_after_lcu_accept(stage: str) -> Optional[bool]:
            nonlocal restart_matching_cycle
            champ_select_attempt = _wait_for_champ_select_after_match_accept(
                lcu,
                stage,
                logger,
                interval_sec=interval_sec,
                timeout_sec=max(12.0, interval_sec * 20.0),
            )
            if (
                champ_select_attempt.loop_action == LcuLoopAction.ACT_LCU
                and champ_select_attempt.phase == PHASE_CHAMP_SELECT
            ):
                return True
            if champ_select_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                logger.info(
                    "LCU 매칭 수락 후 ChampSelect 대기 보류(%s,outcome=%s). "
                    "외부 매칭 사이클로 복귀합니다.",
                    stage,
                    champ_select_attempt.outcome,
                )
                restart_matching_cycle = True
                return False
            logger.info(
                "LCU 매칭 수락 후 ChampSelect 미진입(%s,phase=%s,outcome=%s).",
                stage,
                champ_select_attempt.phase or "UNKNOWN",
                champ_select_attempt.outcome,
            )
            restart_matching_cycle = True
            return False

        logger.info("대전 찾기 버튼 탐색 시작.")
        while True:
            poll_attempt = poll_match_state(
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
            accepted, finding = poll_attempt
            if observed_manual_matchmaking_cancel(poll_attempt):
                return
            if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                time.sleep(interval_sec)
                continue
            if (
                poll_attempt.loop_action == LcuLoopAction.ACT_LCU
                and poll_attempt.phase == PHASE_CHAMP_SELECT
            ):
                logger.info("LCU 챔피언 선택 진입 감지(사이클 진입).")
                accepted_early = True
                break
            if accepted:
                if poll_attempt.loop_action == LcuLoopAction.ACT_LCU:
                    champ_select_ready = confirm_champ_select_after_lcu_accept(
                        "사이클 진입"
                    )
                    if champ_select_ready is None:
                        continue
                    if not champ_select_ready:
                        break
                logger.info(
                    "LCU 매칭 수락 처리 완료(사이클 진입). 수락 단계로 이동합니다."
                )
                accepted_early = True
                break
            if finding:
                logger.info("LCU 매칭 중 감지(사이클 진입). 화면 캡처를 건너뜁니다.")
                finding_early = True
                break

            rect = _visible_rect_or_wait(logger, "사이클 진입", interval_sec)
            if rect is None:
                continue
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

            poll_attempt = poll_match_state(
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
            accepted, finding = poll_attempt
            if observed_manual_matchmaking_cancel(
                poll_attempt, image_observation_complete=True
            ):
                return
            if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                time.sleep(interval_sec)
                continue
            if accepted:
                if poll_attempt.loop_action == LcuLoopAction.ACT_LCU:
                    champ_select_ready = confirm_champ_select_after_lcu_accept(
                        "사이클 진입"
                    )
                    if champ_select_ready is None:
                        continue
                    if not champ_select_ready:
                        break
                logger.info("이미 매칭 수락 감지(사이클 진입). 수락 단계로 이동합니다.")
                accepted_early = True
                break
            if finding:
                logger.info("이미 매칭 중 감지(사이클 진입). 수락 감시로 전환합니다.")
                finding_early = True
                break

            start_attempt = _start_matchmaking_lcu_attempt(lcu, "사이클 진입", logger)
            if start_attempt.completed:
                now = time.monotonic()
                matchmaking_tracker.mark_start_requested(
                    _matchmaking_start_pending_at() or now
                )
                _reset_match_timer_by_find_match_click(now, logger)
                break
            if start_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                time.sleep(interval_sec)
                continue

            success = search_and_act(
                rect, tpl_find_match, threshold=threshold, click=True
            )
            if success:
                now = time.monotonic()
                matchmaking_tracker.mark_start_requested(
                    _record_matchmaking_start_requested(now)
                )
                _reset_match_timer_by_find_match_click(now, logger)
                logger.info("대전 찾기 버튼 클릭 완료.")
                break
            logger.debug("대전 찾기 버튼 미검출. %.1fs 후 재시도...", interval_sec)
            time.sleep(interval_sec)

        if restart_matching_cycle:
            continue

        if not accepted_early:
            last_finding_state = finding_early if finding_early else None
            while True:
                poll_attempt = poll_match_state(
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
                accepted, finding = poll_attempt
                if observed_manual_matchmaking_cancel(poll_attempt):
                    return
                if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    time.sleep(interval_sec)
                    continue
                if (
                    poll_attempt.loop_action == LcuLoopAction.ACT_LCU
                    and poll_attempt.phase == PHASE_CHAMP_SELECT
                ):
                    logger.info("LCU 챔피언 선택 진입 감지(매칭 단계).")
                    break
                if accepted:
                    if poll_attempt.loop_action == LcuLoopAction.ACT_LCU:
                        champ_select_ready = confirm_champ_select_after_lcu_accept(
                            "매칭 단계"
                        )
                        if champ_select_ready is None:
                            continue
                        if not champ_select_ready:
                            break
                    break
                if finding:
                    if finding != last_finding_state:
                        logger.info("매칭 상태 텍스트: 감지")
                        last_finding_state = finding
                    continue

                rect = _visible_rect_or_wait(logger, "매칭 단계", interval_sec)
                if rect is None:
                    continue
                poll_attempt = poll_match_state(
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
                accepted, finding = poll_attempt
                if observed_manual_matchmaking_cancel(
                    poll_attempt, image_observation_complete=True
                ):
                    return
                if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    time.sleep(interval_sec)
                    continue
                if finding != last_finding_state:
                    logger.info("매칭 상태 텍스트: %s", "감지" if finding else "미감지")
                    last_finding_state = finding
                if accepted:
                    if poll_attempt.loop_action == LcuLoopAction.ACT_LCU:
                        champ_select_ready = confirm_champ_select_after_lcu_accept(
                            "매칭 단계"
                        )
                        if champ_select_ready is None:
                            continue
                        if not champ_select_ready:
                            break
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
                if matchmaking_tracker.start_pending:
                    logger.debug(
                        "대전 찾기 요청 반영 대기 중입니다. 자동 재요청을 보류합니다."
                    )
                    time.sleep(interval_sec)
                    continue
                start_attempt = _start_matchmaking_lcu_attempt(lcu, "매칭 단계", logger)
                if start_attempt.completed:
                    now = time.monotonic()
                    matchmaking_tracker.mark_start_requested(
                        _matchmaking_start_pending_at() or now
                    )
                    _reset_match_timer_by_find_match_click(now, logger)
                    logger.info("LCU 대전 찾기 재요청 완료.")
                    time.sleep(interval_sec)
                    continue
                if start_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    time.sleep(interval_sec)
                    continue

                clicked = search_and_act(
                    rect, tpl_find_match, threshold=threshold, click=True
                )
                if clicked:
                    now = time.monotonic()
                    matchmaking_tracker.mark_start_requested(
                        _record_matchmaking_start_requested(now)
                    )
                    _reset_match_timer_by_find_match_click(now, logger)
                    logger.info("대전 찾기 버튼 재클릭.")
                else:
                    logger.debug(
                        "대전 찾기 버튼 미검출. %.1fs 후 재시도...", interval_sec
                    )
                time.sleep(interval_sec)

        if restart_matching_cycle:
            continue

        _set_client_state(ClientState.PREPICK, time.monotonic(), logger)

        def detect_role_by_image() -> Optional[str]:
            nonlocal role_fallback_phase_exit

            if not available_roles:
                logger.debug("포지션 템플릿이 없어 이미지 role fallback을 건너뜁니다.")
                return None

            logger.info("포지션 텍스트 탐색 시작.")
            last_role = None
            stable_hits = 0
            while True:
                rect = _visible_rect_or_wait(logger, "포지션 탐색", interval_sec)
                if rect is None:
                    return None
                try_pick_popups(
                    rect,
                    tpl_confirm_templates,
                    tpl_pick_decline_opt,
                    threshold,
                    logger,
                    lcu=lcu,
                )
                poll_attempt = poll_match_state(
                    rect,
                    "포지션 탐색 중",
                    tpl_finding_match,
                    tpl_accept,
                    threshold,
                    confirm_check_interval,
                    logger,
                    lcu=lcu,
                )
                accepted, finding = poll_attempt
                if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    time.sleep(interval_sec)
                    continue
                phase_exit = _authoritative_champ_select_exit_from_match_poll(
                    poll_attempt
                )
                if phase_exit is not None:
                    role_fallback_phase_exit = phase_exit
                    logger.info(
                        "이미지 role fallback 중 ChampSelect 이탈 감지"
                        "(phase=%s). 외부 매칭 사이클로 복귀합니다.",
                        phase_exit,
                    )
                    return None
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
                        logger.info(
                            "포지션 감지: %s (score=%.3f)",
                            detected_role,
                            best_score,
                        )
                        return detected_role
                else:
                    last_role = None
                    stable_hits = 0

                logger.debug("포지션 미검출. %.1fs 후 재시도...", interval_sec)
                time.sleep(interval_sec)

        role_fallback_phase_exit: Optional[str] = None
        role_attempt = _detect_role_lcu_first_with_retry_attempt(
            lcu,
            stage="포지션 탐색",
            logger=logger,
            image_detector=detect_role_by_image,
            interval_sec=interval_sec,
        )
        if role_attempt.outcome.startswith("phase_exit:"):
            continue
        if role_fallback_phase_exit is not None:
            continue
        role = role_attempt.role

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

            # A ban or completed pick may already have happened by the time role
            # detection finishes.  Reconcile before the first prepick rather than
            # waiting until our own action is available.
            pick_index, availability_candidate_ids = _reconcile_pick_pool_availability(
                lcu,
                pick_pool,
                current_index=pick_index,
                candidate_ids=None,
                logger=logger,
            )
            champion_name, ban_name = pick_pool[pick_index]
            if pick_index:
                logger.info(
                    "프리픽 전 밴/확정 픽을 감지해 예비 픽으로 시작합니다: #%d %s",
                    pick_index + 1,
                    champion_name,
                )

            restart_cycle = False

            prepick_lcu_attempt = _wait_champ_select_action_via_lcu(
                lcu,
                champion_name,
                action_type="pick",
                complete=False,
                stage="프리픽",
                logger=logger,
                interval_sec=interval_sec,
                timeout_sec=max(3.0, interval_sec * 3.0),
            )
            if _handle_champ_select_phase_exit(
                prepick_lcu_attempt, lcu, "프리픽", logger
            ):
                continue
            if prepick_lcu_attempt.completed:
                _set_client_state(ClientState.PREPICK, time.monotonic(), logger)
            elif prepick_lcu_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                logger.info(
                    "LCU 프리픽 미완료(outcome=%s). 외부 매칭 사이클로 돌아가지 않고 ChampSelect 흐름을 유지합니다.",
                    prepick_lcu_attempt.outcome,
                )
            else:
                if not tpl_prepick.exists():
                    logger.warning(
                        "프리픽 검색 fallback 템플릿이 없어 이미지 처리를 건너뜁니다: %s",
                        tpl_prepick,
                    )
                    continue
                while True:
                    rect = _visible_rect_or_wait(logger, "프리픽 검색", interval_sec)
                    if rect is None:
                        restart_cycle = True
                        break
                    try_pick_popups(
                        rect,
                        tpl_confirm_templates,
                        tpl_pick_decline_opt,
                        threshold,
                        logger,
                        lcu=lcu,
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
                        lcu=lcu,
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
                    logger.debug(
                        "프리픽 검색창 미검출. %.1fs 후 재시도...", interval_sec
                    )
                    time.sleep(interval_sec)

                if restart_cycle:
                    continue

                rect = _visible_rect_or_wait(logger, "프리픽 입력", interval_sec)
                if rect is None:
                    continue
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
                    logger.info(
                        "좌표가 설정되지 않았습니다. 수동으로 챔피언을 선택하세요."
                    )

            tpl_banpick_search = selected / "banpick_search-text.png"
            tpl_ban_button = selected / "banpick_ban-button.png"

            (
                pick_index,
                champion_name,
                ban_name,
                ban_lcu_attempt,
                availability_candidate_ids,
            ) = _wait_for_late_ban_and_reconcile_pick_pool(
                lcu,
                pick_pool,
                current_index=pick_index,
                counter_cache_path=counter_cache_path,
                role=role or "",
                logger=logger,
                interval_sec=interval_sec,
                candidate_ids=availability_candidate_ids,
            )
            if _handle_champ_select_phase_exit(ban_lcu_attempt, lcu, "밴", logger):
                continue
            if ban_lcu_attempt.completed:
                _set_client_state(ClientState.BANPICK, time.monotonic(), logger)
            elif ban_lcu_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                if ban_lcu_attempt.outcome != "missing_ban":
                    logger.warning(
                        "LCU 밴 완료 미확정(outcome=%s). 외부 매칭 사이클 재시작 없이 픽 단계 감지를 계속합니다.",
                        ban_lcu_attempt.outcome,
                    )
            else:
                if not tpl_banpick_search.exists():
                    logger.warning(
                        "밴 검색 fallback 템플릿이 없어 이미지 처리를 건너뜁니다: %s",
                        tpl_banpick_search,
                    )
                    return
                logger.info("밴 챔피언 검색 시작: %s", ban_name)
                ban_search_misses = 0
                while True:
                    rect = _visible_rect_or_wait(logger, "밴 검색", interval_sec)
                    if rect is None:
                        restart_cycle = True
                        break
                    try_pick_popups(
                        rect,
                        tpl_confirm_templates,
                        tpl_pick_decline_opt,
                        threshold,
                        logger,
                        lcu=lcu,
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
                        lcu=lcu,
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
                    ban_search_misses += 1
                    if ban_search_misses == 1 or ban_search_misses % 5 == 0:
                        logger.info(
                            "밴 검색창 미검출(%d회). %.1fs 후 재시도...",
                            ban_search_misses,
                            interval_sec,
                        )
                    else:
                        logger.debug(
                            "밴 검색창 미검출. %.1fs 후 재시도...", interval_sec
                        )
                    time.sleep(interval_sec)

                if restart_cycle:
                    continue

                rect = _visible_rect_or_wait(logger, "밴 입력", interval_sec)
                if rect is None:
                    continue
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
                    rect = _visible_rect_or_wait(logger, "밴 버튼 대기", interval_sec)
                    if rect is None:
                        restart_cycle = True
                        break
                    try_pick_popups(
                        rect,
                        tpl_confirm_templates,
                        tpl_pick_decline_opt,
                        threshold,
                        logger,
                        lcu=lcu,
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
                        lcu=lcu,
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
                next_pick_index, availability_candidate_ids = (
                    _reconcile_pick_pool_availability(
                        lcu,
                        pick_pool,
                        current_index=pick_index,
                        candidate_ids=availability_candidate_ids,
                        logger=logger,
                    )
                )
                if next_pick_index != pick_index:
                    pick_index = next_pick_index
                    champion_name, next_ban = pick_pool[pick_index]
                    ban_name = resolve_ban_name_for_runtime(
                        counter_cache_path,
                        role=role or "",
                        champion_name=champion_name,
                        configured_ban=next_ban,
                        logger=logger,
                    )
                    logger.info(
                        "내 차례 전 밴/확정 픽을 감지해 픽 후보를 즉시 전환합니다: #%d %s",
                        pick_index + 1,
                        champion_name,
                    )
                lcu_pick_attempt = _champ_select_action_attempt_via_lcu(
                    lcu,
                    champion_name,
                    action_type="pick",
                    complete=True,
                    stage="픽 준비",
                    logger=logger,
                )
                lcu_pick_attempt = _guard_champ_select_phase_exit(
                    lcu_pick_attempt, lcu, "픽 준비", logger
                )
                if _handle_champ_select_phase_exit(
                    lcu_pick_attempt, lcu, "픽 준비", logger
                ):
                    restart_cycle = True
                    break
                if lcu_pick_attempt.completed:
                    _set_my_pick_turn(False, time.monotonic(), logger)
                    break
                if lcu_pick_attempt.outcome == "action_rejected":
                    logger.info(
                        "LCU 픽 준비가 거절되었습니다. 현재 챔피언을 사용할 수 없는 것으로 판단하고 예비 픽을 시도합니다: %s",
                        champion_name,
                    )
                    switched = False
                    reserve_wait_authoritative = False
                    reserve_fallback_image = False
                    for next_idx in range(pick_index + 1, len(pick_pool)):
                        next_champ, next_ban = pick_pool[next_idx]
                        logger.info(
                            "예비 챔피언 전환 시도: #%d %s", next_idx + 1, next_champ
                        )
                        reserve_lcu_attempt = _champ_select_action_attempt_via_lcu(
                            lcu,
                            next_champ,
                            action_type="pick",
                            complete=True,
                            stage="예비 픽 준비",
                            logger=logger,
                        )
                        reserve_lcu_attempt = _guard_champ_select_phase_exit(
                            reserve_lcu_attempt, lcu, "예비 픽 준비", logger
                        )
                        if _handle_champ_select_phase_exit(
                            reserve_lcu_attempt, lcu, "예비 픽 준비", logger
                        ):
                            restart_cycle = True
                            break
                        if reserve_lcu_attempt.completed:
                            pick_index = next_idx
                            champion_name = next_champ
                            ban_name = resolve_ban_name_for_runtime(
                                counter_cache_path,
                                role=role or "",
                                champion_name=champion_name,
                                configured_ban=next_ban,
                                logger=logger,
                            )
                            logger.info("LCU 예비 픽 준비 완료: %s", champion_name)
                            _set_my_pick_turn(False, time.monotonic(), logger)
                            switched = True
                            break
                        if reserve_lcu_attempt.outcome == "action_rejected":
                            logger.info(
                                "예비 챔피언도 LCU에서 거절되었습니다. 다음 후보로 진행: %s",
                                next_champ,
                            )
                            continue
                        if (
                            reserve_lcu_attempt.loop_action
                            == LcuLoopAction.WAIT_AUTHORITATIVE
                        ):
                            logger.debug(
                                "LCU 예비 픽 대기(outcome=%s). 같은 루프에서 예비 이미지 전환을 스킵합니다.",
                                reserve_lcu_attempt.outcome,
                            )
                            reserve_wait_authoritative = True
                            break

                        reserve_fallback_image = True
                        logger.debug(
                            "LCU 예비 픽 요청 실패(outcome=%s). 기존 이미지 fallback 흐름으로 진행합니다.",
                            reserve_lcu_attempt.outcome,
                        )
                        break

                    if restart_cycle:
                        break
                    if switched:
                        break
                    if reserve_wait_authoritative:
                        time.sleep(interval_sec)
                        continue
                    if not reserve_fallback_image:
                        logger.warning(
                            "사용 가능한 예비 챔피언을 찾지 못했습니다. 수동 픽이 필요합니다."
                        )
                        time.sleep(interval_sec)
                        continue

                if lcu_pick_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    now = time.monotonic()
                    action_type = _apply_lcu_champ_select_action_state(
                        lcu, now, logger, "픽 준비 대기"
                    )
                    if action_type != "pick":
                        _set_my_pick_turn(False, now, logger)
                    time.sleep(interval_sec)
                    continue

                rect = _visible_rect_or_wait(logger, "픽 준비", interval_sec)
                if rect is None:
                    continue
                try_pick_popups(
                    rect,
                    tpl_confirm_templates,
                    tpl_pick_decline_opt,
                    threshold,
                    logger,
                    lcu=lcu,
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
                    lcu=lcu,
                ):
                    restart_cycle = True
                    break

                if not tpl_pick_ready.exists():
                    logger.warning(
                        "픽 준비 fallback 템플릿이 없어 이미지 처리를 건너뜁니다: %s",
                        tpl_pick_ready,
                    )
                    time.sleep(interval_sec)
                    continue

                _update_my_pick_turn_from_image(
                    rect,
                    tpl_pick_myturn_opt,
                    threshold,
                    logger,
                )
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
                    if (
                        wait_hit is not None
                        and ready_hit is None
                        and disabled_hit is None
                    ):
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
                    myturn_updated_at = RUNTIME_STATE.get(
                        "my_pick_turn_updated_at", 0.0
                    )
                    if not isinstance(myturn_updated_at, (int, float)):
                        myturn_updated_at = 0.0
                    myturn_age = now - float(myturn_updated_at)

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
                    reserve_wait_authoritative = False
                    for next_idx in range(pick_index + 1, len(pick_pool)):
                        next_champ, next_ban = pick_pool[next_idx]
                        logger.info(
                            "예비 챔피언 전환 시도: #%d %s", next_idx + 1, next_champ
                        )

                        reserve_lcu_attempt = _champ_select_action_attempt_via_lcu(
                            lcu,
                            next_champ,
                            action_type="pick",
                            complete=True,
                            stage="예비 픽 준비",
                            logger=logger,
                        )
                        reserve_lcu_attempt = _guard_champ_select_phase_exit(
                            reserve_lcu_attempt, lcu, "예비 픽 준비", logger
                        )
                        if _handle_champ_select_phase_exit(
                            reserve_lcu_attempt, lcu, "예비 픽 준비", logger
                        ):
                            restart_cycle = True
                            break
                        if reserve_lcu_attempt.completed:
                            pick_index = next_idx
                            champion_name = next_champ
                            ban_name = resolve_ban_name_for_runtime(
                                counter_cache_path,
                                role=role or "",
                                champion_name=champion_name,
                                configured_ban=next_ban,
                                logger=logger,
                            )
                            logger.info("LCU 예비 픽 준비 완료: %s", champion_name)
                            _set_my_pick_turn(False, time.monotonic(), logger)
                            switched = True
                            break
                        if (
                            reserve_lcu_attempt.loop_action
                            == LcuLoopAction.WAIT_AUTHORITATIVE
                        ):
                            logger.debug(
                                "LCU 예비 픽 대기(outcome=%s). "
                                "같은 루프에서 예비 이미지 전환을 스킵합니다.",
                                reserve_lcu_attempt.outcome,
                            )
                            reserve_wait_authoritative = True
                            break

                        rect2 = _visible_rect_or_wait(
                            logger, "예비 픽 준비", interval_sec
                        )
                        if rect2 is None:
                            reserve_wait_authoritative = True
                            break
                        try_pick_popups(
                            rect2,
                            tpl_confirm_templates,
                            tpl_pick_decline_opt,
                            threshold,
                            logger,
                            lcu=lcu,
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
                        rect3 = _visible_rect_or_wait(
                            logger, "예비 픽 확인", interval_sec
                        )
                        if rect3 is None:
                            reserve_wait_authoritative = True
                            break
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
                        ban_name = resolve_ban_name_for_runtime(
                            counter_cache_path,
                            role=role or "",
                            champion_name=champion_name,
                            configured_ban=next_ban,
                            logger=logger,
                        )
                        logger.info(
                            "픽 준비 버튼 클릭 완료(예비 전환): %s", champion_name
                        )
                        _set_my_pick_turn(False, time.monotonic(), logger)
                        switched = True
                        break

                    if restart_cycle:
                        break
                    if switched:
                        break
                    if reserve_wait_authoritative:
                        time.sleep(interval_sec)
                        continue

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
            while True:
                if _LEAGUE_EXIT_GUARD is not None and _LEAGUE_EXIT_GUARD.should_exit():
                    _exit_after_league_client_closed(logger)
                phase_attempt = _poll_lcu_phase_attempt(
                    lcu, logger, "인게임 시작 대기", max_age_sec=0.5
                )
                phase = phase_attempt.phase
                if phase_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    logger.debug(
                        "LCU phase가 authoritative wait 상태입니다"
                        "(인게임 시작 대기,outcome=%s).",
                        phase_attempt.outcome,
                    )
                    time.sleep(0.3)
                    continue

                if phase in {
                    PHASE_IN_PROGRESS,
                    PHASE_RECONNECT,
                    PHASE_WATCH_IN_PROGRESS,
                }:
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
                if (
                    phase_attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE
                    and is_game_client_active()
                ):
                    _set_client_state(ClientState.INGAME, time.monotonic(), logger)
                    break
                rect = _visible_rect_for_image_scan(logger, "인게임 시작 대기")
                if rect is not None:
                    try_pick_popups(
                        rect,
                        tpl_confirm_templates,
                        tpl_pick_decline_opt,
                        threshold,
                        logger,
                        lcu=lcu,
                    )

                    wait_iter += 1
                    if (
                        phase_attempt.loop_action == LcuLoopAction.FALLBACK_IMAGE
                        and wait_iter % 10 == 0
                    ):
                        if search_and_act(
                            rect, tpl_find_match, threshold=threshold, click=False
                        ):
                            _set_client_state(
                                ClientState.LOBBY, time.monotonic(), logger
                            )
                            logger.info(
                                "대전 찾기 화면 복귀 감지(인게임 시작 대기). 사이클을 재시작합니다."
                            )
                            restart_cycle = True
                            break
                time.sleep(0.3)

            if restart_cycle:
                continue

            should_continue = monitor_ingame_and_postgame(
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
                continue_after_game=current_continue_after_game,
            )
            if not should_continue:
                logger.info("한 게임 모드 완료. 자동화를 종료합니다.")
                return

        logger.info("다음 매칭 사이클을 시작합니다.")


def main(argv: Optional[list[str]] = None) -> None:
    argv_list = list(argv) if argv is not None else sys.argv[1:]
    if argv_list:
        cli_argv = [a for a in argv_list if a != "--cli"]
        cli_main(cli_argv)
        return

    logger = logging.getLogger("lolmanager")
    try:
        ensure_external_apps_running_once(logger=logger)
    except Exception as exc:
        logger.warning("외부 앱 자동 실행 점검 실패: %s", exc)

    from lolmanager.gui.app_gui import main as run_app_gui

    run_app_gui()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.getLogger("lolmanager").info("인터럽트 감지, 스크립트 종료.")
