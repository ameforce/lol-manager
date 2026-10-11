from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from lolmanager.platform.external_apps import LeagueClientExitGuard

from lolmanager.core.client_state import ClientState
from lolmanager.core.lcu_client import (
    PHASE_CHAMP_SELECT,
    PHASE_END_OF_GAME,
    PHASE_IN_PROGRESS,
    PHASE_LOBBY,
    PHASE_MATCHMAKING,
    PHASE_PRE_END_OF_GAME,
    PHASE_READY_CHECK,
    PHASE_RECONNECT,
    PHASE_WAITING_FOR_STATS,
    PHASE_WATCH_IN_PROGRESS,
)


@dataclass
class RuntimeState:
    """Mutable state owned by one automation run, reset together on Start."""

    client_state: ClientState = ClientState.UNKNOWN
    client_state_updated_at: float = 0.0
    is_my_pick_turn: bool = False
    my_pick_turn_updated_at: float = 0.0
    matchmaking_start_pending_at: float | None = None
    my_pick_turn_miss_streak: int = 0
    match_started_at_mono: float | None = None
    last_auto_lobby_create_at: float = 0.0
    last_finding_logged: dict[str, bool] = field(default_factory=dict)
    last_accept_click_at: dict[str, float] = field(default_factory=dict)
    last_lcu_ready_accept_at: dict[str, float] = field(default_factory=dict)
    last_popup_click_at: dict[str, float] = field(default_factory=dict)
    last_restore_wait_log_state: dict[str, object] = field(default_factory=dict)
    league_exit_guard: LeagueClientExitGuard | None = None
    pick_target: str = ""

    def reset(self) -> None:
        fresh = type(self)()
        for item in fields(self):
            setattr(self, item.name, getattr(fresh, item.name))


LCU_UI_ACTION_CLASSIFICATION: dict[str, str] = {
    "ready_check": "lcu-first",
    "matchmaking_start": "lcu-first",
    "role_detection": "lcu-first",
    "champ_select_prepick": "lcu-first",
    "champ_select_ban": "lcu-first",
    "champ_select_pick": "lcu-first",
    "reserve_pick": "lcu-first",
    "pick_popups": "lcu-first",
    "pick_myturn": "fallback-only",
    "postgame_end_buttons": "ui-only",
    "postgame_continue": "lcu-first",
    "postgame_play_again": "lcu-first",
    "postgame_honor_vote": "lcu-only-terminal",
    "blocking_modals": "lcu-first",
}

POSTGAME_PHASES: frozenset[str] = frozenset(
    {
        PHASE_WAITING_FOR_STATS,
        PHASE_PRE_END_OF_GAME,
        PHASE_END_OF_GAME,
    }
)


def client_state_from_lcu_phase(phase: Optional[str]) -> Optional[ClientState]:
    if phase == PHASE_LOBBY:
        return ClientState.LOBBY
    if phase == PHASE_MATCHMAKING:
        return ClientState.MATCH_FINDING
    if phase == PHASE_READY_CHECK:
        return ClientState.MATCH_ACCEPT_WAIT
    if phase == PHASE_CHAMP_SELECT:
        return ClientState.PREPICK
    if phase in {PHASE_IN_PROGRESS, PHASE_RECONNECT, PHASE_WATCH_IN_PROGRESS}:
        return ClientState.INGAME
    if phase in POSTGAME_PHASES:
        return ClientState.POSTGAME_SCORE
    return None


def should_preserve_champ_select_state(
    new_state: Optional[ClientState],
    current_state: object,
) -> bool:
    return new_state == ClientState.PREPICK and current_state in {
        ClientState.BANPICK,
        ClientState.PICK,
        ClientState.WAIT_GAME_START,
    }
