from __future__ import annotations

import json
import subprocess
from copy import deepcopy

import pytest

from scripts import prune_github_releases as retention


def release(tag: str, release_id: int, **changes: object) -> dict:
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-11T10:00:00Z",
        "assets": [
            {"name": name, "state": "uploaded", "size": 100}
            for name in (f"LOLManager-{tag}.exe", f"LOLManager-Setup-{tag}.exe", "SHA256SUMS.txt")
        ],
        **changes,
    }


class FakeGitHub:
    def __init__(self, releases: list[dict]) -> None:
        self.releases = deepcopy(releases)
        self.latest_id: int | None = None
        self.calls: list[tuple[str, ...]] = []
        self.failed_delete_id: int | None = None

    def __call__(self, *arguments: str) -> str:
        self.calls.append(arguments)
        if arguments == ("api", "--paginate", "--slurp", "repos/owner/repo/releases?per_page=100"):
            # Multiple pages, including an empty trailing page.
            return json.dumps([self.releases[:1], self.releases[1:], []])
        if arguments == ("api", "repos/owner/repo/releases/latest"):
            return json.dumps(next(item for item in self.releases if item["id"] == self.latest_id))
        if arguments[:3] == ("api", "--method", "PATCH"):
            assert arguments[4:] == ("-f", "make_latest=true")
            self.latest_id = int(arguments[3].rsplit("/", 1)[1])
            return "{}"
        if arguments[:3] == ("api", "--method", "DELETE"):
            assert arguments[3].startswith("repos/owner/repo/releases/")
            release_id = int(arguments[3].rsplit("/", 1)[1])
            if release_id == self.failed_delete_id:
                raise subprocess.CalledProcessError(1, ["gh", *arguments])
            assert release_id != self.latest_id
            self.releases = [item for item in self.releases if item["id"] != release_id]
            return ""
        raise AssertionError(f"Unexpected command: {arguments}")


def test_old_tag_rerun_keeps_highest_numeric_version_and_does_not_delete_drafts(monkeypatch) -> None:
    github = FakeGitHub([
        release("v1.9.9", 9),
        release("v1.10.0", 10),
        release("v2.0.0", 20, draft=True),
        release("v1.8.0", 8),
        release("v1.10.0-rc1", 11, prerelease=True),
    ])
    monkeypatch.setattr(retention, "gh", github)

    retention.prune_releases("owner/repo", published_tag="v1.9.9")

    assert github.latest_id == 10
    assert {item["id"] for item in github.releases} == {10, 20}
    assert [call[3] for call in github.calls if "DELETE" in call] == [
        "repos/owner/repo/releases/9",
        "repos/owner/repo/releases/8",
        "repos/owner/repo/releases/11",
    ]


@pytest.mark.parametrize("asset_changes", [
    {"state": "starter"},
    {"size": 0},
    {"name": "unexpected.exe"},
])
def test_incomplete_newest_release_never_deletes_previous_release(monkeypatch, asset_changes) -> None:
    newest = release("v1.2.0", 2)
    newest["assets"][1].update(asset_changes)
    github = FakeGitHub([newest, release("v1.1.0", 1)])
    monkeypatch.setattr(retention, "gh", github)

    with pytest.raises(RuntimeError, match="missing/incomplete"):
        retention.prune_releases("owner/repo", published_tag="v1.1.0")

    assert len(github.calls) == 1


@pytest.mark.parametrize("releases", [
    [],
    [release("v1.1.0", 1)],
    [release("v1.2.0", 2, draft=True)],
    [release("v1.2.0", 2, prerelease=True)],
    [release("v1.2.0", 2, published_at=None)],
    [release("v1.2.0", 2, assets=[])],
])
def test_failed_or_unpublished_current_release_blocks_all_mutations(monkeypatch, releases) -> None:
    github = FakeGitHub(releases)
    monkeypatch.setattr(retention, "gh", github)

    with pytest.raises(RuntimeError):
        retention.prune_releases("owner/repo", published_tag="v1.2.0")

    assert len(github.calls) == 1


def test_no_stable_semver_release_cannot_trigger_cleanup(monkeypatch) -> None:
    github = FakeGitHub([release("nightly", 1), release("v1.0.0-rc1", 2, prerelease=True)])
    monkeypatch.setattr(retention, "gh", github)

    with pytest.raises(RuntimeError, match="No stable"):
        retention.prune_releases("owner/repo")

    assert len(github.calls) == 1


def test_dry_run_lists_all_pages_without_mutating_releases(monkeypatch, capsys) -> None:
    github = FakeGitHub([release("v1.0.0", 1), release("v1.1.0", 2)])
    monkeypatch.setattr(retention, "gh", github)

    retention.prune_releases("owner/repo", dry_run=True)

    assert len(github.calls) == 1
    assert "Keep v1.1.0" in capsys.readouterr().out


def test_latest_marker_must_be_confirmed_before_deleting(monkeypatch) -> None:
    github = FakeGitHub([release("v1.1.0", 2), release("v1.0.0", 1)])

    def changed_latest(*arguments: str) -> str:
        if arguments == ("api", "repos/owner/repo/releases/latest"):
            return json.dumps(release("v1.2.0", 3))
        return github(*arguments)

    monkeypatch.setattr(retention, "gh", changed_latest)
    with pytest.raises(RuntimeError, match="Latest release changed"):
        retention.prune_releases("owner/repo")

    assert not any("DELETE" in call for call in github.calls)


def test_delete_failure_stops_cleanup_and_can_be_retried(monkeypatch) -> None:
    github = FakeGitHub([release("v1.3.0", 3), release("v1.0.0", 1), release("v1.1.0", 2)])
    github.failed_delete_id = 2
    monkeypatch.setattr(retention, "gh", github)

    with pytest.raises(subprocess.CalledProcessError):
        retention.prune_releases("owner/repo")
    assert {item["id"] for item in github.releases} == {2, 3}

    github.failed_delete_id = None
    retention.prune_releases("owner/repo")
    assert [item["id"] for item in github.releases] == [3]


def test_retention_verifies_that_no_other_release_remains(monkeypatch) -> None:
    github = FakeGitHub([release("v1.1.0", 2), release("v1.0.0", 1)])

    def ignored_delete(*arguments: str) -> str:
        if "DELETE" in arguments:
            return ""
        return github(*arguments)

    monkeypatch.setattr(retention, "gh", ignored_delete)
    with pytest.raises(RuntimeError, match="exactly one"):
        retention.prune_releases("owner/repo")
