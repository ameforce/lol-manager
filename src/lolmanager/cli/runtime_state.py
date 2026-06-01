from __future__ import annotations

from enum import IntEnum, unique
from typing import Optional

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
