from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from lolmanager.cli import entrypoint
from lolmanager.cli.entrypoint import (
    ContinueAfterGamePolicy,
    process_postgame,
    should_continue_after_game,
)
from lolmanager.core.gui_preferences import save_continue_after_game_preference
from lolmanager.core.lcu_client import LcuLoopAction, PHASE_WAITING_FOR_STATS


def test_next_game_continuation_requires_explicit_opt_in() -> None:
    assert should_continue_after_game(False) is False
    assert should_continue_after_game(True) is True


def test_live_continuation_policy_tracks_both_runtime_transitions_and_falls_back(
    tmp_path: Path,
) -> None:
    preference_path = tmp_path / "gui_preferences.json"
    policy = ContinueAfterGamePolicy(
        initial_value=False,
        preference_path=preference_path,
    )

    assert policy.current() is False

    save_continue_after_game_preference(preference_path, True)
    assert policy.current() is True

    save_continue_after_game_preference(preference_path, False)
    assert policy.current() is False

    preference_path.write_text("not-json", encoding="utf-8")
    assert policy.current() is False
    preference_path.unlink()
    assert policy.current() is False


def test_one_game_mode_does_not_issue_postgame_queue_actions(caplog) -> None:
    caplog.set_level(logging.INFO)

    process_postgame(
        Path("next.png"),
        Path("one-more.png"),
        Path("find-match.png"),
        Path("finding.png"),
        Path("accept.png"),
        (),
        Path("prepick.png"),
        [],
        0.85,
        0.2,
        1.0,
        logging.getLogger("test.one-game"),
        continue_after_game=False,
    )

    assert "한 게임 모드" in caplog.text


def test_continuation_mode_returns_true_after_postgame_processing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    postgame_calls: list[bool] = []
    monkeypatch.setattr(
        entrypoint,
        "_poll_lcu_phase_attempt",
        lambda *_args, **_kwargs: SimpleNamespace(
            phase=PHASE_WAITING_FOR_STATS,
            loop_action=LcuLoopAction.ACT_LCU,
            outcome="success",
        ),
    )
    monkeypatch.setattr(
        entrypoint,
        "process_postgame",
        lambda *_args, **kwargs: postgame_calls.append(
            bool(kwargs["continue_after_game"])
        ),
    )

    result = entrypoint.monitor_ingame_and_postgame(
        tmp_path / "next.png",
        tmp_path / "one-more.png",
        tmp_path / "find-match.png",
        tmp_path / "finding.png",
        tmp_path / "accept.png",
        (),
        tmp_path / "prepick.png",
        [],
        0.85,
        0.2,
        1.0,
        logging.getLogger("test.continuation"),
        continue_after_game=True,
    )

    assert result is True
    assert postgame_calls == [True]


def test_ingame_exit_reads_latest_live_continuation_value(
    monkeypatch,
    tmp_path: Path,
) -> None:
    preference_path = tmp_path / "gui_preferences.json"
    policy = ContinueAfterGamePolicy(
        initial_value=True,
        preference_path=preference_path,
    )
    monkeypatch.setattr(
        entrypoint,
        "_poll_lcu_phase_attempt",
        lambda *_args, **_kwargs: SimpleNamespace(
            phase=entrypoint.PHASE_END_OF_GAME,
            loop_action=LcuLoopAction.ACT_LCU,
            outcome="success",
        ),
    )
    postgame_calls: list[bool] = []
    monkeypatch.setattr(
        entrypoint,
        "process_postgame",
        lambda *_args, **kwargs: postgame_calls.append(
            bool(kwargs["continue_after_game"])
        ),
    )

    save_continue_after_game_preference(preference_path, False)
    stopped = entrypoint.monitor_ingame_and_postgame(
        tmp_path / "next.png",
        tmp_path / "one-more.png",
        tmp_path / "find-match.png",
        tmp_path / "finding.png",
        tmp_path / "accept.png",
        (),
        tmp_path / "prepick.png",
        [],
        0.85,
        0.2,
        1.0,
        logging.getLogger("test.live-continuation-disabled"),
        continue_after_game=policy.current,
    )

    save_continue_after_game_preference(preference_path, True)
    continued = entrypoint.monitor_ingame_and_postgame(
        tmp_path / "next.png",
        tmp_path / "one-more.png",
        tmp_path / "find-match.png",
        tmp_path / "finding.png",
        tmp_path / "accept.png",
        (),
        tmp_path / "prepick.png",
        [],
        0.85,
        0.2,
        1.0,
        logging.getLogger("test.live-continuation-enabled"),
        continue_after_game=policy.current,
    )

    assert stopped is False
    assert continued is True
    assert postgame_calls == [True]
