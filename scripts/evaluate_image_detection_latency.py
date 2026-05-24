from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lolmanager.core import image_search


WINDOW_RECT = (0, 0, 640, 360)
PRIMARY_SCENARIO = "stale_roi_miss_sequence"
SECONDARY_REGRESSION_BUDGET = 0.10


def reset_image_search_state() -> None:
    image_search._TEMPLATE_ROI_CACHE.clear()
    image_search._TEMPLATE_MISS_CACHE.clear()
    image_search._TEMPLATE_MATCH_RESULT_CACHE.clear()
    image_search._load_template_gray_cached.cache_clear()
    image_search._LAST_GRAB_RECT = None
    image_search._LAST_GRAB_AT_MONO = 0.0
    image_search._LAST_GRAB_BGRA = None
    image_search._LAST_GRAB_GRAY = None


def template_array(size: int = 72) -> np.ndarray:
    rng = np.random.default_rng(111)
    return rng.integers(0, 255, size=(size, size), dtype=np.uint8)


def screen_with_template(template: np.ndarray, *, x: int = 220, y: int = 120) -> np.ndarray:
    rng = np.random.default_rng(222)
    screen = rng.integers(0, 90, size=(WINDOW_RECT[3], WINDOW_RECT[2]), dtype=np.uint8)
    h, w = template.shape
    screen[y : y + h, x : x + w] = template
    return screen


def screen_without_template() -> np.ndarray:
    rng = np.random.default_rng(333)
    return rng.integers(0, 90, size=(WINDOW_RECT[3], WINDOW_RECT[2]), dtype=np.uint8)


def bgra_from_gray(gray: np.ndarray) -> np.ndarray:
    bgr = np.repeat(gray[:, :, None], 3, axis=2)
    alpha = np.full(gray.shape + (1,), 255, dtype=np.uint8)
    return np.concatenate([bgr, alpha], axis=2)


@contextmanager
def template_file() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "template.png"
        path.write_bytes(b"placeholder")
        yield path


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return float(ordered[index])


def summarize(samples_ms: list[float], match_counts: list[int]) -> dict[str, float]:
    return {
        "median_ms": float(statistics.median(samples_ms)),
        "p95_ms": percentile(samples_ms, 0.95),
        "worst_ms": float(max(samples_ms)),
        "match_calls_median": float(statistics.median(match_counts)),
    }


def measure(
    fn: Callable[[], int], *, repeats: int, warmups: int = 3
) -> dict[str, float]:
    for _ in range(warmups):
        fn()

    samples_ms: list[float] = []
    match_counts: list[int] = []
    for _ in range(repeats):
        start = time.perf_counter()
        match_counts.append(int(fn()))
        samples_ms.append((time.perf_counter() - start) * 1000.0)
    return summarize(samples_ms, match_counts)


def stale_roi_miss_sequence(*, repeats_per_run: int = 8) -> int:
    reset_image_search_state()
    template = template_array()
    hit_screen = screen_with_template(template)
    miss_screen = screen_without_template()
    current = {"gray": hit_screen}
    match_calls = {"count": 0}
    original_match = image_search._match_template_maxloc

    def fake_grab(_rect):
        gray = current["gray"]
        return bgra_from_gray(gray), gray

    def counted_match(search_gray, template_gray):
        match_calls["count"] += 1
        return original_match(search_gray, template_gray)

    with template_file() as path:
        with (
            mock.patch.object(image_search, "_load_template_gray", return_value=template),
            mock.patch.object(image_search, "_grab_region_bgra_and_gray", side_effect=fake_grab),
            mock.patch.object(image_search, "_match_template_maxloc", side_effect=counted_match),
        ):
            image_search.find_template_center(WINDOW_RECT, path, threshold=0.999)
            current["gray"] = miss_screen
            match_calls["count"] = 0
            for _ in range(repeats_per_run):
                image_search.find_template_center(WINDOW_RECT, path, threshold=0.999)

    return match_calls["count"]


def cached_roi_hit_sequence(*, repeats_per_run: int = 8) -> int:
    reset_image_search_state()
    template = template_array()
    screen = screen_with_template(template)
    match_calls = {"count": 0}
    original_match = image_search._match_template_maxloc

    def counted_match(search_gray, template_gray):
        match_calls["count"] += 1
        return original_match(search_gray, template_gray)

    with template_file() as path:
        with (
            mock.patch.object(image_search, "_load_template_gray", return_value=template),
            mock.patch.object(
                image_search,
                "_grab_region_bgra_and_gray",
                return_value=(bgra_from_gray(screen), screen),
            ),
            mock.patch.object(image_search, "_match_template_maxloc", side_effect=counted_match),
        ):
            image_search.find_template_center(WINDOW_RECT, path, threshold=0.999)
            match_calls["count"] = 0
            for _ in range(repeats_per_run):
                image_search.find_template_center(WINDOW_RECT, path, threshold=0.999)

    return match_calls["count"]


def multi_template_search_sequence(*, template_count: int = 8) -> int:
    reset_image_search_state()
    template = template_array()
    screen = screen_with_template(template)
    match_calls = {"count": 0}
    original_match = image_search._match_template_maxloc

    def counted_match(search_gray, template_gray):
        match_calls["count"] += 1
        return original_match(search_gray, template_gray)

    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for idx in range(template_count):
            path = Path(tmp) / f"template-{idx}.png"
            path.write_bytes(b"placeholder")
            paths.append((f"template-{idx}", path))
        with (
            mock.patch.object(image_search, "_load_template_gray", return_value=template),
            mock.patch.object(
                image_search,
                "_grab_region_bgra_and_gray",
                return_value=(bgra_from_gray(screen), screen),
            ),
            mock.patch.object(image_search, "_match_template_maxloc", side_effect=counted_match),
        ):
            image_search.find_template_matches_once(WINDOW_RECT, paths, threshold=0.999)

    return match_calls["count"]


def run_benchmarks(*, repeats: int) -> dict[str, object]:
    scenarios: dict[str, Callable[[], int]] = {
        "stale_roi_miss_sequence": stale_roi_miss_sequence,
        "cached_roi_hit_sequence": cached_roi_hit_sequence,
        "multi_template_search_sequence": multi_template_search_sequence,
    }
    return {
        "version": 1,
        "primary": PRIMARY_SCENARIO,
        "secondary_regression_budget": SECONDARY_REGRESSION_BUDGET,
        "scenarios": {
            name: measure(fn, repeats=repeats)
            for name, fn in scenarios.items()
        },
    }


def compare_metrics(baseline: dict[str, object], current: dict[str, object]) -> tuple[bool, list[str]]:
    messages: list[str] = []
    base_scenarios = baseline["scenarios"]
    current_scenarios = current["scenarios"]

    base_primary = base_scenarios[PRIMARY_SCENARIO]
    current_primary = current_scenarios[PRIMARY_SCENARIO]
    median_improved = current_primary["median_ms"] < base_primary["median_ms"]
    p95_improved = current_primary["p95_ms"] < base_primary["p95_ms"]
    calls_improved = current_primary["match_calls_median"] < base_primary["match_calls_median"]
    if median_improved or p95_improved:
        messages.append(
            f"PASS primary latency improved: baseline median={base_primary['median_ms']:.3f}ms "
            f"p95={base_primary['p95_ms']:.3f}ms; current median={current_primary['median_ms']:.3f}ms "
            f"p95={current_primary['p95_ms']:.3f}ms"
        )
    else:
        messages.append(
            f"FAIL primary latency did not improve: baseline median={base_primary['median_ms']:.3f}ms "
            f"p95={base_primary['p95_ms']:.3f}ms; current median={current_primary['median_ms']:.3f}ms "
            f"p95={current_primary['p95_ms']:.3f}ms"
        )
    if calls_improved:
        messages.append(
            f"Primary match calls improved: baseline={base_primary['match_calls_median']:.1f}, "
            f"current={current_primary['match_calls_median']:.1f}"
        )

    secondary_ok = True
    for name, base in base_scenarios.items():
        if name == PRIMARY_SCENARIO:
            continue
        curr = current_scenarios[name]
        limit = float(base["median_ms"]) * (1.0 + SECONDARY_REGRESSION_BUDGET)
        if float(curr["median_ms"]) > limit:
            secondary_ok = False
            messages.append(
                f"FAIL secondary regression {name}: baseline median={base['median_ms']:.3f}ms, "
                f"current median={curr['median_ms']:.3f}ms, limit={limit:.3f}ms"
            )
        else:
            messages.append(
                f"PASS secondary {name}: baseline median={base['median_ms']:.3f}ms, "
                f"current median={curr['median_ms']:.3f}ms"
            )

    return bool((median_improved or p95_improved) and secondary_ok), messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", default="")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--repeats", type=int, default=25)
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline)
    current_path = Path(args.current) if args.current else None

    metrics = run_benchmarks(repeats=max(5, int(args.repeats)))

    if args.write_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        print(f"Wrote baseline: {baseline_path}")
        print(json.dumps(metrics, indent=2))
        return 0

    if current_path is not None:
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    if not baseline_path.exists():
        print(f"Missing baseline file: {baseline_path}", file=sys.stderr)
        return 2

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    ok, messages = compare_metrics(baseline, metrics)
    for message in messages:
        print(message)
    print(json.dumps(metrics, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
