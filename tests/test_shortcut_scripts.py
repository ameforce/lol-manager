from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _dry_run_paths(script_name: str) -> dict[str, str]:
    result = subprocess.run(
        ["cmd", "/c", str(ROOT / "scripts" / script_name), "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    paths: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("[DRY-RUN] "):
            continue
        key, value = line[len("[DRY-RUN] ") :].split("=", maxsplit=1)
        paths[key] = value
    return paths


def test_shortcut_install_and_uninstall_dry_run_resolve_same_paths() -> None:
    install_paths = _dry_run_paths("install_shortcuts.bat")
    uninstall_paths = _dry_run_paths("uninstall_shortcuts.bat")

    assert install_paths["LM_DESKTOP_LNK"] == uninstall_paths["LM_DESKTOP_LNK"]
    assert install_paths["LM_START_DIR"] == uninstall_paths["LM_START_DIR"]
    assert install_paths["LM_START_LNK"] == uninstall_paths["LM_START_LNK"]
