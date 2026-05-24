from __future__ import annotations

import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lolmanager.core.lcu_client import LcuClient


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


if __name__ == "__main__":
    unittest.main()
