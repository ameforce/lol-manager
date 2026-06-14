from __future__ import annotations

import logging

import pytest

from lolmanager.cli import entrypoint


def test_cli_league_client_closed_shutdowns_owned_opgg_before_exit(
    monkeypatch,
) -> None:
    calls: list[str] = []
    logger = logging.getLogger("test.cli.external-app-shutdown")
    monkeypatch.setattr(
        entrypoint,
        "close_owned_opgg_for_current_session",
        lambda *, logger=None: calls.append("close-owned-opgg"),
    )

    with pytest.raises(SystemExit) as raised:
        entrypoint._exit_after_league_client_closed(logger)

    assert raised.value.code == 0
    assert calls == ["close-owned-opgg"]
