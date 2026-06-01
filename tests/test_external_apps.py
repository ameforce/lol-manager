from __future__ import annotations

import logging
from pathlib import Path

from lolmanager.platform import external_apps


def _touch_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _isolate_app_env(monkeypatch) -> None:
    for name in (
        external_apps.ENV_OPGG_EXE,
        external_apps.ENV_LEAGUE_CLIENT_EXE,
        external_apps.ENV_RIOT_CLIENT_SERVICES_EXE,
        external_apps.ENV_ALLOW_UNTRUSTED_APP_PATHS,
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    ):
        monkeypatch.delenv(name, raising=False)


def test_opgg_path_discovers_localappdata_install_without_user_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    local_app_data = tmp_path / "LocalAppData"
    exe = _touch_exe(local_app_data / "Programs" / "OP.GG" / "OP.GG.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert external_apps.opgg_exe_path() == str(exe)
    assert "Users\\enmso" not in external_apps.DEFAULT_OPGG_EXE


def test_opgg_path_is_empty_when_not_discovered(monkeypatch, tmp_path: Path) -> None:
    _isolate_app_env(monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "missing-local"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "missing-program-files"))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "missing-program-files-x86"))

    assert external_apps.opgg_exe_path() == ""


def test_ensure_external_apps_skips_untrusted_opgg_override(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    opgg = _touch_exe(tmp_path / "Downloads" / "OP.GG.exe")
    monkeypatch.setenv(external_apps.ENV_OPGG_EXE, str(opgg))
    monkeypatch.setattr(
        external_apps,
        "running_status_for_exe_paths",
        lambda _paths: {str(league): True},
    )
    starts: list[str] = []
    monkeypatch.setattr(
        external_apps,
        "start_exe_once",
        lambda exe_path, *, logger=None: starts.append(str(exe_path)) or True,
    )
    caplog.set_level(logging.WARNING)

    external_apps.ensure_external_apps_running_once(
        league_exe=str(league),
        logger=logging.getLogger("test.external_apps"),
    )

    assert starts == []
    assert external_apps.ENV_OPGG_EXE in caplog.text
    assert external_apps.ENV_ALLOW_UNTRUSTED_APP_PATHS in caplog.text


def test_ensure_external_apps_allows_explicit_untrusted_opgg_override(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    opgg = _touch_exe(tmp_path / "PortableApps" / "OP.GG.exe")
    monkeypatch.setenv(external_apps.ENV_OPGG_EXE, str(opgg))
    monkeypatch.setenv(external_apps.ENV_ALLOW_UNTRUSTED_APP_PATHS, "1")
    monkeypatch.setattr(
        external_apps,
        "running_status_for_exe_paths",
        lambda paths: {str(path): str(path) == str(league) for path in paths},
    )
    starts: list[str] = []
    monkeypatch.setattr(
        external_apps,
        "start_exe_once",
        lambda exe_path, *, logger=None: starts.append(str(exe_path)) or True,
    )

    external_apps.ensure_external_apps_running_once(league_exe=str(league))

    assert starts == [str(opgg)]


def test_start_cmd_once_rejects_directory_targets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        external_apps.subprocess,
        "Popen",
        lambda cmd, **_kwargs: calls.append(list(cmd)),
    )

    assert external_apps.start_cmd_once([str(tmp_path)]) is False
    assert calls == []
