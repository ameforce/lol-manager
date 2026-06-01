from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import psutil


DEFAULT_LEAGUE_CLIENT_EXE = r"C:\Riot Games\League of Legends\LeagueClient.exe"
DEFAULT_RIOT_CLIENT_SERVICES_EXE = r"C:\Riot Games\Riot Client\RiotClientServices.exe"
DEFAULT_OPGG_EXE = ""

ENV_LEAGUE_CLIENT_EXE = "LOLMANAGER_LEAGUE_CLIENT_EXE"
ENV_RIOT_CLIENT_SERVICES_EXE = "LOLMANAGER_RIOT_CLIENT_SERVICES_EXE"
ENV_OPGG_EXE = "LOLMANAGER_OPGG_EXE"
ENV_ALLOW_UNTRUSTED_APP_PATHS = "LOLMANAGER_ALLOW_UNTRUSTED_APP_PATHS"

LEAGUE_CLIENT_EXE_NAME = "LeagueClient.exe"
RIOT_CLIENT_SERVICES_EXE_NAME = "RiotClientServices.exe"
OPGG_EXE_NAME = "OP.GG.exe"

LEAGUE_RIOT_LAUNCH_ARGS: tuple[str, ...] = (
    "--launch-product=league_of_legends",
    "--launch-patchline=live",
)


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


def start_cmd_once(
    cmd: list[str],
    *,
    cwd: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    if not cmd:
        if logger:
            logger.warning("프로세스 실행 스킵: 빈 커맨드")
        return False

    exe = Path(str(cmd[0]))
    if not exe.is_file():
        if logger:
            logger.warning("실행 파일이 아닙니다(스킵): %s", exe)
        return False
    if exe.suffix.casefold() != ".exe":
        if logger:
            logger.warning("Windows .exe 실행 파일이 아닙니다(스킵): %s", exe)
        return False

    try:
        subprocess.Popen(
            cmd,
            cwd=str(cwd or exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        if logger:
            logger.warning("프로세스 실행 실패: %s (%s)", _format_cmd(cmd), exc)
        return False

    if logger:
        logger.info("프로세스 실행 요청: %s", _format_cmd(cmd))
    return True


def start_exe_once(exe_path: str, *, logger: Optional[logging.Logger] = None) -> bool:
    p = Path(str(exe_path))
    return start_cmd_once([str(p)], logger=logger)


def ensure_external_apps_running_once(
    *,
    league_exe: Optional[str] = None,
    opgg_exe: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    league_exe = str(league_exe or league_client_exe_path())
    opgg_exe = str(opgg_exe or opgg_exe_path())
    valid_opgg_exe = _validate_app_exe_path(
        opgg_exe,
        expected_name=OPGG_EXE_NAME,
        trusted_roots=_known_opgg_install_roots(),
        require_trusted_root=True,
        env_name=ENV_OPGG_EXE,
        logger=logger,
    )

    status = running_status_for_exe_paths([league_exe, valid_opgg_exe])
    league_running = bool(status.get(league_exe, False))
    opgg_running = bool(valid_opgg_exe and status.get(valid_opgg_exe, False))

    if logger:
        logger.info(
            "외부 앱 실행 상태: LeagueClient=%s, OP.GG=%s",
            "RUNNING" if league_running else "NOT_RUNNING",
            "RUNNING" if opgg_running else "NOT_RUNNING",
        )

    if not league_running:
        riot_exe = riot_client_services_exe_path(league_exe=league_exe)
        valid_riot_exe = _validate_app_exe_path(
            riot_exe,
            expected_name=RIOT_CLIENT_SERVICES_EXE_NAME,
            env_name=ENV_RIOT_CLIENT_SERVICES_EXE,
            logger=logger,
        )
        ok = False
        if valid_riot_exe:
            ok = start_cmd_once(
                [valid_riot_exe, *LEAGUE_RIOT_LAUNCH_ARGS],
                logger=logger,
            )
        if not ok and logger:
            logger.warning(
                "LoL 자동 실행 실패. Riot Client 설치 경로가 다르면 환경 변수 %s에 "
                "RiotClientServices.exe 경로를 지정하세요.",
                ENV_RIOT_CLIENT_SERVICES_EXE,
            )
    if valid_opgg_exe and not opgg_running:
        ok = start_exe_once(valid_opgg_exe, logger=logger)
        if not ok and logger:
            logger.warning(
                "OP.GG 자동 실행 실패. 설치 경로가 다르면 환경 변수 %s에 "
                "OP.GG.exe 경로를 지정하세요.",
                ENV_OPGG_EXE,
            )


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
