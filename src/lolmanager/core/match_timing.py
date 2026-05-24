from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional, Tuple

from lolmanager.platform.paths import match_timing_stats_path


STATS_VERSION = 1
DEFAULT_MAX_SAMPLES = 60


MIN_DURATION_SEC = 5.0
MAX_DURATION_SEC = 60.0 * 60.0


def format_duration_mmss(seconds: float) -> str:
    try:
        total = int(round(float(seconds)))
    except Exception:
        total = 0
    if total < 0:
        total = 0
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _avg_sec(durations: list[float]) -> Optional[float]:
    if not durations:
        return None

    total = 0.0
    for x in durations:
        total += float(x)
    return total / float(len(durations))


def load_match_timing_stats(
    path: Optional[Path] = None,
) -> Tuple[list[float], Optional[float]]:
    p = match_timing_stats_path() if path is None else path
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return ([], None)

    try:
        data = json.loads(raw)
    except Exception:
        return ([], None)

    durations_raw = data.get("durations_sec", [])
    if not isinstance(durations_raw, list):
        return ([], None)

    out: list[float] = []
    for v in durations_raw:
        try:
            f = float(v)
        except Exception:
            continue
        if not (MIN_DURATION_SEC <= f <= MAX_DURATION_SEC):
            continue
        out.append(f)
    return (out, _avg_sec(out))


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)


def append_match_duration(
    duration_sec: float,
    *,
    path: Optional[Path] = None,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> Tuple[list[float], Optional[float]]:
    p = match_timing_stats_path() if path is None else path
    durations, _avg = load_match_timing_stats(p)

    try:
        d = float(duration_sec)
    except Exception:
        return (durations, _avg)
    if not (MIN_DURATION_SEC <= d <= MAX_DURATION_SEC):
        return (durations, _avg)

    durations.append(d)
    if max_samples > 0 and len(durations) > int(max_samples):
        durations = durations[-int(max_samples) :]

    payload = {
        "version": STATS_VERSION,
        "durations_sec": durations,
        "updated_at_unix": int(time.time()),
    }
    try:
        _atomic_write_json(p, payload)
    except Exception:
        return (durations, _avg_sec(durations))

    return (durations, _avg_sec(durations))
