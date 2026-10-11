from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lolmanager.cli.matchmaking import (
    MatchmakingActions, MatchmakingContext, MatchmakingController, MatchmakingOutcome,
    MatchPollAttempt,
)
from lolmanager.cli.postgame import PostgameController
from lolmanager.cli.runtime_state import RuntimeState
from lolmanager.core.lcu_client import (
    LcuLoopAction, PHASE_CHAMP_SELECT, PHASE_END_OF_GAME,
    PHASE_IN_PROGRESS, PHASE_LOBBY, PHASE_MATCHMAKING, PHASE_READY_CHECK,
)


def context():
    return MatchmakingContext(
        state=RuntimeState(), lcu=Mock(), logger=logging.getLogger("test.controller"),
        tpl_find_match=Path("find.png"), tpl_finding_match=Path("finding.png"),
        tpl_accept=Path("accept.png"), tpl_confirm_templates=(),
        tpl_prepick=Path("prepick.png"), available_roles=[],
        threshold=0.85, confirm_check_interval=0.2, interval_sec=1.0,
    )


@pytest.mark.parametrize(("initial", "observed", "expected"), [
    (PHASE_MATCHMAKING, PHASE_LOBBY, MatchmakingOutcome.STOP),
    (PHASE_CHAMP_SELECT, PHASE_CHAMP_SELECT, MatchmakingOutcome.READY),
    (PHASE_READY_CHECK, PHASE_READY_CHECK, MatchmakingOutcome.RETRY),
])
def test_queue_transitions_do_not_fall_back_to_clicks(initial, observed, expected):
    actions = MatchmakingActions(
        set_client_state=Mock(),
        poll_match_state=Mock(return_value=MatchPollAttempt(
            observed == PHASE_READY_CHECK, False, LcuLoopAction.ACT_LCU, "success", observed,
        )),
        visible_rect_or_wait=Mock(), detect_champion_select=Mock(),
        start_matchmaking=Mock(),
        wait_for_champ_select=Mock(return_value=SimpleNamespace(
            phase=PHASE_LOBBY, loop_action=LcuLoopAction.ACT_LCU, outcome="success",
        )),
        record_start_requested=Mock(), start_pending_at=Mock(return_value=None),
        reset_match_timer=Mock(), search_and_act=Mock(),
    )
    result = MatchmakingController(context(), actions).run(initial, LcuLoopAction.ACT_LCU)
    assert result == expected
    actions.visible_rect_or_wait.assert_not_called()
    actions.search_and_act.assert_not_called()
    actions.start_matchmaking.assert_not_called()


def test_postgame_controller_forces_only_the_initial_scoreboard_cycle():
    live = lambda: False
    process, monitor, wait = Mock(return_value=True), Mock(), Mock()
    controller = PostgameController(context(), Path("next"), Path("again"), None,
                                    live, process, monitor, wait)
    assert controller.resume(PHASE_END_OF_GAME, in_game=False, cycle_count=1) is True
    assert process.call_args.kwargs["continue_after_game"] is True
    controller.resume(PHASE_END_OF_GAME, in_game=False, cycle_count=2)
    assert process.call_args.kwargs["continue_after_game"] is live
    monitor.assert_not_called()


def test_postgame_controller_preserves_live_preference_and_aram_policy():
    from lolmanager.core.aram import AramPreferences

    selected = True
    def live():
        return selected
    process, monitor, wait = Mock(), Mock(return_value=False), Mock(return_value=None)
    controller = PostgameController(context(), Path("next"), Path("again"), None,
                                    live, process, monitor, wait)
    selected = False
    assert controller.resume(PHASE_IN_PROGRESS, in_game=True, cycle_count=2) is False
    assert monitor.call_args.kwargs["continue_after_game"]() is False
    preferences = AramPreferences(True, ("아리",))
    assert controller.wait_for_game(passive=True, aram_preferences=preferences) is None
    assert wait.call_args.kwargs["aram_preferences"] == preferences
    assert wait.call_args.kwargs["passive_champ_select"] is True
    assert wait.call_args.kwargs["continue_after_game"] is live
    process.assert_not_called()
