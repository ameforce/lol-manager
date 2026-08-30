from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_double_click_shim_delegates_without_duplicating_build_logic() -> None:
    script = (PROJECT_ROOT / "build_and_install.bat").read_text("utf-8")

    assert "scripts\\build_and_install.ps1" in script
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File" in script
    assert "LOLMANAGER_NO_PAUSE" in script
    assert "pause" in script.lower()
    assert "does not accept arguments" in script
    assert "pyinstaller" not in script.lower()
    assert "iscc.exe" not in script.lower()


def test_double_click_shim_can_be_tested_without_pausing_or_building() -> None:
    environment = os.environ | {"LOLMANAGER_NO_PAUSE": "1"}
    result = subprocess.run(
        ["cmd", "/c", str(PROJECT_ROOT / "build_and_install.bat"), "unexpected"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert b"does not accept arguments" in result.stdout
    assert result.stderr == b""


def test_build_and_install_helper_uses_canonical_builder_and_reads_back_install() -> None:
    script_path = PROJECT_ROOT / "scripts" / "build_and_install.ps1"
    script = script_path.read_text("utf-8-sig")

    assert "build_release.ps1" in script
    assert "& $releaseBuilder -Version $version -InnoCompiler $innoCompiler" in script
    assert "build_and_install_last.log" in script
    assert "Get-ProjectVersion" in script
    assert "Get-Command uv" in script
    assert "Find-InnoCompiler" in script
    assert "winget install" not in script.lower()
    assert "choco install" not in script.lower()
    assert "'/VERYSILENT'" in script
    assert "'/TASKS=desktopicon'" in script
    assert "Start-Process -FilePath $setupPath" in script
    assert "$env:LOCALAPPDATA 'Programs\\LOLManager'" in script
    assert "VersionInfo.FileVersion" in script
    assert "Assert-ShortcutTarget" in script
    assert "DesktopDirectory" in script
    assert "SpecialFolder]::Programs" in script
    assert "installed EXE" in script
    assert "*>&1" not in script
    assert "--onefile" not in script
    assert "--version-file" not in script
    assert script_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert (PROJECT_ROOT / "scripts" / "build_release.ps1").read_bytes().startswith(
        b"\xef\xbb\xbf"
    )


def test_release_workflow_is_tag_only_pinned_and_publishes_all_artifacts() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text("utf-8")

    assert re.search(r"^\s*tags:\s*$", workflow, re.MULTILINE)
    assert '"v*.*.*"' in workflow
    assert "^v(?<version>\\d+\\.\\d+\\.\\d+)$" in workflow
    assert "contents: write" in workflow
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in workflow
    assert "astral-sh/setup-uv@d0d8abe699bfb85fec6de9f7adb5ae17292296ff" in workflow
    assert "persist-credentials: false" in workflow
    assert "LOLMANAGER_RELEASE_TAG: ${{ github.ref_name }}" in workflow
    assert "$tag = $env:LOLMANAGER_RELEASE_TAG" in workflow
    assert "$tag = '${{ github.ref_name }}'" not in workflow
    assert "uv lock --check" in workflow
    assert "uv run --group dev python -m pytest -q" in workflow
    assert "uv build" in workflow
    assert "scripts\\build_release.ps1" in workflow
    assert '"dist\\release\\LOLManager-v$env:LOLMANAGER_VERSION.exe"' in workflow
    assert '"dist\\release\\LOLManager-Setup-v$env:LOLMANAGER_VERSION.exe"' in workflow
    assert "'dist\\release\\SHA256SUMS.txt'" in workflow
    assert "gh release create" in workflow
    assert "--verify-tag" in workflow
    assert "GITHUB_REPOSITORY" in workflow
    assert "*>" not in workflow
    assert "--clobber" not in workflow
    assert "required assets are already published" in workflow


def test_installer_verifier_derives_its_default_from_project_metadata() -> None:
    script_path = PROJECT_ROOT / "scripts" / "verify_installer.ps1"
    script = script_path.read_text("utf-8-sig")

    assert "1.1.12" not in script
    assert "pyproject.toml" in script
    assert "if (-not $Version)" in script
    assert "versionMatch" in script
    assert script_path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_project_and_lock_versions_are_bumped_together() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text("utf-8")
    lockfile = (PROJECT_ROOT / "uv.lock").read_text("utf-8")

    assert 'version = "1.1.27"' in pyproject
    package_match = re.search(
        r'(?ms)^\[\[package\]\]\nname = "lolmanager"\nversion = "(?P<version>[^"]+)"',
        lockfile,
    )
    assert package_match
    assert package_match.group("version") == "1.1.27"
