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


def test_ensure_external_apps_launches_league_via_riot_client_api(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    monkeypatch.setenv(external_apps.ENV_LEAGUE_CLIENT_EXE, str(league))
    monkeypatch.setattr(
        external_apps,
        "running_status_for_exe_paths",
        lambda paths: {str(path): False for path in paths},
    )
    launched: list[dict] = []
    monkeypatch.setattr(
        external_apps,
        "launch_league_via_riot_client_api",
        lambda **kwargs: launched.append(kwargs) or True,
    )
    verified: list[dict] = []
    monkeypatch.setattr(
        external_apps,
        "verify_league_client_started",
        lambda **kwargs: verified.append(kwargs) or True,
    )
    legacy_cmds: list[list[str]] = []
    monkeypatch.setattr(
        external_apps,
        "start_cmd_once",
        lambda cmd, **_kwargs: legacy_cmds.append([str(x) for x in cmd]) or True,
    )

    external_apps.ensure_external_apps_running_once(league_exe=str(league))

    assert len(launched) == 1
    assert len(verified) == 1
    assert legacy_cmds == []


def test_ensure_external_apps_falls_back_to_launch_args_when_api_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    rcs = _touch_exe(tmp_path / "Riot Games" / "Riot Client" / "RiotClientServices.exe")
    monkeypatch.setenv(external_apps.ENV_LEAGUE_CLIENT_EXE, str(league))
    monkeypatch.setenv(external_apps.ENV_RIOT_CLIENT_SERVICES_EXE, str(rcs))
    monkeypatch.setattr(
        external_apps,
        "running_status_for_exe_paths",
        lambda paths: {str(path): False for path in paths},
    )
    monkeypatch.setattr(
        external_apps,
        "launch_league_via_riot_client_api",
        lambda **_kwargs: False,
    )
    cmds: list[list[str]] = []
    monkeypatch.setattr(
        external_apps,
        "start_cmd_once",
        lambda cmd, **_kwargs: cmds.append([str(x) for x in cmd]) or True,
    )
    verified: list[dict] = []
    monkeypatch.setattr(
        external_apps,
        "verify_league_client_started",
        lambda **kwargs: verified.append(kwargs) or True,
    )

    external_apps.ensure_external_apps_running_once(league_exe=str(league))

    assert cmds == [
        [
            str(rcs),
            "--launch-product=league_of_legends",
            "--launch-patchline=live",
        ]
    ]
    assert len(verified) == 1


def test_start_league_client_once_falls_back_to_direct_leagueclient_without_riot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    monkeypatch.setattr(
        external_apps,
        "launch_league_via_riot_client_api",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        external_apps,
        "riot_client_services_exe_path",
        lambda **_kwargs: "",
    )
    started: list[str] = []
    monkeypatch.setattr(
        external_apps,
        "start_exe_process_once",
        lambda exe_path, *, logger=None: started.append(str(exe_path)) or _FakePopen(),
    )
    monkeypatch.setattr(
        external_apps,
        "start_cmd_once",
        lambda cmd, **_kwargs: (_ for _ in ()).throw(AssertionError("cmd used")),
    )

    assert external_apps.start_league_client_once(league_exe=str(league)) is True
    assert started == [str(league)]


def test_start_league_client_once_falls_back_when_riot_launch_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    rcs = _touch_exe(tmp_path / "Riot Games" / "Riot Client" / "RiotClientServices.exe")
    monkeypatch.setenv(external_apps.ENV_LEAGUE_CLIENT_EXE, str(league))
    monkeypatch.setenv(external_apps.ENV_RIOT_CLIENT_SERVICES_EXE, str(rcs))
    monkeypatch.setattr(
        external_apps,
        "launch_league_via_riot_client_api",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(external_apps, "start_cmd_once", lambda cmd, **_kw: False)
    started: list[str] = []
    monkeypatch.setattr(
        external_apps,
        "start_exe_process_once",
        lambda exe_path, *, logger=None: started.append(str(exe_path)) or _FakePopen(),
    )

    assert external_apps.start_league_client_once(league_exe=str(league)) is True
    assert started == [str(league)]


def test_start_league_client_once_returns_false_without_any_valid_exe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    monkeypatch.setattr(
        external_apps,
        "launch_league_via_riot_client_api",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        external_apps,
        "riot_client_services_exe_path",
        lambda **_kwargs: "",
    )
    monkeypatch.setenv(
        external_apps.ENV_LEAGUE_CLIENT_EXE,
        str(tmp_path / "missing" / "LeagueClient.exe"),
    )

    assert external_apps.start_league_client_once(
        league_exe=str(tmp_path / "missing" / "LeagueClient.exe")
    ) is False


def test_verify_league_client_started_detects_process_by_name(monkeypatch) -> None:
    monkeypatch.setattr(
        external_apps, "_league_client_process_seen", lambda: True
    )

    assert external_apps.verify_league_client_started(timeout_sec=0.1) is True


def test_verify_league_client_started_times_out_without_process(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        external_apps, "_league_client_process_seen", lambda: False
    )

    assert external_apps.verify_league_client_started(timeout_sec=0.05) is False


def test_close_riot_client_windows_after_launch_posts_close_messages(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        external_apps, "_riot_client_window_handles", lambda: [111, 222]
    )
    posted: list[int] = []
    monkeypatch.setattr(
        external_apps, "_post_close_message", posted.append
    )

    closed = external_apps.close_riot_client_windows_after_launch(settle_sec=0)

    assert closed == 2
    assert posted == [111, 222]


def test_close_riot_client_windows_after_launch_respects_keep_env(
    monkeypatch,
) -> None:
    _isolate_app_env(monkeypatch)
    monkeypatch.setenv(external_apps.ENV_KEEP_RIOT_CLIENT_WINDOW, "1")
    monkeypatch.setattr(
        external_apps,
        "_riot_client_window_handles",
        lambda: (_ for _ in ()).throw(AssertionError("windows scanned")),
    )

    assert external_apps.close_riot_client_windows_after_launch(settle_sec=0) == 0


def test_ensure_external_apps_closes_riot_client_window_after_own_launch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    monkeypatch.setenv(external_apps.ENV_LEAGUE_CLIENT_EXE, str(league))
    monkeypatch.setattr(
        external_apps,
        "running_status_for_exe_paths",
        lambda paths: {str(path): False for path in paths},
    )
    monkeypatch.setattr(
        external_apps, "launch_league_via_riot_client_api", lambda **_kw: True
    )
    monkeypatch.setattr(
        external_apps, "verify_league_client_started", lambda **_kw: True
    )
    close_calls: list[dict] = []
    monkeypatch.setattr(
        external_apps,
        "close_riot_client_windows_after_launch",
        lambda **kwargs: close_calls.append(kwargs) or 1,
    )

    external_apps.ensure_external_apps_running_once(league_exe=str(league))

    assert len(close_calls) == 1


def test_ensure_external_apps_keeps_riot_client_when_league_pre_running(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    monkeypatch.setenv(external_apps.ENV_LEAGUE_CLIENT_EXE, str(league))
    monkeypatch.setattr(
        external_apps,
        "running_status_for_exe_paths",
        lambda paths: {str(path): True for path in paths},
    )
    monkeypatch.setattr(
        external_apps,
        "close_riot_client_windows_after_launch",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("close called")),
    )

    session = external_apps.ensure_external_apps_running_once(
        league_exe=str(league)
    )

    assert session.owned_opgg is None


class _FakeApiProcess:
    def __init__(self, info: dict) -> None:
        self.info = info


def test_find_running_riot_client_api_parses_cmdline(monkeypatch) -> None:
    processes = [
        _FakeApiProcess({"name": "explorer.exe", "cmdline": None}),
        _FakeApiProcess(
            {
                "name": "RiotClientServices.exe",
                "cmdline": [
                    r"C:\Riot Games\Riot Client\RiotClientServices.exe",
                    "--remoting-auth-token=abc123",
                    "--app-port=29543",
                ],
            }
        ),
    ]
    monkeypatch.setattr(
        external_apps.psutil, "process_iter", lambda attrs: iter(processes)
    )

    endpoint = external_apps._find_running_riot_client_api()

    assert endpoint == ("https://127.0.0.1:29543", ("riot", "abc123"))


def test_find_running_riot_client_api_returns_none_without_credentials(
    monkeypatch,
) -> None:
    processes = [
        _FakeApiProcess(
            {
                "name": "RiotClientServices.exe",
                "cmdline": [r"C:\Riot Games\Riot Client\RiotClientServices.exe"],
            }
        )
    ]
    monkeypatch.setattr(
        external_apps.psutil, "process_iter", lambda attrs: iter(processes)
    )

    assert external_apps._find_running_riot_client_api() is None


def test_free_localhost_port_returns_bindable_port() -> None:
    import socket

    port = external_apps._free_localhost_port()

    assert isinstance(port, int)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_launch_league_via_riot_client_api_posts_product_launch(monkeypatch) -> None:
    endpoint = ("https://127.0.0.1:29543", ("riot", "tok123"))
    monkeypatch.setattr(external_apps, "_find_running_riot_client_api", lambda: endpoint)
    calls: list[dict] = []

    class _Response:
        status_code = 200

    def fake_post(url, *, auth, verify, timeout):
        calls.append({"url": url, "auth": auth, "verify": verify})
        return _Response()

    monkeypatch.setattr(external_apps.requests, "post", fake_post)

    assert external_apps.launch_league_via_riot_client_api() is True
    assert calls == [
        {
            "url": "https://127.0.0.1:29543"
            + external_apps.RIOT_PRODUCT_LAUNCH_PATH,
            "auth": ("riot", "tok123"),
            "verify": False,
        }
    ]


def test_launch_league_via_riot_client_api_treats_read_timeout_as_dispatched(
    monkeypatch,
) -> None:
    endpoint = ("https://127.0.0.1:29543", ("riot", "tok123"))
    monkeypatch.setattr(external_apps, "_find_running_riot_client_api", lambda: endpoint)

    def fake_post(url, *, auth, verify, timeout):
        raise external_apps.requests.ReadTimeout("slow launcher")

    monkeypatch.setattr(external_apps.requests, "post", fake_post)

    assert external_apps.launch_league_via_riot_client_api() is True


def test_launch_league_via_riot_client_api_rejects_error_status(monkeypatch) -> None:
    endpoint = ("https://127.0.0.1:29543", ("riot", "tok123"))
    monkeypatch.setattr(external_apps, "_find_running_riot_client_api", lambda: endpoint)

    class _Response:
        status_code = 403

    monkeypatch.setattr(
        external_apps.requests, "post", lambda url, **_kw: _Response()
    )

    assert external_apps.launch_league_via_riot_client_api() is False


def test_launch_league_via_riot_client_api_fails_on_connection_error(
    monkeypatch,
) -> None:
    endpoint = ("https://127.0.0.1:29543", ("riot", "tok123"))
    monkeypatch.setattr(external_apps, "_find_running_riot_client_api", lambda: endpoint)

    def fake_post(url, *, auth, verify, timeout):
        raise external_apps.requests.ConnectionError("api down")

    monkeypatch.setattr(external_apps.requests, "post", fake_post)

    assert external_apps.launch_league_via_riot_client_api() is False


def test_launch_league_via_riot_client_api_starts_services_when_absent(
    monkeypatch,
) -> None:
    started_cmds: list[list[str]] = []
    monkeypatch.setattr(
        external_apps,
        "_start_riot_client_services_with_api",
        lambda *, logger=None: (
            started_cmds.append(["started"]) or True
        ),
    )
    endpoint = ("https://127.0.0.1:29543", ("riot", "tok123"))
    findings: list[object] = [None, None, endpoint]
    monkeypatch.setattr(
        external_apps,
        "_find_running_riot_client_api",
        lambda: findings.pop(0),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(external_apps.time, "sleep", sleeps.append)

    class _Response:
        status_code = 200

    monkeypatch.setattr(
        external_apps.requests, "post", lambda url, **_kw: _Response()
    )

    assert external_apps.launch_league_via_riot_client_api() is True
    assert started_cmds == [["started"]]
    assert len(sleeps) == 1


def test_start_riot_client_services_with_api_builds_credential_args(
    monkeypatch,
    tmp_path: Path,
) -> None:
    rcs = _touch_exe(tmp_path / "Riot Client" / "RiotClientServices.exe")
    monkeypatch.setenv(external_apps.ENV_RIOT_CLIENT_SERVICES_EXE, str(rcs))
    cmds: list[list[str]] = []
    monkeypatch.setattr(
        external_apps,
        "start_cmd_process_once",
        lambda cmd, *, cwd=None, logger=None: cmds.append([str(x) for x in cmd])
        or _FakePopen(),
    )

    assert external_apps._start_riot_client_services_with_api() is True
    assert len(cmds) == 1
    cmd = cmds[0]
    assert cmd[0] == str(rcs)
    assert cmd[1].startswith("--remoting-auth-token=")
    assert len(cmd[1].split("=", 1)[1]) >= 16
    assert cmd[2].startswith("--app-port=")
    assert int(cmd[2].split("=", 1)[1]) > 0


def test_start_riot_client_services_with_api_fails_without_valid_exe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_app_env(monkeypatch)
    monkeypatch.setenv(
        external_apps.ENV_RIOT_CLIENT_SERVICES_EXE,
        str(tmp_path / "missing" / "RiotClientServices.exe"),
    )
    monkeypatch.setattr(
        external_apps,
        "start_cmd_process_once",
        lambda cmd, **_kwargs: (_ for _ in ()).throw(AssertionError("started")),
    )

    assert external_apps._start_riot_client_services_with_api() is False


def test_start_league_client_once_prefers_riot_client_api(
    monkeypatch,
    tmp_path: Path,
) -> None:
    league = _touch_exe(
        tmp_path / "Riot Games" / "League of Legends" / "LeagueClient.exe"
    )
    monkeypatch.setattr(
        external_apps,
        "launch_league_via_riot_client_api",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        external_apps,
        "start_cmd_once",
        lambda cmd, **_kwargs: (_ for _ in ()).throw(AssertionError("cmd used")),
    )
    monkeypatch.setattr(
        external_apps,
        "start_exe_process_once",
        lambda exe_path, **_kw: (_ for _ in ()).throw(AssertionError("direct used")),
    )

    assert external_apps.start_league_client_once(league_exe=str(league)) is True


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
