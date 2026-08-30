"""Installer-only GitHub Release updater for frozen LOLManager builds.

The updater deliberately treats a GitHub release as untrusted until the exact
installer asset has been downloaded within a fixed size budget and verified
against the published SHA256SUMS entry (and GitHub's optional asset digest).
Only an installer-managed frozen executable is eligible; source and portable
builds never call this module from the GUI lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import requests

from lolmanager.core.app_version import get_app_version
from lolmanager.platform.paths import user_data_dir
from lolmanager.platform.runtime import is_frozen

GITHUB_REPOSITORY = "ameforce/LOLManager"
LATEST_RELEASE_URL = (
    f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
)
INSTALLER_MARKER_FILENAME = ".lolmanager-installer-managed"
UPDATE_STATE_FILENAME = "update-state.json"
UPDATE_STAGING_DIRECTORY = "updates"
UPDATE_RESULT_FILENAME = "apply-result.json"
UPDATER_SCHEMA_VERSION = 1

CONNECT_TIMEOUT_SEC = 3.0
READ_TIMEOUT_SEC = 8.0
DOWNLOAD_CHUNK_BYTES = 128 * 1024
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
MAX_CHECKSUM_BYTES = 1024 * 1024
MAX_RELEASE_METADATA_BYTES = 1024 * 1024
USER_AGENT = "LOLManager-Updater/1"

_RELEASE_TAG_RE = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
_APP_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:\.\d+)?$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_SHA256SUM_LINE_RE = re.compile(
    r"^(?P<digest>[0-9a-fA-F]{64})\s+\*?(?P<filename>[^\r\n]+?)\s*$"
)


class UpdateError(RuntimeError):
    """An update could not be safely checked, staged, or applied."""


class UpdateBusyError(UpdateError):
    """Another update check or staging operation already owns the service."""


@dataclass(frozen=True)
class ReleaseVersion:
    major: int
    minor: int
    patch: int

    @property
    def text(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def tag(self) -> str:
        return f"v{self.text}"

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


@dataclass(frozen=True)
class ReleaseUpdateCandidate:
    version: ReleaseVersion
    installer_name: str
    installer_url: str
    installer_size: int
    checksum_url: str
    github_digest: str | None


@dataclass(frozen=True)
class PendingUpdate:
    target_version: str
    installer_name: str
    installer_path: str
    sha256: str
    github_digest: str | None
    created_at_unix: float
    schema_version: int = UPDATER_SCHEMA_VERSION

    @property
    def result_path(self) -> Path:
        return Path(self.installer_path).parent / UPDATE_RESULT_FILENAME


@dataclass(frozen=True)
class StagedUpdate:
    pending: PendingUpdate
    downloaded_bytes: int


@dataclass(frozen=True)
class StartupUpdateStatus:
    kind: str
    message: str
    pending: PendingUpdate | None = None


def parse_release_tag(value: object) -> ReleaseVersion:
    match = _RELEASE_TAG_RE.fullmatch(str(value or "").strip())
    if match is None:
        raise UpdateError("릴리스 태그가 vX.Y.Z 형식이 아닙니다.")
    return ReleaseVersion(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def parse_app_release_version(value: object) -> ReleaseVersion:
    match = _APP_VERSION_RE.fullmatch(str(value or "").strip())
    if match is None:
        raise UpdateError("현재 앱 버전이 X.Y.Z 또는 X.Y.Z.N 형식이 아닙니다.")
    return ReleaseVersion(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def installer_asset_name(version: ReleaseVersion | str) -> str:
    text = version.text if isinstance(version, ReleaseVersion) else str(version)
    return f"LOLManager-Setup-v{text}.exe"


def _normalise_sha256(value: object) -> str:
    text = str(value or "").strip().casefold()
    if not _SHA256_RE.fullmatch(text):
        raise UpdateError("SHA-256 값 형식이 올바르지 않습니다.")
    return text


def _normalise_github_digest(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    prefix, separator, digest = text.partition(":")
    if separator != ":" or prefix.casefold() != "sha256":
        raise UpdateError("GitHub asset digest 형식이 올바르지 않습니다.")
    return _normalise_sha256(digest)


def _require_https_url(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        raise UpdateError(f"{label} URL은 HTTPS여야 합니다.")
    return text


def _bounded_content_length(response: object, *, maximum: int) -> int | None:
    headers = getattr(response, "headers", {})
    raw = headers.get("Content-Length") if isinstance(headers, Mapping) else None
    if raw in (None, ""):
        return None
    try:
        size = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise UpdateError("다운로드 응답의 Content-Length가 올바르지 않습니다.") from exc
    if size < 0 or size > maximum:
        raise UpdateError("다운로드 크기가 허용 범위를 벗어났습니다.")
    return size


def _close_response(response: object) -> None:
    """Close a streamed HTTP response without hiding the original update error."""
    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        close()
    except OSError:
        return


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def update_staging_root(data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else user_data_dir()
    return base / UPDATE_STAGING_DIRECTORY


def update_state_path(data_dir: Path | None = None) -> Path:
    return update_staging_root(data_dir) / UPDATE_STATE_FILENAME


def is_installer_managed_build(
    *,
    executable: Path | None = None,
    frozen: bool | None = None,
) -> bool:
    """Return true only for an installed frozen executable carrying our marker."""
    frozen_build = is_frozen() if frozen is None else bool(frozen)
    if not frozen_build:
        return False
    exe = Path(executable) if executable is not None else Path(sys.executable)
    try:
        install_dir = exe.resolve().parent
        marker = install_dir / INSTALLER_MARKER_FILENAME
        if marker.is_file():
            return True
        # v1.1.24 and earlier installers predate the marker.  Their default
        # per-user path plus Inno uninstaller is a narrow migration proof, not
        # a general portable-build heuristic.
        local_app_data = os.environ.get("LOCALAPPDATA")
        default_dir = (
            Path(local_app_data) / "Programs" / "LOLManager"
            if local_app_data
            else None
        )
        return bool(
            default_dir is not None
            and install_dir == default_dir.resolve()
            and (install_dir / "unins000.exe").is_file()
        )
    except OSError:
        return False


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("업데이트 상태 파일을 읽을 수 없습니다.") from exc


def _pending_from_mapping(value: object) -> PendingUpdate:
    if not isinstance(value, Mapping):
        raise UpdateError("업데이트 상태 형식이 올바르지 않습니다.")
    expected_fields = {
        "schema_version",
        "target_version",
        "installer_name",
        "installer_path",
        "sha256",
        "github_digest",
        "created_at_unix",
    }
    if set(value) != expected_fields:
        raise UpdateError("업데이트 상태 필드가 예상과 다릅니다.")
    if value.get("schema_version") != UPDATER_SCHEMA_VERSION:
        raise UpdateError("지원하지 않는 업데이트 상태 버전입니다.")
    version = parse_app_release_version(value.get("target_version"))
    installer_name = str(value.get("installer_name") or "")
    if installer_name != installer_asset_name(version):
        raise UpdateError("업데이트 installer 이름이 대상 버전과 일치하지 않습니다.")
    installer_path = Path(str(value.get("installer_path") or ""))
    if not installer_path.is_absolute() or installer_path.name != installer_name:
        raise UpdateError("업데이트 installer 경로가 올바르지 않습니다.")
    try:
        created_at_unix = float(value.get("created_at_unix"))
    except (TypeError, ValueError) as exc:
        raise UpdateError("업데이트 상태 시간이 올바르지 않습니다.") from exc
    if created_at_unix <= 0:
        raise UpdateError("업데이트 상태 시간이 올바르지 않습니다.")
    stored_github_digest = value.get("github_digest")
    if stored_github_digest in (None, ""):
        github_digest = None
    else:
        github_digest = _normalise_sha256(stored_github_digest)
    return PendingUpdate(
        target_version=version.text,
        installer_name=installer_name,
        installer_path=str(installer_path),
        sha256=_normalise_sha256(value.get("sha256")),
        github_digest=github_digest,
        created_at_unix=created_at_unix,
    )


def load_pending_update(state_path: Path | None = None) -> PendingUpdate | None:
    path = Path(state_path) if state_path is not None else update_state_path()
    try:
        raw = _read_json(path)
    except FileNotFoundError:
        return None
    return _pending_from_mapping(raw)


def _pending_mapping(pending: PendingUpdate) -> dict[str, object]:
    return asdict(pending)


def _write_pending_update(pending: PendingUpdate, *, state_path: Path) -> None:
    _atomic_write_json(state_path, _pending_mapping(pending))


def _sha256_file(path: Path, *, maximum: int) -> tuple[str, int]:
    try:
        size = int(path.stat().st_size)
    except OSError as exc:
        raise UpdateError("스테이징된 installer 파일을 찾을 수 없습니다.") from exc
    if size <= 0 or size > maximum:
        raise UpdateError("스테이징된 installer 크기가 허용 범위를 벗어났습니다.")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(DOWNLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > maximum:
                    raise UpdateError("스테이징된 installer 크기가 허용 범위를 벗어났습니다.")
                digest.update(chunk)
    except OSError as exc:
        raise UpdateError("스테이징된 installer를 읽을 수 없습니다.") from exc
    if total != size:
        raise UpdateError("스테이징된 installer 크기가 변경되었습니다.")
    return digest.hexdigest(), total


def validate_staged_update(
    pending: PendingUpdate | None = None,
    *,
    state_path: Path | None = None,
    data_dir: Path | None = None,
) -> PendingUpdate:
    state_file = Path(state_path) if state_path is not None else update_state_path(data_dir)
    active = pending or load_pending_update(state_file)
    if active is None:
        raise UpdateError("적용할 업데이트가 없습니다.")
    installer = Path(active.installer_path)
    staging_root = (
        update_staging_root(data_dir) if data_dir is not None else state_file.parent
    )
    if not _is_within(installer, staging_root):
        raise UpdateError("업데이트 installer가 허용된 스테이징 경로 밖에 있습니다.")
    expected_parent = staging_root / active.target_version
    if installer.parent.resolve() != expected_parent.resolve():
        raise UpdateError("업데이트 installer 스테이징 경로가 대상 버전과 일치하지 않습니다.")
    actual_digest, _ = _sha256_file(installer, maximum=MAX_INSTALLER_BYTES)
    if actual_digest != active.sha256:
        raise UpdateError("스테이징된 installer SHA-256 검증에 실패했습니다.")
    if active.github_digest is not None and actual_digest != active.github_digest:
        raise UpdateError("스테이징된 installer SHA-256이 GitHub asset digest와 다릅니다.")
    return active


def _write_apply_result(
    pending: PendingUpdate,
    *,
    status: str,
    message: str,
) -> None:
    if status not in {"failed", "success"}:
        raise ValueError("unsupported update result status")
    _atomic_write_json(
        pending.result_path,
        {
            "schema_version": UPDATER_SCHEMA_VERSION,
            "status": status,
            "target_version": pending.target_version,
            "message": str(message),
            "recorded_at_unix": time.time(),
        },
    )


def _read_apply_result(pending: PendingUpdate) -> dict[str, object] | None:
    try:
        raw = _read_json(pending.result_path)
    except FileNotFoundError:
        return None
    if not isinstance(raw, Mapping):
        raise UpdateError("업데이트 적용 결과 형식이 올바르지 않습니다.")
    expected = {
        "schema_version",
        "status",
        "target_version",
        "message",
        "recorded_at_unix",
    }
    if set(raw) != expected or raw.get("schema_version") != UPDATER_SCHEMA_VERSION:
        raise UpdateError("업데이트 적용 결과 필드가 올바르지 않습니다.")
    status = str(raw.get("status") or "")
    if status not in {"failed", "success"}:
        raise UpdateError("업데이트 적용 결과 상태가 올바르지 않습니다.")
    if str(raw.get("target_version") or "") != pending.target_version:
        raise UpdateError("업데이트 적용 결과 버전이 일치하지 않습니다.")
    message = str(raw.get("message") or "").strip()
    if not message:
        raise UpdateError("업데이트 적용 결과 메시지가 없습니다.")
    return {
        "status": status,
        "message": message,
    }


def _clear_pending_update(pending: PendingUpdate, *, state_path: Path, data_dir: Path | None) -> None:
    installer = Path(pending.installer_path)
    root = update_staging_root(data_dir)
    if _is_within(installer.parent, root):
        try:
            shutil.rmtree(installer.parent)
        except OSError:
            pass
    try:
        state_path.unlink(missing_ok=True)
    except OSError:
        pass


def inspect_startup_update(
    *,
    current_version: str | None = None,
    state_path: Path | None = None,
    data_dir: Path | None = None,
) -> StartupUpdateStatus:
    """Read a prior staged/apply result before the GUI starts a new check."""
    state_file = Path(state_path) if state_path is not None else update_state_path(data_dir)
    try:
        pending = load_pending_update(state_file)
    except UpdateError as exc:
        return StartupUpdateStatus("failed", str(exc))
    if pending is None:
        return StartupUpdateStatus("none", "")

    try:
        current = parse_app_release_version(current_version or get_app_version())
        target = parse_app_release_version(pending.target_version)
    except UpdateError as exc:
        return StartupUpdateStatus("failed", str(exc), pending)

    if current.as_tuple() >= target.as_tuple():
        _clear_pending_update(pending, state_path=state_file, data_dir=data_dir)
        return StartupUpdateStatus(
            "applied",
            f"v{target.text} 업데이트 설치를 확인했습니다.",
        )

    try:
        result = _read_apply_result(pending)
    except UpdateError as exc:
        return StartupUpdateStatus("failed", str(exc), pending)
    if result is not None and result["status"] == "failed":
        return StartupUpdateStatus("failed", str(result["message"]), pending)
    if result is not None and result["status"] == "success":
        return StartupUpdateStatus(
            "failed",
            "installer는 성공을 보고했지만 새 앱 버전을 확인하지 못했습니다.",
            pending,
        )
    return StartupUpdateStatus(
        "pending",
        f"v{target.text} 업데이트가 준비되어 있습니다.",
        pending,
    )


class InstallerUpdateService:
    """A process-local single-flight GitHub Release check and staging service."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        state_path: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._session = session or requests.Session()
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._state_path = (
            Path(state_path) if state_path is not None else update_state_path(self._data_dir)
        )
        self._single_flight = threading.Lock()

    def _request(self, url: str, *, stream: bool) -> requests.Response:
        response: requests.Response | None = None
        try:
            response = self._session.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": USER_AGENT,
                },
                timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC),
                stream=stream,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if response is not None:
                _close_response(response)
            raise UpdateError("GitHub 릴리스 정보를 가져오지 못했습니다.") from exc

    @staticmethod
    def _release_asset(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise UpdateError("GitHub 릴리스 asset 목록 형식이 올바르지 않습니다.")
        matches = [item for item in assets if isinstance(item, Mapping) and item.get("name") == name]
        if len(matches) != 1:
            raise UpdateError(f"릴리스의 정확한 asset을 찾을 수 없습니다: {name}")
        return matches[0]

    def check_for_update(
        self,
        *,
        current_version: str | None = None,
    ) -> ReleaseUpdateCandidate | None:
        if not self._single_flight.acquire(blocking=False):
            raise UpdateBusyError("업데이트 확인 또는 다운로드가 이미 진행 중입니다.")
        try:
            raw_payload = self._download_limited_bytes(
                LATEST_RELEASE_URL,
                maximum=MAX_RELEASE_METADATA_BYTES,
            )
            try:
                payload = json.loads(raw_payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UpdateError("GitHub 릴리스 응답 JSON 형식이 올바르지 않습니다.") from exc
            if not isinstance(payload, Mapping):
                raise UpdateError("GitHub 릴리스 응답 형식이 올바르지 않습니다.")
            if bool(payload.get("draft")) or bool(payload.get("prerelease")):
                return None
            target = parse_release_tag(payload.get("tag_name"))
            current = parse_app_release_version(current_version or get_app_version())
            if target.as_tuple() <= current.as_tuple():
                return None

            installer_name = installer_asset_name(target)
            installer = self._release_asset(payload, installer_name)
            checksums = self._release_asset(payload, "SHA256SUMS.txt")
            installer_url = _require_https_url(
                installer.get("browser_download_url"), label="installer"
            )
            checksum_url = _require_https_url(
                checksums.get("browser_download_url"), label="SHA256SUMS"
            )
            try:
                installer_size = int(installer.get("size"))
            except (TypeError, ValueError) as exc:
                raise UpdateError("installer asset 크기 정보가 올바르지 않습니다.") from exc
            if installer_size <= 0 or installer_size > MAX_INSTALLER_BYTES:
                raise UpdateError("installer asset 크기가 허용 범위를 벗어났습니다.")
            return ReleaseUpdateCandidate(
                version=target,
                installer_name=installer_name,
                installer_url=installer_url,
                installer_size=installer_size,
                checksum_url=checksum_url,
                github_digest=_normalise_github_digest(installer.get("digest")),
            )
        finally:
            self._single_flight.release()

    def _download_limited_bytes(self, url: str, *, maximum: int) -> bytes:
        response = self._request(url, stream=True)
        chunks: list[bytes] = []
        total = 0
        try:
            _bounded_content_length(response, maximum=maximum)
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                total += len(chunk)
                if total > maximum:
                    raise UpdateError("다운로드 크기가 허용 범위를 벗어났습니다.")
                chunks.append(bytes(chunk))
        finally:
            _close_response(response)
        return b"".join(chunks)

    @staticmethod
    def _checksum_for_asset(checksum_text: str, asset_name: str) -> str:
        matches: list[str] = []
        for raw_line in checksum_text.splitlines():
            match = _SHA256SUM_LINE_RE.fullmatch(raw_line)
            if match is None:
                continue
            if match.group("filename") == asset_name:
                matches.append(_normalise_sha256(match.group("digest")))
        if len(matches) != 1:
            raise UpdateError("SHA256SUMS에서 정확한 installer checksum을 찾을 수 없습니다.")
        return matches[0]

    def _download_installer(
        self,
        candidate: ReleaseUpdateCandidate,
        *,
        destination: Path,
        expected_sha256: str,
    ) -> int:
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        response = self._request(candidate.installer_url, stream=True)
        digest = hashlib.sha256()
        total = 0
        try:
            response_length = _bounded_content_length(
                response, maximum=MAX_INSTALLER_BYTES
            )
            if response_length is not None and response_length != candidate.installer_size:
                raise UpdateError("installer 응답 크기가 GitHub asset 메타데이터와 다릅니다.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as stream:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_INSTALLER_BYTES:
                        raise UpdateError("installer 다운로드 크기가 허용 범위를 벗어났습니다.")
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if total != candidate.installer_size:
                raise UpdateError("installer 다운로드 크기가 GitHub asset 메타데이터와 다릅니다.")
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise UpdateError("installer SHA-256이 SHA256SUMS와 다릅니다.")
            if candidate.github_digest is not None and actual != candidate.github_digest:
                raise UpdateError("installer SHA-256이 GitHub asset digest와 다릅니다.")
            os.replace(temporary, destination)
            return total
        finally:
            _close_response(response)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def stage_update(self, candidate: ReleaseUpdateCandidate) -> StagedUpdate:
        if not self._single_flight.acquire(blocking=False):
            raise UpdateBusyError("업데이트 확인 또는 다운로드가 이미 진행 중입니다.")
        try:
            checksum_bytes = self._download_limited_bytes(
                candidate.checksum_url, maximum=MAX_CHECKSUM_BYTES
            )
            try:
                checksum_text = checksum_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UpdateError("SHA256SUMS가 UTF-8 텍스트가 아닙니다.") from exc
            expected_sha256 = self._checksum_for_asset(
                checksum_text, candidate.installer_name
            )

            stage_dir = update_staging_root(self._data_dir) / candidate.version.text
            destination = stage_dir / candidate.installer_name
            downloaded_bytes = self._download_installer(
                candidate,
                destination=destination,
                expected_sha256=expected_sha256,
            )
            pending = PendingUpdate(
                target_version=candidate.version.text,
                installer_name=candidate.installer_name,
                installer_path=str(destination.resolve()),
                sha256=expected_sha256,
                github_digest=candidate.github_digest,
                created_at_unix=time.time(),
            )
            _write_pending_update(pending, state_path=self._state_path)
            return StagedUpdate(pending=pending, downloaded_bytes=downloaded_bytes)
        finally:
            self._single_flight.release()


def _windows_detached_flags() -> int:
    if os.name != "nt":
        return 0
    return int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )


def launch_staged_installer_update(
    *,
    state_path: Path | None = None,
    data_dir: Path | None = None,
    wait_for_pid: int | None = None,
    popen: object = subprocess.Popen,
) -> None:
    """Start Inno Setup directly after the GUI normal mainloop has stopped.

    The installer receives the original LOLManager PID and waits for this
    bootstrap process to end before replacing the executable.  Inno Setup's
    update mode then relaunches the new binary from its own successful [Run]
    lifecycle, so no updater executable, PowerShell runner, or shell helper is
    shipped or left running.
    """
    state_file = (
        Path(state_path) if state_path is not None else update_state_path(data_dir)
    )
    pending = validate_staged_update(state_path=state_file, data_dir=data_dir)
    pid = os.getpid() if wait_for_pid is None else int(wait_for_pid)
    if pid <= 0:
        raise UpdateError("대기할 앱 PID가 올바르지 않습니다.")
    installer_log = pending.result_path.parent / "apply-installer.log"
    command = [
        pending.installer_path,
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        f"/LOG={installer_log}",
        "/LOLMANAGERUPDATEMODE",
        f"/LOLMANAGERWAITPID={pid}",
        f"/LOLMANAGERRESULT={pending.result_path}",
        f"/LOLMANAGERTARGETVERSION={pending.target_version}",
    ]
    try:
        popen(
            command,
            cwd=str(Path(pending.installer_path).parent),
            close_fds=True,
            creationflags=_windows_detached_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("silent installer 업데이트를 시작하지 못했습니다.") from exc


def mark_installer_launch_failure(
    *,
    state_path: Path | None = None,
    data_dir: Path | None = None,
    message: str,
) -> None:
    """Persist a local failure message when the installer process cannot start."""
    state_file = (
        Path(state_path) if state_path is not None else update_state_path(data_dir)
    )
    try:
        pending = load_pending_update(state_file)
        if pending is not None:
            _write_apply_result(pending, status="failed", message=message)
    except (OSError, UpdateError):
        return
