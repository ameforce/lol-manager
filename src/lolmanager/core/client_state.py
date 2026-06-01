from __future__ import annotations

from enum import IntEnum, unique


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
