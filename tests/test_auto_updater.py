from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from lolmanager.core import auto_updater
from lolmanager.core.auto_updater import (
    InstallerUpdateService,
    ReleaseUpdateCandidate,
    ReleaseVersion,
    UpdateBusyError,
    UpdateError,
    inspect_startup_update,
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
    ) -> None:
        self._payload = payload
        self._content = (
            json.dumps(payload).encode("utf-8")
            if payload is not None and not content
            else content
        )
        self.headers = headers or {"Content-Length": str(len(self._content))}
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload

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


def _candidate(
    *,
    version: str = "1.1.26",
    payload: bytes = b"verified-installer",
    github_digest: str | None = None,
) -> ReleaseUpdateCandidate:
    return ReleaseUpdateCandidate(
        version=parse_app_release_version(version),
        installer_name=f"LOLManager-Setup-v{version}.exe",
        installer_url="https://downloads.example.test/LOLManager-Setup.exe",
        installer_size=len(payload),
        checksum_url="https://downloads.example.test/SHA256SUMS.txt",
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


def test_release_version_parser_accepts_tag_and_embedded_build_version() -> None:
    assert parse_release_tag("v1.1.25").text == "1.1.25"
    assert parse_app_release_version("1.1.25.0").text == "1.1.25"
    with pytest.raises(UpdateError):
        parse_release_tag("1.1.25")
    with pytest.raises(UpdateError):
        parse_app_release_version("1.1")


def test_installer_managed_gate_excludes_source_and_accepts_marker(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    exe = installed / "LOLManager.exe"
    exe.write_bytes(b"app")

    assert not is_installer_managed_build(executable=exe, frozen=False)
    with mock.patch.object(auto_updater, "is_frozen", return_value=True):
        assert not is_installer_managed_build(executable=exe)
        (installed / auto_updater.INSTALLER_MARKER_FILENAME).write_text("managed\n")
        assert is_installer_managed_build(executable=exe)


def test_check_uses_stable_latest_release_and_exact_assets() -> None:
    installer_bytes = b"installer"
    installer_url = "https://downloads.example.test/setup"
    checksum_url = "https://downloads.example.test/checksums"
    digest = hashlib.sha256(installer_bytes).hexdigest()
    release = _release_payload(
        version="1.1.26",
        installer_url=installer_url,
        checksum_url=checksum_url,
        installer_size=len(installer_bytes),
        digest=f"sha256:{digest}",
    )
    release["assets"].append(
        {
            "name": "LOLManager-v1.1.26.exe",
            "browser_download_url": "https://downloads.example.test/portable",
            "size": 1,
        }
    )
    session = _Session(
        {
            auto_updater.LATEST_RELEASE_URL: _Response(
                payload=release
            )
        }
    )
    service = InstallerUpdateService(session=session)

    candidate = service.check_for_update(current_version="1.1.25.0")

    assert candidate is not None
    assert candidate.version.text == "1.1.26"
    assert candidate.installer_name == "LOLManager-Setup-v1.1.26.exe"
    assert candidate.github_digest == digest
    assert session.calls[0][1]["timeout"] == (
        auto_updater.CONNECT_TIMEOUT_SEC,
        auto_updater.READ_TIMEOUT_SEC,
    )
    assert session.calls[0][1]["stream"] is True


def test_check_is_single_flight_and_rejects_oversized_release_metadata() -> None:
    service = InstallerUpdateService(
        session=_Session(
            {
                auto_updater.LATEST_RELEASE_URL: _Response(
                    headers={
                        "Content-Length": str(
                            auto_updater.MAX_RELEASE_METADATA_BYTES + 1
                        )
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
    session = _Session(
        {
            auto_updater.LATEST_RELEASE_URL: _Response(
                payload=_release_payload(
                    version="1.1.26",
                    installer_url="https://downloads.example.test/setup",
                    checksum_url="https://downloads.example.test/checksums",
                    installer_size=1,
                    prerelease=True,
                )
            )
        }
    )

    assert InstallerUpdateService(session=session).check_for_update(
        current_version="1.1.25.0"
    ) is None


def test_stage_streams_exact_installer_and_verifies_both_digests(tmp_path: Path) -> None:
    installer_bytes = b"installer-content" * 31
    candidate = _candidate(
        payload=installer_bytes,
        github_digest=hashlib.sha256(installer_bytes).hexdigest(),
    )
    checksum = hashlib.sha256(installer_bytes).hexdigest()
    checksum_bytes = f"{checksum}  {candidate.installer_name}\n".encode()
    session = _Session(
        {
            candidate.checksum_url: _Response(content=checksum_bytes),
            candidate.installer_url: _Response(content=installer_bytes),
        }
    )
    service = InstallerUpdateService(session=session, data_dir=tmp_path)

    staged = service.stage_update(candidate)
    loaded = load_pending_update(auto_updater.update_state_path(tmp_path))

    assert staged.downloaded_bytes == len(installer_bytes)
    assert loaded == staged.pending
    assert Path(staged.pending.installer_path).read_bytes() == installer_bytes
    assert validate_staged_update(data_dir=tmp_path) == staged.pending
    assert not list((tmp_path / "updates" / "1.1.26").glob("*.part"))


def test_stage_rejects_checksum_mismatch_without_promoting_partial_file(
    tmp_path: Path,
) -> None:
    installer_bytes = b"installer-content"
    candidate = _candidate(payload=installer_bytes)
    checksum_bytes = (
        f"{'0' * 64}  {candidate.installer_name}\n".encode()
    )
    session = _Session(
        {
            candidate.checksum_url: _Response(content=checksum_bytes),
            candidate.installer_url: _Response(content=installer_bytes),
        }
    )

    with pytest.raises(UpdateError, match="SHA-256"):
        InstallerUpdateService(session=session, data_dir=tmp_path).stage_update(candidate)

    stage_dir = tmp_path / "updates" / candidate.version.text
    assert not (stage_dir / candidate.installer_name).exists()
    assert not list(stage_dir.glob("*.part"))


def test_apply_revalidates_the_optional_github_digest_before_launch(
    tmp_path: Path,
) -> None:
    installer_bytes = b"installer-content"
    candidate = _candidate(
        payload=installer_bytes,
        github_digest=hashlib.sha256(installer_bytes).hexdigest(),
    )
    checksum = hashlib.sha256(installer_bytes).hexdigest()
    session = _Session(
        {
            candidate.checksum_url: _Response(
                content=f"{checksum}  {candidate.installer_name}\n".encode()
            ),
            candidate.installer_url: _Response(content=installer_bytes),
        }
    )

    service = InstallerUpdateService(session=session, data_dir=tmp_path)
    service.stage_update(candidate)
    state_file = auto_updater.update_state_path(tmp_path)
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    stored["github_digest"] = "0" * 64
    state_file.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(UpdateError, match="GitHub asset digest"):
        validate_staged_update(data_dir=tmp_path)


def test_startup_status_keeps_failure_for_user_retry_then_clears_verified_update(
    tmp_path: Path,
) -> None:
    installer_bytes = b"installer-content"
    candidate = _candidate(payload=installer_bytes)
    checksum = hashlib.sha256(installer_bytes).hexdigest()
    session = _Session(
        {
            candidate.checksum_url: _Response(
                content=f"{checksum}  {candidate.installer_name}\n".encode()
            ),
            candidate.installer_url: _Response(content=installer_bytes),
        }
    )
    service = InstallerUpdateService(session=session, data_dir=tmp_path)
    staged = service.stage_update(candidate)
    staged.pending.result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "failed",
                "target_version": staged.pending.target_version,
                "message": "silent installer failed",
                "recorded_at_unix": 1,
            }
        ),
        encoding="utf-8",
    )

    failed = inspect_startup_update(current_version="1.1.25.0", data_dir=tmp_path)
    assert failed.kind == "failed"
    assert failed.pending == staged.pending

    applied = inspect_startup_update(current_version="1.1.26.0", data_dir=tmp_path)
    assert applied.kind == "applied"
    assert not auto_updater.update_state_path(tmp_path).exists()
    assert not (tmp_path / "updates" / "1.1.26").exists()


def test_bootstrap_starts_only_the_verified_installer_with_fixed_update_arguments(
    tmp_path: Path,
) -> None:
    installer_bytes = b"installer-content"
    candidate = _candidate(payload=installer_bytes)
    checksum = hashlib.sha256(installer_bytes).hexdigest()
    service = InstallerUpdateService(
        session=_Session(
            {
                candidate.checksum_url: _Response(
                    content=f"{checksum}  {candidate.installer_name}\n".encode()
                ),
                candidate.installer_url: _Response(content=installer_bytes),
            }
        ),
        data_dir=tmp_path,
    )
    staged = service.stage_update(candidate)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def record(command: list[str], **kwargs: object) -> None:
        calls.append((command, kwargs))

    launch_staged_installer_update(
        data_dir=tmp_path,
        wait_for_pid=4321,
        popen=record,
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == staged.pending.installer_path
    assert command[1:4] == ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    assert "/LOLMANAGERUPDATEMODE" in command
    assert "/LOLMANAGERWAITPID=4321" in command
    assert any(item.startswith("/LOLMANAGERRESULT=") for item in command)
    assert all("powershell" not in item.casefold() for item in command)
    assert all(not item.casefold().endswith(".ps1") for item in command)
    assert kwargs["close_fds"] is True


def test_installer_asset_name_is_exact_release_asset_name() -> None:
    assert installer_asset_name(ReleaseVersion(1, 1, 25)) == "LOLManager-Setup-v1.1.25.exe"
