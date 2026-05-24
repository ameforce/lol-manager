from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Any, Optional
import warnings

import requests
from urllib3.exceptions import InsecureRequestWarning


DEFAULT_LOCKFILE = Path(r"C:\Riot Games\League of Legends\lockfile")
PHASE_MATCHMAKING = "Matchmaking"
PHASE_READY_CHECK = "ReadyCheck"
PHASE_CHAMP_SELECT = "ChampSelect"
PHASE_IN_PROGRESS = "InProgress"
PHASE_WAITING_FOR_STATS = "WaitingForStats"
PHASE_PRE_END_OF_GAME = "PreEndOfGame"
PHASE_END_OF_GAME = "EndOfGame"
PHASE_LOBBY = "Lobby"


@dataclass(frozen=True)
class LcuConnection:
    pid: int
    port: int
    password: str
    protocol: str

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"

    @property
    def authorization_header(self) -> str:
        token = base64.b64encode(f"riot:{self.password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"


@dataclass(frozen=True)
class LcuResult:
    status_code: Optional[int]
    data: Any = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 300


class LcuClient:
    def __init__(
        self,
        *,
        lockfile: Optional[Path] = None,
        session: Optional[requests.Session] = None,
        timeout_sec: float = 0.7,
    ) -> None:
        env_lockfile = os.environ.get("LOLMANAGER_LCU_LOCKFILE")
        self.lockfile = Path(env_lockfile) if env_lockfile else (lockfile or DEFAULT_LOCKFILE)
        self.session = session or requests.Session()
        self.timeout_sec = float(timeout_sec)
        self._phase_cache: tuple[float, Optional[str]] = (0.0, None)
        self._last_logged_phase: Optional[str] = None

    def read_connection(self) -> Optional[LcuConnection]:
        try:
            raw = self.lockfile.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None

        parts = raw.split(":")
        if len(parts) != 5:
            return None

        _name, pid_raw, port_raw, password, protocol = parts
        try:
            pid = int(pid_raw)
            port = int(port_raw)
        except ValueError:
            return None
        if not password or protocol not in {"http", "https"}:
            return None
        return LcuConnection(pid=pid, port=port, password=password, protocol=protocol)

    def request(self, method: str, endpoint: str, *, timeout_sec: Optional[float] = None) -> LcuResult:
        conn = self.read_connection()
        if conn is None:
            return LcuResult(status_code=None, error="lockfile unavailable")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                response = self.session.request(
                    method.upper(),
                    conn.base_url + endpoint,
                    headers={"Authorization": conn.authorization_header},
                    verify=False,
                    timeout=self.timeout_sec if timeout_sec is None else float(timeout_sec),
                )
        except Exception as exc:  # noqa: BLE001 - LCU is an optional local integration.
            return LcuResult(status_code=None, error=f"{type(exc).__name__}: {exc}")

        data: Any = None
        if response.content:
            try:
                data = response.json()
            except ValueError:
                data = response.text
        return LcuResult(status_code=int(response.status_code), data=data)

    def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> Optional[str]:
        now = time.monotonic()
        cached_at, cached = self._phase_cache
        if cached_at and (now - cached_at) <= max(0.0, float(max_age_sec)):
            return cached

        result = self.request("GET", "/lol-gameflow/v1/gameflow-phase")
        phase = result.data if result.ok and isinstance(result.data, str) else None
        self._phase_cache = (now, phase)
        return phase

    def consume_phase_transition(self, phase: Optional[str]) -> Optional[tuple[Optional[str], str]]:
        if not phase:
            return None
        previous = self._last_logged_phase
        if previous == phase:
            return None
        self._last_logged_phase = phase
        return (previous, phase)

    def accept_ready_check(self) -> bool:
        result = self.request("POST", "/lol-matchmaking/v1/ready-check/accept")
        return result.ok or result.status_code == 204

    def is_end_of_game_stats_available(self) -> bool:
        return self.request("GET", "/lol-end-of-game/v1/eog-stats-block").ok
