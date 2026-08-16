from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from lolmanager.cli import entrypoint
from lolmanager.cli.entrypoint import process_postgame, should_continue_after_game
from lolmanager.core.lcu_client import LcuLoopAction, PHASE_WAITING_FOR_STATS


def test_next_game_continuation_requires_explicit_opt_in() -> None:
    assert should_continue_after_game(False) is False
    assert should_continue_after_game(True) is True


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
