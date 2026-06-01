from __future__ import annotations

from lolmanager.cli import entrypoint
from lolmanager.cli.runtime_state import (
    ClientState,
    LCU_UI_ACTION_CLASSIFICATION,
    POSTGAME_PHASES,
    client_state_from_lcu_phase,
    should_preserve_champ_select_state,
)
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
from lolmanager.gui import app_gui, config_gui
from lolmanager.gui.log_view_model import (
    ROLE_CLEAR_STATES,
    ROLE_LABEL_KO,
    compact_role_ban_label_for_main_ui,
    role_key_from_log_line,
)


def test_cli_runtime_state_seam_preserves_phase_mapping() -> None:
    assert client_state_from_lcu_phase(PHASE_LOBBY) == ClientState.LOBBY
    assert client_state_from_lcu_phase(PHASE_MATCHMAKING) == ClientState.MATCH_FINDING
    assert (
        client_state_from_lcu_phase(PHASE_READY_CHECK)
        == ClientState.MATCH_ACCEPT_WAIT
    )
    assert client_state_from_lcu_phase(PHASE_CHAMP_SELECT) == ClientState.PREPICK
    assert client_state_from_lcu_phase(PHASE_IN_PROGRESS) == ClientState.INGAME
    assert client_state_from_lcu_phase(PHASE_RECONNECT) == ClientState.INGAME
    assert client_state_from_lcu_phase(PHASE_WATCH_IN_PROGRESS) == ClientState.INGAME
    assert client_state_from_lcu_phase(PHASE_WAITING_FOR_STATS) == ClientState.POSTGAME_SCORE
    assert client_state_from_lcu_phase(PHASE_PRE_END_OF_GAME) == ClientState.POSTGAME_SCORE
    assert client_state_from_lcu_phase(PHASE_END_OF_GAME) == ClientState.POSTGAME_SCORE
    assert client_state_from_lcu_phase("UnknownPhase") is None


def test_cli_runtime_state_seam_preserves_champ_select_guard() -> None:
    assert POSTGAME_PHASES == {
        PHASE_WAITING_FOR_STATS,
        PHASE_PRE_END_OF_GAME,
        PHASE_END_OF_GAME,
    }
    assert should_preserve_champ_select_state(
        ClientState.PREPICK,
        ClientState.BANPICK,
    )
    assert should_preserve_champ_select_state(ClientState.PREPICK, ClientState.PICK)
    assert should_preserve_champ_select_state(
        ClientState.PREPICK,
        ClientState.WAIT_GAME_START,
    )
    assert not should_preserve_champ_select_state(
        ClientState.PREPICK,
        ClientState.LOBBY,
    )
    assert not should_preserve_champ_select_state(
        ClientState.INGAME,
        ClientState.PICK,
    )


def test_entrypoint_reexports_runtime_state_seam() -> None:
    assert entrypoint.ClientState is ClientState
    assert entrypoint.LCU_UI_ACTION_CLASSIFICATION is LCU_UI_ACTION_CLASSIFICATION


def test_gui_log_view_model_seam_preserves_role_helpers() -> None:
    assert ROLE_LABEL_KO["mid"] == "미드"
    assert ROLE_CLEAR_STATES == {"UNKNOWN", "LOBBY", "MATCH_FINDING", "MATCH_ACCEPT_WAIT"}
    assert (
        compact_role_ban_label_for_main_ui(
            "자동 추천 (현재 최고: 블라디미르, 2티어, 45.8%, score 61.0)"
        )
        == "블라디미르 2T 45.8%"
    )
    assert role_key_from_log_line("[INFO] 포지션 감지: mid (score=0.932)") == "mid"


def test_app_gui_reexports_log_view_model_seam() -> None:
    assert app_gui.ROLE_LABEL_KO is ROLE_LABEL_KO
    assert app_gui.ROLE_CLEAR_STATES is ROLE_CLEAR_STATES
    assert app_gui.role_key_from_log_line is role_key_from_log_line


def test_config_gui_uses_shared_role_label_seam() -> None:
    assert config_gui.ROLE_LABEL_KO is ROLE_LABEL_KO
