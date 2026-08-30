"""Installer-only GitHub Release updater for frozen LOLManager builds.

The updater accepts only an Inno Setup registered LOLManager installation. It
stages one exact GitHub Release installer under ``%LOCALAPPDATA%`` and starts
that installer directly after the normal GUI mainloop has stopped. No helper
executable, shell runner, or PowerShell script participates in the update
lifecycle.
"""

from __future__ import annotations

import ctypes
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
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psutil
import requests

from lolmanager.core.app_version import get_app_version
from lolmanager.platform.runtime import is_frozen

GITHUB_REPOSITORY = "ameforce/lol-manager"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
APPROVED_GITHUB_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "objects-origin.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)

INNO_APP_ID = "{F1E18E34-A5B3-4DE8-8E91-74DC33D66D15}"
INNO_UNINSTALL_REGISTRY_KEY = (
    rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{INNO_APP_ID}_is1"
)
INNO_INSTALL_LOCATION_VALUE = "InstallLocation"
INSTALLED_EXE_NAME = "LOLManager.exe"

PENDING_UPDATE_FILENAME = "pending-update.json"
UPDATE_STATE_FILENAME = PENDING_UPDATE_FILENAME
UPDATE_STAGING_DIRECTORY = "updates"
INSTALLER_LOG_FILENAME = "installer-update.log"
UPDATER_SCHEMA_VERSION = 1

CONNECT_TIMEOUT_SEC = 3.0
READ_TIMEOUT_SEC = 8.0
DOWNLOAD_CHUNK_BYTES = 128 * 1024
MAX_INSTALLER_BYTES = 300 * 1024 * 1024
MAX_CHECKSUM_BYTES = 1024 * 1024
MAX_RELEASE_METADATA_BYTES = 1024 * 1024
LAUNCHED_STATE_WRITE_ATTEMPTS = 3
LAUNCHED_STATE_WRITE_RETRY_SECONDS = 0.05
USER_AGENT = "LOLManager-Updater/1"

_RELEASE_TAG_RE = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
_APP_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:\.\d+)?$"
)
_QUAD_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
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
class ReleaseInfo:
    """The exact stable GitHub Release installer selected for staging."""

    version: ReleaseVersion
    tag: str
    installer_name: str
    installer_url: str
    installer_size: int
    checksum_url: str
    github_digest: str | None


@dataclass(frozen=True)
class UpdateState:
    """Schema-1 durable state for one staged installer update."""

    phase: str
    target_version: str
    tag: str
    installer_path: str
    sha256: str
    installer_log_path: str
    created_at_unix: float
    schema_version: int = UPDATER_SCHEMA_VERSION

    @property
    def installer_name(self) -> str:
        return Path(self.installer_path).name


@dataclass(frozen=True)
class UpdateApplyRequest:
    """The direct native installer process request issued after GUI shutdown."""

    state: UpdateState
    install_location: Path
    wait_for_pid: int
    command: tuple[str, ...]


# These aliases retain source compatibility while exposing the planned internal
# contract names above. They intentionally carry the same schema and semantics.
ReleaseUpdateCandidate = ReleaseInfo
PendingUpdate = UpdateState


@dataclass(frozen=True)
class StagedUpdate:
    state: UpdateState
    downloaded_bytes: int

    @property
    def pending(self) -> UpdateState:
        return self.state


@dataclass(frozen=True)
class StartupUpdateStatus:
    kind: str
    message: str
    state: UpdateState | None = None

    @property
    def pending(self) -> UpdateState | None:
        return self.state


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


def _require_github_https_url(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UpdateError(f"{label} URL 포트 형식이 올바르지 않습니다.") from exc
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or host not in APPROVED_GITHUB_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise UpdateError(f"{label} URL은 승인된 GitHub HTTPS host여야 합니다.")
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


def local_update_data_dir() -> Path:
    """Return the required per-user update data root, never the roaming path."""
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise UpdateError("LOCALAPPDATA 업데이트 경로를 확인할 수 없습니다.")
    return Path(local_app_data) / "LOLManager"


def update_staging_root(data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else local_update_data_dir()
    return base / UPDATE_STAGING_DIRECTORY


def update_state_path(data_dir: Path | None = None) -> Path:
    return update_staging_root(data_dir) / PENDING_UPDATE_FILENAME


def _read_inno_install_location() -> Path | None:
    """Read exactly the HKCU uninstall record written by our Inno AppId."""
    try:
        import winreg  # type: ignore[attr-defined]
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            INNO_UNINSTALL_REGISTRY_KEY,
            0,
            winreg.KEY_READ,
        ) as key:
            value, _value_type = winreg.QueryValueEx(key, INNO_INSTALL_LOCATION_VALUE)
    except OSError:
        return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(text).resolve()
    except OSError:
        return None


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


def _get_embedded_file_version(executable: Path) -> str | None:
    """Return the PE fixed FileVersion as X.Y.Z.N, without a text fallback."""
    try:
        version_dll = ctypes.WinDLL("version", use_last_error=True)
    except (AttributeError, OSError):
        return None
    try:
        handle = ctypes.c_uint32(0)
        size = int(
            version_dll.GetFileVersionInfoSizeW(str(executable), ctypes.byref(handle))
        )
        if size <= 0:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(str(executable), 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint32(0)
        if not version_dll.VerQueryValueW(
            buffer,
            "\\",
            ctypes.byref(pointer),
            ctypes.byref(length),
        ):
            return None
        if not pointer.value or length.value < ctypes.sizeof(_VS_FIXEDFILEINFO):
            return None
        info = ctypes.cast(pointer, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
    except (AttributeError, OSError, ValueError):
        return None
    if info.dwSignature != 0xFEEF04BD:
        return None
    return ".".join(
        str(value)
        for value in (
            (info.dwFileVersionMS >> 16) & 0xFFFF,
            info.dwFileVersionMS & 0xFFFF,
            (info.dwFileVersionLS >> 16) & 0xFFFF,
            info.dwFileVersionLS & 0xFFFF,
        )
    )


def _expected_installed_file_version() -> str | None:
    try:
        version = parse_app_release_version(get_app_version())
    except UpdateError:
        return None
    return f"{version.text}.0"


def is_installer_managed_build(
    *,
    executable: Path | None = None,
    frozen: bool | None = None,
) -> bool:
    """Require the exact HKCU Inno record, installed EXE path, and FileVersion."""
    if not (is_frozen() if frozen is None else bool(frozen)):
        return False
    install_location = _read_inno_install_location()
    if install_location is None:
        return False
    try:
        current_executable = (
            Path(executable) if executable is not None else Path(sys.executable)
        ).resolve()
        registered_executable = (install_location / INSTALLED_EXE_NAME).resolve()
    except OSError:
        return False
    if current_executable != registered_executable:
        return False
    expected_version = _expected_installed_file_version()
    actual_version = _get_embedded_file_version(current_executable)
    return bool(
        expected_version
        and actual_version
        and _QUAD_VERSION_RE.fullmatch(actual_version)
        and actual_version == expected_version
    )


def has_other_installer_instance(
    *,
    executable: Path | None = None,
    current_pid: int | None = None,
) -> bool:
    """Return whether another process is running this installed executable."""
    try:
        expected = (
            Path(executable) if executable is not None else Path(sys.executable)
        ).resolve()
    except OSError as exc:
        raise UpdateError("설치된 LOLManager 실행 파일 경로를 확인할 수 없습니다.") from exc

    pid = os.getpid() if current_pid is None else int(current_pid)
    if pid <= 0:
        raise UpdateError("현재 LOLManager PID가 올바르지 않습니다.")
    ignored_pids = {pid}
    try:
        ignored_pids.update(int(parent.pid) for parent in psutil.Process(pid).parents())
    except (psutil.Error, OSError):
        pass

    try:
        for process in psutil.process_iter(["pid", "exe"]):
            try:
                info = process.info
                process_pid = int(info.get("pid", process.pid))
                if process_pid in ignored_pids:
                    continue
                executable_path = info.get("exe") or process.exe()
                if executable_path and Path(str(executable_path)).resolve() == expected:
                    return True
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
                OSError,
            ):
                continue
    except (psutil.Error, OSError) as exc:
        raise UpdateError("다른 LOLManager 실행 상태를 확인할 수 없습니다.") from exc
    return False


def installer_bootstrap_wait_pid(*, current_pid: int | None = None) -> int:
    """Return the one-file bootstrap PID that owns this installed process.

    PyInstaller's one-file bootloader can be the direct parent of the GUI
    child.  Waiting only for that child races the bootloader's own teardown,
    which the installer must conservatively treat as a residual instance.  We
    therefore wait for the highest direct ancestor running the *same installed
    executable*.  A normal launcher such as Explorer is never selected.
    """
    pid = os.getpid() if current_pid is None else int(current_pid)
    if pid <= 0:
        raise UpdateError("현재 LOLManager PID가 올바르지 않습니다.")
    try:
        expected = Path(sys.executable).resolve()
    except OSError as exc:
        raise UpdateError("설치된 LOLManager 실행 파일 경로를 확인할 수 없습니다.") from exc

    wait_pid = pid
    try:
        for parent in psutil.Process(pid).parents():
            try:
                parent_executable = Path(parent.exe()).resolve()
            except (psutil.Error, OSError):
                break
            if parent_executable != expected:
                break
            parent_pid = int(parent.pid)
            if parent_pid <= 0:
                break
            wait_pid = parent_pid
    except (psutil.Error, OSError):
        return pid
    return wait_pid


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


def _state_from_mapping(value: object) -> UpdateState:
    if not isinstance(value, Mapping):
        raise UpdateError("업데이트 상태 형식이 올바르지 않습니다.")
    expected_fields = {
        "schema_version",
        "phase",
        "target_version",
        "tag",
        "installer_path",
        "sha256",
        "installer_log_path",
        "created_at_unix",
    }
    if set(value) != expected_fields:
        raise UpdateError("업데이트 상태 필드가 예상과 다릅니다.")
    if value.get("schema_version") != UPDATER_SCHEMA_VERSION:
        raise UpdateError("지원하지 않는 업데이트 상태 버전입니다.")
    phase = str(value.get("phase") or "")
    if phase not in {"ready", "launched"}:
        raise UpdateError("업데이트 상태 phase가 올바르지 않습니다.")
    version = parse_app_release_version(value.get("target_version"))
    tag = str(value.get("tag") or "")
    if tag != version.tag:
        raise UpdateError("업데이트 상태 tag가 대상 버전과 일치하지 않습니다.")
    installer_path = Path(str(value.get("installer_path") or ""))
    if not installer_path.is_absolute() or installer_path.name != installer_asset_name(version):
        raise UpdateError("업데이트 installer 경로가 올바르지 않습니다.")
    installer_log_path = Path(str(value.get("installer_log_path") or ""))
    if (
        not installer_log_path.is_absolute()
        or installer_log_path.parent != installer_path.parent
        or installer_log_path.name != INSTALLER_LOG_FILENAME
    ):
        raise UpdateError("업데이트 installer 로그 경로가 올바르지 않습니다.")
    try:
        created_at_unix = float(value.get("created_at_unix"))
    except (TypeError, ValueError) as exc:
        raise UpdateError("업데이트 상태 시간이 올바르지 않습니다.") from exc
    if created_at_unix <= 0:
        raise UpdateError("업데이트 상태 시간이 올바르지 않습니다.")
    return UpdateState(
        phase=phase,
        target_version=version.text,
        tag=tag,
        installer_path=str(installer_path),
        sha256=_normalise_sha256(value.get("sha256")),
        installer_log_path=str(installer_log_path),
        created_at_unix=created_at_unix,
    )


def load_pending_update(state_path: Path | None = None) -> UpdateState | None:
    path = Path(state_path) if state_path is not None else update_state_path()
    try:
        raw = _read_json(path)
    except FileNotFoundError:
        return None
    return _state_from_mapping(raw)


def _state_mapping(state: UpdateState) -> dict[str, object]:
    return {
        "schema_version": state.schema_version,
        "phase": state.phase,
        "target_version": state.target_version,
        "tag": state.tag,
        "installer_path": state.installer_path,
        "sha256": state.sha256,
        "installer_log_path": state.installer_log_path,
        "created_at_unix": state.created_at_unix,
    }


def _write_update_state(state: UpdateState, *, state_path: Path) -> None:
    _atomic_write_json(state_path, _state_mapping(state))


def _write_launched_update_state(state: UpdateState, *, state_path: Path) -> None:
    """Persist a spawned installer state, tolerating transient replacement locks."""
    last_error: OSError | None = None
    for attempt in range(LAUNCHED_STATE_WRITE_ATTEMPTS):
        try:
            _write_update_state(state, state_path=state_path)
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 < LAUNCHED_STATE_WRITE_ATTEMPTS:
                time.sleep(LAUNCHED_STATE_WRITE_RETRY_SECONDS)
    raise UpdateError(
        "installer는 시작되었지만 launched 상태를 기록하지 못했습니다. "
        "installer 로그와 스테이징 파일은 보존됩니다."
    ) from last_error


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
    state: UpdateState | None = None,
    *,
    state_path: Path | None = None,
    data_dir: Path | None = None,
) -> UpdateState:
    state_file = Path(state_path) if state_path is not None else update_state_path(data_dir)
    active = state or load_pending_update(state_file)
    if active is None:
        raise UpdateError("적용할 업데이트가 없습니다.")
    installer = Path(active.installer_path)
    installer_log = Path(active.installer_log_path)
    staging_root = update_staging_root(data_dir) if data_dir is not None else state_file.parent
    if not _is_within(installer, staging_root):
        raise UpdateError("업데이트 installer가 허용된 스테이징 경로 밖에 있습니다.")
    expected_parent = staging_root / f"v{active.target_version}"
    if installer.parent.resolve() != expected_parent.resolve():
        raise UpdateError("업데이트 installer 스테이징 경로가 대상 버전과 일치하지 않습니다.")
    if installer_log.parent.resolve() != expected_parent.resolve():
        raise UpdateError("업데이트 installer 로그 경로가 대상 버전과 일치하지 않습니다.")
    actual_digest, _ = _sha256_file(installer, maximum=MAX_INSTALLER_BYTES)
    if actual_digest != active.sha256:
        raise UpdateError("스테이징된 installer SHA-256 검증에 실패했습니다.")
    return active


def _clear_update_state(
    state: UpdateState,
    *,
    state_path: Path,
    data_dir: Path | None,
) -> bool:
    """Remove a verified update only after its versioned staging directory is gone."""
    installer = Path(state.installer_path)
    root = update_staging_root(data_dir) if data_dir is not None else state_path.parent
    if not _is_within(installer.parent, root):
        return False
    try:
        active = load_pending_update(state_path)
    except UpdateError:
        return False
    if active is not None and active != state:
        return True
    try:
        shutil.rmtree(installer.parent)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        active = load_pending_update(state_path)
    except UpdateError:
        return False
    if active is not None and active != state:
        return True
    try:
        state_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _remove_superseded_ready_stage(
    previous: UpdateState | None,
    replacement: UpdateState,
    *,
    state_path: Path,
    data_dir: Path | None,
) -> None:
    """Best-effort cleanup of a superseded, never-launched installer only."""
    if previous is None or previous.phase != "ready":
        return
    old_stage = Path(previous.installer_path).parent
    replacement_stage = Path(replacement.installer_path).parent
    if old_stage == replacement_stage:
        return
    staging_root = update_staging_root(data_dir)
    if (
        old_stage.name != f"v{previous.target_version}"
        or not _is_within(old_stage, staging_root)
        or not _is_within(replacement_stage, staging_root)
    ):
        return
    try:
        active = load_pending_update(state_path)
    except UpdateError:
        return
    if active != replacement:
        return
    try:
        shutil.rmtree(old_stage)
    except FileNotFoundError:
        return
    except OSError:
        return


def inspect_startup_update(
    *,
    executable: Path | None = None,
    state_path: Path | None = None,
    data_dir: Path | None = None,
) -> StartupUpdateStatus:
    """Verify a launched target against the real embedded/FileVersion at startup."""
    state_file = Path(state_path) if state_path is not None else update_state_path(data_dir)
    try:
        state = load_pending_update(state_file)
    except UpdateError as exc:
        return StartupUpdateStatus("failed", str(exc))
    if state is None:
        return StartupUpdateStatus("none", "")
    if state.phase == "ready":
        return StartupUpdateStatus(
            "pending", f"v{state.target_version} 업데이트가 준비되어 있습니다.", state
        )

    expected_version = f"{state.target_version}.0"
    actual_version = _get_embedded_file_version(
        Path(executable) if executable is not None else Path(sys.executable)
    )
    if actual_version == expected_version:
        cleaned = _clear_update_state(state, state_path=state_file, data_dir=data_dir)
        return StartupUpdateStatus(
            "applied",
            (
                f"v{state.target_version} 업데이트 설치를 FileVersion으로 확인했습니다."
                if cleaned
                else f"v{state.target_version} 업데이트 설치를 확인했고 임시 installer 정리를 재시도합니다."
            ),
            None if cleaned else state,
        )
    actual_text = actual_version or "확인할 수 없음"
    return StartupUpdateStatus(
        "failed",
        (
            f"v{state.target_version} 업데이트 후 FileVersion이 일치하지 않습니다 "
            f"(현재: {actual_text}). installer 로그를 확인하고 재시도하세요."
        ),
        state,
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
        requested_url = _require_github_https_url(url, label="GitHub")
        response: requests.Response | None = None
        try:
            response = self._session.get(
                requested_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": USER_AGENT,
                },
                timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC),
                stream=stream,
            )
            _require_github_https_url(
                getattr(response, "url", None) or requested_url,
                label="GitHub redirect",
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if response is not None:
                _close_response(response)
            raise UpdateError("GitHub 릴리스 정보를 가져오지 못했습니다.") from exc
        except Exception:
            if response is not None:
                _close_response(response)
            raise

    @staticmethod
    def _release_asset(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise UpdateError("GitHub 릴리스 asset 목록 형식이 올바르지 않습니다.")
        matches = [item for item in assets if isinstance(item, Mapping) and item.get("name") == name]
        if len(matches) != 1:
            raise UpdateError(f"릴리스의 정확한 asset을 찾을 수 없습니다: {name}")
        return matches[0]

    def check_for_update(self, *, current_version: str | None = None) -> ReleaseInfo | None:
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
            installer_url = _require_github_https_url(
                installer.get("browser_download_url"), label="installer"
            )
            checksum_url = _require_github_https_url(
                checksums.get("browser_download_url"), label="SHA256SUMS"
            )
            try:
                installer_size = int(installer.get("size"))
            except (TypeError, ValueError) as exc:
                raise UpdateError("installer asset 크기 정보가 올바르지 않습니다.") from exc
            if installer_size <= 0 or installer_size > MAX_INSTALLER_BYTES:
                raise UpdateError("installer asset 크기가 허용 범위를 벗어났습니다.")
            return ReleaseInfo(
                version=target,
                tag=target.tag,
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
            if match is not None and match.group("filename") == asset_name:
                matches.append(_normalise_sha256(match.group("digest")))
        if len(matches) != 1:
            raise UpdateError("SHA256SUMS에서 정확한 installer checksum을 찾을 수 없습니다.")
        return matches[0]

    def _download_installer(
        self,
        release: ReleaseInfo,
        *,
        destination: Path,
        expected_sha256: str,
    ) -> int:
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        response = self._request(release.installer_url, stream=True)
        digest = hashlib.sha256()
        total = 0
        try:
            response_length = _bounded_content_length(response, maximum=MAX_INSTALLER_BYTES)
            if response_length is not None and response_length != release.installer_size:
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
            if total != release.installer_size:
                raise UpdateError("installer 다운로드 크기가 GitHub asset 메타데이터와 다릅니다.")
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise UpdateError("installer SHA-256이 SHA256SUMS와 다릅니다.")
            if release.github_digest is not None and actual != release.github_digest:
                raise UpdateError("installer SHA-256이 GitHub asset digest와 다릅니다.")
            os.replace(temporary, destination)
            return total
        finally:
            _close_response(response)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def stage_update(self, release: ReleaseInfo) -> StagedUpdate:
        if not self._single_flight.acquire(blocking=False):
            raise UpdateBusyError("업데이트 확인 또는 다운로드가 이미 진행 중입니다.")
        try:
            previous = load_pending_update(self._state_path)
            if previous is not None and previous.phase == "launched":
                raise UpdateBusyError("이미 적용을 시작한 업데이트가 있습니다.")
            checksum_bytes = self._download_limited_bytes(
                release.checksum_url,
                maximum=MAX_CHECKSUM_BYTES,
            )
            try:
                checksum_text = checksum_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UpdateError("SHA256SUMS가 UTF-8 텍스트가 아닙니다.") from exc
            expected_sha256 = self._checksum_for_asset(checksum_text, release.installer_name)
            stage_dir = update_staging_root(self._data_dir) / f"v{release.version.text}"
            destination = stage_dir / release.installer_name
            downloaded_bytes = self._download_installer(
                release,
                destination=destination,
                expected_sha256=expected_sha256,
            )
            state = UpdateState(
                phase="ready",
                target_version=release.version.text,
                tag=release.tag,
                installer_path=str(destination.resolve()),
                sha256=expected_sha256,
                installer_log_path=str((stage_dir / INSTALLER_LOG_FILENAME).resolve()),
                created_at_unix=time.time(),
            )
            try:
                _write_update_state(state, state_path=self._state_path)
            except OSError as exc:
                if previous is None or Path(previous.installer_path).parent != stage_dir:
                    try:
                        shutil.rmtree(stage_dir)
                    except OSError:
                        pass
                raise UpdateError("스테이징된 업데이트 상태를 기록하지 못했습니다.") from exc
            _remove_superseded_ready_stage(
                previous,
                state,
                state_path=self._state_path,
                data_dir=self._data_dir,
            )
            return StagedUpdate(state=state, downloaded_bytes=downloaded_bytes)
        finally:
            self._single_flight.release()


def _windows_detached_flags() -> int:
    if os.name != "nt":
        return 0
    return int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )


def _build_update_apply_request(
    state: UpdateState,
    *,
    wait_for_pid: int,
) -> UpdateApplyRequest:
    install_location = _read_inno_install_location()
    if install_location is None:
        raise UpdateError("HKCU Inno 설치 경로를 확인하지 못했습니다.")
    if wait_for_pid <= 0:
        raise UpdateError("대기할 앱 PID가 올바르지 않습니다.")
    command = (
        state.installer_path,
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        f"/DIR={install_location}",
        "/LOLMANAGER_RELAUNCH=1",
        f"/LOG={state.installer_log_path}",
        f"/LOLMANAGERWAITPID={wait_for_pid}",
    )
    return UpdateApplyRequest(
        state=state,
        install_location=install_location,
        wait_for_pid=wait_for_pid,
        command=command,
    )


def launch_staged_installer_update(
    *,
    state_path: Path | None = None,
    data_dir: Path | None = None,
    wait_for_pid: int | None = None,
    popen: object = subprocess.Popen,
) -> UpdateApplyRequest:
    """Start the verified Inno installer directly after normal GUI shutdown.

    ``launched`` is committed only after process creation succeeds. A failed
    process spawn deliberately leaves durable state at ``ready`` with its
    installer log path unchanged, so the next startup can offer a retry.
    """
    if not is_installer_managed_build():
        raise UpdateError("등록된 installer 관리 LOLManager에서만 업데이트를 적용할 수 있습니다.")
    state_file = Path(state_path) if state_path is not None else update_state_path(data_dir)
    active = validate_staged_update(state_path=state_file, data_dir=data_dir)
    ready = active if active.phase == "ready" else replace(active, phase="ready")
    if ready != active:
        try:
            _write_update_state(ready, state_path=state_file)
        except OSError as exc:
            raise UpdateError("업데이트 상태를 ready로 기록하지 못했습니다.") from exc
    pid = installer_bootstrap_wait_pid() if wait_for_pid is None else int(wait_for_pid)
    request = _build_update_apply_request(ready, wait_for_pid=pid)
    installer_env = os.environ.copy()
    # Inno Setup launches the updated onefile executable as its child. Without
    # an explicit reset, that executable inherits the old PyInstaller process
    # markers through Inno and mistakes itself for a worker of Inno Setup.
    installer_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        popen(
            list(request.command),
            cwd=str(Path(ready.installer_path).parent),
            close_fds=True,
            creationflags=_windows_detached_flags(),
            env=installer_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("silent installer 업데이트를 시작하지 못했습니다.") from exc
    launched = replace(ready, phase="launched")
    _write_launched_update_state(launched, state_path=state_file)
    return replace(request, state=launched)


def mark_installer_launch_failure(
    *,
    state_path: Path | None = None,
    data_dir: Path | None = None,
    message: str,
) -> None:
    """Keep a failed launch retryable without adding a second result schema."""
    del message
    state_file = Path(state_path) if state_path is not None else update_state_path(data_dir)
    try:
        state = load_pending_update(state_file)
        if state is not None and state.phase != "ready":
            _write_update_state(replace(state, phase="ready"), state_path=state_file)
    except (OSError, UpdateError):
        return
