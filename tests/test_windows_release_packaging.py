from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_inno_installer_keeps_stable_per_user_contract() -> None:
    script = (PROJECT_ROOT / "installer" / "LOLManager.iss").read_text("utf-8")

    assert "AppId={{F1E18E34-A5B3-4DE8-8E91-74DC33D66D15}" in script
    assert r"DefaultDirName={localappdata}\Programs\{#MyAppName}" in script
    assert "PrivilegesRequired=lowest" in script
    assert "CloseApplications=no" in script
    assert "CloseApplicationsFilter={#MyAppExeName}" in script
    assert "PrepareToInstall" in script
    assert "StopRunningLOLManager" in script


def test_inno_installer_creates_required_shortcuts_and_launch_option() -> None:
    script = (PROJECT_ROOT / "installer" / "LOLManager.iss").read_text("utf-8")

    assert re.search(r'^Name: "\{userprograms\}.*"; Filename:', script, re.MULTILINE)
    assert re.search(r'^Name: "\{userdesktop\}.*"; Filename:', script, re.MULTILINE)
    assert "Flags: checkedonce" in script
    assert "Flags: nowait postinstall skipifsilent" in script


def test_inno_installer_update_mode_waits_for_bootstrap_and_relaunches_only_after_success() -> None:
    script = (PROJECT_ROOT / "installer" / "LOLManager.iss").read_text("utf-8")

    assert "LOLMANAGERUPDATEMODE" in script
    assert "LOLMANAGERWAITPID" in script
    assert "LOLMANAGERRESULT" in script
    assert "WaitForUpdateBootstrapExit" in script
    assert "OpenProcess@kernel32.dll" in script
    assert "WaitForSingleObject@kernel32.dll" in script
    assert "Check: IsUpdaterInstallMode" in script
    assert "Flags: nowait skipifnotsilent" in script
    assert "WriteUpdateSuccessResult" in script


def test_release_package_contains_no_separate_updater_or_powershell_helper() -> None:
    resources = PROJECT_ROOT / "src" / "lolmanager" / "resources"

    assert not list(resources.rglob("*.ps1"))
    assert not list(resources.rglob("*updater*.exe"))


def test_inno_installer_does_not_delete_user_settings() -> None:
    script = (PROJECT_ROOT / "installer" / "LOLManager.iss").read_text("utf-8")

    assert "[UninstallDelete]" in script
    assert 'Type: filesandordirs; Name: "{app}\\logs"' in script
    assert script.count("Type: filesandordirs;") == 1
    assert "{userappdata}" not in script
    assert "%APPDATA%" not in script


def test_release_build_outputs_versioned_unsigned_artifacts() -> None:
    script = (PROJECT_ROOT / "scripts" / "build_release.ps1").read_text("utf-8")

    assert '"LOLManager-v$Version.exe"' in script
    assert '"LOLManager-Setup-v$Version.exe"' in script
    assert "SHA256SUMS.txt" in script
    assert "--version-file" in script
    assert "[Security.Cryptography.SHA256]::Create()" in script
    assert "Get-FileHash" not in script
    assert "signtool" not in script.lower()


def test_installer_verification_covers_real_lifecycle() -> None:
    script = (PROJECT_ROOT / "scripts" / "verify_installer.ps1").read_text("utf-8")

    assert "Start-Process -FilePath $installedExe" in script
    assert "Invoke-Installer $installLog2" in script
    assert "Assert-ShortcutTarget $desktopShortcut" in script
    assert "Assert-ShortcutTarget $startShortcut" in script
    assert "VersionInfo.FileVersion" in script
    assert "UseDefaultInstallPath" in script
    assert "제거 후 설치 경로가 남아 있습니다." in script
    assert "LOLMANAGERUPDATEMODE" in script
    assert "업데이트 installer가 원본 LOLManager 종료 전에 대기하지 않았습니다." in script
    assert "update mode waited for bootstrap exit and relaunched: yes" in script
    assert "settings preserved after reinstall/uninstall: yes" in script
