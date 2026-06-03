# LOLManager Agent Rules

## Release Version Rule

- When working on `hotfix/vX.Y.Z`, `release/vX.Y.Z`, or an exact `vX.Y.Z` tag, keep `pyproject.toml` `[project].version` equal to `X.Y.Z`.
- Before closing a release or hotfix branch, run `uv run --group dev python -m pytest -q` and `uv build`; the build artifacts must use the same `X.Y.Z` package version.
- Do not close or tag a release when the Git release version, app-visible version, and Python package metadata disagree.

## Build Branch Rule

- Unless the task is explicitly validating a release/hotfix branch or another non-main branch-specific artifact, run package builds such as `uv build` from `main`.
- If an ordinary build is requested while on a feature, release, or hotfix branch, switch back to `main` first or build after the branch has been merged to `main`.
