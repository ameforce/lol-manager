from __future__ import annotations

import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lolmanager.core.lcu_client import (
    ChampSelectAction,
    LcuClient,
    LcuDecision,
    LcuLoopAction,
    LcuOutcome,
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

    def test_gameflow_phase_is_cached(self) -> None:
        session = _FakeSession([_FakeResponse(200, "Matchmaking")])
        client = LcuClient(lockfile=self.lockfile, session=session)

        self.assertEqual(client.get_gameflow_phase(max_age_sec=60), "Matchmaking")
        self.assertEqual(client.get_gameflow_phase(max_age_sec=60), "Matchmaking")

        self.assertEqual(len(session.calls), 1)
        self.assertTrue(
            str(session.calls[0]["url"]).endswith("/lol-gameflow/v1/gameflow-phase")
        )

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
            lcu_loop_action_for(LcuOutcome.MALFORMED_SESSION, context="role"),
            LcuLoopAction.FALLBACK_IMAGE,
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
