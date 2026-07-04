# LOLManager Agent Rules

## Release Version Rule

- When working on `hotfix/vX.Y.Z`, `release/vX.Y.Z`, or an exact `vX.Y.Z` tag, keep `pyproject.toml` `[project].version` equal to `X.Y.Z`.
- If `uv.lock` contains the editable `lolmanager` package entry, keep that package version equal to the same `X.Y.Z`.
- Before closing a release or hotfix branch, run `uv run --group dev python -m pytest -q` and `uv build`; the build artifacts must use the same `X.Y.Z` package version.
- Do not close or tag a release when the Git release version, app-visible version, and Python package metadata disagree.

## Build Branch Rule

- Unless the task is explicitly validating a release/hotfix branch or another non-main branch-specific artifact, run package builds such as `uv build` from `main`.
- If an ordinary build is requested while on a feature, release, or hotfix branch, switch back to `main` first or build after the branch has been merged to `main`.

## Hotfix Scope Rule

- Treat bugfix and application improvement work as hotfix work by default, even when the user does not explicitly say "hotfix".
- For those default-hotfix tasks, create or use `hotfix/vX.Y.Z` as the integration branch, but land implementation changes through `fix/*` branches that target `hotfix/vX.Y.Z` by PR before finishing the hotfix.
- Repo-local hotfix rules do not waive the global PR/review gate. If a hotfix needs emergency direct commits, explicitly record the direct-hotfix exception and review evidence before merging to `main`.
- Treat a user request to "hotfix" as: prepare `hotfix/vX.Y.Z`, land the reviewed `fix/*` PR changes into it, finish the hotfix through `main` + annotated `vX.Y.Z` tag + `develop`, push `main`, `develop`, and the tag, then run the final package build from `main`.
- If the requested or inferred hotfix version tag already exists, move to the next patch version instead of reusing or moving an existing tag.

## Hotfix Completion Procedure

- Before creating the hotfix branch, run `git fetch --all --tags --prune`, verify `main`, `origin/main`, `develop`, `origin/develop`, existing `v*` tags, and worktree cleanliness.
- Choose the hotfix version from the requested version or the current package version. If the matching `vX.Y.Z` tag already exists, increment to the next patch version.
- Create or use `hotfix/vX.Y.Z`; before final hotfix validation, update `pyproject.toml` and the editable `lolmanager` entry in `uv.lock` to `X.Y.Z`.
- Land code, test, docs, and metadata changes into `hotfix/vX.Y.Z` through reviewed `fix/*` PRs unless an emergency direct-hotfix exception has been explicitly recorded.
- After the reviewed changes are present on `hotfix/vX.Y.Z`, run `uv lock --check`, `uv run --group dev python -m pytest -q`, and `uv build` on the hotfix branch.
- For normal `<type>: <subject>` commits on `fix/*` or documented direct-hotfix branches, write the subject in Korean unless there is a necessary reason to use another language. Keep prescribed merge and tag messages exactly as documented.
- Finish the hotfix with Git-flow style merge commits:
  - Merge the hotfix into `main` with `git merge --no-ff hotfix/vX.Y.Z -m "Merge branch 'hotfix/vX.Y.Z'"`.
  - Create an annotated tag on the main merge commit with `git tag -a vX.Y.Z -m "Version X.Y.Z"`.
  - Merge the tag into `develop` with `git merge --no-ff vX.Y.Z -m "Merge tag 'vX.Y.Z' into develop"`.
- Verify the release graph before pushing:
  - `git rev-list --parents -n 1 main` has two parents.
  - `git rev-list --parents -n 1 develop` has two parents.
  - `git rev-parse 'vX.Y.Z^{}'` equals `git rev-parse main`.
- Push `main`, `develop`, and `vX.Y.Z` together. After push, verify `git ls-remote --heads origin main develop` and `git ls-remote --tags origin vX.Y.Z`.
- Run the final `uv build` from `main` after the merge and push; report the generated `dist/lolmanager-X.Y.Z*` artifacts.
- After a successful hotfix finish, delete the local `hotfix/vX.Y.Z` branch if it is no longer checked out in any worktree and no remote hotfix branch needs deletion.
