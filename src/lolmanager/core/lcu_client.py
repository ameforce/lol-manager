from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import getpass
import os
from pathlib import Path
import random
import time
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote
import warnings

import psutil
import requests
from urllib3.exceptions import InsecureRequestWarning


DEFAULT_LOCKFILE = Path(r"C:\Riot Games\League of Legends\lockfile")
CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_ENV = (
    "LOLMANAGER_CHAMPSELECT_ACTION_CONFIRM_TIMEOUT_SEC"
)
DEFAULT_CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_SEC = 2.0
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
END_OF_GAME_DISMISS_STATS_ENDPOINT = "/lol-end-of-game/v1/state/dismiss-stats"
LOBBY_PLAY_AGAIN_ENDPOINT = "/lol-lobby/v2/play-again"
SIMPLE_DIALOG_MESSAGES_ENDPOINT = "/lol-simple-dialog-messages/v1/messages"
REMEDY_NOTIFICATIONS_ENDPOINT = "/lol-remedy/v1/remedy-notifications"
REMEDY_NOTIFICATION_ACK_ENDPOINT = "/lol-remedy/v1/ack-remedy-notification"
PLAYER_BEHAVIOR_V2_REPORTER_FEEDBACK_ENDPOINT = (
    "/lol-player-behavior/v2/reporter-feedback"
)
PLAYER_BEHAVIOR_V1_REPORTER_FEEDBACK_ENDPOINT = (
    "/lol-player-behavior/v1/reporter-feedback"
)
PLAYER_NOTIFICATIONS_ENDPOINT = "/player-notifications/v1/notifications"
PLAYER_MESSAGING_NOTIFICATION_ENDPOINT = "/lol-player-messaging/v1/notification"
RANKED_NOTIFICATIONS_ENDPOINT = "/lol-ranked/v1/notifications"
CHAMP_SELECT_PICK_ORDER_SWAPS_ENDPOINT = (
    "/lol-champ-select/v1/session/pick-order-swaps"
)
CHAMP_SELECT_ONGOING_PICK_ORDER_SWAP_ENDPOINT = (
    "/lol-champ-select/v1/ongoing-pick-order-swap"
)
RIOTCLIENT_UX_ALLOW_FOREGROUND_ENDPOINT = "/riotclient/ux-allow-foreground"
RIOTCLIENT_UX_SHOW_ENDPOINT = "/riotclient/ux-show"
HONOR_VOTE_TYPE = "HEART"
LCU_TERMINAL_CONTEXTS = frozenset(
    {
        "postgame_honor_vote",
    }
)
# Endpoint provenance: confirmed from the installed rcp-fe-lol-postgame WAD.
_ALLOWED_LCU_PROCESS_NAMES = frozenset(
    {
        "leagueclient.exe",
        "leagueclientux.exe",
        "riotclientservices.exe",
    }
)
_LOOPBACK_BIND_IPS = frozenset({"127.0.0.1", "::1", "0.0.0.0", "::"})


def _normalize_lookup_key(value: object) -> str:
    return "".join(str(value or "").strip().casefold().split())


def _parse_optional_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _current_account_name() -> str:
    return str(getpass.getuser() or "").strip().casefold()


def _process_account_name(process: psutil.Process) -> str:
    raw = str(process.username() or "").strip().replace("/", "\\")
    return raw.rsplit("\\", maxsplit=1)[-1].casefold()


def _process_name(process: psutil.Process) -> str:
    return str(process.name() or "").strip().casefold()


def _is_allowed_lcu_process(process: psutil.Process) -> bool:
    name = _process_name(process)
    if name not in _ALLOWED_LCU_PROCESS_NAMES and not name.startswith("leagueclient"):
        return False

    identity_parts = [name]
    try:
        identity_parts.append(str(process.exe() or ""))
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return False
    try:
        identity_parts.extend(str(part or "") for part in process.cmdline())
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        pass

    identity = " ".join(identity_parts).casefold()
    return any(
        marker in identity
        for marker in ("leagueclient", "riotclientservices", "riot games")
    )


def _connection_laddr_parts(connection: object) -> tuple[str, Optional[int]]:
    laddr = getattr(connection, "laddr", None)
    if isinstance(laddr, tuple) and len(laddr) >= 2:
        return (str(laddr[0]), _parse_optional_int(laddr[1]))
    return (
        str(getattr(laddr, "ip", "") or ""),
        _parse_optional_int(getattr(laddr, "port", None)),
    )


def _process_owns_loopback_port(pid: int, port: int) -> bool:
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        return False

    for connection in connections:
        if getattr(connection, "pid", None) != pid:
            continue
        ip, local_port = _connection_laddr_parts(connection)
        if local_port != port or ip not in _LOOPBACK_BIND_IPS:
            continue
        status = str(getattr(connection, "status", "") or "").upper()
        if status and status != str(psutil.CONN_LISTEN).upper():
            continue
        return True
    return False


def _default_lcu_connection_validator(conn: "LcuConnection") -> bool:
    if conn.pid <= 0 or conn.port <= 0 or conn.port > 65535:
        return False
    try:
        process = psutil.Process(conn.pid)
        if not _is_allowed_lcu_process(process):
            return False
        if _process_account_name(process) != _current_account_name():
            return False
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return False

    return _process_owns_loopback_port(conn.pid, conn.port)


def _non_empty_identifier(item: object, *keys: str) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    for key in keys:
        raw = item.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text and text != "0":
            return text
    return None


def _received_pick_order_swap_id(item: object) -> Optional[int]:
    if not isinstance(item, dict):
        return None
    if _normalize_lookup_key(item.get("state")) != "received":
        return None
    swap_id = _parse_optional_int(item.get("id"))
    if swap_id is None or swap_id <= 0:
        return None
    return swap_id


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


def champ_select_action_confirm_timeout_sec(
    environ: Optional[Mapping[str, str]] = None,
) -> float:
    env = os.environ if environ is None else environ
    raw = str(env.get(CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_SEC
    try:
        value = float(raw)
    except ValueError:
        warnings.warn(
            f"{CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_ENV} must be a positive number; "
            f"using {DEFAULT_CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_SEC:.1f}s",
            RuntimeWarning,
            stacklevel=2,
        )
        return DEFAULT_CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_SEC
    if value <= 0:
        warnings.warn(
            f"{CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_ENV} must be positive; "
            f"using {DEFAULT_CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_SEC:.1f}s",
            RuntimeWarning,
            stacklevel=2,
        )
        return DEFAULT_CHAMP_SELECT_ACTION_CONFIRM_TIMEOUT_SEC
    return value


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
        connection_validator: Optional[Callable[[LcuConnection], bool]] = None,
    ) -> None:
        env_lockfile = os.environ.get("LOLMANAGER_LCU_LOCKFILE")
        self.lockfile = Path(env_lockfile) if env_lockfile else (lockfile or DEFAULT_LOCKFILE)
        self.session = session or requests.Session()
        self.timeout_sec = float(timeout_sec)
        self._connection_validator = (
            connection_validator
            if connection_validator is not None
            else (_default_lcu_connection_validator if session is None else None)
        )
        self._phase_cache: tuple[float, Optional[LcuDecision]] = (0.0, None)
        self._last_logged_phase: Optional[str] = None
        self.last_champ_select_action_timings: dict[str, float] = {}

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

    def _read_trusted_connection(self) -> tuple[Optional[LcuConnection], str]:
        conn = self.read_connection()
        if conn is None:
            return (None, "lockfile unavailable")
        if self._connection_validator is not None and not self._connection_validator(conn):
            return (None, "lockfile rejected")
        return (conn, "")

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        timeout_sec: Optional[float] = None,
        json_body: Any = None,
    ) -> LcuResult:
        conn, connection_error = self._read_trusted_connection()
        if conn is None:
            return LcuResult(status_code=None, error=connection_error)

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

    def show_ux_decision(self) -> LcuDecision:
        allow = self.request("POST", RIOTCLIENT_UX_ALLOW_FOREGROUND_ENDPOINT)
        if allow.error:
            return LcuDecision(
                _connection_outcome_for_error(allow.error),
                reason=allow.error,
                status_code=allow.status_code,
                error=allow.error,
            )
        if allow.status_code not in {200, 204, 404, 405}:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if allow.status_code is None or allow.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="riotclient ux allow foreground rejected",
                status_code=allow.status_code,
            )

        show = self.request("POST", RIOTCLIENT_UX_SHOW_ENDPOINT)
        return _write_or_unsupported_decision(
            show,
            success_reason="riotclient ux show accepted",
            rejected_reason="riotclient ux show rejected",
            unsupported_reason="riotclient ux show endpoint is not available",
        )

    def is_end_of_game_stats_available(self) -> bool:
        return self.request("GET", "/lol-end-of-game/v1/eog-stats-block").ok

    def dismiss_end_of_game_stats_decision(self) -> LcuDecision:
        result = self.request("POST", END_OF_GAME_DISMISS_STATS_ENDPOINT)
        return _write_or_unsupported_decision(
            result,
            success_reason="end-of-game stats dismiss accepted",
            rejected_reason="end-of-game stats dismiss rejected",
            unsupported_reason="end-of-game stats dismiss endpoint is not available",
        )

    def dismiss_end_of_game_stats(self) -> bool:
        return self.dismiss_end_of_game_stats_decision().ok

    def play_again_decision(self) -> LcuDecision:
        result = self.request("POST", LOBBY_PLAY_AGAIN_ENDPOINT)
        return _write_or_unsupported_decision(
            result,
            success_reason="lobby play again accepted",
            rejected_reason="lobby play again rejected",
            unsupported_reason="lobby play again endpoint is not available",
        )

    def play_again(self) -> bool:
        return self.play_again_decision().ok

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
        handlers = (
            self._decline_received_pick_order_swap_decision,
            self._dismiss_simple_dialog_messages_decision,
            self._dismiss_remedy_notifications_decision,
            self._dismiss_player_behavior_v2_reporter_feedback_decision,
            self._dismiss_player_behavior_v1_reporter_feedback_decision,
            self._dismiss_player_notifications_decision,
            self._dismiss_player_messaging_notification_decision,
            self._dismiss_ranked_notifications_decision,
        )
        saw_supported_empty = False
        for handler in handlers:
            result = handler()
            if result.ok:
                return result
            if result.status == LcuOutcome.NO_CURRENT_ACTION:
                saw_supported_empty = True
                continue
            if result.status == LcuOutcome.UNSUPPORTED:
                continue
            return result

        if saw_supported_empty:
            return LcuDecision(
                LcuOutcome.NO_CURRENT_ACTION,
                reason="no blocking client modal notification",
            )
        return LcuDecision(
            LcuOutcome.UNSUPPORTED,
            reason="no supported LCU route for blocking client modal dismissal",
        )

    def dismiss_blocking_modal(self) -> bool:
        return self.dismiss_blocking_modal_decision().ok

    def _decline_received_pick_order_swap_decision(self) -> LcuDecision:
        result = self.request("GET", CHAMP_SELECT_PICK_ORDER_SWAPS_ENDPOINT)
        if result.error:
            return LcuDecision(
                _connection_outcome_for_error(result.error),
                reason=result.error,
                status_code=result.status_code,
                error=result.error,
            )
        if result.status_code in {404, 405}:
            session_decision = (
                self._decline_received_pick_order_swap_from_session_decision()
            )
            if session_decision.ok:
                return session_decision
            if session_decision.status not in {
                LcuOutcome.NO_CURRENT_ACTION,
                LcuOutcome.NO_SESSION,
            }:
                return session_decision
            ongoing_decision = self._decline_ongoing_pick_order_swap_decision()
            if ongoing_decision.status != LcuOutcome.UNSUPPORTED:
                return ongoing_decision
            if session_decision.status == LcuOutcome.NO_CURRENT_ACTION:
                return session_decision
            return LcuDecision(
                LcuOutcome.UNSUPPORTED,
                reason="pick-order swap endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="pick-order swap request failed",
                status_code=result.status_code,
            )
        if not isinstance(result.data, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="pick-order swap response is not a list",
                status_code=result.status_code,
            )

        endpoint_decision = self._decline_received_pick_order_swaps_decision(
            result.data,
            status_code=result.status_code,
        )
        if endpoint_decision.status != LcuOutcome.NO_CURRENT_ACTION:
            return endpoint_decision
        session_decision = self._decline_received_pick_order_swap_from_session_decision()
        if session_decision.ok:
            return session_decision
        if session_decision.status not in {
            LcuOutcome.NO_CURRENT_ACTION,
            LcuOutcome.NO_SESSION,
        }:
            return session_decision
        ongoing_decision = self._decline_ongoing_pick_order_swap_decision()
        if ongoing_decision.status == LcuOutcome.UNSUPPORTED:
            return endpoint_decision
        return ongoing_decision

    def _decline_received_pick_order_swap_from_session_decision(self) -> LcuDecision:
        result = self._champ_select_session_decision()
        if not result.ok:
            return result
        raw_swaps = result.value.get("pickOrderSwaps")
        if raw_swaps is None:
            return LcuDecision(
                LcuOutcome.NO_CURRENT_ACTION,
                reason="no pick-order swaps in champ-select session",
                status_code=result.status_code,
            )
        if not isinstance(raw_swaps, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="champ-select pickOrderSwaps payload is not a list",
                status_code=result.status_code,
            )
        return self._decline_received_pick_order_swaps_decision(
            raw_swaps,
            status_code=result.status_code,
        )

    def _decline_received_pick_order_swaps_decision(
        self,
        swaps: list[object],
        *,
        status_code: Optional[int],
    ) -> LcuDecision:
        for swap in swaps:
            swap_id = _received_pick_order_swap_id(swap)
            if swap_id is None:
                continue
            decline = self.request(
                "POST",
                f"{CHAMP_SELECT_PICK_ORDER_SWAPS_ENDPOINT}/{swap_id}/decline",
            )
            return _write_or_unsupported_decision(
                decline,
                success_reason="pick-order swap declined",
                rejected_reason="pick-order swap decline rejected",
                unsupported_reason="pick-order swap decline endpoint is not available",
            )

        return LcuDecision(
            LcuOutcome.NO_CURRENT_ACTION,
            reason="no received pick-order swap request",
            status_code=status_code,
        )

    def _decline_ongoing_pick_order_swap_decision(self) -> LcuDecision:
        result = self.request("GET", CHAMP_SELECT_ONGOING_PICK_ORDER_SWAP_ENDPOINT)
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
                reason="ongoing pick-order swap endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="ongoing pick-order swap request failed",
                status_code=result.status_code,
            )
        if result.data in (None, ""):
            return LcuDecision(
                LcuOutcome.NO_CURRENT_ACTION,
                reason="no ongoing pick-order swap request",
                status_code=result.status_code,
            )
        if not isinstance(result.data, dict):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="ongoing pick-order swap response is not an object",
                status_code=result.status_code,
            )
        if not result.data:
            return LcuDecision(
                LcuOutcome.NO_CURRENT_ACTION,
                reason="no ongoing pick-order swap request",
                status_code=result.status_code,
            )
        swap_id = _received_pick_order_swap_id(result.data)
        if swap_id is None:
            if _normalize_lookup_key(result.data.get("state")) == "received":
                return LcuDecision(
                    LcuOutcome.MALFORMED_RESPONSE,
                    reason="ongoing pick-order swap response has no positive id",
                    status_code=result.status_code,
                )
            return LcuDecision(
                LcuOutcome.NO_CURRENT_ACTION,
                reason="no received ongoing pick-order swap request",
                status_code=result.status_code,
            )
        clear = self.request(
            "POST",
            f"{CHAMP_SELECT_ONGOING_PICK_ORDER_SWAP_ENDPOINT}/{swap_id}/clear",
        )
        return _write_or_unsupported_decision(
            clear,
            success_reason="ongoing pick-order swap notification cleared",
            rejected_reason="ongoing pick-order swap notification clear rejected",
            unsupported_reason=(
                "ongoing pick-order swap notification clear endpoint is not available"
            ),
        )

    def _dismiss_simple_dialog_messages_decision(self) -> LcuDecision:
        result = self.request("GET", SIMPLE_DIALOG_MESSAGES_ENDPOINT)
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
                reason="simple dialog messages endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="simple dialog messages request failed",
                status_code=result.status_code,
            )
        if not isinstance(result.data, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="simple dialog messages response is not a list",
                status_code=result.status_code,
            )

        dismissed = 0
        for message in result.data:
            message_id = _non_empty_identifier(message, "messageId", "id", "msgId")
            if not message_id:
                continue
            delete = self.request(
                "DELETE",
                f"{SIMPLE_DIALOG_MESSAGES_ENDPOINT}/{quote(message_id, safe='')}",
            )
            decision = _write_or_unsupported_decision(
                delete,
                success_reason="simple dialog message dismissed",
                rejected_reason="simple dialog message dismiss rejected",
                unsupported_reason="simple dialog message dismiss endpoint is not available",
            )
            if not decision.ok:
                return decision
            dismissed += 1

        if dismissed:
            return LcuDecision(
                LcuOutcome.SUCCESS,
                reason="simple dialog messages dismissed",
                status_code=result.status_code,
            )
        return LcuDecision(
            LcuOutcome.NO_CURRENT_ACTION,
            reason="no simple dialog messages",
            status_code=result.status_code,
        )

    def _dismiss_remedy_notifications_decision(self) -> LcuDecision:
        result = self.request("GET", REMEDY_NOTIFICATIONS_ENDPOINT)
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
                reason="remedy notifications endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="remedy notifications request failed",
                status_code=result.status_code,
            )
        if not isinstance(result.data, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="remedy notifications response is not a list",
                status_code=result.status_code,
            )

        dismissed = 0
        for notification in result.data:
            mail_id = _non_empty_identifier(notification, "mailId", "id")
            if not mail_id:
                continue
            acknowledge = self.request(
                "PUT",
                f"{REMEDY_NOTIFICATION_ACK_ENDPOINT}/{quote(mail_id, safe='')}",
            )
            decision = _write_or_unsupported_decision(
                acknowledge,
                success_reason="remedy notification acknowledged",
                rejected_reason="remedy notification acknowledge rejected",
                unsupported_reason="remedy notification acknowledge endpoint is not available",
            )
            if not decision.ok:
                return decision
            dismissed += 1

        if dismissed:
            return LcuDecision(
                LcuOutcome.SUCCESS,
                reason="remedy notifications acknowledged",
                status_code=result.status_code,
            )
        return LcuDecision(
            LcuOutcome.NO_CURRENT_ACTION,
            reason="no remedy notifications",
            status_code=result.status_code,
        )

    def _dismiss_player_behavior_v2_reporter_feedback_decision(self) -> LcuDecision:
        result = self.request("GET", PLAYER_BEHAVIOR_V2_REPORTER_FEEDBACK_ENDPOINT)
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
                reason="reporter feedback v2 endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="reporter feedback v2 request failed",
                status_code=result.status_code,
            )
        if not isinstance(result.data, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="reporter feedback v2 response is not a list",
                status_code=result.status_code,
            )

        dismissed = 0
        for feedback in result.data:
            feedback_key = _non_empty_identifier(feedback, "key", "id", "feedbackId")
            if not feedback_key:
                continue
            acknowledge = self.request(
                "POST",
                (
                    f"{PLAYER_BEHAVIOR_V2_REPORTER_FEEDBACK_ENDPOINT}/"
                    f"{quote(feedback_key, safe='')}"
                ),
            )
            decision = _write_or_unsupported_decision(
                acknowledge,
                success_reason="reporter feedback acknowledged",
                rejected_reason="reporter feedback acknowledge rejected",
                unsupported_reason=(
                    "reporter feedback acknowledge endpoint is not available"
                ),
            )
            if not decision.ok:
                return decision
            dismissed += 1

        if dismissed:
            return LcuDecision(
                LcuOutcome.SUCCESS,
                reason="reporter feedback acknowledged",
                status_code=result.status_code,
            )
        return LcuDecision(
            LcuOutcome.NO_CURRENT_ACTION,
            reason="no reporter feedback v2 notification",
            status_code=result.status_code,
        )

    def _dismiss_player_behavior_v1_reporter_feedback_decision(self) -> LcuDecision:
        result = self.request("GET", PLAYER_BEHAVIOR_V1_REPORTER_FEEDBACK_ENDPOINT)
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
                reason="reporter feedback v1 endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="reporter feedback v1 request failed",
                status_code=result.status_code,
            )
        if not isinstance(result.data, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="reporter feedback v1 response is not a list",
                status_code=result.status_code,
            )

        dismissed = 0
        for feedback in result.data:
            feedback_id = _non_empty_identifier(feedback, "id", "feedbackId", "key")
            if not feedback_id:
                continue
            delete = self.request(
                "DELETE",
                (
                    f"{PLAYER_BEHAVIOR_V1_REPORTER_FEEDBACK_ENDPOINT}/"
                    f"{quote(feedback_id, safe='')}"
                ),
            )
            decision = _write_or_unsupported_decision(
                delete,
                success_reason="reporter feedback dismissed",
                rejected_reason="reporter feedback dismiss rejected",
                unsupported_reason="reporter feedback dismiss endpoint is not available",
            )
            if not decision.ok:
                return decision
            dismissed += 1

        if dismissed:
            return LcuDecision(
                LcuOutcome.SUCCESS,
                reason="reporter feedback dismissed",
                status_code=result.status_code,
            )
        return LcuDecision(
            LcuOutcome.NO_CURRENT_ACTION,
            reason="no reporter feedback v1 notification",
            status_code=result.status_code,
        )

    def _dismiss_player_notifications_decision(self) -> LcuDecision:
        result = self.request("GET", PLAYER_NOTIFICATIONS_ENDPOINT)
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
                reason="player notifications endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="player notifications request failed",
                status_code=result.status_code,
            )
        if not isinstance(result.data, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="player notifications response is not a list",
                status_code=result.status_code,
            )

        dismissed = 0
        for notification in result.data:
            notification_id = _non_empty_identifier(notification, "id", "notificationId")
            if not notification_id:
                continue
            delete = self.request(
                "DELETE",
                f"{PLAYER_NOTIFICATIONS_ENDPOINT}/{quote(notification_id, safe='')}",
            )
            decision = _write_or_unsupported_decision(
                delete,
                success_reason="player notification dismissed",
                rejected_reason="player notification dismiss rejected",
                unsupported_reason="player notification dismiss endpoint is not available",
            )
            if not decision.ok:
                return decision
            dismissed += 1

        if dismissed:
            return LcuDecision(
                LcuOutcome.SUCCESS,
                reason="player notifications dismissed",
                status_code=result.status_code,
            )
        return LcuDecision(
            LcuOutcome.NO_CURRENT_ACTION,
            reason="no player notifications",
            status_code=result.status_code,
        )

    def _dismiss_player_messaging_notification_decision(self) -> LcuDecision:
        result = self.request("GET", PLAYER_MESSAGING_NOTIFICATION_ENDPOINT)
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
                reason="player messaging notification endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="player messaging notification request failed",
                status_code=result.status_code,
            )
        if not isinstance(result.data, dict):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="player messaging notification response is not an object",
                status_code=result.status_code,
            )

        notification_id = _non_empty_identifier(result.data, "id", "notificationId")
        if not notification_id:
            return LcuDecision(
                LcuOutcome.NO_CURRENT_ACTION,
                reason="no player messaging notification",
                status_code=result.status_code,
            )

        ack = self.request(
            "DELETE",
            f"{PLAYER_MESSAGING_NOTIFICATION_ENDPOINT}/{quote(notification_id, safe='')}/acknowledge",
        )
        return _write_or_unsupported_decision(
            ack,
            success_reason="player messaging notification acknowledged",
            rejected_reason="player messaging notification acknowledge rejected",
            unsupported_reason="player messaging notification acknowledge endpoint is not available",
        )

    def _dismiss_ranked_notifications_decision(self) -> LcuDecision:
        result = self.request("GET", RANKED_NOTIFICATIONS_ENDPOINT)
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
                reason="ranked notifications endpoint is not available",
                status_code=result.status_code,
            )
        if not result.ok:
            return LcuDecision(
                LcuOutcome.REQUEST_FAILED
                if result.status_code is None or result.status_code >= 500
                else LcuOutcome.ACTION_REJECTED,
                reason="ranked notifications request failed",
                status_code=result.status_code,
            )
        if not isinstance(result.data, list):
            return LcuDecision(
                LcuOutcome.MALFORMED_RESPONSE,
                reason="ranked notifications response is not a list",
                status_code=result.status_code,
            )

        dismissed = 0
        for notification in result.data:
            if not isinstance(notification, dict):
                continue
            if _normalize_lookup_key(notification.get("displayType")) != "vignette":
                continue
            notification_id = _non_empty_identifier(
                notification, "id", "notificationId"
            )
            if not notification_id:
                continue
            acknowledge = self.request(
                "POST",
                (
                    f"{RANKED_NOTIFICATIONS_ENDPOINT}/"
                    f"{quote(notification_id, safe='')}/acknowledge"
                ),
            )
            decision = _write_or_unsupported_decision(
                acknowledge,
                success_reason="ranked notification acknowledged",
                rejected_reason="ranked notification acknowledge rejected",
                unsupported_reason="ranked notification acknowledge endpoint is not available",
            )
            if not decision.ok:
                return decision
            dismissed += 1

        if dismissed:
            return LcuDecision(
                LcuOutcome.SUCCESS,
                reason="ranked notifications acknowledged",
                status_code=result.status_code,
            )
        return LcuDecision(
            LcuOutcome.NO_CURRENT_ACTION,
            reason="no ranked notifications",
            status_code=result.status_code,
        )

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

    def _record_champ_select_action_timing(
        self,
        metric_name: str,
        *,
        elapsed_sec: float,
        timeout_sec: float,
    ) -> None:
        key = str(metric_name or "wait").strip() or "wait"
        self.last_champ_select_action_timings[f"{key}_elapsed_sec"] = max(
            0.0, float(elapsed_sec)
        )
        self.last_champ_select_action_timings[f"{key}_timeout_sec"] = max(
            0.0, float(timeout_sec)
        )

    def _wait_for_action_champion(
        self,
        action_id: int,
        champion_id: int,
        *,
        timeout_sec: float = 0.8,
        interval_sec: float = 0.05,
        metric_name: str = "assignment",
    ) -> LcuDecision:
        timeout_sec = max(0.0, float(timeout_sec))
        started_at = time.monotonic()
        deadline = started_at + timeout_sec
        last: LcuDecision = LcuDecision(
            LcuOutcome.ACTION_REJECTED,
            reason="champ-select action champion assignment not confirmed",
        )

        while True:
            action = self._get_local_action_by_id(action_id)
            now = time.monotonic()
            if action.ok and action.value.champion_id == champion_id:
                self._record_champ_select_action_timing(
                    metric_name,
                    elapsed_sec=now - started_at,
                    timeout_sec=timeout_sec,
                )
                return action
            if action.ok:
                last = LcuDecision(
                    LcuOutcome.ACTION_REJECTED,
                    value=action.value,
                    reason="champ-select action champion assignment not confirmed",
                )
            else:
                last = action

            if now >= deadline:
                self._record_champ_select_action_timing(
                    metric_name,
                    elapsed_sec=now - started_at,
                    timeout_sec=timeout_sec,
                )
                return last
            time.sleep(min(max(0.0, interval_sec), deadline - now))

    def _wait_for_action_completed(
        self,
        action_id: int,
        *,
        timeout_sec: float = 0.8,
        interval_sec: float = 0.05,
        metric_name: str = "completion",
    ) -> LcuDecision:
        timeout_sec = max(0.0, float(timeout_sec))
        started_at = time.monotonic()
        deadline = started_at + timeout_sec
        last: LcuDecision = LcuDecision(
            LcuOutcome.ACTION_REJECTED,
            reason="champ-select action completion not confirmed",
        )

        while True:
            action = self._get_local_action_by_id(action_id)
            now = time.monotonic()
            if action.ok and action.value.completed:
                self._record_champ_select_action_timing(
                    metric_name,
                    elapsed_sec=now - started_at,
                    timeout_sec=timeout_sec,
                )
                return action
            if action.ok:
                last = LcuDecision(
                    LcuOutcome.ACTION_REJECTED,
                    value=action.value,
                    reason="champ-select action completion not confirmed",
                )
            else:
                last = action

            if now >= deadline:
                self._record_champ_select_action_timing(
                    metric_name,
                    elapsed_sec=now - started_at,
                    timeout_sec=timeout_sec,
                )
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
            confirm_timeout_sec = champ_select_action_confirm_timeout_sec()
            self.last_champ_select_action_timings = {}
            self._wait_for_action_champion(
                action.value.id,
                int(champion.value),
                timeout_sec=confirm_timeout_sec,
                metric_name="assignment",
            )

            completed = self._complete_champ_select_action(
                action.value.id, int(champion.value)
            )
            if not completed.ok:
                return completed
            completed_action = self._wait_for_action_completed(
                action.value.id,
                timeout_sec=confirm_timeout_sec,
                metric_name="completion",
            )
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
                        action.value.id,
                        timeout_sec=confirm_timeout_sec,
                        metric_name="fallback_completion",
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
