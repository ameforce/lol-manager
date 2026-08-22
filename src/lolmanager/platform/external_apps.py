from __future__ import annotations

import ctypes
import logging
import os
import secrets
import socket
import subprocess
import time
import warnings
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import psutil
import requests
from urllib3.exceptions import InsecureRequestWarning


DEFAULT_LEAGUE_CLIENT_EXE = r"C:\Riot Games\League of Legends\LeagueClient.exe"
DEFAULT_RIOT_CLIENT_SERVICES_EXE = r"C:\Riot Games\Riot Client\RiotClientServices.exe"
DEFAULT_OPGG_EXE = ""

ENV_LEAGUE_CLIENT_EXE = "LOLMANAGER_LEAGUE_CLIENT_EXE"
ENV_RIOT_CLIENT_SERVICES_EXE = "LOLMANAGER_RIOT_CLIENT_SERVICES_EXE"
ENV_OPGG_EXE = "LOLMANAGER_OPGG_EXE"
ENV_ALLOW_UNTRUSTED_APP_PATHS = "LOLMANAGER_ALLOW_UNTRUSTED_APP_PATHS"
ENV_KEEP_RIOT_CLIENT_WINDOW = "LOLMANAGER_KEEP_RIOT_CLIENT_WINDOW"

LEAGUE_CLIENT_EXE_NAME = "LeagueClient.exe"
RIOT_CLIENT_SERVICES_EXE_NAME = "RiotClientServices.exe"
OPGG_EXE_NAME = "OP.GG.exe"
OPGG_SHUTDOWN_TIMEOUT_SEC = 2.0

LEAGUE_RIOT_LAUNCH_ARGS: tuple[str, ...] = (
    "--launch-product=league_of_legends",
    "--launch-patchline=live",
)

RIOT_PRODUCT_ID = "league_of_legends"
RIOT_PATCHLINE_ID = "live"
RIOT_PRODUCT_LAUNCH_PATH = (
    f"/product-launcher/v1/products/{RIOT_PRODUCT_ID}/patchlines/{RIOT_PATCHLINE_ID}"
)
RIOT_CLIENT_API_READY_TIMEOUT_SEC = 15.0
RIOT_CLIENT_API_REQUEST_TIMEOUT_SEC = (3.0, 5.0)
RIOT_API_ACCEPTED_STATUS_CODES = frozenset({200, 201, 202, 204})

LEAGUE_LAUNCH_VERIFY_TIMEOUT_SEC = 10.0
LEAGUE_LAUNCH_VERIFY_POLL_SEC = 1.0

RIOT_CLIENT_WINDOW_TITLE = "Riot Client"
RIOT_CLIENT_CLOSE_DELAY_SEC = 3.0
WM_CLOSE = 0x0010


def _norm_exe_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _env_dir(name: str) -> Optional[Path]:
    value = str(os.environ.get(name) or "").strip()
    return Path(value) if value else None


def _existing_exe(paths: Iterable[Path]) -> str:
    for path in paths:
        try:
            if path.is_file() and path.suffix.casefold() == ".exe":
                return str(path)
        except OSError:
            continue
    return ""


def _known_opgg_install_paths() -> list[Path]:
    candidates: list[Path] = []
    local_app_data = _env_dir("LOCALAPPDATA")
    if local_app_data is not None:
        candidates.append(local_app_data / "Programs" / "OP.GG" / OPGG_EXE_NAME)

    program_files = _env_dir("PROGRAMFILES")
    if program_files is not None:
        candidates.append(program_files / "OP.GG" / OPGG_EXE_NAME)

    program_files_x86 = _env_dir("PROGRAMFILES(X86)")
    if program_files_x86 is not None:
        candidates.append(program_files_x86 / "OP.GG" / OPGG_EXE_NAME)

    return candidates


def _known_opgg_install_roots() -> list[Path]:
    return [path.parent for path in _known_opgg_install_paths()]


def _norm_path_text(path: Path) -> str:
    return _norm_exe_path(str(path.resolve(strict=False)))


def _is_relative_to_path(path: Path, root: Path) -> bool:
    candidate = _norm_path_text(path)
    base = _norm_path_text(root)
    return candidate == base or candidate.startswith(base + os.sep)


def _validate_app_exe_path(
    raw_path: str,
    *,
    expected_name: str,
    trusted_roots: Iterable[Path] = (),
    require_trusted_root: bool = False,
    env_name: str = "",
    logger: Optional[logging.Logger] = None,
) -> str:
    value = str(raw_path or "").strip()
    if not value:
        if logger and env_name:
            logger.warning(
                "실행 파일을 찾지 못했습니다. 필요한 경우 환경 변수 %s에 경로를 지정하세요.",
                env_name,
            )
        return ""

    path = Path(value)
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    if not is_file:
        if logger:
            logger.warning("실행 파일이 아닙니다(스킵): %s", path)
        return ""

    if path.suffix.casefold() != ".exe":
        if logger:
            logger.warning("Windows .exe 실행 파일이 아닙니다(스킵): %s", path)
        return ""

    if path.name.casefold() != expected_name.casefold():
        if logger:
            logger.warning(
                "예상 실행 파일명이 아닙니다(스킵): %s (expected: %s)",
                path,
                expected_name,
            )
        return ""

    roots = list(trusted_roots)
    outside_trusted_roots = not roots or not any(
        _is_relative_to_path(path, root) for root in roots
    )
    if require_trusted_root and outside_trusted_roots:
        if not _truthy_env(ENV_ALLOW_UNTRUSTED_APP_PATHS):
            if logger:
                logger.warning(
                    "환경 변수 %s의 실행 파일이 신뢰된 설치 위치 밖이어서 스킵합니다: %s. "
                    "%s=1 로 명시적으로 허용할 수 있습니다.",
                    env_name or "(direct path)",
                    path,
                    ENV_ALLOW_UNTRUSTED_APP_PATHS,
                )
            return ""
        if logger:
            logger.warning(
                "명시적 허용으로 신뢰된 설치 위치 밖의 실행 파일을 사용합니다: %s",
                path,
            )

    return str(path)


def league_client_exe_path() -> str:
    return str(os.environ.get(ENV_LEAGUE_CLIENT_EXE) or DEFAULT_LEAGUE_CLIENT_EXE)


def riot_client_services_exe_path(*, league_exe: str = "") -> str:
    env = os.environ.get(ENV_RIOT_CLIENT_SERVICES_EXE)
    if env:
        return str(env)

    league_exe = str(league_exe or "").strip()
    if league_exe:
        try:
            p = Path(league_exe)
            if p.exists():
                root = p.parent.parent
                derived = root / "Riot Client" / "RiotClientServices.exe"
                if derived.exists():
                    return str(derived)
        except Exception:
            pass

    candidates = (
        DEFAULT_RIOT_CLIENT_SERVICES_EXE,
        r"C:\Program Files\Riot Games\Riot Client\RiotClientServices.exe",
        r"C:\Program Files (x86)\Riot Games\Riot Client\RiotClientServices.exe",
    )
    for c in candidates:
        try:
            if Path(c).exists():
                return c
        except Exception:
            continue
    return DEFAULT_RIOT_CLIENT_SERVICES_EXE


def opgg_exe_path() -> str:
    env = str(os.environ.get(ENV_OPGG_EXE) or "").strip()
    if env:
        return env
    discovered = _existing_exe(_known_opgg_install_paths())
    return discovered or DEFAULT_OPGG_EXE


def running_status_for_exe_paths(paths: Iterable[str]) -> dict[str, bool]:
    targets: list[tuple[str, str, str]] = []
    for p in paths:
        s = str(p or "").strip()
        if not s:
            continue
        targets.append((_norm_exe_path(s), Path(s).name.casefold(), s))
    if not targets:
        return {}

    by_key: dict[tuple[str, str], list[str]] = {}
    wanted_names: set[str] = set()
    for norm_p, name_cf, orig in targets:
        key = (name_cf, norm_p)
        by_key.setdefault(key, []).append(orig)
        wanted_names.add(name_cf)

    remaining: set[tuple[str, str]] = set(by_key.keys())
    running: dict[str, bool] = {orig: False for _norm_p, _name_cf, orig in targets}

    for proc in psutil.process_iter(["pid", "name"]):
        if not remaining:
            break
        try:
            name = proc.info.get("name") or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if not name:
            continue
        name_cf = str(name).casefold()
        if name_cf not in wanted_names:
            continue

        try:
            exe = proc.exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if not exe:
            continue

        key = (name_cf, _norm_exe_path(exe))
        if key not in remaining:
            continue

        for orig in by_key.get(key, []):
            running[orig] = True
        remaining.discard(key)

    return running


def _format_cmd(cmd: Iterable[str]) -> str:
    try:
        return subprocess.list2cmdline([str(x) for x in cmd])
    except Exception:
        return " ".join(str(x) for x in cmd)


@dataclass
class OwnedExternalProcess:
    exe_path: str
    process: object


def _terminate_psutil_process_targets(
    targets: list,
    *,
    timeout_sec: float,
    timeout_warning: str,
    logger: Optional[logging.Logger] = None,
) -> list:
    """Terminate targets, wait, then force-kill survivors. Returns survivors."""
    if not targets:
        return []
    alive = list(targets)
    try:
        # Stop descendants before their owning launcher so no child is orphaned
        # while the process tree is being cleaned up.
        for target in targets:
            try:
                target.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        _gone, alive = psutil.wait_procs(
            targets, timeout=max(0.0, float(timeout_sec))
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return list(targets)
    if alive and logger and timeout_warning:
        logger.warning(timeout_warning)
    for target in alive:
        try:
            target.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    if alive:
        try:
            _gone, alive = psutil.wait_procs(
                alive, timeout=max(0.0, float(timeout_sec))
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return list(alive)
    return list(alive)


def _terminate_owned_process_tree(
    process: object,
    *,
    timeout_sec: float,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Stop only descendants of the process this session launched."""
    try:
        pid = int(getattr(process, "pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return False

    try:
        root = psutil.Process(pid)
        targets = [*root.children(recursive=True), root]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    if not targets:
        return False

    survivors = _terminate_psutil_process_targets(
        targets,
        timeout_sec=timeout_sec,
        timeout_warning="OP.GG 소유 프로세스 트리 종료 대기 시간 초과. 강제 종료합니다.",
        logger=logger,
    )
    return not survivors


@dataclass
class ExternalAppsSession:
    owned_opgg: Optional[OwnedExternalProcess] = None

    def close_owned_opgg(
        self,
        *,
        timeout_sec: float = OPGG_SHUTDOWN_TIMEOUT_SEC,
        logger: Optional[logging.Logger] = None,
    ) -> bool:
        owned = self.owned_opgg
        if owned is None:
            return False

        proc = owned.process
        try:
            poll = getattr(proc, "poll", None)
            if callable(poll) and poll() is not None:
                self.owned_opgg = None
                return False

            if _terminate_owned_process_tree(
                proc,
                timeout_sec=timeout_sec,
                logger=logger,
            ):
                if logger:
                    logger.info("OP.GG 소유 프로세스 트리 종료 완료: %s", owned.exe_path)
                return True

            if logger:
                logger.info("OP.GG 자동 종료 요청: %s", owned.exe_path)
            getattr(proc, "terminate")()
            getattr(proc, "wait")(timeout=float(timeout_sec))
        except subprocess.TimeoutExpired:
            if logger:
                logger.warning(
                    "OP.GG 종료 대기 시간 초과. 강제 종료합니다: %s",
                    owned.exe_path,
                )
            try:
                getattr(proc, "kill")()
                getattr(proc, "wait")(timeout=float(timeout_sec))
            except Exception as exc:
                if logger:
                    logger.warning("OP.GG 강제 종료 실패: %s", exc)
        except Exception as exc:
            if logger:
                logger.warning("OP.GG 자동 종료 실패: %s", exc)
        finally:
            self.owned_opgg = None
        return True


_CURRENT_EXTERNAL_APPS_SESSION = ExternalAppsSession()


def current_external_apps_session() -> ExternalAppsSession:
    return _CURRENT_EXTERNAL_APPS_SESSION


def set_current_external_apps_session(
    session: ExternalAppsSession,
) -> ExternalAppsSession:
    global _CURRENT_EXTERNAL_APPS_SESSION
    _CURRENT_EXTERNAL_APPS_SESSION = session
    return session


def close_owned_opgg_for_current_session(
    *,
    timeout_sec: float = OPGG_SHUTDOWN_TIMEOUT_SEC,
    logger: Optional[logging.Logger] = None,
) -> bool:
    return _CURRENT_EXTERNAL_APPS_SESSION.close_owned_opgg(
        timeout_sec=timeout_sec,
        logger=logger,
    )


def _running_opgg_processes(opgg_exe: str) -> list:
    """Return live OP.GG processes (roots first) whose exe path matches."""
    norm_target = _norm_exe_path(opgg_exe)
    name_cf = OPGG_EXE_NAME.casefold()
    matches: list = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info.get("name") or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if not name or str(name).casefold() != name_cf:
            continue
        try:
            exe = proc.exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if exe and _norm_exe_path(exe) == norm_target:
            matches.append(proc)

    match_pids = {getattr(proc, "pid", None) for proc in matches}
    roots: list = []
    for proc in matches:
        try:
            parent = proc.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            parent = None
        if parent is None or getattr(parent, "pid", None) not in match_pids:
            roots.append(proc)

    targets: list = []
    seen: set = set()
    for root in roots:
        candidates = [root]
        try:
            candidates.extend(root.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass
        for target in candidates:
            pid = getattr(target, "pid", None)
            if pid in seen:
                continue
            seen.add(pid)
            targets.append(target)
    return targets


def close_running_opgg(
    *,
    timeout_sec: float = OPGG_SHUTDOWN_TIMEOUT_SEC,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Terminate every running OP.GG from the trusted install location.

    Unlike close_owned_opgg this also adopts OP.GG instances earlier
    lolmanager sessions started but never cleaned up.
    """
    raw_path = opgg_exe_path()
    if not raw_path:
        return 0
    opgg_exe = _validate_app_exe_path(
        raw_path,
        expected_name=OPGG_EXE_NAME,
        trusted_roots=_known_opgg_install_roots(),
        require_trusted_root=True,
        env_name=ENV_OPGG_EXE,
        logger=logger,
    )
    if not opgg_exe:
        return 0

    closed = 0
    for _attempt in range(2):
        targets = _running_opgg_processes(opgg_exe)
        if not targets:
            break
        if logger:
            logger.info(
                "OP.GG 실행 중 종료 요청: %s (%d개 프로세스)",
                opgg_exe,
                len(targets),
            )
        survivors = _terminate_psutil_process_targets(
            targets,
            timeout_sec=timeout_sec,
            timeout_warning="OP.GG 프로세스 종료 대기 시간 초과. 강제 종료합니다.",
            logger=logger,
        )
        closed += len(targets)
        if not survivors:
            break
    remaining = _running_opgg_processes(opgg_exe)
    if remaining and logger:
        logger.warning(
            "OP.GG 종료 후에도 %d개 프로세스가 실행 중입니다.", len(remaining)
        )
    return closed


def start_cmd_process_once(
    cmd: list[str],
    *,
    cwd: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[subprocess.Popen[bytes]]:
    if not cmd:
        if logger:
            logger.warning("프로세스 실행 스킵: 빈 커맨드")
        return None

    exe = Path(str(cmd[0]))
    if not exe.is_file():
        if logger:
            logger.warning("실행 파일이 아닙니다(스킵): %s", exe)
        return None
    if exe.suffix.casefold() != ".exe":
        if logger:
            logger.warning("Windows .exe 실행 파일이 아닙니다(스킵): %s", exe)
        return None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd or exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        if logger:
            logger.warning("프로세스 실행 실패: %s (%s)", _format_cmd(cmd), exc)
        return None

    if logger:
        logger.info("프로세스 실행 요청: %s", _format_cmd(cmd))
    return proc


def start_cmd_once(
    cmd: list[str],
    *,
    cwd: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    return start_cmd_process_once(cmd, cwd=cwd, logger=logger) is not None


def start_exe_process_once(
    exe_path: str,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[subprocess.Popen[bytes]]:
    p = Path(str(exe_path))
    return start_cmd_process_once([str(p)], logger=logger)


def start_exe_once(exe_path: str, *, logger: Optional[logging.Logger] = None) -> bool:
    return start_exe_process_once(exe_path, logger=logger) is not None


def _league_client_process_seen() -> bool:
    target = LEAGUE_CLIENT_EXE_NAME.casefold()
    for proc in psutil.process_iter(["name"]):
        try:
            name = str(proc.info.get("name") or "")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
        if name.casefold() == target:
            return True
    return False


def _find_running_riot_client_api() -> Optional[tuple[str, tuple[str, str]]]:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if str(proc.info.get("name") or "").casefold() != RIOT_CLIENT_SERVICES_EXE_NAME.casefold():
                continue
            cmdline = proc.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        token = port = ""
        for arg in cmdline:
            text = str(arg)
            if text.startswith("--remoting-auth-token="):
                token = text.split("=", 1)[1]
            elif text.startswith("--app-port="):
                port = text.split("=", 1)[1]
        if token and port:
            return f"https://127.0.0.1:{port}", ("riot", token)
    return None


def _free_localhost_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_riot_client_services_with_api(*, logger: Optional[logging.Logger] = None) -> bool:
    riot_exe = riot_client_services_exe_path()
    valid_riot_exe = _validate_app_exe_path(
        riot_exe,
        expected_name=RIOT_CLIENT_SERVICES_EXE_NAME,
        env_name=ENV_RIOT_CLIENT_SERVICES_EXE,
        logger=logger,
    )
    if not valid_riot_exe:
        return False

    cmd = [
        valid_riot_exe,
        f"--remoting-auth-token={secrets.token_hex(16)}",
        f"--app-port={_free_localhost_port()}",
    ]
    return start_cmd_process_once(
        cmd,
        cwd=str(Path(valid_riot_exe).parent),
        logger=logger,
    ) is not None


def launch_league_via_riot_client_api(
    *,
    logger: Optional[logging.Logger] = None,
    ready_timeout_sec: float = RIOT_CLIENT_API_READY_TIMEOUT_SEC,
) -> bool:
    endpoint = _find_running_riot_client_api()
    if endpoint is None:
        if not _start_riot_client_services_with_api(logger=logger):
            return False
        deadline = time.monotonic() + max(0.0, float(ready_timeout_sec))
        while endpoint is None:
            endpoint = _find_running_riot_client_api()
            if endpoint or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if endpoint is None:
            if logger:
                logger.warning(
                    "Riot Client API 준비 대기 시간 초과(%.1fs). 기존 실행 경로로 대체합니다.",
                    float(ready_timeout_sec),
                )
            return False

    base_url, auth = endpoint
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            response = requests.post(
                base_url + RIOT_PRODUCT_LAUNCH_PATH,
                auth=auth,
                verify=False,
                timeout=RIOT_CLIENT_API_REQUEST_TIMEOUT_SEC,
            )
    except requests.ReadTimeout:
        if logger:
            logger.info(
                "Riot Client 실행 요청이 처리 중입니다(응답 지연). 실행 결과를 별도 확인합니다."
            )
        return True
    except requests.RequestException as exc:
        if logger:
            logger.warning("Riot Client 실행 API 호출 실패: %s", type(exc).__name__)
        return False

    accepted = response.status_code in RIOT_API_ACCEPTED_STATUS_CODES
    if logger:
        level = logging.INFO if accepted else logging.WARNING
        logger.log(
            level,
            "Riot Client 실행 API 응답: %s",
            response.status_code,
        )
    return accepted


def verify_league_client_started(
    *,
    timeout_sec: float = LEAGUE_LAUNCH_VERIFY_TIMEOUT_SEC,
    poll_sec: float = LEAGUE_LAUNCH_VERIFY_POLL_SEC,
    logger: Optional[logging.Logger] = None,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        if _league_client_process_seen():
            if logger:
                logger.info("LeagueClient 실행 확인 완료.")
            return True

        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(float(poll_sec), max(0.0, deadline - now)))

    if logger:
        logger.warning(
            "LeagueClient 실행 확인 대기 시간 초과(%.1fs). "
            "Riot Client 로그인 상태 또는 클라이언트 업데이트 진행 여부를 확인하세요.",
            float(timeout_sec),
        )
    return False


def start_league_client_once(
    *,
    league_exe: str,
    logger: Optional[logging.Logger] = None,
) -> bool:
    if launch_league_via_riot_client_api(logger=logger):
        return True

    riot_exe = riot_client_services_exe_path(league_exe=league_exe)
    valid_riot_exe = _validate_app_exe_path(
        riot_exe,
        expected_name=RIOT_CLIENT_SERVICES_EXE_NAME,
        env_name=ENV_RIOT_CLIENT_SERVICES_EXE,
        logger=logger,
    )
    if valid_riot_exe:
        if start_cmd_once(
            [valid_riot_exe, *LEAGUE_RIOT_LAUNCH_ARGS], logger=logger
        ):
            return True
    elif logger and str(riot_exe or "").strip():
        logger.warning("Riot Client 실행 파일을 찾지 못했습니다: %s", riot_exe)

    valid_league_exe = _validate_app_exe_path(
        league_exe,
        expected_name=LEAGUE_CLIENT_EXE_NAME,
        env_name=ENV_LEAGUE_CLIENT_EXE,
        logger=logger,
    )
    if not valid_league_exe:
        return False

    if logger:
        logger.warning(
            "Riot Client 경로로 실행하지 못해 LeagueClient를 직접 실행합니다(fallback)."
        )
    return start_exe_once(valid_league_exe, logger=logger)


def _riot_client_window_handles() -> list[int]:
    user32 = ctypes.windll.user32
    handles: list[int] = []

    def _on_window(handle: int, _lparam: int) -> int:
        try:
            if not user32.IsWindowVisible(int(handle)):
                return 1
            length = user32.GetWindowTextLengthW(int(handle))
            if length <= 0:
                return 1
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(int(handle), buffer, length + 1)
            if buffer.value == RIOT_CLIENT_WINDOW_TITLE:
                handles.append(int(handle))
        except Exception:
            return 1
        return 1

    prototype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    try:
        user32.EnumWindows(prototype(_on_window), 0)
    except Exception:
        return []
    return handles


def _post_close_message(handle: int) -> None:
    ctypes.windll.user32.PostMessageW(int(handle), WM_CLOSE, 0, 0)


def close_riot_client_windows_after_launch(
    *,
    logger: Optional[logging.Logger] = None,
    settle_sec: float = RIOT_CLIENT_CLOSE_DELAY_SEC,
) -> int:
    if _truthy_env(ENV_KEEP_RIOT_CLIENT_WINDOW):
        if logger:
            logger.info(
                "환경 변수 %s 설정으로 Riot Client 창을 열어 둡니다.",
                ENV_KEEP_RIOT_CLIENT_WINDOW,
            )
        return 0

    time.sleep(max(0.0, float(settle_sec)))
    handles = _riot_client_window_handles()
    for handle in handles:
        try:
            _post_close_message(handle)
        except Exception as exc:
            if logger:
                logger.warning("Riot Client 창 종료 메시지 전송 실패: %s", exc)
    if logger:
        if handles:
            logger.info(
                "LeagueClient 기동 확인 후 Riot Client 창 %d개에 종료를 요청했습니다.",
                len(handles),
            )
        else:
            logger.debug("종료할 Riot Client 창이 없습니다.")
    return len(handles)


def ensure_external_apps_running_once(
    *,
    league_exe: Optional[str] = None,
    opgg_exe: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> ExternalAppsSession:
    session = set_current_external_apps_session(ExternalAppsSession())
    league_exe = str(league_exe or league_client_exe_path())
    opgg_exe = str(opgg_exe or opgg_exe_path())
    valid_league_exe = _validate_app_exe_path(
        league_exe,
        expected_name=LEAGUE_CLIENT_EXE_NAME,
        env_name=ENV_LEAGUE_CLIENT_EXE,
        logger=logger,
    )
    valid_opgg_exe = _validate_app_exe_path(
        opgg_exe,
        expected_name=OPGG_EXE_NAME,
        trusted_roots=_known_opgg_install_roots(),
        require_trusted_root=True,
        env_name=ENV_OPGG_EXE,
        logger=logger,
    )

    status = running_status_for_exe_paths([valid_league_exe, valid_opgg_exe])
    league_running = bool(valid_league_exe and status.get(valid_league_exe, False))
    opgg_running = bool(valid_opgg_exe and status.get(valid_opgg_exe, False))

    if logger:
        logger.info(
            "외부 앱 실행 상태: LeagueClient=%s, OP.GG=%s",
            "RUNNING" if league_running else "NOT_RUNNING",
            "RUNNING" if opgg_running else "NOT_RUNNING",
        )

    if not league_running:
        ok = start_league_client_once(league_exe=str(league_exe), logger=logger)
        if ok:
            verified = verify_league_client_started(logger=logger)
            if verified:
                close_riot_client_windows_after_launch(logger=logger)
        elif logger:
            logger.warning(
                "LoL 자동 실행 실패. 설치 경로가 다르면 환경 변수 %s에 "
                "LeagueClient.exe 경로를, %s에 RiotClientServices.exe 경로를 "
                "지정하세요.",
                ENV_LEAGUE_CLIENT_EXE,
                ENV_RIOT_CLIENT_SERVICES_EXE,
            )
    if valid_opgg_exe and not opgg_running:
        opgg_proc = start_exe_process_once(valid_opgg_exe, logger=logger)
        ok = opgg_proc is not None
        if opgg_proc is not None:
            session.owned_opgg = OwnedExternalProcess(
                exe_path=valid_opgg_exe,
                process=opgg_proc,
            )
        if not ok and logger:
            logger.warning(
                "OP.GG 자동 실행 실패. 설치 경로가 다르면 환경 변수 %s에 "
                "OP.GG.exe 경로를 지정하세요.",
                ENV_OPGG_EXE,
            )
    return session


@dataclass
class LeagueClientExitGuard:
    league_exe: str
    seen_running: bool = False
    _use_path_check: bool = False
    _target_name_cf: str = ""
    _target_norm_path: str = ""

    def __post_init__(self) -> None:
        league_exe = str(self.league_exe or "").strip()
        self.league_exe = league_exe
        self._target_name_cf = Path(league_exe).name.casefold() if league_exe else ""
        self._target_norm_path = _norm_exe_path(league_exe) if league_exe else ""
        self._use_path_check = bool(league_exe and Path(league_exe).exists())

    def poll_is_running(self) -> bool:
        league_exe = self.league_exe
        if not league_exe:
            return False

        if self._use_path_check and self._target_name_cf and self._target_norm_path:
            target_name_cf = self._target_name_cf
            target_norm = self._target_norm_path
            running = False
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    name = proc.info.get("name") or ""
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    continue
                if not name or str(name).casefold() != target_name_cf:
                    continue
                try:
                    exe = proc.exe()
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    continue
                if exe and _norm_exe_path(exe) == target_norm:
                    running = True
                    break
        else:
            running = False
            target_name_cf = self._target_name_cf
            if target_name_cf:
                for proc in psutil.process_iter(["name"]):
                    try:
                        name = proc.info.get("name") or ""
                    except (
                        psutil.NoSuchProcess,
                        psutil.AccessDenied,
                        psutil.ZombieProcess,
                    ):
                        continue
                    if name and str(name).casefold() == target_name_cf:
                        running = True
                        break

        if running:
            self.seen_running = True
        return running

    def should_exit(self) -> bool:
        running = self.poll_is_running()
        return bool(self.seen_running and not running)
