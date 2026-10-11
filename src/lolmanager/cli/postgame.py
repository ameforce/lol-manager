from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from lolmanager.cli.matchmaking import MatchmakingContext
from lolmanager.cli.runtime_state import POSTGAME_PHASES
from lolmanager.core.aram import AramPreferences
from lolmanager.core.gui_preferences import load_continue_after_game_preference


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


def _should_process_postgame_at_cycle(phase: object) -> bool:
    return phase in POSTGAME_PHASES


def _should_force_first_cycle_postgame(
    *, cycle_count: int, continue_after_game: object
) -> bool:
    """시작 직후 첫 사이클이 점수판이면 한 게임 모드라도 1회를 보장한다."""
    if cycle_count != 1:
        return False
    return not should_continue_after_game(continue_after_game)


@dataclass
class PostgameController:
    """Route game-boundary decisions while keeping the live preference callable."""

    context: MatchmakingContext
    tpl_end_next: Path
    tpl_end_one_more: Path
    tpl_pick_decline: Path | None
    continue_after_game: object
    process: Callable[..., bool]
    monitor: Callable[..., bool]
    wait: Callable[..., bool | None]

    def _arguments(self) -> dict[str, object]:
        context = self.context
        return {
            "lcu": context.lcu,
            "logger": context.logger,
            "tpl_end_next": self.tpl_end_next,
            "tpl_end_one_more": self.tpl_end_one_more,
            "tpl_find_match": context.tpl_find_match,
            "tpl_finding_match": context.tpl_finding_match,
            "tpl_accept": context.tpl_accept,
            "tpl_confirm_templates": context.tpl_confirm_templates,
            "tpl_prepick": context.tpl_prepick,
            "available_roles": context.available_roles,
            "threshold": context.threshold,
            "confirm_check_interval": context.confirm_check_interval,
            "interval_sec": context.interval_sec,
        }

    def resume(self, phase: str | None, *, in_game: bool, cycle_count: int) -> bool | None:
        logger = self.context.logger
        if _should_process_postgame_at_cycle(phase):
            logger.info(
                "LCU postgame 단계 감지(사이클 시작,phase=%s). 엔드 화면 처리로 전환합니다.",
                phase,
            )
            effective = self.continue_after_game
            if _should_force_first_cycle_postgame(
                cycle_count=cycle_count,
                continue_after_game=effective,
            ):
                logger.info("시작 시 점수판 감지(한 게임 모드). Start 1회 보장을 위해 다음 매칭 1회 진행합니다.")
                effective = True
            return self.process(**self._arguments(), continue_after_game=effective)
        if in_game:
            logger.info("이미 인게임 상태 감지(사이클 시작). 게임 종료 감시로 전환합니다.")
            return self.monitor(
                **self._arguments(), continue_after_game=self.continue_after_game
            )
        return None

    def wait_for_game(
        self,
        *,
        passive: bool = False,
        aram_preferences: AramPreferences | None = None,
    ) -> bool | None:
        return self.wait(
            **self._arguments(),
            tpl_pick_decline=self.tpl_pick_decline,
            continue_after_game=self.continue_after_game,
            passive_champ_select=passive,
            aram_preferences=aram_preferences,
        )
