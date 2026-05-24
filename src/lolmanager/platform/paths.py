from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from lolmanager.platform.runtime import is_frozen


APP_NAME = "LOLManager"


@lru_cache(maxsize=1)
def project_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent

    try:
        return here.parents[2]
    except IndexError:
        try:
            return here.parents[1]
        except IndexError:
            return here.parent


@lru_cache(maxsize=1)
def resource_root() -> Path:
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        base_path = Path(str(base)) if base else Path(sys.executable).resolve().parent

        cand = base_path / "lolmanager" / "resources"
        if cand.exists():
            return cand

        if (base_path / "images").exists() or (base_path / "assets").exists():
            return base_path

        return base_path

    pkg_dir = Path(__file__).resolve().parent
    cand = pkg_dir / "resources"
    if cand.exists():
        return cand
    return project_root()


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)


def images_dir() -> Path:
    return resource_path("images")


def assets_dir() -> Path:
    return resource_path("assets")


@lru_cache(maxsize=1)
def user_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
    return Path.home() / ".lolmanager"


def champion_config_path() -> Path:
    return user_data_dir() / "champion_config.json"


def match_timing_stats_path() -> Path:
    return user_data_dir() / "match_timing_stats.json"
