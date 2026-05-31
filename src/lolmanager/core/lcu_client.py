from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import random
import time
from typing import Any, Callable, Optional, Sequence
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
PHASE_NONE = "None"
PHASE_RECONNECT = "Reconnect"
PHASE_WATCH_IN_PROGRESS = "WatchInProgress"
KNOWN_GAMEFLOW_PHASES = frozenset(
    {
        PHASE_NONE,
        PHASE_LOBBY,
        PHASE_MATCHMAKING,
        PHASE_READY_CHECK,
        PHASE_CHAMP_SELECT,
        PHASE_IN_PROGRESS,
        PHASE_WAITING_FOR_STATS,
        PHASE_PRE_END_OF_GAME,
        PHASE_END_OF_GAME,
        PHASE_RECONNECT,
        PHASE_WATCH_IN_PROGRESS,
    }
)
HONOR_BALLOT_ENDPOINT = "/lol-honor-v2/v1/ballot"
HONOR_VOTE_ENDPOINT = "/lol-honor/v1/honor"
HONOR_BALLOT_SUBMIT_ENDPOINT = "/lol-honor/v1/ballot"
HONOR_VOTE_TYPE = "HEART"
LCU_TERMINAL_CONTEXTS = frozenset(
    {
        "postgame_honor_vote",
        "blocking_modal",
    }
)
# Endpoint provenance: confirmed from the installed rcp-fe-lol-postgame WAD.
# Keep modal dismissal unsupported unless a concrete LCU close route is proven.


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


def _write_result_decision(
    result: "LcuResult",
    *,
    success_reason: str,
    rejected_reason: str,
) -> "LcuDecision":
    if result.error:
        return LcuDecision(
            _connection_outcome_for_error(result.error),
            reason=result.error,
            status_code=result.status_code,
            error=result.error,
        )
    if result.ok or result.status_code == 204:
        return LcuDecision(
            LcuOutcome.SUCCESS,
            reason=success_reason,
            status_code=result.status_code,
        )
    if result.status_code is None or result.status_code >= 500:
        return LcuDecision(
            LcuOutcome.REQUEST_FAILED,
            reason="LCU request failed",
            status_code=result.status_code,
        )
    return LcuDecision(
        LcuOutcome.ACTION_REJECTED,
        reason=rejected_reason,
        status_code=result.status_code,
    )


def _write_or_unsupported_decision(
    result: "LcuResult",
    *,
    success_reason: str,
    rejected_reason: str,
    unsupported_reason: str,
) -> "LcuDecision":
    if result.error:
        return LcuDecision(
            _connection_outcome_for_error(result.error),
            reason=result.error,
            status_code=result.status_code,
            error=result.error,
        )
    if result.status_code in {404, 405}:
        return LcuDecision(
            LcuOutcome.UNSUPPORTED,
            reason=unsupported_reason,
            status_code=result.status_code,
        )
    return _write_result_decision(
        result,
        success_reason=success_reason,
        rejected_reason=rejected_reason,
    )


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


def is_known_gameflow_phase(value: object) -> bool:
    return isinstance(value, str) and value.strip() in KNOWN_GAMEFLOW_PHASES


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
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    NO_SESSION = "no_session"
    MALFORMED_SESSION = "malformed_session"
    MALFORMED_RESPONSE = "malformed_response"
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


@dataclass(frozen=True)
class HonorVoteCandidate:
    puuid: str
    raw: dict[str, Any]


def _coerce_lcu_outcome(outcome: object) -> LcuOutcome:
    if isinstance(outcome, LcuDecision):
        return outcome.status
    if isinstance(outcome, LcuOutcome):
        return outcome
    try:
        return LcuOutcome(str(outcome))
    except ValueError:
        return LcuOutcome.UNKNOWN


def lcu_loop_action_for(outcome: object, *, context: str) -> LcuLoopAction:
    status = _coerce_lcu_outcome(outcome)

    if status == LcuOutcome.SUCCESS:
        return LcuLoopAction.ACT_LCU
    if str(context or "") in LCU_TERMINAL_CONTEXTS:
        return LcuLoopAction.ABORT_LOG
    if status in {LcuOutcome.UNAVAILABLE, LcuOutcome.REQUEST_FAILED}:
        return LcuLoopAction.FALLBACK_IMAGE
    return LcuLoopAction.WAIT_AUTHORITATIVE


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
        self._phase_cache: tuple[float, Optional[LcuDecision]] = (0.0, None)
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
        except requests.RequestException as exc:
            return LcuResult(status_code=None, error=f"{type(exc).__name__}: {exc}")

        data: Any = None
        if response.content:
            try:
                data = response.json()
            except ValueError:
                data = response.text
        return LcuResult(status_code=int(response.status_code), data=data)

    def get_gameflow_phase(self, *, max_age_sec: float = 0.25) -> Optional[str]:
        result = self.get_gameflow_phase_decision(max_age_sec=max_age_sec)
        return str(result.value) if result.ok else None

    def get_gameflow_phase_decision(
        self, *, max_age_sec: float = 0.25
    ) -> LcuDecision:
        now = time.monotonic()
        cached_at, cached = self._phase_cache
        if (
            cached_at
            and cached is not None
            and (now - cached_at) <= max(0.0, float(max_age_sec))
        ):
            return cached

        result = self.request("GET", "/lol-gameflow/v1/gameflow-phase")
        if result.error:
            decision = LcuDecision(
                _connection_outcome_for_error(result.error),
                reason=result.error,
                status_code=result.status_code,
                error=result.error,
            )
        elif not result.ok:
            decision = LcuDecision(
                LcuOutcome.REQUEST_FAILED,
                reason="gameflow phase request failed",
                status_code=result.status_code,
            )
        elif not is_known_gameflow_phase(result.data):
            decision = LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="gameflow phase response is unknown or malformed",
                status_code=result.status_code,
            )
        else:
            decision = LcuDecision(
                LcuOutcome.SUCCESS,
                value=result.data.strip(),
                status_code=result.status_code,
            )
        self._phase_cache = (now, decision)
        return decision

    def consume_phase_transition(self, phase: Optional[str]) -> Optional[tuple[Optional[str], str]]:
        if not phase:
            return None
        previous = self._last_logged_phase
        if previous == phase:
            return None
        self._last_logged_phase = phase
        return (previous, phase)

    def accept_ready_check(self) -> bool:
        return self.accept_ready_check_decision().ok

    def accept_ready_check_decision(self) -> LcuDecision:
        result = self.request("POST", "/lol-matchmaking/v1/ready-check/accept")
        return _write_result_decision(
            result,
            success_reason="ready check accept accepted",
            rejected_reason="ready check accept rejected",
        )

    def start_matchmaking(self) -> bool:
        return self.start_matchmaking_decision().ok

    def start_matchmaking_decision(self) -> LcuDecision:
        result = self.request("POST", "/lol-lobby/v2/lobby/matchmaking/search")
        return _write_result_decision(
            result,
            success_reason="matchmaking search accepted",
            rejected_reason="matchmaking search rejected",
        )

    def is_end_of_game_stats_available(self) -> bool:
        return self.request("GET", "/lol-end-of-game/v1/eog-stats-block").ok

    def get_honor_ballot_decision(self) -> LcuDecision:
        result = self.request("GET", HONOR_BALLOT_ENDPOINT)
        if result.error:
            return LcuDecision(
                _connection_outcome_for_error(result.error),
                reason=result.error,
                status_code=result.status_code,
                error=result.error,
            )
        if result.status_code in {404, 405}:
            return LcuDecision(
                LcuOutcome.UNSUPPORTED,
                reason="honor ballot endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="honor ballot request failed",
                status_code=result.status_code,
            )
        if not isinstance(result.data, dict):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="honor ballot response is not an object",
                status_code=result.status_code,
            )
        return LcuDecision(
            LcuOutcome.SUCCESS,
            value=result.data,
            status_code=result.status_code,
        )

    def _honor_vote_candidates(
        self,
        ballot: dict[str, Any],
    ) -> LcuDecision:
        eligible_allies = ballot.get("eligibleAllies", [])
        if eligible_allies is None:
            eligible_allies = []
        if not isinstance(eligible_allies, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="honor ballot eligibleAllies is not a list",
            )

        honored_players = ballot.get("honoredPlayers", [])
        if honored_players is None:
            honored_players = []
        if not isinstance(honored_players, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="honor ballot honoredPlayers is not a list",
            )

        honored_puuids = {
            str(player.get("recipientPuuid") or player.get("puuid") or "").strip()
            for player in honored_players
            if isinstance(player, dict)
        }
        honored_puuids.discard("")

        vote_pool = ballot.get("votePool")
        if isinstance(vote_pool, dict):
            votes = _parse_optional_int(vote_pool.get("votes"))
            if votes is not None and votes <= len(honored_puuids):
                return LcuDecision(
                    LcuOutcome.NO_CURRENT_ACTION,
                    reason="no honor votes remain",
                )

        candidates: list[HonorVoteCandidate] = []
        for candidate in eligible_allies:
            if not isinstance(candidate, dict):
                continue
            puuid = str(candidate.get("puuid") or "").strip()
            if not puuid or puuid in honored_puuids:
                continue
            candidates.append(HonorVoteCandidate(puuid=puuid, raw=candidate))

        if not candidates:
            return LcuDecision(
                LcuOutcome.NO_CURRENT_ACTION,
                reason="no eligible ally honor candidate",
            )
        return LcuDecision(LcuOutcome.SUCCESS, value=tuple(candidates))

    def honor_random_eligible_teammate_decision(
        self,
        *,
        choice: Optional[
            Callable[[Sequence[HonorVoteCandidate]], HonorVoteCandidate]
        ] = None,
    ) -> LcuDecision:
        ballot = self.get_honor_ballot_decision()
        if not ballot.ok:
            return ballot

        candidates_result = self._honor_vote_candidates(ballot.value)
        if not candidates_result.ok:
            return candidates_result

        candidates: Sequence[HonorVoteCandidate] = candidates_result.value
        chooser = choice or random.choice
        selected = chooser(candidates)
        if not isinstance(selected, HonorVoteCandidate):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="honor vote choice did not return a candidate",
            )

        honor = self.request(
            "POST",
            HONOR_VOTE_ENDPOINT,
            json_body={
                "recipientPuuid": selected.puuid,
                "honorType": HONOR_VOTE_TYPE,
            },
        )
        honor_decision = _write_or_unsupported_decision(
            honor,
            success_reason="honor vote accepted",
            rejected_reason="honor vote rejected",
            unsupported_reason="honor vote endpoint is not available",
        )
        if not honor_decision.ok:
            return honor_decision

        submit = self.request("POST", HONOR_BALLOT_SUBMIT_ENDPOINT)
        submit_decision = _write_or_unsupported_decision(
            submit,
            success_reason="honor ballot submit accepted",
            rejected_reason="honor ballot submit rejected",
            unsupported_reason="honor ballot submit endpoint is not available",
        )
        if not submit_decision.ok:
            return submit_decision

        return LcuDecision(
            LcuOutcome.SUCCESS,
            value=selected,
            reason="honor vote submitted",
            status_code=submit_decision.status_code,
        )

    def honor_random_eligible_teammate(self) -> bool:
        return self.honor_random_eligible_teammate_decision().ok

    def dismiss_blocking_modal_decision(self) -> LcuDecision:
        return LcuDecision(
            LcuOutcome.UNSUPPORTED,
            reason="no confirmed LCU route for blocking client modal dismissal",
        )

    def dismiss_blocking_modal(self) -> bool:
        return self.dismiss_blocking_modal_decision().ok

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
        completed_decision = _write_result_decision(
            completed,
            success_reason="champ-select action complete post accepted",
            rejected_reason="champ-select action complete post rejected",
        )
        if completed_decision.ok:
            return completed_decision

        fallback = self._patch_completed_champ_select_action(action_id, champion_id)
        if fallback.ok:
            return fallback
        if completed_decision.status in {
            LcuOutcome.UNAVAILABLE,
            LcuOutcome.REQUEST_FAILED,
        }:
            return LcuDecision(
                completed_decision.status,
                reason=(
                    f"{completed_decision.reason}; "
                    f"fallback patch failed: {fallback.reason}"
                ),
                status_code=completed_decision.status_code,
                error=completed_decision.error or fallback.error,
            )
        return fallback

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
        return _write_result_decision(
            fallback,
            success_reason="champ-select action complete fallback patch accepted",
            rejected_reason="champ-select action complete fallback patch rejected",
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
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED,
                reason="champion grid unavailable",
                status_code=result.status_code,
            )
        if not isinstance(result.data, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="champion grid response is not a list",
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
        patch_decision = _write_result_decision(
            patch,
            success_reason="champ-select action patch accepted",
            rejected_reason="champ-select action patch rejected",
        )
        if not patch_decision.ok:
            return patch_decision

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
                        if completed_action.status in {
                            LcuOutcome.UNAVAILABLE,
                            LcuOutcome.REQUEST_FAILED,
                        }:
                            return LcuDecision(
                                completed_action.status,
                                reason=(
                                    f"{completed_action.reason}; "
                                    f"fallback patch failed: {fallback_completed.reason}"
                                ),
                                status_code=completed_action.status_code,
                                error=completed_action.error
                                or fallback_completed.error,
                            )
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
