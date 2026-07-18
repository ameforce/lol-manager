from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=2.0,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _release_context_version() -> str:
    branch = _git_output("branch", "--show-current")
    for prefix in ("hotfix/v", "release/v"):
        if branch.startswith(prefix):
            return branch.removeprefix(prefix)
    if branch:
        return ""

    tag = _git_output("describe", "--tags", "--exact-match", "--match", "v[0-9]*")
    return tag.removeprefix("v") if tag.startswith("v") else ""


def test_project_version_matches_release_context() -> None:
    expected_version = _release_context_version()
    if not expected_version:
        pytest.skip("not on a release or hotfix version context")

    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))

    assert project["project"]["version"] == expected_version
