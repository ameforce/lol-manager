from lolmanager.core.champion_fetcher import sort_counter_candidates_by_role_rank


def test_sort_counter_candidates_prioritizes_ranked_opgg_counters() -> None:
    counters = ["가렌", "퀸", "말파이트", "다리우스"]
    ranked_entries = [
        ("다리우스", ("1티어", "blue"), "/ko/lol/champions/darius/build/top"),
        ("퀸", ("2티어", "green"), "/ko/lol/champions/quinn/build/top"),
        ("가렌", ("3티어", "yellow"), "/ko/lol/champions/garen/build/top"),
    ]

    assert sort_counter_candidates_by_role_rank(counters, ranked_entries) == [
        "다리우스",
        "퀸",
        "가렌",
        "말파이트",
    ]


def test_sort_counter_candidates_falls_back_to_counter_order_without_rank() -> None:
    counters = [" 말파이트 ", "가렌", "말파이트", "퀸"]

    assert sort_counter_candidates_by_role_rank(counters, []) == [
        "말파이트",
        "가렌",
        "퀸",
    ]
