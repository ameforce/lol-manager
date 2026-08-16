from __future__ import annotations

import logging
import subprocess
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
        "start_exe_process_once",
        lambda exe_path, *, logger=None: starts.append(str(exe_path)) or _FakePopen(),
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
        "start_exe_process_once",
        lambda exe_path, *, logger=None: starts.append(str(exe_path)) or _FakePopen(),
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


class _FakePopen:
    def __init__(self, *, stays_alive_after_terminate: bool = False) -> None:
        self.stays_alive_after_terminate = stays_alive_after_terminate
        self.alive = True
        self.calls: list[str] = []

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.calls.append("terminate")
        if not self.stays_alive_after_terminate:
            self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(f"wait:{timeout}")
        if self.alive:
            raise subprocess.TimeoutExpired("OP.GG.exe", timeout)
        return 0

    def kill(self) -> None:
        self.calls.append("kill")
        self.alive = False


class _FakePsProcess:
    def __init__(
        self,
        children: list["_FakePsProcess"] | None = None,
        *,
        name: str = "process",
        termination_order: list[str] | None = None,
    ) -> None:
        self._children = list(children or [])
        self.name = name
        self.termination_order = termination_order
        self.terminated = False
        self.killed = False

    def children(self, *, recursive: bool) -> list["_FakePsProcess"]:
        assert recursive is True
        return list(self._children)

    def terminate(self) -> None:
        self.terminated = True
        if self.termination_order is not None:
            self.termination_order.append(self.name)

    def kill(self) -> None:
        self.killed = True


def test_ensure_external_apps_records_owned_opgg_only_when_this_run_starts_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    local_app_data = tmp_path / "LocalAppData"
    opgg = _touch_exe(local_app_data / "Programs" / "OP.GG" / "OP.GG.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(
        external_apps,
        "running_status_for_exe_paths",
        lambda paths: {str(path): str(path) == str(league) for path in paths},
    )
    proc = _FakePopen()
    monkeypatch.setattr(
        external_apps,
        "start_exe_process_once",
        lambda exe_path, *, logger=None: proc,
    )

    session = external_apps.ensure_external_apps_running_once(
        league_exe=str(league),
        opgg_exe=str(opgg),
    )

    assert session.owned_opgg is not None
    assert session.owned_opgg.process is proc
    assert external_apps.current_external_apps_session() is session


def test_ensure_external_apps_starts_league_client_directly_not_riot_launcher(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    started: list[str] = []
    monkeypatch.setattr(
        external_apps,
        "running_status_for_exe_paths",
        lambda paths: {str(path): False for path in paths},
    )
    monkeypatch.setattr(
        external_apps,
        "start_exe_process_once",
        lambda exe_path, *, logger=None: started.append(str(exe_path)) or _FakePopen(),
    )
    monkeypatch.setattr(
        external_apps,
        "riot_client_services_exe_path",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Riot launcher used")),
    )

    external_apps.ensure_external_apps_running_once(league_exe=str(league))

    assert started == [str(league)]


def test_ensure_external_apps_resets_session_and_does_not_own_preexisting_opgg(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    local_app_data = tmp_path / "LocalAppData"
    opgg = _touch_exe(local_app_data / "Programs" / "OP.GG" / "OP.GG.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    previous_proc = _FakePopen()
    external_apps.set_current_external_apps_session(
        external_apps.ExternalAppsSession(
            owned_opgg=external_apps.OwnedExternalProcess(
                exe_path=str(opgg),
                process=previous_proc,
            )
        )
    )
    monkeypatch.setattr(
        external_apps,
        "running_status_for_exe_paths",
        lambda paths: {str(path): True for path in paths},
    )
    starts: list[str] = []
    monkeypatch.setattr(
        external_apps,
        "start_exe_process_once",
        lambda exe_path, *, logger=None: starts.append(str(exe_path)),
    )

    session = external_apps.ensure_external_apps_running_once(
        league_exe=str(league),
        opgg_exe=str(opgg),
    )

    assert session.owned_opgg is None
    assert external_apps.current_external_apps_session() is session
    assert starts == []


def test_close_owned_opgg_terminates_then_kills_after_timeout() -> None:
    proc = _FakePopen(stays_alive_after_terminate=True)
    session = external_apps.ExternalAppsSession(
        owned_opgg=external_apps.OwnedExternalProcess(
            exe_path=r"C:\Users\me\AppData\Local\Programs\OP.GG\OP.GG.exe",
            process=proc,
        )
    )

    assert session.close_owned_opgg(timeout_sec=0.25) is True

    assert proc.calls == ["terminate", "wait:0.25", "kill", "wait:0.25"]
    assert session.owned_opgg is None


def test_close_owned_opgg_terminates_only_the_owned_process_tree(monkeypatch) -> None:
    proc = _FakePopen()
    proc.pid = 123
    termination_order: list[str] = []
    child = _FakePsProcess(name="child", termination_order=termination_order)
    root = _FakePsProcess(
        [child],
        name="root",
        termination_order=termination_order,
    )
    monkeypatch.setattr(external_apps.psutil, "Process", lambda pid: root)
    monkeypatch.setattr(
        external_apps.psutil,
        "wait_procs",
        lambda processes, timeout: (list(processes), []),
    )
    session = external_apps.ExternalAppsSession(
        owned_opgg=external_apps.OwnedExternalProcess(
            exe_path=r"C:\Users\me\AppData\Local\Programs\OP.GG\OP.GG.exe",
            process=proc,
        )
    )

    assert session.close_owned_opgg(timeout_sec=0.25) is True

    assert child.terminated is True
    assert root.terminated is True
    assert termination_order == ["child", "root"]
    assert proc.calls == []
    assert session.owned_opgg is None


def test_close_owned_opgg_noops_without_owned_process() -> None:
    session = external_apps.ExternalAppsSession()

    assert session.close_owned_opgg(timeout_sec=0.25) is False
