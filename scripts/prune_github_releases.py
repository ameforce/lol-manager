"""Keep only the newest complete stable GitHub Release, preserving Git tags."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Sequence
from typing import Any

Release = dict[str, Any]
VERSION_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def gh(*arguments: str) -> str:
    result = subprocess.run(
        ["gh", *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def list_releases(repo: str) -> list[Release]:
    pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"))
    return [release for page in pages for release in page]


def require_complete_release(release: Release) -> None:
    tag = release["tag_name"]
    required = {f"LOLManager-{tag}.exe", f"LOLManager-Setup-{tag}.exe", "SHA256SUMS.txt"}
    uploaded = {
        asset["name"]
        for asset in release.get("assets", [])
        if asset.get("state") == "uploaded" and asset.get("size", 0) > 0
    }
    if release["draft"] or release["prerelease"] or not release.get("published_at"):
        raise RuntimeError(f"Release {tag} is not a published stable release; nothing was deleted.")
    if missing := required - uploaded:
        raise RuntimeError(f"Release {tag} has missing/incomplete assets: {sorted(missing)}; nothing was deleted.")


def retention_plan(
    releases: Sequence[Release], *, published_tag: str | None = None
) -> tuple[Release, list[Release]]:
    if published_tag is not None:
        published = next((release for release in releases if release["tag_name"] == published_tag), None)
        if published is None:
            raise RuntimeError(f"Release {published_tag} was not published; nothing was deleted.")
        require_complete_release(published)

    candidates = [
        (tuple(map(int, match.groups())), release)
        for release in releases
        if not release["draft"] and not release["prerelease"] and release.get("published_at")
        if (match := VERSION_TAG.fullmatch(release["tag_name"]))
    ]
    if not candidates:
        raise RuntimeError("No stable semantic-version release exists; nothing was deleted.")
    # An old tag's workflow may finish later than a newer tag's workflow.
    # Never let completion order downgrade the release we retain.
    _, latest = max(candidates, key=lambda item: item[0])
    require_complete_release(latest)
    obsolete = [release for release in releases if not release["draft"] and release["id"] != latest["id"]]
    return latest, obsolete


def prune_releases(repo: str, *, published_tag: str | None = None, dry_run: bool = False) -> None:
    latest, obsolete = retention_plan(list_releases(repo), published_tag=published_tag)
    print(f"Keep {latest['tag_name']}; remove {len(obsolete)} older published release(s). Git tags are preserved.")
    for release in obsolete:
        print(f"  {'Would delete' if dry_run else 'Planned deletion'}: {release['tag_name']}")
    if dry_run:
        return

    gh("api", "--method", "PATCH", f"repos/{repo}/releases/{latest['id']}", "-f", "make_latest=true")
    current_latest = json.loads(gh("api", f"repos/{repo}/releases/latest"))
    if current_latest["id"] != latest["id"]:
        raise RuntimeError("Latest release changed during cleanup; nothing was deleted.")
    for release in obsolete:
        # Delete release records/assets by ID, never Git refs or tags.
        gh("api", "--method", "DELETE", f"repos/{repo}/releases/{release['id']}")
        print(f"Deleted {release['tag_name']}.")

    remaining = [release for release in list_releases(repo) if not release["draft"]]
    if [release["id"] for release in remaining] != [latest["id"]]:
        raise RuntimeError("Release cleanup did not leave exactly one published release; rerun cleanup.")
    print(f"Verified: only {latest['tag_name']} remains published.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub owner/repository")
    parser.add_argument("--published-tag", help="Require this just-published release to be complete first")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    prune_releases(args.repo, published_tag=args.published_tag, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
