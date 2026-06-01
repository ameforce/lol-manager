from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock

from lolmanager.core.app_version import (
    DEFAULT_APP_VERSION,
    find_git_repo_root,
    format_app_version_label,
    get_app_version,
    version_from_git_describe,
)


def test_version_from_git_describe_uses_tag_and_commit_count() -> None:
    assert version_from_git_describe("v1.0.3-1-ga7229ab") == "1.0.3.1"


def test_version_from_git_describe_exact_tag_uses_zero_count() -> None:
    assert version_from_git_describe("v1.0.3-0-ga7229ab") == "1.0.3.0"
    assert version_from_git_describe("v1.0.3") == "1.0.3.0"


def test_version_from_git_describe_dirty_build_advances_count() -> None:
    assert version_from_git_describe("v1.0.3-1-ga7229ab-dirty") == "1.0.3.2"


def test_version_from_git_describe_rejects_non_semver_tag() -> None:
    assert version_from_git_describe("release-2026-05-31") == DEFAULT_APP_VERSION


def test_format_app_version_label_prefixes_v() -> None:
    assert format_app_version_label("1.0.3.1") == "v1.0.3.1"


def test_find_git_repo_root_checks_candidate_parents(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()

    assert find_git_repo_root([nested]) == repo


def test_find_git_repo_root_handles_missing_candidates() -> None:
    with mock.patch.object(Path, "exists", return_value=False):
        assert find_git_repo_root([Path("missing")]) is None


def test_get_app_version_uses_embedded_build_version_without_git(
    monkeypatch,
    tmp_path: Path,
) -> None:
    embedded = types.ModuleType("lolmanager.core._build_version")
    embedded.BUILD_VERSION = "1.2.3.4"
    monkeypatch.setitem(sys.modules, "lolmanager.core._build_version", embedded)
    monkeypatch.delenv("LOLMANAGER_VERSION", raising=False)

    assert get_app_version(repo_root=tmp_path) == "1.2.3.4"


def test_get_app_version_ignores_invalid_embedded_build_version(
    monkeypatch,
    tmp_path: Path,
) -> None:
    embedded = types.ModuleType("lolmanager.core._build_version")
    embedded.BUILD_VERSION = "not-a-version"
    monkeypatch.setitem(sys.modules, "lolmanager.core._build_version", embedded)
    monkeypatch.delenv("LOLMANAGER_VERSION", raising=False)

    assert get_app_version(repo_root=tmp_path) == DEFAULT_APP_VERSION
