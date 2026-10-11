from __future__ import annotations

import json
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lolmanager.core import opgg_counter_recommendations as cache
from lolmanager.platform.file_storage import file_transaction


def _save_process_cache(path: str, role: str, ready, start) -> None:
    original = cache._read_cache_payload

    def slow_read(target):
        payload = original(target)
        time.sleep(0.05)  # Keep the read/modify/write windows overlapping.
        return payload

    cache._read_cache_payload = slow_read
    ready.put(role)
    assert start.wait(10)
    for index in range(3):
        cache.save_recommendation_cache(
            Path(path), role=role, configured_pick=str(index), recommendations=[],
        )


def test_simultaneous_processes_preserve_each_recommendation_entry(tmp_path):
    path = tmp_path / "recommendations.json"
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    processes = [context.Process(target=_save_process_cache, args=(str(path), role, ready, start))
                 for role in ("top", "mid")]
    try:
        for process in processes:
            process.start()
        assert {ready.get(timeout=15), ready.get(timeout=15)} == {"top", "mid"}
        start.set()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        ready.close()
    entries = json.loads(path.read_text("utf-8"))["entries"]
    assert {entry["key"] for entry in entries} == {
        f"{role}::{index}" for role in ("top", "mid") for index in range(3)
    }


def test_simultaneous_threads_preserve_each_entry(tmp_path, monkeypatch):
    path = tmp_path / "recommendations.json"
    original = cache._read_cache_payload

    def slow_read(target):
        payload = original(target)
        time.sleep(0.03)
        return payload

    monkeypatch.setattr(cache, "_read_cache_payload", slow_read)
    def save(index):
        cache.save_recommendation_cache(path, role="mid", configured_pick=str(index), recommendations=[])
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(save, range(8)))
    assert len(json.loads(path.read_text("utf-8"))["entries"]) == 8


def test_failed_transaction_releases_the_lock(tmp_path):
    path = tmp_path / "cache.json"
    with pytest.raises(ValueError):
        with file_transaction(path):
            raise ValueError("write failed")
    with file_transaction(path):
        path.write_text("recovered", encoding="utf-8")
    assert path.read_text("utf-8") == "recovered"
