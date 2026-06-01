from __future__ import annotations

import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from requests import Timeout

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lolmanager.core.lcu_client import (
    ChampSelectAction,
    LcuConnection,
    LcuClient,
    LcuDecision,
    LcuLoopAction,
    LcuOutcome,
    _default_lcu_connection_validator,
    lcu_loop_action_for,
)


class _FakeResponse:
    def __init__(self, status_code: int, data=None) -> None:
        self.status_code = status_code
        self._data = data
        self.content = b"x" if data is not None else b""
        self.text = "" if data is None else str(data)

    def json(self):
        return self._data


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


class _FakeProcessInfo:
    def __init__(self, *, name: str, exe: str, username: str) -> None:
        self._name = name
        self._exe = exe
        self._username = username

    def name(self) -> str:
        return self._name

    def exe(self) -> str:
        return self._exe

    def cmdline(self) -> list[str]:
        return [self._exe]

    def username(self) -> str:
        return self._username


class _FakeLaddr:
    def __init__(self, ip: str, port: int) -> None:
        self.ip = ip
        self.port = port


class _FakeNetConnection:
    def __init__(self, *, pid: int, ip: str, port: int, status: str = "LISTEN") -> None:
        self.pid = pid
        self.laddr = _FakeLaddr(ip, port)
        self.status = status


class LcuClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.lockfile = Path(self.tmpdir.name) / "lockfile"
        self.lockfile.write_text(
            "LeagueClient:1234:2999:secret-token:https", encoding="utf-8"
        )

    def test_reads_valid_lockfile(self) -> None:
        conn = LcuClient(lockfile=self.lockfile).read_connection()

        self.assertIsNotNone(conn)
        assert conn is not None
        self.assertEqual(conn.pid, 1234)
        self.assertEqual(conn.port, 2999)
        self.assertEqual(conn.base_url, "https://127.0.0.1:2999")
        self.assertTrue(conn.authorization_header.startswith("Basic "))

    def test_malformed_lockfile_returns_none(self) -> None:
        self.lockfile.write_text("bad:data", encoding="utf-8")

        self.assertIsNone(LcuClient(lockfile=self.lockfile).read_connection())

    def test_request_rejects_untrusted_lockfile_without_contacting_session(self) -> None:
        session = _FakeSession([_FakeResponse(200, "Lobby")])
        client = LcuClient(
            lockfile=self.lockfile,
            session=session,
            connection_validator=lambda _conn: False,
        )

        result = client.request("GET", "/lol-gameflow/v1/gameflow-phase")

        self.assertEqual(result.error, "lockfile rejected")
        self.assertEqual(session.calls, [])

    def test_default_lockfile_validator_requires_matching_riot_process_and_port(
        self,
    ) -> None:
        conn = LcuConnection(
            pid=1234, port=2999, password="secret-token", protocol="https"
        )
        process = _FakeProcessInfo(
            name="LeagueClientUx.exe",
            exe=r"C:\Riot Games\League of Legends\LeagueClientUx.exe",
            username=r"DAENG\enmso",
        )

        with (
            mock.patch("lolmanager.core.lcu_client.getpass.getuser", return_value="enmso"),
            mock.patch("lolmanager.core.lcu_client.psutil.Process", return_value=process),
            mock.patch(
                "lolmanager.core.lcu_client.psutil.net_connections",
                return_value=[
                    _FakeNetConnection(pid=1234, ip="127.0.0.1", port=2999)
                ],
            ),
        ):
            self.assertTrue(_default_lcu_connection_validator(conn))

    def test_default_lockfile_validator_rejects_non_riot_process_name(self) -> None:
        conn = LcuConnection(
            pid=1234, port=2999, password="secret-token", protocol="https"
        )
        process = _FakeProcessInfo(
            name="python.exe",
            exe=r"C:\Users\enmso\AppData\Local\Programs\Python\python.exe",
            username=r"DAENG\enmso",
        )

        with (
            mock.patch("lolmanager.core.lcu_client.getpass.getuser", return_value="enmso"),
            mock.patch("lolmanager.core.lcu_client.psutil.Process", return_value=process),
            mock.patch(
                "lolmanager.core.lcu_client.psutil.net_connections",
                return_value=[
                    _FakeNetConnection(pid=1234, ip="127.0.0.1", port=2999)
                ],
            ),
        ):
            self.assertFalse(_default_lcu_connection_validator(conn))

    def test_default_lockfile_validator_rejects_port_owned_by_other_pid(self) -> None:
        conn = LcuConnection(
            pid=1234, port=2999, password="secret-token", protocol="https"
        )
        process = _FakeProcessInfo(
            name="LeagueClientUx.exe",
            exe=r"C:\Riot Games\League of Legends\LeagueClientUx.exe",
            username=r"DAENG\enmso",
        )

        with (
            mock.patch("lolmanager.core.lcu_client.getpass.getuser", return_value="enmso"),
            mock.patch("lolmanager.core.lcu_client.psutil.Process", return_value=process),
            mock.patch(
                "lolmanager.core.lcu_client.psutil.net_connections",
                return_value=[
                    _FakeNetConnection(pid=9999, ip="127.0.0.1", port=2999)
                ],
            ),
        ):
            self.assertFalse(_default_lcu_connection_validator(conn))

    def test_gameflow_phase_is_cached(self) -> None:
        session = _FakeSession([_FakeResponse(200, "Matchmaking")])
        client = LcuClient(lockfile=self.lockfile, session=session)

        self.assertEqual(client.get_gameflow_phase(max_age_sec=60), "Matchmaking")
        self.assertEqual(client.get_gameflow_phase(max_age_sec=60), "Matchmaking")

        self.assertEqual(len(session.calls), 1)
        self.assertTrue(
            str(session.calls[0]["url"]).endswith("/lol-gameflow/v1/gameflow-phase")
        )

    def test_gameflow_phase_decision_reports_malformed_response(self) -> None:
        session = _FakeSession([_FakeResponse(200, {"phase": "Matchmaking"})])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.get_gameflow_phase_decision()

        self.assertEqual(result.status, LcuOutcome.MALFORMED_RESPONSE)
        self.assertEqual(
            lcu_loop_action_for(result, context="phase"),
            LcuLoopAction.WAIT_AUTHORITATIVE,
        )

    def test_gameflow_phase_decision_rejects_unknown_phase(self) -> None:
        session = _FakeSession([_FakeResponse(200, "FuturePhase")])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.get_gameflow_phase_decision()

        self.assertEqual(result.status, LcuOutcome.MALFORMED_RESPONSE)
        self.assertEqual(
            lcu_loop_action_for(result, context="phase"),
            LcuLoopAction.WAIT_AUTHORITATIVE,
        )

    def test_request_converts_requests_exception_to_transport_failure(self) -> None:
        session = mock.Mock()
        session.request.side_effect = Timeout("LCU down")
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.request("GET", "/lol-gameflow/v1/gameflow-phase")

        self.assertIn("Timeout", result.error or "")

    def test_request_does_not_convert_unexpected_exception_to_transport_failure(
        self,
    ) -> None:
        session = mock.Mock()
        session.request.side_effect = RuntimeError("programmer fault")
        client = LcuClient(lockfile=self.lockfile, session=session)

        with self.assertRaisesRegex(RuntimeError, "programmer fault"):
            client.request("GET", "/lol-gameflow/v1/gameflow-phase")

    def test_show_ux_decision_allows_foreground_then_shows_client(self) -> None:
        session = _FakeSession([_FakeResponse(204), _FakeResponse(204)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.show_ux_decision()

        self.assertEqual(result.status, LcuOutcome.SUCCESS)
        self.assertEqual(
            [str(call["url"]).split("2999", maxsplit=1)[1] for call in session.calls],
            ["/riotclient/ux-allow-foreground", "/riotclient/ux-show"],
        )
        self.assertEqual([call["method"] for call in session.calls], ["POST", "POST"])

    def test_accept_ready_check_posts_to_lcu_endpoint(self) -> None:
        session = _FakeSession([_FakeResponse(204)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        self.assertTrue(client.accept_ready_check())

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertTrue(
            str(session.calls[0]["url"]).endswith(
                "/lol-matchmaking/v1/ready-check/accept"
            )
        )

    def test_start_matchmaking_posts_to_lobby_search_endpoint(self) -> None:
        session = _FakeSession([_FakeResponse(204)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        self.assertTrue(client.start_matchmaking())

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertTrue(
            str(session.calls[0]["url"]).endswith(
                "/lol-lobby/v2/lobby/matchmaking/search"
            )
        )

    def test_dismiss_end_of_game_stats_posts_to_continue_endpoint(self) -> None:
        session = _FakeSession([_FakeResponse(204)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.dismiss_end_of_game_stats_decision()

        self.assertEqual(result.status, LcuOutcome.SUCCESS)
        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertTrue(
            str(session.calls[0]["url"]).endswith(
                "/lol-end-of-game/v1/state/dismiss-stats"
            )
        )

    def test_honor_random_eligible_teammate_votes_random_ally_and_submits(
        self,
    ) -> None:
        ballot = {
            "eligibleAllies": [
                {"puuid": "ally-a", "summonerName": "A"},
                {"puuid": "ally-b", "summonerName": "B"},
            ],
            "eligibleOpponents": [{"puuid": "enemy-a"}],
            "honoredPlayers": [],
            "votePool": {"votes": 1},
        }
        session = _FakeSession(
            [_FakeResponse(200, ballot), _FakeResponse(204), _FakeResponse(204)]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.honor_random_eligible_teammate_decision(
            choice=lambda candidates: candidates[1]
        )

        self.assertEqual(result.status, LcuOutcome.SUCCESS)
        self.assertEqual(result.value.puuid, "ally-b")
        self.assertTrue(
            str(session.calls[0]["url"]).endswith("/lol-honor-v2/v1/ballot")
        )
        self.assertEqual(session.calls[1]["method"], "POST")
        self.assertTrue(str(session.calls[1]["url"]).endswith("/lol-honor/v1/honor"))
        self.assertEqual(
            session.calls[1]["kwargs"]["json"],
            {"recipientPuuid": "ally-b", "honorType": "HEART"},
        )
        self.assertEqual(session.calls[2]["method"], "POST")
        self.assertTrue(
            str(session.calls[2]["url"]).endswith("/lol-honor/v1/ballot")
        )

    def test_honor_random_eligible_teammate_ignores_opponents(self) -> None:
        ballot = {
            "eligibleAllies": [],
            "eligibleOpponents": [{"puuid": "enemy-a"}],
            "honoredPlayers": [],
            "votePool": {"votes": 1},
        }
        session = _FakeSession([_FakeResponse(200, ballot)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.honor_random_eligible_teammate_decision()

        self.assertEqual(result.status, LcuOutcome.NO_CURRENT_ACTION)
        self.assertEqual(len(session.calls), 1)

    def test_honor_random_eligible_teammate_reports_unsupported_ballot(
        self,
    ) -> None:
        session = _FakeSession([_FakeResponse(404, {"message": "not found"})])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.honor_random_eligible_teammate_decision()

        self.assertEqual(result.status, LcuOutcome.UNSUPPORTED)

    def test_dismiss_blocking_modal_deletes_simple_dialog_messages(
        self,
    ) -> None:
        session = _FakeSession(
            [
                _FakeResponse(200, [{"id": "dialog-a", "title": "알림"}]),
                _FakeResponse(204),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.dismiss_blocking_modal_decision()

        self.assertEqual(result.status, LcuOutcome.SUCCESS)
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertTrue(
            str(session.calls[0]["url"]).endswith(
                "/lol-simple-dialog-messages/v1/messages"
            )
        )
        self.assertEqual(session.calls[1]["method"], "DELETE")
        self.assertTrue(
            str(session.calls[1]["url"]).endswith(
                "/lol-simple-dialog-messages/v1/messages/dialog-a"
            )
        )

    def test_dismiss_blocking_modal_acknowledges_remedy_notification(
        self,
    ) -> None:
        session = _FakeSession(
            [
                _FakeResponse(200, []),
                _FakeResponse(
                    200,
                    [
                        {
                            "mailId": "remedy-a",
                            "state": "NEW",
                            "message": "{\"didReportOffender\": true}",
                        }
                    ],
                ),
                _FakeResponse(201),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.dismiss_blocking_modal_decision()

        self.assertEqual(result.status, LcuOutcome.SUCCESS)
        self.assertEqual(session.calls[1]["method"], "GET")
        self.assertTrue(
            str(session.calls[1]["url"]).endswith("/lol-remedy/v1/remedy-notifications")
        )
        self.assertEqual(session.calls[2]["method"], "PUT")
        self.assertTrue(
            str(session.calls[2]["url"]).endswith(
                "/lol-remedy/v1/ack-remedy-notification/remedy-a"
            )
        )

    def test_dismiss_blocking_modal_acknowledges_v2_reporter_feedback(
        self,
    ) -> None:
        session = _FakeSession(
            [
                _FakeResponse(200, []),
                _FakeResponse(200, []),
                _FakeResponse(200, [{"key": "feedback-a", "title": "신고 피드백"}]),
                _FakeResponse(204),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.dismiss_blocking_modal_decision()

        self.assertEqual(result.status, LcuOutcome.SUCCESS)
        self.assertEqual(session.calls[2]["method"], "GET")
        self.assertTrue(
            str(session.calls[2]["url"]).endswith(
                "/lol-player-behavior/v2/reporter-feedback"
            )
        )
        self.assertEqual(session.calls[3]["method"], "POST")
        self.assertTrue(
            str(session.calls[3]["url"]).endswith(
                "/lol-player-behavior/v2/reporter-feedback/feedback-a"
            )
        )

    def test_dismiss_blocking_modal_deletes_v1_reporter_feedback(
        self,
    ) -> None:
        session = _FakeSession(
            [
                _FakeResponse(200, []),
                _FakeResponse(200, []),
                _FakeResponse(404, {"message": "not found"}),
                _FakeResponse(200, [{"id": 7, "title": "신고 피드백"}]),
                _FakeResponse(204),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.dismiss_blocking_modal_decision()

        self.assertEqual(result.status, LcuOutcome.SUCCESS)
        self.assertEqual(session.calls[3]["method"], "GET")
        self.assertTrue(
            str(session.calls[3]["url"]).endswith(
                "/lol-player-behavior/v1/reporter-feedback"
            )
        )
        self.assertEqual(session.calls[4]["method"], "DELETE")
        self.assertTrue(
            str(session.calls[4]["url"]).endswith(
                "/lol-player-behavior/v1/reporter-feedback/7"
            )
        )

    def test_dismiss_blocking_modal_reports_no_current_action_when_empty(
        self,
    ) -> None:
        session = _FakeSession(
            [
                _FakeResponse(200, []),
                _FakeResponse(200, []),
                _FakeResponse(200, []),
                _FakeResponse(200, []),
                _FakeResponse(200, []),
                _FakeResponse(
                    200,
                    {"accountId": 0, "body": "", "id": 0, "msgId": "", "title": ""},
                ),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.dismiss_blocking_modal_decision()

        self.assertEqual(result.status, LcuOutcome.NO_CURRENT_ACTION)

    def test_get_local_player_position_reads_assigned_position_from_session(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "myTeam": [{"cellId": 7, "assignedPosition": "middle"}],
            "actions": [],
        }
        session = _FakeSession([_FakeResponse(200, champ_select_session)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.get_local_player_position()

        self.assertEqual(result.status, LcuOutcome.SUCCESS)
        self.assertEqual(result.value, "mid")

    def test_get_local_action_state_returns_current_local_action(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "myTeam": [{"cellId": 7, "assignedPosition": "bottom"}],
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 103,
                    },
                    {
                        "id": 32,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": False,
                        "completed": False,
                        "championId": 54,
                    },
                ]
            ],
        }
        session = _FakeSession([_FakeResponse(200, champ_select_session)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.get_local_action_state("pick", require_in_progress=True)

        self.assertEqual(result.status, LcuOutcome.SUCCESS)
        self.assertEqual(result.value.id, 31)
        self.assertEqual(result.value.champion_id, 103)

    def test_get_local_action_state_reports_malformed_session(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "myTeam": [{"cellId": 7, "assignedPosition": "middle"}],
            "actions": {"bad": "shape"},
        }
        session = _FakeSession([_FakeResponse(200, champ_select_session)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.get_local_action_state("pick", require_in_progress=True)

        self.assertEqual(result.status, LcuOutcome.MALFORMED_SESSION)

    def test_get_local_action_state_reports_no_current_action(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "myTeam": [{"cellId": 7, "assignedPosition": "middle"}],
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": False,
                        "completed": False,
                    }
                ]
            ],
        }
        session = _FakeSession([_FakeResponse(200, champ_select_session)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.get_local_action_state("pick", require_in_progress=True)

        self.assertEqual(result.status, LcuOutcome.NO_CURRENT_ACTION)

    def test_select_champ_select_champion_decision_reports_champion_not_found(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "myTeam": [{"cellId": 7, "assignedPosition": "middle"}],
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": False,
                    }
                ]
            ],
        }
        session = _FakeSession(
            [_FakeResponse(200, champ_select_session), _FakeResponse(200, [])]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.select_champ_select_champion_decision(
            "없는챔피언", action_type="pick", complete=True
        )

        self.assertEqual(result.status, LcuOutcome.CHAMPION_NOT_FOUND)
        self.assertEqual(len(session.calls), 2)

    def test_select_champ_select_champion_decision_reports_action_rejected(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "myTeam": [{"cellId": 7, "assignedPosition": "middle"}],
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": False,
                    }
                ]
            ],
        }
        session = _FakeSession(
            [
                _FakeResponse(200, champ_select_session),
                _FakeResponse(200, [{"id": 103, "name": "아리", "alias": "Ahri"}]),
                _FakeResponse(400, {"message": "not allowed"}),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.select_champ_select_champion_decision(
            "아리", action_type="pick", complete=True
        )

        self.assertEqual(result.status, LcuOutcome.ACTION_REJECTED)
        self.assertEqual(result.status_code, 400)

    def test_select_champ_select_champion_decision_preserves_request_failure(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "myTeam": [{"cellId": 7, "assignedPosition": "middle"}],
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": False,
                    }
                ]
            ],
        }
        session = _FakeSession(
            [
                _FakeResponse(200, champ_select_session),
                _FakeResponse(200, [{"id": 103, "name": "아리", "alias": "Ahri"}]),
                _FakeResponse(500, {"message": "unavailable"}),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.select_champ_select_champion_decision(
            "아리", action_type="pick", complete=True
        )

        self.assertEqual(result.status, LcuOutcome.REQUEST_FAILED)
        self.assertEqual(
            lcu_loop_action_for(result, context="write"),
            LcuLoopAction.FALLBACK_IMAGE,
        )

    def test_complete_champ_select_action_preserves_fallback_patch_request_failure(
        self,
    ) -> None:
        session = _FakeSession(
            [
                _FakeResponse(400, {"message": "complete rejected"}),
                _FakeResponse(500, {"message": "patch unavailable"}),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client._complete_champ_select_action(31, 103)

        self.assertEqual(result.status, LcuOutcome.REQUEST_FAILED)
        self.assertEqual(result.status_code, 500)

    def test_complete_champ_select_action_preserves_primary_request_failure(
        self,
    ) -> None:
        session = _FakeSession(
            [
                _FakeResponse(500, {"message": "complete unavailable"}),
                _FakeResponse(400, {"message": "patch rejected"}),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client._complete_champ_select_action(31, 103)

        self.assertEqual(result.status, LcuOutcome.REQUEST_FAILED)
        self.assertEqual(result.status_code, 500)
        self.assertIn("fallback patch failed", result.reason)

    def test_champion_grid_malformed_response_waits_authoritatively(self) -> None:
        session = _FakeSession([_FakeResponse(200, {"champions": []})])
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client._champ_select_grid_champions_decision()

        self.assertEqual(result.status, LcuOutcome.MALFORMED_RESPONSE)
        self.assertEqual(
            lcu_loop_action_for(result, context="write"),
            LcuLoopAction.WAIT_AUTHORITATIVE,
        )

    def test_lcu_loop_action_policy_distinguishes_semantic_wait_from_fallback(
        self,
    ) -> None:
        self.assertEqual(
            lcu_loop_action_for(LcuOutcome.SUCCESS, context="write"),
            LcuLoopAction.ACT_LCU,
        )
        self.assertEqual(
            lcu_loop_action_for(LcuOutcome.NO_CURRENT_ACTION, context="write"),
            LcuLoopAction.WAIT_AUTHORITATIVE,
        )
        self.assertEqual(
            lcu_loop_action_for(LcuOutcome.UNAVAILABLE, context="write"),
            LcuLoopAction.FALLBACK_IMAGE,
        )
        self.assertEqual(
            lcu_loop_action_for(LcuOutcome.REQUEST_FAILED, context="write"),
            LcuLoopAction.FALLBACK_IMAGE,
        )
        self.assertEqual(
            lcu_loop_action_for(LcuOutcome.UNSUPPORTED, context="write"),
            LcuLoopAction.WAIT_AUTHORITATIVE,
        )
        self.assertEqual(
            lcu_loop_action_for(
                LcuOutcome.REQUEST_FAILED, context="postgame_honor_vote"
            ),
            LcuLoopAction.ABORT_LOG,
        )
        self.assertEqual(
            lcu_loop_action_for(LcuOutcome.ACTION_REJECTED, context="blocking_modal"),
            LcuLoopAction.WAIT_AUTHORITATIVE,
        )
        self.assertEqual(
            lcu_loop_action_for("not-a-real-lcu-outcome", context="write"),
            LcuLoopAction.WAIT_AUTHORITATIVE,
        )
        for semantic_outcome in (
            LcuOutcome.UNKNOWN,
            LcuOutcome.NO_CURRENT_ACTION,
            LcuOutcome.NO_POSITION,
            LcuOutcome.CHAMPION_NOT_FOUND,
            LcuOutcome.ACTION_REJECTED,
            LcuOutcome.NO_SESSION,
            LcuOutcome.MALFORMED_SESSION,
            LcuOutcome.MALFORMED_RESPONSE,
        ):
            with self.subTest(outcome=semantic_outcome):
                self.assertEqual(
                    lcu_loop_action_for(semantic_outcome, context="role"),
                    LcuLoopAction.WAIT_AUTHORITATIVE,
                )

    def test_select_champ_select_champion_patches_and_completes_action(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": False,
                    }
                ]
            ],
        }
        assigned_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 103,
                    }
                ]
            ],
        }
        completed_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": True,
                        "championId": 103,
                    }
                ]
            ],
        }
        grid_champions = [
            {"id": 103, "name": "아리", "alias": "Ahri"},
            {"id": 54, "name": "말파이트", "alias": "Malphite"},
        ]
        session = _FakeSession(
            [
                _FakeResponse(200, champ_select_session),
                _FakeResponse(200, grid_champions),
                _FakeResponse(204),
                _FakeResponse(200, assigned_session),
                _FakeResponse(204),
                _FakeResponse(200, completed_session),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        self.assertTrue(
            client.select_champ_select_champion(
                "아리", action_type="pick", complete=True
            )
        )

        self.assertTrue(
            str(session.calls[0]["url"]).endswith("/lol-champ-select/v1/session")
        )
        self.assertTrue(
            str(session.calls[1]["url"]).endswith(
                "/lol-champ-select/v1/all-grid-champions"
            )
        )
        self.assertEqual(session.calls[2]["method"], "PATCH")
        self.assertTrue(
            str(session.calls[2]["url"]).endswith(
                "/lol-champ-select/v1/session/actions/31"
            )
        )
        self.assertEqual(session.calls[2]["kwargs"]["json"], {"championId": 103})
        self.assertEqual(session.calls[3]["method"], "GET")
        self.assertEqual(session.calls[4]["method"], "POST")
        self.assertTrue(
            str(session.calls[4]["url"]).endswith(
                "/lol-champ-select/v1/session/actions/31/complete"
            )
        )
        self.assertEqual(session.calls[5]["method"], "GET")

    def test_select_champ_select_champion_waits_for_champion_id_before_complete(
        self,
    ) -> None:
        before_patch_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 0,
                    }
                ]
            ],
        }
        still_unassigned_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 0,
                    }
                ]
            ],
        }
        assigned_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 8,
                    }
                ]
            ],
        }
        completed_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": True,
                        "championId": 8,
                    }
                ]
            ],
        }
        session = _FakeSession(
            [
                _FakeResponse(200, before_patch_session),
                _FakeResponse(200, [{"id": 8, "name": "블라디미르", "alias": "Vladimir"}]),
                _FakeResponse(204),
                _FakeResponse(200, still_unassigned_session),
                _FakeResponse(200, assigned_session),
                _FakeResponse(204),
                _FakeResponse(200, completed_session),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        self.assertTrue(
            client.select_champ_select_champion(
                "블라디미르", action_type="ban", complete=True
            )
        )

        self.assertEqual(session.calls[2]["method"], "PATCH")
        self.assertEqual(session.calls[3]["method"], "GET")
        self.assertTrue(
            str(session.calls[3]["url"]).endswith("/lol-champ-select/v1/session")
        )
        self.assertEqual(session.calls[4]["method"], "GET")
        self.assertEqual(session.calls[5]["method"], "POST")
        self.assertEqual(session.calls[6]["method"], "GET")

    def test_select_champ_select_champion_attempts_complete_when_assignment_confirmation_lags(
        self,
    ) -> None:
        before_patch_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 0,
                    }
                ]
            ],
        }
        completed_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": True,
                        "championId": 8,
                    }
                ]
            ],
        }
        session = _FakeSession(
            [
                _FakeResponse(200, before_patch_session),
                _FakeResponse(200, [{"id": 8, "name": "블라디미르", "alias": "Vladimir"}]),
                _FakeResponse(204),
                _FakeResponse(204),
                _FakeResponse(200, completed_session),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        with mock.patch.object(
            client,
            "_wait_for_action_champion",
            return_value=LcuDecision(
                LcuOutcome.ACTION_REJECTED,
                reason="champion assignment confirmation lagged",
            ),
        ):
            result = client.select_champ_select_champion_decision(
                "블라디미르", action_type="ban", complete=True
            )

        self.assertTrue(result.ok)
        self.assertEqual(session.calls[2]["method"], "PATCH")
        self.assertEqual(session.calls[3]["method"], "POST")
        self.assertTrue(
            str(session.calls[3]["url"]).endswith(
                "/lol-champ-select/v1/session/actions/31/complete"
            )
        )
        self.assertEqual(session.calls[4]["method"], "GET")

    def test_select_champ_select_champion_falls_back_to_completed_patch_when_complete_post_rejected(
        self,
    ) -> None:
        before_patch_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 0,
                    }
                ]
            ],
        }
        assigned_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 8,
                    }
                ]
            ],
        }
        completed_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": True,
                        "championId": 8,
                    }
                ]
            ],
        }
        session = _FakeSession(
            [
                _FakeResponse(200, before_patch_session),
                _FakeResponse(200, [{"id": 8, "name": "신 짜오", "alias": "XinZhao"}]),
                _FakeResponse(204),
                _FakeResponse(200, assigned_session),
                _FakeResponse(400, {"message": "complete rejected"}),
                _FakeResponse(204),
                _FakeResponse(200, completed_session),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        result = client.select_champ_select_champion_decision(
            "신 짜오", action_type="ban", complete=True
        )

        self.assertTrue(result.ok)
        self.assertEqual(session.calls[2]["method"], "PATCH")
        self.assertEqual(session.calls[2]["kwargs"]["json"], {"championId": 8})
        self.assertEqual(session.calls[4]["method"], "POST")
        self.assertTrue(
            str(session.calls[4]["url"]).endswith(
                "/lol-champ-select/v1/session/actions/31/complete"
            )
        )
        self.assertEqual(session.calls[5]["method"], "PATCH")
        self.assertEqual(
            session.calls[5]["kwargs"]["json"],
            {"championId": 8, "completed": True},
        )
        self.assertEqual(session.calls[6]["method"], "GET")

    def test_select_champ_select_champion_falls_back_when_complete_post_does_not_mark_completed(
        self,
    ) -> None:
        before_patch_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "ban",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 0,
                    }
                ]
            ],
        }
        completed_action = ChampSelectAction(
            id=31,
            type="ban",
            is_in_progress=True,
            completed=True,
            champion_id=8,
        )
        session = _FakeSession(
            [
                _FakeResponse(200, before_patch_session),
                _FakeResponse(200, [{"id": 8, "name": "신 짜오", "alias": "XinZhao"}]),
                _FakeResponse(204),
                _FakeResponse(204),
                _FakeResponse(204),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        with (
            mock.patch.object(
                client,
                "_wait_for_action_champion",
                return_value=LcuDecision(LcuOutcome.SUCCESS),
            ),
            mock.patch.object(
                client,
                "_wait_for_action_completed",
                side_effect=[
                    LcuDecision(
                        LcuOutcome.ACTION_REJECTED,
                        reason="champ-select action completion not confirmed",
                    ),
                    LcuDecision(LcuOutcome.SUCCESS, value=completed_action),
                ],
            ) as wait_completed,
        ):
            result = client.select_champ_select_champion_decision(
                "신 짜오", action_type="ban", complete=True
            )

        self.assertTrue(result.ok)
        self.assertEqual(wait_completed.call_count, 2)
        self.assertEqual(session.calls[3]["method"], "POST")
        self.assertTrue(
            str(session.calls[3]["url"]).endswith(
                "/lol-champ-select/v1/session/actions/31/complete"
            )
        )
        self.assertEqual(session.calls[4]["method"], "PATCH")
        self.assertEqual(
            session.calls[4]["kwargs"]["json"],
            {"championId": 8, "completed": True},
        )

    def test_select_champ_select_champion_requires_completed_after_complete(
        self,
    ) -> None:
        before_patch_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 0,
                    }
                ]
            ],
        }
        session = _FakeSession(
            [
                _FakeResponse(200, before_patch_session),
                _FakeResponse(200, [{"id": 103, "name": "아리", "alias": "Ahri"}]),
                _FakeResponse(204),
                _FakeResponse(204),
                _FakeResponse(400, {"message": "complete fallback rejected"}),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        with (
            mock.patch.object(
                client,
                "_wait_for_action_champion",
                return_value=LcuDecision(LcuOutcome.SUCCESS),
            ),
            mock.patch.object(
                client,
                "_wait_for_action_completed",
                return_value=LcuDecision(
                    LcuOutcome.ACTION_REJECTED,
                    reason="champ-select action completion not confirmed",
                ),
            ) as wait_completed,
        ):
            result = client.select_champ_select_champion_decision(
                "아리", action_type="pick", complete=True
            )

        self.assertEqual(result.status, LcuOutcome.ACTION_REJECTED)
        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.ok)
        self.assertEqual(wait_completed.call_count, 1)
        self.assertEqual(session.calls[3]["method"], "POST")
        self.assertEqual(session.calls[4]["method"], "PATCH")
        self.assertEqual(
            session.calls[4]["kwargs"]["json"],
            {"championId": 103, "completed": True},
        )

    def test_select_champ_select_champion_preserves_completion_check_request_failure(
        self,
    ) -> None:
        before_patch_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": False,
                        "championId": 0,
                    }
                ]
            ],
        }
        session = _FakeSession(
            [
                _FakeResponse(200, before_patch_session),
                _FakeResponse(200, [{"id": 103, "name": "아리", "alias": "Ahri"}]),
                _FakeResponse(204),
                _FakeResponse(204),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        with (
            mock.patch.object(
                client,
                "_wait_for_action_champion",
                return_value=LcuDecision(LcuOutcome.SUCCESS),
            ),
            mock.patch.object(
                client,
                "_wait_for_action_completed",
                return_value=LcuDecision(
                    LcuOutcome.REQUEST_FAILED,
                    reason="ReadTimeout",
                    error="ReadTimeout",
                ),
            ),
            mock.patch.object(
                client,
                "_patch_completed_champ_select_action",
                return_value=LcuDecision(
                    LcuOutcome.ACTION_REJECTED,
                    reason="fallback patch rejected",
                    status_code=400,
                ),
            ),
        ):
            result = client.select_champ_select_champion_decision(
                "아리", action_type="pick", complete=True
            )

        self.assertEqual(result.status, LcuOutcome.REQUEST_FAILED)
        self.assertEqual(result.error, "ReadTimeout")
        self.assertIn("fallback patch failed", result.reason)

    def test_select_champ_select_champion_can_patch_future_action_without_complete(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": False,
                        "completed": False,
                    }
                ]
            ],
        }
        grid_champions = [{"id": 103, "name": "아리", "alias": "Ahri"}]
        session = _FakeSession(
            [
                _FakeResponse(200, champ_select_session),
                _FakeResponse(200, grid_champions),
                _FakeResponse(204),
            ]
        )
        client = LcuClient(lockfile=self.lockfile, session=session)

        self.assertTrue(
            client.select_champ_select_champion(
                "아리", action_type="pick", complete=False
            )
        )
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[2]["method"], "PATCH")

    def test_select_champ_select_champion_returns_false_for_future_action_when_complete_requested(
        self,
    ) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 7,
                        "type": "pick",
                        "isInProgress": False,
                        "completed": False,
                    }
                ]
            ],
        }
        session = _FakeSession([_FakeResponse(200, champ_select_session)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        self.assertFalse(
            client.select_champ_select_champion(
                "아리", action_type="pick", complete=True
            )
        )
        self.assertEqual(len(session.calls), 1)

    def test_select_champ_select_champion_returns_false_without_local_action(self) -> None:
        champ_select_session = {
            "localPlayerCellId": 7,
            "actions": [
                [
                    {
                        "id": 31,
                        "actorCellId": 8,
                        "type": "pick",
                        "isInProgress": True,
                        "completed": False,
                    }
                ]
            ],
        }
        session = _FakeSession([_FakeResponse(200, champ_select_session)])
        client = LcuClient(lockfile=self.lockfile, session=session)

        self.assertFalse(client.select_champ_select_champion("아리", action_type="pick"))
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
