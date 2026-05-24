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
DEFAULT_OPGG_EXE = r"C:\Users\enmso\AppData\Local\Programs\OP.GG\OP.GG.exe"

ENV_LEAGUE_CLIENT_EXE = "LOLMANAGER_LEAGUE_CLIENT_EXE"
ENV_RIOT_CLIENT_SERVICES_EXE = "LOLMANAGER_RIOT_CLIENT_SERVICES_EXE"
ENV_OPGG_EXE = "LOLMANAGER_OPGG_EXE"

LEAGUE_RIOT_LAUNCH_ARGS: tuple[str, ...] = (
    "--launch-product=league_of_legends",
    "--launch-patchline=live",
)


def _norm_exe_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


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
    return str(os.environ.get(ENV_OPGG_EXE) or DEFAULT_OPGG_EXE)


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
    if not exe.exists():
        if logger:
            logger.warning("실행 파일이 없습니다(스킵): %s", exe)
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

    status = running_status_for_exe_paths([league_exe, opgg_exe])
    league_running = bool(status.get(league_exe, False))
    opgg_running = bool(status.get(opgg_exe, False))

    if logger:
        logger.info(
            "외부 앱 실행 상태: LeagueClient=%s, OP.GG=%s",
            "RUNNING" if league_running else "NOT_RUNNING",
            "RUNNING" if opgg_running else "NOT_RUNNING",
        )

    if not league_running:
        riot_exe = riot_client_services_exe_path(league_exe=league_exe)
        ok = start_cmd_once(
            [riot_exe, *LEAGUE_RIOT_LAUNCH_ARGS],
            logger=logger,
        )
        if not ok and logger:
            logger.warning(
                "LoL 자동 실행 실패. Riot Client 설치 경로가 다르면 환경 변수 %s에 "
                "RiotClientServices.exe 경로를 지정하세요.",
                ENV_RIOT_CLIENT_SERVICES_EXE,
            )
    if not opgg_running:
        start_exe_once(opgg_exe, logger=logger)


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
