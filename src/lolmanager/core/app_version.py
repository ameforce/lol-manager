from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Optional


DEFAULT_APP_VERSION = "0.0.0.0"
_DESCRIBE_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<count>\d+)-g[0-9a-fA-F]+)?(?P<dirty>-dirty)?$"
)
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def version_from_git_describe(describe: object) -> str:
    text = str(describe or "").strip()
    match = _DESCRIBE_RE.match(text)
    if not match:
        return DEFAULT_APP_VERSION

    count = int(match.group("count") or "0")
    if match.group("dirty"):
        count += 1

    return (
        f"{match.group('major')}.{match.group('minor')}."
        f"{match.group('patch')}.{count}"
    )


def format_app_version_label(version: object) -> str:
    text = str(version or DEFAULT_APP_VERSION).strip()
    if not _VERSION_RE.match(text):
        text = DEFAULT_APP_VERSION
    return f"v{text}"


def find_git_repo_root(candidates: Iterable[Path]) -> Optional[Path]:
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            path = Path(candidate).resolve()
        except OSError:
            path = Path(candidate)

        try:
            if path.exists() and path.is_file():
                path = path.parent
        except OSError:
            continue

        for current in (path, *path.parents):
            if current in seen:
                continue
            seen.add(current)
            try:
                if (current / ".git").exists():
                    return current
            except OSError:
                continue
    return None


def _runtime_git_candidates() -> list[Path]:
    candidates = [Path.cwd(), Path(__file__).resolve()]
    executable = getattr(sys, "executable", "")
    if executable:
        candidates.insert(0, Path(executable))
    return candidates


def get_app_version(repo_root: Optional[Path] = None) -> str:
    env_version = os.environ.get("LOLMANAGER_VERSION", "").strip()
    if _VERSION_RE.match(env_version):
        return env_version

    root = repo_root or find_git_repo_root(_runtime_git_candidates())
    if root is None:
        return DEFAULT_APP_VERSION

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "describe",
                "--tags",
                "--long",
                "--dirty",
                "--match",
                "v[0-9]*",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return DEFAULT_APP_VERSION

    if result.returncode != 0:
        return DEFAULT_APP_VERSION
    return version_from_git_describe(result.stdout)
