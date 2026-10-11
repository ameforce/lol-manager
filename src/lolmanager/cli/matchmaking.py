from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional, Protocol, Sequence

from lolmanager.cli.runtime_state import RuntimeState
from lolmanager.core.client_state import ClientState
from lolmanager.core.lcu_client import (
    LcuClient, LcuLoopAction, PHASE_LOBBY, PHASE_CHAMP_SELECT,
    PHASE_IN_PROGRESS, PHASE_MATCHMAKING, PHASE_READY_CHECK, PHASE_RECONNECT,
    PHASE_WATCH_IN_PROGRESS,
)


MATCHMAKING_START_CONFIRM_GRACE_SEC = 1.0


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


class PhaseObservation(Protocol):
    phase: str | None
    loop_action: LcuLoopAction
    outcome: str


class ActionAttempt(Protocol):
    completed: bool
    loop_action: LcuLoopAction


@dataclass(frozen=True)
class MatchmakingContext:
    state: RuntimeState
    lcu: LcuClient
    logger: logging.Logger
    tpl_find_match: Path
    tpl_finding_match: Path
    tpl_accept: Path
    tpl_confirm_templates: Sequence[Path]
    tpl_prepick: Path
    available_roles: list[tuple[str, Path]]
    threshold: float
    confirm_check_interval: float
    interval_sec: float


@dataclass(frozen=True)
class MatchmakingActions:
    set_client_state: Callable[[ClientState, float, logging.Logger], None]
    poll_match_state: Callable[..., MatchPollAttempt]
    visible_rect_or_wait: Callable[..., tuple[int, int, int, int] | None]
    detect_champion_select: Callable[..., bool]
    start_matchmaking: Callable[..., ActionAttempt]
    wait_for_champ_select: Callable[..., PhaseObservation]
    record_start_requested: Callable[[float], float]
    start_pending_at: Callable[[], float | None]
    reset_match_timer: Callable[[float, logging.Logger], None]
    search_and_act: Callable[..., bool]


class MatchmakingOutcome(Enum):
    READY = "ready"
    RETRY = "retry"
    STOP = "stop"


@dataclass
class MatchmakingController:
    """Own queue entry, acceptance and manual-cancellation transitions."""

    context: MatchmakingContext
    actions: MatchmakingActions

    def run(self, phase: str | None, phase_action: LcuLoopAction) -> MatchmakingOutcome:
        self.actions.set_client_state(ClientState.LOBBY, time.monotonic(), self.context.logger)
        accepted_early = False
        finding_early = False
        initial_authoritative_phase: Optional[str] = (
            phase
            if phase_action == LcuLoopAction.ACT_LCU
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
                else self.actions.start_pending_at()
            ),
        )
        self.context.state.matchmaking_start_pending_at = (
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
            self.context.state.matchmaking_start_pending_at = (
                matchmaking_tracker.start_pending_since
            )
            if cancelled:
                self.context.logger.info(
                    "매칭 검색 종료를 감지했습니다(source=%s). "
                    "사용자 매칭 취소로 판단해 자동화를 종료합니다.",
                    source,
                )
                return True
            return False

        def confirm_champ_select_after_lcu_accept(stage: str) -> Optional[bool]:
            nonlocal restart_matching_cycle
            champ_select_attempt = self.actions.wait_for_champ_select(
                self.context.lcu,
                stage,
                self.context.logger,
                interval_sec=self.context.interval_sec,
                timeout_sec=max(12.0, self.context.interval_sec * 20.0),
            )
            if (
                champ_select_attempt.loop_action == LcuLoopAction.ACT_LCU
                and champ_select_attempt.phase == PHASE_CHAMP_SELECT
            ):
                return True
            if champ_select_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                self.context.logger.info(
                    "LCU 매칭 수락 후 ChampSelect 대기 보류(%s,outcome=%s). "
                    "외부 매칭 사이클로 복귀합니다.",
                    stage,
                    champ_select_attempt.outcome,
                )
                restart_matching_cycle = True
                return False
            self.context.logger.info(
                "LCU 매칭 수락 후 ChampSelect 미진입(%s,phase=%s,outcome=%s).",
                stage,
                champ_select_attempt.phase or "UNKNOWN",
                champ_select_attempt.outcome,
            )
            restart_matching_cycle = True
            return False

        self.context.logger.info("대전 찾기 버튼 탐색 시작.")
        while True:
            poll_attempt = self.actions.poll_match_state(
                None,
                "사이클 진입",
                self.context.tpl_finding_match,
                self.context.tpl_accept,
                self.context.threshold,
                self.context.confirm_check_interval,
                self.context.logger,
                tpl_confirm_templates=self.context.tpl_confirm_templates,
                lcu=self.context.lcu,
            )
            accepted, finding = poll_attempt
            if observed_manual_matchmaking_cancel(poll_attempt):
                return MatchmakingOutcome.STOP
            if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                time.sleep(self.context.interval_sec)
                continue
            if (
                poll_attempt.loop_action == LcuLoopAction.ACT_LCU
                and poll_attempt.phase == PHASE_CHAMP_SELECT
            ):
                self.context.logger.info("LCU 챔피언 선택 진입 감지(사이클 진입).")
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
                self.context.logger.info(
                    "LCU 매칭 수락 처리 완료(사이클 진입). 수락 단계로 이동합니다."
                )
                accepted_early = True
                break
            if finding:
                self.context.logger.info("LCU 매칭 중 감지(사이클 진입). 화면 캡처를 건너뜁니다.")
                finding_early = True
                break

            rect = self.actions.visible_rect_or_wait(self.context.logger, "사이클 진입", self.context.interval_sec)
            if rect is None:
                continue
            if self.actions.detect_champion_select(
                rect,
                "사이클 진입",
                self.context.tpl_prepick,
                self.context.available_roles,
                self.context.threshold,
                self.context.logger,
                lcu=self.context.lcu,
            ):
                accepted_early = True
                break

            poll_attempt = self.actions.poll_match_state(
                rect,
                "사이클 진입",
                self.context.tpl_finding_match,
                self.context.tpl_accept,
                self.context.threshold,
                self.context.confirm_check_interval,
                self.context.logger,
                tpl_confirm_templates=self.context.tpl_confirm_templates,
                lcu=self.context.lcu,
            )
            accepted, finding = poll_attempt
            if observed_manual_matchmaking_cancel(
                poll_attempt, image_observation_complete=True
            ):
                return MatchmakingOutcome.STOP
            if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                time.sleep(self.context.interval_sec)
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
                self.context.logger.info("이미 매칭 수락 감지(사이클 진입). 수락 단계로 이동합니다.")
                accepted_early = True
                break
            if finding:
                self.context.logger.info("이미 매칭 중 감지(사이클 진입). 수락 감시로 전환합니다.")
                finding_early = True
                break

            start_attempt = self.actions.start_matchmaking(self.context.lcu, "사이클 진입", self.context.logger)
            if start_attempt.completed:
                now = time.monotonic()
                matchmaking_tracker.mark_start_requested(
                    self.actions.start_pending_at() or now
                )
                self.actions.reset_match_timer(now, self.context.logger)
                break
            if start_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                time.sleep(self.context.interval_sec)
                continue

            success = self.actions.search_and_act(
                rect, self.context.tpl_find_match, threshold=self.context.threshold, click=True
            )
            if success:
                now = time.monotonic()
                matchmaking_tracker.mark_start_requested(
                    self.actions.record_start_requested(now)
                )
                self.actions.reset_match_timer(now, self.context.logger)
                self.context.logger.info("대전 찾기 버튼 클릭 완료.")
                break
            self.context.logger.debug("대전 찾기 버튼 미검출. %.1fs 후 재시도...", self.context.interval_sec)
            time.sleep(self.context.interval_sec)

        if restart_matching_cycle:
            return MatchmakingOutcome.RETRY

        if not accepted_early:
            last_finding_state = finding_early if finding_early else None
            while True:
                poll_attempt = self.actions.poll_match_state(
                    None,
                    "매칭 단계",
                    self.context.tpl_finding_match,
                    self.context.tpl_accept,
                    self.context.threshold,
                    self.context.confirm_check_interval,
                    self.context.logger,
                    tpl_confirm_templates=self.context.tpl_confirm_templates,
                    lcu=self.context.lcu,
                )
                accepted, finding = poll_attempt
                if observed_manual_matchmaking_cancel(poll_attempt):
                    return MatchmakingOutcome.STOP
                if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    time.sleep(self.context.interval_sec)
                    continue
                if (
                    poll_attempt.loop_action == LcuLoopAction.ACT_LCU
                    and poll_attempt.phase == PHASE_CHAMP_SELECT
                ):
                    self.context.logger.info("LCU 챔피언 선택 진입 감지(매칭 단계).")
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
                        self.context.logger.info("매칭 상태 텍스트: 감지")
                        last_finding_state = finding
                    continue

                rect = self.actions.visible_rect_or_wait(self.context.logger, "매칭 단계", self.context.interval_sec)
                if rect is None:
                    continue
                poll_attempt = self.actions.poll_match_state(
                    rect,
                    "매칭 단계",
                    self.context.tpl_finding_match,
                    self.context.tpl_accept,
                    self.context.threshold,
                    self.context.confirm_check_interval,
                    self.context.logger,
                    tpl_confirm_templates=self.context.tpl_confirm_templates,
                    lcu=self.context.lcu,
                )
                accepted, finding = poll_attempt
                if observed_manual_matchmaking_cancel(
                    poll_attempt, image_observation_complete=True
                ):
                    return MatchmakingOutcome.STOP
                if poll_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    time.sleep(self.context.interval_sec)
                    continue
                if finding != last_finding_state:
                    self.context.logger.info("매칭 상태 텍스트: %s", "감지" if finding else "미감지")
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
                if self.actions.detect_champion_select(
                    rect,
                    "매칭 단계",
                    self.context.tpl_prepick,
                    self.context.available_roles,
                    self.context.threshold,
                    self.context.logger,
                    lcu=self.context.lcu,
                ):
                    break
                if matchmaking_tracker.start_pending:
                    self.context.logger.debug(
                        "대전 찾기 요청 반영 대기 중입니다. 자동 재요청을 보류합니다."
                    )
                    time.sleep(self.context.interval_sec)
                    continue
                start_attempt = self.actions.start_matchmaking(self.context.lcu, "매칭 단계", self.context.logger)
                if start_attempt.completed:
                    now = time.monotonic()
                    matchmaking_tracker.mark_start_requested(
                        self.actions.start_pending_at() or now
                    )
                    self.actions.reset_match_timer(now, self.context.logger)
                    self.context.logger.info("LCU 대전 찾기 재요청 완료.")
                    time.sleep(self.context.interval_sec)
                    continue
                if start_attempt.loop_action == LcuLoopAction.WAIT_AUTHORITATIVE:
                    time.sleep(self.context.interval_sec)
                    continue

                clicked = self.actions.search_and_act(
                    rect, self.context.tpl_find_match, threshold=self.context.threshold, click=True
                )
                if clicked:
                    now = time.monotonic()
                    matchmaking_tracker.mark_start_requested(
                        self.actions.record_start_requested(now)
                    )
                    self.actions.reset_match_timer(now, self.context.logger)
                    self.context.logger.info("대전 찾기 버튼 재클릭.")
                else:
                    self.context.logger.debug(
                        "대전 찾기 버튼 미검출. %.1fs 후 재시도...", self.context.interval_sec
                    )
                time.sleep(self.context.interval_sec)

        if restart_matching_cycle:
            return MatchmakingOutcome.RETRY
        return MatchmakingOutcome.READY
