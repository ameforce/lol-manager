from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from unittest import mock

from bs4 import BeautifulSoup
import pytest

from lolmanager.core.opgg_rank import LeagueAccount, OpggRankClient, parse_opgg_rank


ACCOUNT = LeagueAccount("kr", "Test Player", "KR1")
ASSET = "https://c-lol-web.op.gg/production/_next/static/chunks/1874-test.js"
PUBLIC_ID = "public-account-identifier"


def profile(*, updated: float, tier: str, points: int, name="Test Player#KR1"):
    date = datetime.fromtimestamp(updated, timezone.utc).isoformat()
    graph = {
        "@graph": [
            {"@type": "ProfilePage", "dateModified": date},
            {"@type": "Person", "name": name},
        ]
    }
    action = ["$", "$L75", None, {"puuid": PUBLIC_ID, "initRenewableAt": None}]
    stream = (
        '75:I[4826,["1874","static/chunks/1874-test.js"],"default"]\n9:'
        + json.dumps(action, separators=(",", ":"))
    )
    next_data = json.dumps([1, stream])
    return BeautifulSoup(
        f"""
        <script type="application/ld+json">{json.dumps(graph)}</script>
        <script>self.__next_f.push({next_data})</script>
        <script src="{ASSET}"></script>
        <section><span>Ranked Flex</span><strong>Gold 1</strong><span>50 LP</span></section>
        <section><span>Ranked Solo/Duo</span>
          <div><strong>{tier}</strong><span>{points:,}<!-- --> LP</span></div>
        </section>
    """,
        "lxml",
    )


def test_stale_rank_requests_profile_update_and_reads_new_lp():
    now = time.time()
    client = OpggRankClient(
        mock.Mock(), mock.Mock(is_set=lambda: False, wait=lambda _: False)
    )
    client._profile = mock.Mock(
        side_effect=[
            profile(updated=now - 3600, tier="Platinum 2", points=50),
            profile(updated=now, tier="Platinum 2", points=25),
        ]
    )
    client._refresh_actions = mock.Mock(
        return_value={"renewal": "update", "renewalStatus": "status"}
    )
    client._run_action = mock.Mock(
        side_effect=[{"status": "RENEWING", "delay": 500}, {"status": "RENEWAL_FINISH"}]
    )

    rank = client.lookup(ACCOUNT, now - 180)

    assert rank.label() == "솔로 랭크 · 플래티넘 2 · 25 LP"
    client._refresh_actions.assert_called_once_with([ASSET])
    client._run_action.assert_has_calls(
        [
            mock.call(
                ACCOUNT,
                "update",
                {"region": "kr", "puuid": PUBLIC_ID, "isPremiumPrimary": False},
            ),
            mock.call(ACCOUNT, "status", {"region": "kr", "puuid": PUBLIC_ID}),
        ]
    )
    assert client._profile.call_args == mock.call(ACCOUNT, fresh=True)


def test_opgg_parser_rejects_a_different_player_and_preserves_zero_lp():
    now = time.time()
    rank = parse_opgg_rank(profile(updated=now, tier="Gold 1", points=0), ACCOUNT)
    assert rank.label() == "솔로 랭크 · 골드 1 · 0 LP"
    with pytest.raises(ValueError, match="does not match"):
        parse_opgg_rank(
            profile(updated=now, tier="Master", points=1500, name="Someone Else#KR1"),
            ACCOUNT,
        )
