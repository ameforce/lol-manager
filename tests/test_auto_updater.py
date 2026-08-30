from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
from unittest import mock

import pytest

from lolmanager.core import auto_updater
from lolmanager.core.auto_updater import (
    InstallerUpdateService,
    PendingUpdate,
    ReleaseInfo,
    ReleaseUpdateCandidate,
    ReleaseVersion,
    UpdateApplyRequest,
    UpdateBusyError,
    UpdateError,
    UpdateState,
    has_other_installer_instance,
    inspect_startup_update,
    installer_bootstrap_wait_pid,
    installer_asset_name,
    is_installer_managed_build,
    launch_staged_installer_update,
    load_pending_update,
    parse_app_release_version,
    parse_release_tag,
    validate_staged_update,
)


class _Response:
    def __init__(
        self,
        *,
        payload: object = None,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ) -> None:
        self._payload = payload
        self._content = (
            json.dumps(payload).encode("utf-8")
            if payload is not None and not content
            else content
        )
        self.headers = headers or {"Content-Length": str(len(self._content))}
        self.url = url
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [
            self._content[index : index + chunk_size]
            for index in range(0, len(self._content), chunk_size)
        ]

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        return self.responses[url]


def _github_asset_url(version: str, name: str) -> str:
    return f"https://github.com/ameforce/lol-manager/releases/download/v{version}/{name}"


def _candidate(
    *,
    version: str = "1.1.26",
    payload: bytes = b"verified-installer",
    github_digest: str | None = None,
) -> ReleaseInfo:
    name = f"LOLManager-Setup-v{version}.exe"
    return ReleaseInfo(
        version=parse_app_release_version(version),
        tag=f"v{version}",
        installer_name=name,
        installer_url=_github_asset_url(version, name),
        installer_size=len(payload),
        checksum_url=_github_asset_url(version, "SHA256SUMS.txt"),
        github_digest=github_digest,
    )


def _release_payload(
    *,
    version: str,
    installer_url: str,
    checksum_url: str,
    installer_size: int,
    digest: str | None = None,
    prerelease: bool = False,
) -> dict[str, object]:
    installer: dict[str, object] = {
        "name": f"LOLManager-Setup-v{version}.exe",
        "browser_download_url": installer_url,
        "size": installer_size,
    }
    if digest is not None:
        installer["digest"] = digest
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": prerelease,
        "assets": [
            installer,
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": checksum_url,
                "size": 64,
            },
        ],
    }


def _staged_update(tmp_path: Path, *, version: str = "1.1.26") -> tuple[InstallerUpdateService, UpdateState]:
    installer_bytes = b"installer-content"
    release = _candidate(version=version, payload=installer_bytes)
    checksum = hashlib.sha256(installer_bytes).hexdigest()
    service = InstallerUpdateService(
        session=_Session(
            {
                release.checksum_url: _Response(
                    content=f"{checksum}  {release.installer_name}\n".encode()
                ),
                release.installer_url: _Response(content=installer_bytes),
            }
        ),
        data_dir=tmp_path,
    )
    return service, service.stage_update(release).state


def test_release_version_parser_accepts_tag_and_embedded_build_version() -> None:
    assert parse_release_tag("v1.1.25").text == "1.1.25"
    assert parse_app_release_version("1.1.25.0").text == "1.1.25"
    with pytest.raises(UpdateError):
        parse_release_tag("1.1.25")
    with pytest.raises(UpdateError):
        parse_app_release_version("1.1")


def test_planned_internal_types_are_canonical_and_aliases_preserve_schema() -> None:
    assert ReleaseUpdateCandidate is ReleaseInfo
    assert PendingUpdate is UpdateState
    assert set(UpdateState.__dataclass_fields__) == {
        "phase",
        "target_version",
        "tag",
        "installer_path",
        "sha256",
        "installer_log_path",
        "created_at_unix",
        "schema_version",
    }
    assert set(UpdateApplyRequest.__dataclass_fields__) == {
        "state",
        "install_location",
        "wait_for_pid",
        "command",
    }


def test_installer_managed_gate_requires_exact_hkcu_record_exe_and_file_version(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "registered-install"
    installed.mkdir()
    exe = installed / "LOLManager.exe"
    exe.write_bytes(b"app")
    portable = tmp_path / "LOLManager.exe"
    portable.write_bytes(b"portable")

    with (
        mock.patch.object(auto_updater, "_read_inno_install_location", return_value=installed),
        mock.patch.object(auto_updater, "_get_embedded_file_version", return_value="1.1.25.0"),
        mock.patch.object(auto_updater, "get_app_version", return_value="1.1.25.0"),
    ):
        assert is_installer_managed_build(executable=exe, frozen=True)
        assert not is_installer_managed_build(executable=portable, frozen=True)
        assert not is_installer_managed_build(executable=exe, frozen=False)

    (installed / ".lolmanager-installer-managed").write_text("legacy marker\n")
    with (
        mock.patch.object(auto_updater, "_read_inno_install_location", return_value=None),
        mock.patch.object(auto_updater, "_get_embedded_file_version", return_value="1.1.25.0"),
        mock.patch.object(auto_updater, "get_app_version", return_value="1.1.25.0"),
    ):
        assert not is_installer_managed_build(executable=exe, frozen=True)
    with (
        mock.patch.object(auto_updater, "_read_inno_install_location", return_value=installed),
        mock.patch.object(auto_updater, "_get_embedded_file_version", return_value="1.1.25.1"),
        mock.patch.object(auto_updater, "get_app_version", return_value="1.1.25.0"),
    ):
        assert not is_installer_managed_build(executable=exe, frozen=True)

    source = inspect.getsource(auto_updater.is_installer_managed_build)
    assert "INSTALLER_MARKER" not in source
    assert "Programs" not in source


def test_other_installer_instance_detects_a_distinct_matching_executable(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "LOLManager.exe"

    class _Process:
        def __init__(self, pid: int, exe: Path) -> None:
            self.info = {"pid": pid, "exe": str(exe)}
            self.pid = pid

        def exe(self) -> str:
            return str(self.info["exe"])

    current = mock.Mock()
    current.parents.return_value = []
    with (
        mock.patch.object(auto_updater.psutil, "Process", return_value=current),
        mock.patch.object(
            auto_updater.psutil,
            "process_iter",
            return_value=[_Process(101, executable), _Process(202, executable)],
        ),
    ):
        assert has_other_installer_instance(executable=executable, current_pid=101)


def test_bootstrap_wait_pid_uses_only_matching_onefile_bootstrap_parent(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "LOLManager.exe"
    bootstrap = mock.Mock(pid=88)
    bootstrap.exe.return_value = str(executable)
    explorer = mock.Mock(pid=12)
    explorer.exe.return_value = str(tmp_path / "explorer.exe")
    current = mock.Mock()
    current.parents.return_value = [bootstrap, explorer]

    with (
        mock.patch.object(auto_updater.sys, "executable", str(executable)),
        mock.patch.object(auto_updater.psutil, "Process", return_value=current),
    ):
        assert installer_bootstrap_wait_pid(current_pid=77) == 88

    current.parents.return_value = [explorer]
    with (
        mock.patch.object(auto_updater.sys, "executable", str(executable)),
        mock.patch.object(auto_updater.psutil, "Process", return_value=current),
    ):
        assert installer_bootstrap_wait_pid(current_pid=77) == 77


def test_check_uses_stable_latest_release_exact_assets_and_approved_hosts() -> None:
    installer_bytes = b"installer"
    version = "1.1.26"
    installer_url = _github_asset_url(version, "LOLManager-Setup-v1.1.26.exe")
    checksum_url = _github_asset_url(version, "SHA256SUMS.txt")
    digest = hashlib.sha256(installer_bytes).hexdigest()
    release = _release_payload(
        version=version,
        installer_url=installer_url,
        checksum_url=checksum_url,
        installer_size=len(installer_bytes),
        digest=f"sha256:{digest}",
    )
    release["assets"].append(
        {
            "name": "LOLManager-v1.1.26.exe",
            "browser_download_url": _github_asset_url(version, "LOLManager-v1.1.26.exe"),
            "size": 1,
        }
    )
    session = _Session({auto_updater.LATEST_RELEASE_URL: _Response(payload=release)})

    candidate = InstallerUpdateService(session=session).check_for_update(
        current_version="1.1.25.0"
    )

    assert candidate is not None
    assert candidate.tag == "v1.1.26"
    assert candidate.installer_name == "LOLManager-Setup-v1.1.26.exe"
    assert candidate.github_digest == digest
    assert session.calls[0][1]["timeout"] == (
        auto_updater.CONNECT_TIMEOUT_SEC,
        auto_updater.READ_TIMEOUT_SEC,
    )
    assert session.calls[0][1]["stream"] is True


def test_check_rejects_arbitrary_https_host_and_enforces_exact_300_mib_limit() -> None:
    assert auto_updater.MAX_INSTALLER_BYTES == 300 * 1024 * 1024
    version = "1.1.26"
    checksum_url = _github_asset_url(version, "SHA256SUMS.txt")
    bad_release = _release_payload(
        version=version,
        installer_url="https://downloads.example.test/setup.exe",
        checksum_url=checksum_url,
        installer_size=1,
    )
    with pytest.raises(UpdateError, match="승인된 GitHub"):
        InstallerUpdateService(
            session=_Session({auto_updater.LATEST_RELEASE_URL: _Response(payload=bad_release)})
        ).check_for_update(current_version="1.1.25.0")

    exact_release = _release_payload(
        version=version,
        installer_url=_github_asset_url(version, "LOLManager-Setup-v1.1.26.exe"),
        checksum_url=checksum_url,
        installer_size=auto_updater.MAX_INSTALLER_BYTES,
    )
    exact = InstallerUpdateService(
        session=_Session({auto_updater.LATEST_RELEASE_URL: _Response(payload=exact_release)})
    ).check_for_update(current_version="1.1.25.0")
    assert exact is not None and exact.installer_size == auto_updater.MAX_INSTALLER_BYTES

    too_large = _release_payload(
        version=version,
        installer_url=_github_asset_url(version, "LOLManager-Setup-v1.1.26.exe"),
        checksum_url=checksum_url,
        installer_size=auto_updater.MAX_INSTALLER_BYTES + 1,
    )
    with pytest.raises(UpdateError, match="크기"):
        InstallerUpdateService(
            session=_Session({auto_updater.LATEST_RELEASE_URL: _Response(payload=too_large)})
        ).check_for_update(current_version="1.1.25.0")


def test_check_is_single_flight_and_rejects_oversized_release_metadata() -> None:
    service = InstallerUpdateService(
        session=_Session(
            {
                auto_updater.LATEST_RELEASE_URL: _Response(
                    headers={
                        "Content-Length": str(auto_updater.MAX_RELEASE_METADATA_BYTES + 1)
                    }
                )
            }
        )
    )
    assert service._single_flight.acquire(blocking=False)
    try:
        with pytest.raises(UpdateBusyError):
            service.check_for_update(current_version="1.1.25.0")
    finally:
        service._single_flight.release()
    with pytest.raises(UpdateError, match="크기"):
        service.check_for_update(current_version="1.1.25.0")


def test_check_ignores_prerelease_and_never_offers_portable_asset() -> None:
    version = "1.1.26"
    session = _Session(
        {
            auto_updater.LATEST_RELEASE_URL: _Response(
                payload=_release_payload(
                    version=version,
                    installer_url=_github_asset_url(version, "LOLManager-Setup-v1.1.26.exe"),
                    checksum_url=_github_asset_url(version, "SHA256SUMS.txt"),
                    installer_size=1,
                    prerelease=True,
                )
            )
        }
    )
    assert InstallerUpdateService(session=session).check_for_update(
        current_version="1.1.25.0"
    ) is None


def test_stage_streams_exact_installer_and_writes_ready_schema_one_state(tmp_path: Path) -> None:
    installer_bytes = b"installer-content" * 31
    release = _candidate(
        payload=installer_bytes,
        github_digest=hashlib.sha256(installer_bytes).hexdigest(),
    )
    checksum = hashlib.sha256(installer_bytes).hexdigest()
    service = InstallerUpdateService(
        session=_Session(
            {
                release.checksum_url: _Response(
                    content=f"{checksum}  {release.installer_name}\n".encode()
                ),
                release.installer_url: _Response(content=installer_bytes),
            }
        ),
        data_dir=tmp_path,
    )

    staged = service.stage_update(release)
    state_path = auto_updater.update_state_path(tmp_path)
    stored = json.loads(state_path.read_text(encoding="utf-8"))

    assert staged.downloaded_bytes == len(installer_bytes)
    assert load_pending_update(state_path) == staged.state
    assert staged.state.phase == "ready"
    assert staged.state.tag == "v1.1.26"
    assert Path(staged.state.installer_path).read_bytes() == installer_bytes
    assert Path(staged.state.installer_path).parent.name == "v1.1.26"
    assert Path(staged.state.installer_log_path).name == "installer-update.log"
    assert set(stored) == {
        "schema_version",
        "phase",
        "target_version",
        "tag",
        "installer_path",
        "sha256",
        "installer_log_path",
        "created_at_unix",
    }
    assert validate_staged_update(data_dir=tmp_path) == staged.state
    assert not list((tmp_path / "updates" / "v1.1.26").glob("*.part"))


def test_stage_replaces_only_a_superseded_ready_installer_after_new_state_commits(
    tmp_path: Path,
) -> None:
    old_bytes = b"old-installer"
    new_bytes = b"new-installer"
    old_release = _candidate(version="1.1.26", payload=old_bytes)
    new_release = _candidate(version="1.1.27", payload=new_bytes)
    session = _Session(
        {
            old_release.checksum_url: _Response(
                content=(
                    f"{hashlib.sha256(old_bytes).hexdigest()}  {old_release.installer_name}\n"
                ).encode()
            ),
            old_release.installer_url: _Response(content=old_bytes),
            new_release.checksum_url: _Response(
                content=(
                    f"{hashlib.sha256(new_bytes).hexdigest()}  {new_release.installer_name}\n"
                ).encode()
            ),
            new_release.installer_url: _Response(content=new_bytes),
        }
    )
    service = InstallerUpdateService(session=session, data_dir=tmp_path)

    previous = service.stage_update(old_release).state
    previous_stage = Path(previous.installer_path).parent
    replacement = service.stage_update(new_release).state

    assert not previous_stage.exists()
    assert Path(replacement.installer_path).exists()
    assert load_pending_update(auto_updater.update_state_path(tmp_path)) == replacement


def test_stage_refuses_to_replace_an_already_launched_installer(tmp_path: Path) -> None:
    service, previous = _staged_update(tmp_path)
    state_path = auto_updater.update_state_path(tmp_path)
    auto_updater._write_update_state(replace(previous, phase="launched"), state_path=state_path)

    with pytest.raises(UpdateBusyError, match="이미 적용"):
        service.stage_update(_candidate(version="1.1.27"))

    assert Path(previous.installer_path).exists()
    assert load_pending_update(state_path).phase == "launched"


def test_stage_rejects_checksum_or_github_digest_mismatch_without_promoting_partial_file(
    tmp_path: Path,
) -> None:
    installer_bytes = b"installer-content"
    release = _candidate(
        payload=installer_bytes,
        github_digest="0" * 64,
    )
    checksum = hashlib.sha256(installer_bytes).hexdigest()
    service = InstallerUpdateService(
        session=_Session(
            {
                release.checksum_url: _Response(
                    content=f"{checksum}  {release.installer_name}\n".encode()
                ),
                release.installer_url: _Response(content=installer_bytes),
            }
        ),
        data_dir=tmp_path,
    )
    with pytest.raises(UpdateError, match="GitHub asset digest"):
        service.stage_update(release)
    stage_dir = tmp_path / "updates" / "v1.1.26"
    assert not (stage_dir / release.installer_name).exists()
    assert not list(stage_dir.glob("*.part"))


def test_validate_rejects_state_outside_v_prefixed_staging_directory(tmp_path: Path) -> None:
    _service, state = _staged_update(tmp_path)
    state_path = auto_updater.update_state_path(tmp_path)
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    wrong_dir = tmp_path / "updates" / state.target_version
    wrong_dir.mkdir()
    wrong_installer = wrong_dir / Path(state.installer_path).name
    wrong_installer.write_bytes(Path(state.installer_path).read_bytes())
    raw["installer_path"] = str(wrong_installer.resolve())
    raw["installer_log_path"] = str((wrong_dir / "installer-update.log").resolve())
    state_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(UpdateError, match="스테이징 경로"):
        validate_staged_update(data_dir=tmp_path)


def test_startup_verifies_launched_target_against_embedded_file_version_and_preserves_failure(
    tmp_path: Path,
) -> None:
    _service, staged = _staged_update(tmp_path)
    state_path = auto_updater.update_state_path(tmp_path)
    launched = replace(staged, phase="launched")
    auto_updater._write_update_state(launched, state_path=state_path)

    with mock.patch.object(auto_updater, "_get_embedded_file_version", return_value="1.1.25.0"):
        failed = inspect_startup_update(data_dir=tmp_path)
    assert failed.kind == "failed"
    assert failed.state == launched
    assert load_pending_update(state_path) == launched

    with mock.patch.object(auto_updater, "_get_embedded_file_version", return_value="1.1.26.0"):
        applied = inspect_startup_update(data_dir=tmp_path)
    assert applied.kind == "applied"
    assert applied.state is None
    assert not state_path.exists()
    assert not (tmp_path / "updates" / "v1.1.26").exists()


def test_ready_state_is_actionable_without_claiming_install_success(tmp_path: Path) -> None:
    _service, state = _staged_update(tmp_path)
    with mock.patch.object(auto_updater, "_get_embedded_file_version") as version_reader:
        status = inspect_startup_update(data_dir=tmp_path)
    assert status.kind == "pending"
    assert status.state == state
    version_reader.assert_not_called()


def test_completed_update_keeps_state_until_staging_cleanup_succeeds(tmp_path: Path) -> None:
    _service, staged = _staged_update(tmp_path)
    state_path = auto_updater.update_state_path(tmp_path)
    launched = replace(staged, phase="launched")
    auto_updater._write_update_state(launched, state_path=state_path)

    with (
        mock.patch.object(auto_updater, "_get_embedded_file_version", return_value="1.1.26.0"),
        mock.patch.object(auto_updater.shutil, "rmtree", side_effect=OSError("busy")),
    ):
        retained = inspect_startup_update(data_dir=tmp_path)
    assert retained.kind == "applied"
    assert retained.state == launched
    assert state_path.exists()

    with mock.patch.object(auto_updater, "_get_embedded_file_version", return_value="1.1.26.0"):
        cleaned = inspect_startup_update(data_dir=tmp_path)
    assert cleaned.kind == "applied"
    assert cleaned.state is None
    assert not state_path.exists()


def test_bootstrap_starts_only_verified_installer_with_fixed_contract_arguments(
    tmp_path: Path,
) -> None:
    _service, staged = _staged_update(tmp_path)
    install_location = tmp_path / "registered-install"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def record(command: list[str], **kwargs: object) -> None:
        calls.append((command, kwargs))

    with (
        mock.patch.object(auto_updater, "is_installer_managed_build", return_value=True),
        mock.patch.object(auto_updater, "_read_inno_install_location", return_value=install_location),
    ):
        request = launch_staged_installer_update(
            data_dir=tmp_path,
            wait_for_pid=4321,
            popen=record,
        )

    assert isinstance(request, UpdateApplyRequest)
    assert request.state.phase == "launched"
    assert load_pending_update(auto_updater.update_state_path(tmp_path)).phase == "launched"
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        staged.installer_path,
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        f"/DIR={install_location}",
        "/LOLMANAGER_RELAUNCH=1",
        f"/LOG={staged.installer_log_path}",
        "/LOLMANAGERWAITPID=4321",
    ]
    assert all("powershell" not in item.casefold() for item in command)
    assert all(not item.casefold().endswith(".ps1") for item in command)
    assert kwargs["close_fds"] is True


def test_bootstrap_waits_for_matching_onefile_parent_by_default(tmp_path: Path) -> None:
    _service, staged = _staged_update(tmp_path)
    install_location = tmp_path / "registered-install"
    calls: list[list[str]] = []

    with (
        mock.patch.object(auto_updater, "is_installer_managed_build", return_value=True),
        mock.patch.object(auto_updater, "_read_inno_install_location", return_value=install_location),
        mock.patch.object(auto_updater, "installer_bootstrap_wait_pid", return_value=8765),
    ):
        launch_staged_installer_update(
            data_dir=tmp_path,
            popen=lambda command, **_kwargs: calls.append(command),
        )

    assert calls == [
        [
            staged.installer_path,
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/DIR={install_location}",
            "/LOLMANAGER_RELAUNCH=1",
            f"/LOG={staged.installer_log_path}",
            "/LOLMANAGERWAITPID=8765",
        ]
    ]


def test_spawn_failure_keeps_ready_state_and_persisted_log_for_retry(tmp_path: Path) -> None:
    _service, staged = _staged_update(tmp_path)
    state_path = auto_updater.update_state_path(tmp_path)
    install_location = tmp_path / "registered-install"

    with (
        mock.patch.object(auto_updater, "is_installer_managed_build", return_value=True),
        mock.patch.object(auto_updater, "_read_inno_install_location", return_value=install_location),
        pytest.raises(UpdateError, match="시작하지 못했습니다"),
    ):
        launch_staged_installer_update(
            data_dir=tmp_path,
            wait_for_pid=4321,
            popen=mock.Mock(side_effect=OSError("denied")),
        )
    retained = load_pending_update(state_path)
    assert retained is not None
    assert retained.phase == "ready"
    assert retained.installer_log_path == staged.installer_log_path


def test_launch_retries_persisting_launched_state_after_successful_spawn(
    tmp_path: Path,
) -> None:
    _service, staged = _staged_update(tmp_path)
    install_location = tmp_path / "registered-install"
    real_write = auto_updater._write_update_state
    launched_write_attempts = 0

    def flaky_write(state: UpdateState, *, state_path: Path) -> None:
        nonlocal launched_write_attempts
        if state.phase == "launched":
            launched_write_attempts += 1
            if launched_write_attempts == 1:
                raise OSError("temporarily locked")
        real_write(state, state_path=state_path)

    with (
        mock.patch.object(auto_updater, "is_installer_managed_build", return_value=True),
        mock.patch.object(auto_updater, "_read_inno_install_location", return_value=install_location),
        mock.patch.object(auto_updater, "_write_update_state", side_effect=flaky_write),
        mock.patch.object(auto_updater.time, "sleep") as sleep,
    ):
        request = launch_staged_installer_update(
            data_dir=tmp_path,
            wait_for_pid=4321,
            popen=lambda *_args, **_kwargs: None,
        )

    assert request.state.phase == "launched"
    assert load_pending_update(auto_updater.update_state_path(tmp_path)).phase == "launched"
    assert launched_write_attempts == 2
    sleep.assert_called_once_with(auto_updater.LAUNCHED_STATE_WRITE_RETRY_SECONDS)
    assert Path(staged.installer_path).exists()


def test_launch_state_persistence_failure_is_a_retryable_update_error(tmp_path: Path) -> None:
    _service, staged = _staged_update(tmp_path)
    install_location = tmp_path / "registered-install"
    spawned: list[list[str]] = []

    with (
        mock.patch.object(auto_updater, "is_installer_managed_build", return_value=True),
        mock.patch.object(auto_updater, "_read_inno_install_location", return_value=install_location),
        mock.patch.object(auto_updater, "LAUNCHED_STATE_WRITE_ATTEMPTS", 2),
        mock.patch.object(auto_updater, "_write_update_state", side_effect=OSError("denied")),
        mock.patch.object(auto_updater.time, "sleep"),
        pytest.raises(UpdateError, match="launched 상태를 기록하지 못했습니다"),
    ):
        launch_staged_installer_update(
            data_dir=tmp_path,
            wait_for_pid=4321,
            popen=lambda command, **_kwargs: spawned.append(command),
        )

    assert len(spawned) == 1
    retained = load_pending_update(auto_updater.update_state_path(tmp_path))
    assert retained is not None and retained.phase == "ready"
    assert retained.installer_log_path == staged.installer_log_path


def test_installer_asset_name_is_exact_release_asset_name() -> None:
    assert installer_asset_name(ReleaseVersion(1, 1, 25)) == "LOLManager-Setup-v1.1.25.exe"
