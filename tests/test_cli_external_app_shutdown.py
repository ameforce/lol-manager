from __future__ import annotations

import logging

import pytest

from lolmanager.cli import entrypoint


def test_cli_league_client_closed_shutdowns_running_opgg_before_exit(
    monkeypatch,
) -> None:
    calls: list[str] = []
    logger = logging.getLogger("test.cli.external-app-shutdown")
    monkeypatch.setattr(
        entrypoint,
        "close_running_opgg",
        lambda *, logger=None: calls.append("close-running-opgg"),
    )

    with pytest.raises(SystemExit) as raised:
        entrypoint._exit_after_league_client_closed(logger)

    assert raised.value.code == 0
    assert calls == ["close-running-opgg"]
