from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lolmanager.core.match_timing import append_match_duration, load_match_timing_stats


def test_parallel_match_records_are_not_lost(tmp_path: Path) -> None:
    path = tmp_path / "match_timing_stats.json"
    durations = [60.0 + index for index in range(12)]

    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(lambda value: append_match_duration(value, path=path), durations))

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(saved["durations_sec"]) == durations


def test_match_timing_loader_ignores_valid_json_with_wrong_root_shape(
    tmp_path: Path,
) -> None:
    path = tmp_path / "match_timing_stats.json"
    path.write_text("[]", encoding="utf-8")

    assert load_match_timing_stats(path) == ([], None)
