from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
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


def _normalize_lookup_key(value: object) -> str:
    return "".join(str(value or "").strip().casefold().split())


def _parse_optional_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _connection_outcome_for_error(error: Optional[str]) -> "LcuOutcome":
    if error and "lockfile unavailable" in error:
        return LcuOutcome.UNAVAILABLE
    return LcuOutcome.REQUEST_FAILED


_POSITION_ALIASES = {
    "top": "top",
    "jungle": "jungle",
    "middle": "mid",
    "mid": "mid",
    "bottom": "adc",
    "bot": "adc",
    "adc": "adc",
    "utility": "support",
    "support": "support",
}


def _canonical_position(value: object) -> Optional[str]:
    return _POSITION_ALIASES.get(_normalize_lookup_key(value))


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


class LcuOutcome(str, Enum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    NO_SESSION = "no_session"
    MALFORMED_SESSION = "malformed_session"
    NO_LOCAL_PLAYER = "no_local_player"
    NO_POSITION = "no_position"
    NO_CURRENT_ACTION = "no_current_action"
    CHAMPION_NOT_FOUND = "champion_not_found"
    REQUEST_FAILED = "request_failed"
    ACTION_REJECTED = "action_rejected"


class LcuLoopAction(str, Enum):
    ACT_LCU = "act_lcu"
    WAIT_AUTHORITATIVE = "wait_authoritative"
    FALLBACK_IMAGE = "fallback_image"
    RETRY_LCU = "retry_lcu"
    ABORT_LOG = "abort_log"


@dataclass(frozen=True)
class LcuDecision:
    status: LcuOutcome
    value: Any = None
    reason: str = ""
    status_code: Optional[int] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == LcuOutcome.SUCCESS


@dataclass(frozen=True)
class ChampSelectAction:
    id: int
    type: str
    is_in_progress: bool
    completed: bool
    champion_id: Optional[int] = None


@dataclass(frozen=True)
class ChampSelectSnapshot:
    local_player_cell_id: int
    assigned_position: Optional[str]
    local_player: dict[str, Any]
    actions: tuple[ChampSelectAction, ...]
    raw: dict[str, Any]


def _coerce_lcu_outcome(outcome: object) -> LcuOutcome:
    if isinstance(outcome, LcuDecision):
        return outcome.status
    if isinstance(outcome, LcuOutcome):
        return outcome
    try:
        return LcuOutcome(str(outcome))
    except ValueError:
        return LcuOutcome.REQUEST_FAILED


def lcu_loop_action_for(outcome: object, *, context: str) -> LcuLoopAction:
    status = _coerce_lcu_outcome(outcome)
    context_key = _normalize_lookup_key(context)

    if status == LcuOutcome.SUCCESS:
        return LcuLoopAction.ACT_LCU
    if status == LcuOutcome.NO_CURRENT_ACTION:
        if context_key in {"write", "pick", "ban", "reserve", "champselect"}:
            return LcuLoopAction.WAIT_AUTHORITATIVE
        return LcuLoopAction.FALLBACK_IMAGE
    return LcuLoopAction.FALLBACK_IMAGE


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

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        timeout_sec: Optional[float] = None,
        json_body: Any = None,
    ) -> LcuResult:
        conn = self.read_connection()
        if conn is None:
            return LcuResult(status_code=None, error="lockfile unavailable")

        kwargs: dict[str, Any] = {
            "headers": {"Authorization": conn.authorization_header},
            "verify": False,
            "timeout": self.timeout_sec if timeout_sec is None else float(timeout_sec),
        }
        if json_body is not None:
            kwargs["json"] = json_body

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                response = self.session.request(
                    method.upper(),
                    conn.base_url + endpoint,
                    **kwargs,
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

    def start_matchmaking(self) -> bool:
        result = self.request("POST", "/lol-lobby/v2/lobby/matchmaking/search")
        return result.ok or result.status_code == 204

    def is_end_of_game_stats_available(self) -> bool:
        return self.request("GET", "/lol-end-of-game/v1/eog-stats-block").ok

    def get_champ_select_session(self) -> Optional[dict[str, Any]]:
        result = self.request("GET", "/lol-champ-select/v1/session")
        return result.data if result.ok and isinstance(result.data, dict) else None

    def _champ_select_session_decision(self) -> LcuDecision:
        result = self.request("GET", "/lol-champ-select/v1/session")
        if result.error:
            return LcuDecision(
                _connection_outcome_for_error(result.error),
                reason=result.error,
                status_code=result.status_code,
                error=result.error,
            )
        if not result.ok:
            status = (
                LcuOutcome.NO_SESSION
                if result.status_code in {400, 404}
                else LcuOutcome.REQUEST_FAILED
            )
            return LcuDecision(
                status,
                reason="champ-select session unavailable",
                status_code=result.status_code,
            )
        if not isinstance(result.data, dict):
            return LcuDecision(
                LcuOutcome.MALFORMED_SESSION,
                reason="champ-select session payload is not an object",
                status_code=result.status_code,
            )
        return LcuDecision(
            LcuOutcome.SUCCESS, value=result.data, status_code=result.status_code
        )

    def _build_champ_select_snapshot(self, session: dict[str, Any]) -> LcuDecision:
        local_cell_id = _parse_optional_int(session.get("localPlayerCellId"))
        if local_cell_id is None:
            return LcuDecision(
                LcuOutcome.NO_LOCAL_PLAYER,
                reason="missing or invalid localPlayerCellId",
            )

        raw_team = session.get("myTeam")
        local_player: dict[str, Any] = {}
        if raw_team is not None:
            if not isinstance(raw_team, list):
                return LcuDecision(
                    LcuOutcome.MALFORMED_SESSION,
                    reason="myTeam payload is not a list",
                )
            for player in raw_team:
                if not isinstance(player, dict):
                    continue
                if _parse_optional_int(player.get("cellId")) == local_cell_id:
                    local_player = player
                    break
            if raw_team and not local_player:
                return LcuDecision(
                    LcuOutcome.NO_LOCAL_PLAYER,
                    reason="local player not found in myTeam",
                )

        assigned_position = None
        for key in (
            "assignedPosition",
            "position",
            "selectedPosition",
            "assignedRole",
            "role",
        ):
            assigned_position = _canonical_position(local_player.get(key))
            if assigned_position:
                break
        if not assigned_position:
            assigned_position = _canonical_position(session.get("assignedPosition"))

        raw_groups = session.get("actions", [])
        if raw_groups is None:
            raw_groups = []
        if not isinstance(raw_groups, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_SESSION,
                reason="actions payload is not a list",
            )

        actions: list[ChampSelectAction] = []
        for group in raw_groups:
            if not isinstance(group, list):
                return LcuDecision(
                    LcuOutcome.MALFORMED_SESSION,
                    reason="action group payload is not a list",
                )
            for action in group:
                if not isinstance(action, dict):
                    return LcuDecision(
                        LcuOutcome.MALFORMED_SESSION,
                        reason="action payload is not an object",
                    )
                if _parse_optional_int(action.get("actorCellId")) != local_cell_id:
                    continue
                action_id = _parse_optional_int(action.get("id"))
                if action_id is None:
                    continue
                action_type = str(action.get("type") or "").casefold()
                if not action_type:
                    continue
                actions.append(
                    ChampSelectAction(
                        id=action_id,
                        type=action_type,
                        is_in_progress=bool(action.get("isInProgress")),
                        completed=bool(action.get("completed")),
                        champion_id=_parse_optional_int(action.get("championId")),
                    )
                )

        return LcuDecision(
            LcuOutcome.SUCCESS,
            value=ChampSelectSnapshot(
                local_player_cell_id=local_cell_id,
                assigned_position=assigned_position,
                local_player=local_player,
                actions=tuple(actions),
                raw=session,
            ),
        )

    def get_champ_select_snapshot(self) -> LcuDecision:
        result = self._champ_select_session_decision()
        if not result.ok:
            return result
        return self._build_champ_select_snapshot(result.value)

    def get_local_player_position(self) -> LcuDecision:
        result = self.get_champ_select_snapshot()
        if not result.ok:
            return result

        snapshot = result.value
        if snapshot.assigned_position:
            return LcuDecision(LcuOutcome.SUCCESS, value=snapshot.assigned_position)
        return LcuDecision(
            LcuOutcome.NO_POSITION,
            reason="local player position unavailable",
        )

    def get_local_action_state(
        self, action_type: str, *, require_in_progress: bool = False
    ) -> LcuDecision:
        result = self.get_champ_select_snapshot()
        if not result.ok:
            return result

        action_key = str(action_type or "").casefold()
        candidates = [
            action
            for action in result.value.actions
            if action.type == action_key
            and not action.completed
            and (action.is_in_progress or not require_in_progress)
        ]
        if not candidates:
            return LcuDecision(
                LcuOutcome.NO_CURRENT_ACTION,
                reason=f"no current local {action_key} action",
            )

        in_progress = [action for action in candidates if action.is_in_progress]
        return LcuDecision(
            LcuOutcome.SUCCESS,
            value=(in_progress or candidates)[0],
        )

    def _get_local_action_by_id(self, action_id: int) -> LcuDecision:
        result = self.get_champ_select_snapshot()
        if not result.ok:
            return result

        for action in result.value.actions:
            if action.id == action_id:
                return LcuDecision(LcuOutcome.SUCCESS, value=action)
        return LcuDecision(
            LcuOutcome.NO_CURRENT_ACTION,
            reason=f"local action not found: {action_id}",
        )

    def _wait_for_action_champion(
        self,
        action_id: int,
        champion_id: int,
        *,
        timeout_sec: float = 0.8,
        interval_sec: float = 0.05,
    ) -> LcuDecision:
        deadline = time.monotonic() + max(0.0, timeout_sec)
        last: LcuDecision = LcuDecision(
            LcuOutcome.ACTION_REJECTED,
            reason="champ-select action champion assignment not confirmed",
        )

        while True:
            action = self._get_local_action_by_id(action_id)
            if action.ok and action.value.champion_id == champion_id:
                return action
            if action.ok:
                last = LcuDecision(
                    LcuOutcome.ACTION_REJECTED,
                    value=action.value,
                    reason="champ-select action champion assignment not confirmed",
                )
            else:
                last = action

            now = time.monotonic()
            if now >= deadline:
                return last
            time.sleep(min(max(0.0, interval_sec), deadline - now))

    def _wait_for_action_completed(
        self,
        action_id: int,
        *,
        timeout_sec: float = 0.8,
        interval_sec: float = 0.05,
    ) -> LcuDecision:
        deadline = time.monotonic() + max(0.0, timeout_sec)
        last: LcuDecision = LcuDecision(
            LcuOutcome.ACTION_REJECTED,
            reason="champ-select action completion not confirmed",
        )

        while True:
            action = self._get_local_action_by_id(action_id)
            if action.ok and action.value.completed:
                return action
            if action.ok:
                last = LcuDecision(
                    LcuOutcome.ACTION_REJECTED,
                    value=action.value,
                    reason="champ-select action completion not confirmed",
                )
            else:
                last = action

            now = time.monotonic()
            if now >= deadline:
                return last
            time.sleep(min(max(0.0, interval_sec), deadline - now))

    def _complete_champ_select_action(
        self,
        action_id: int,
        champion_id: int,
    ) -> LcuDecision:
        completed = self.request(
            "POST",
            f"/lol-champ-select/v1/session/actions/{action_id}/complete",
        )
        if completed.ok or completed.status_code == 204:
            return LcuDecision(
                LcuOutcome.SUCCESS,
                reason="champ-select action complete post accepted",
                status_code=completed.status_code,
            )

        fallback = self._patch_completed_champ_select_action(action_id, champion_id)
        if fallback.ok:
            return fallback

        return LcuDecision(
            LcuOutcome.ACTION_REJECTED,
            reason="champ-select action complete rejected",
            status_code=completed.status_code,
            error=completed.error or fallback.error,
        )

    def _patch_completed_champ_select_action(
        self,
        action_id: int,
        champion_id: int,
    ) -> LcuDecision:
        fallback = self.request(
            "PATCH",
            f"/lol-champ-select/v1/session/actions/{action_id}",
            json_body={"championId": champion_id, "completed": True},
        )
        if fallback.ok or fallback.status_code == 204:
            return LcuDecision(
                LcuOutcome.SUCCESS,
                reason="champ-select action complete fallback patch accepted",
                status_code=fallback.status_code,
            )

        return LcuDecision(
            LcuOutcome.ACTION_REJECTED,
            reason="champ-select action complete fallback patch rejected",
            status_code=fallback.status_code,
            error=fallback.error,
        )

    def get_champ_select_grid_champions(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/lol-champ-select/v1/all-grid-champions")
        if not result.ok or not isinstance(result.data, list):
            return []
        return [x for x in result.data if isinstance(x, dict)]

    def _champ_select_grid_champions_decision(self) -> LcuDecision:
        result = self.request("GET", "/lol-champ-select/v1/all-grid-champions")
        if result.error:
            return LcuDecision(
                _connection_outcome_for_error(result.error),
                reason=result.error,
                status_code=result.status_code,
                error=result.error,
            )
        if not result.ok or not isinstance(result.data, list):
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED,
                reason="champion grid unavailable",
                status_code=result.status_code,
            )
        return LcuDecision(
            LcuOutcome.SUCCESS,
            value=[x for x in result.data if isinstance(x, dict)],
            status_code=result.status_code,
        )

    def _find_local_champ_select_action(
        self,
        session: dict[str, Any],
        action_type: str,
        *,
        require_in_progress: bool = False,
    ) -> Optional[dict[str, Any]]:
        local_cell_id = session.get("localPlayerCellId")
        if local_cell_id is None:
            return None

        candidates: list[dict[str, Any]] = []
        raw_groups = session.get("actions")
        if not isinstance(raw_groups, list):
            return None

        for group in raw_groups:
            if not isinstance(group, list):
                continue
            for action in group:
                if not isinstance(action, dict):
                    continue
                if action.get("actorCellId") != local_cell_id:
                    continue
                if str(action.get("type") or "").casefold() != action_type.casefold():
                    continue
                if bool(action.get("completed")):
                    continue
                if require_in_progress and not bool(action.get("isInProgress")):
                    continue
                if action.get("id") is None:
                    continue
                candidates.append(action)

        if not candidates:
            return None

        in_progress = [x for x in candidates if bool(x.get("isInProgress"))]
        return (in_progress or candidates)[0]

    def _resolve_champion_id(self, champion_name: object) -> Optional[int]:
        result = self._resolve_champion_id_decision(champion_name)
        return result.value if result.ok else None

    def _resolve_champion_id_decision(self, champion_name: object) -> LcuDecision:
        if isinstance(champion_name, int) and champion_name > 0:
            return LcuDecision(LcuOutcome.SUCCESS, value=champion_name)

        raw = str(champion_name or "").strip()
        if raw.isdigit():
            parsed = int(raw)
            if parsed > 0:
                return LcuDecision(LcuOutcome.SUCCESS, value=parsed)
            return LcuDecision(
                LcuOutcome.CHAMPION_NOT_FOUND,
                reason="champion id must be positive",
            )

        target = _normalize_lookup_key(raw)
        if not target:
            return LcuDecision(
                LcuOutcome.CHAMPION_NOT_FOUND,
                reason="empty champion name",
            )

        champions = self._champ_select_grid_champions_decision()
        if not champions.ok:
            return champions

        for champion in champions.value:
            cid = champion.get("id")
            try:
                champion_id = int(cid)
            except (TypeError, ValueError):
                continue
            if champion_id <= 0:
                continue

            keys = (
                champion.get("name"),
                champion.get("displayName"),
                champion.get("alias"),
                champion.get("squarePortraitPath"),
            )
            if any(_normalize_lookup_key(x) == target for x in keys):
                return LcuDecision(LcuOutcome.SUCCESS, value=champion_id)

        return LcuDecision(
            LcuOutcome.CHAMPION_NOT_FOUND,
            reason="champion not found in LCU grid",
        )

    def select_champ_select_champion_decision(
        self,
        champion_name: object,
        *,
        action_type: str,
        complete: bool = False,
    ) -> LcuDecision:
        action = self.get_local_action_state(
            action_type, require_in_progress=complete
        )
        if not action.ok:
            return action

        champion = self._resolve_champion_id_decision(champion_name)
        if not champion.ok:
            return champion

        patch = self.request(
            "PATCH",
            f"/lol-champ-select/v1/session/actions/{action.value.id}",
            json_body={"championId": champion.value},
        )
        if not (patch.ok or patch.status_code == 204):
            return LcuDecision(
                LcuOutcome.ACTION_REJECTED,
                reason="champ-select action patch rejected",
                status_code=patch.status_code,
                error=patch.error,
            )

        if complete:
            self._wait_for_action_champion(action.value.id, int(champion.value))

            completed = self._complete_champ_select_action(
                action.value.id, int(champion.value)
            )
            if not completed.ok:
                return completed
            completed_action = self._wait_for_action_completed(action.value.id)
            if not completed_action.ok:
                if (
                    completed.reason
                    != "champ-select action complete fallback patch accepted"
                ):
                    fallback_completed = self._patch_completed_champ_select_action(
                        action.value.id, int(champion.value)
                    )
                    if not fallback_completed.ok:
                        return fallback_completed
                    completed_action = self._wait_for_action_completed(
                        action.value.id
                    )
                if not completed_action.ok:
                    return completed_action

        return LcuDecision(LcuOutcome.SUCCESS, value=action.value)

    def select_champ_select_champion(
        self,
        champion_name: object,
        *,
        action_type: str,
        complete: bool = False,
    ) -> bool:
        result = self.select_champ_select_champion_decision(
            champion_name, action_type=action_type, complete=complete
        )
        return result.ok
