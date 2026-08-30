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
    assert "[InstallDelete]" in script
    assert 'Type: files; Name: "{app}\\.lolmanager-installer-managed"' in script


def test_inno_installer_creates_required_shortcuts_and_launch_option() -> None:
    script = (PROJECT_ROOT / "installer" / "LOLManager.iss").read_text("utf-8")

    assert re.search(r'^Name: "\{userprograms\}.*"; Filename:', script, re.MULTILINE)
    assert re.search(r'^Name: "\{userdesktop\}.*"; Filename:', script, re.MULTILINE)
    assert "Flags: checkedonce" in script
    assert "Flags: nowait postinstall skipifsilent" in script
    assert "ShouldLaunchLOLManager" in script
    assert "{param:LOLMANAGER_RELAUNCH|0}" in script


def test_inno_installer_update_mode_waits_then_fails_safe_on_residual_process() -> None:
    script = (PROJECT_ROOT / "installer" / "LOLManager.iss").read_text("utf-8")

    assert "LOLMANAGERUPDATEMODE" not in script
    assert "LOLMANAGERWAITPID" in script
    assert "LOLMANAGER_RELAUNCH" in script
    assert "WaitForUpdateBootstrapExit" in script
    assert "OpenProcess@kernel32.dll" in script
    assert "WaitForSingleObject@kernel32.dll" in script
    assert "RequireNoResidualLOLManagerProcess" in script
    assert "ExecAndCaptureOutput" in script
    assert "Check: ShouldLaunchLOLManager" in script
    assert "Flags: nowait skipifnotsilent" in script
    assert "WriteUpdateSuccessResult" not in script
    updater_branch = script[
        script.index("if IsUpdaterInstallMode() then") : script.index(
            "  Result := StopRunningLOLManager();", script.index("function PrepareToInstall")
        )
    ]
    assert "StopRunningLOLManager();" not in updater_branch
    assert "RequireNoResidualLOLManagerProcess();" in updater_branch


def test_inno_update_relaunch_resets_inherited_pyinstaller_environment() -> None:
    script = (PROJECT_ROOT / "installer" / "LOLManager.iss").read_text("utf-8")

    assert "SetEnvironmentVariableW@kernel32.dll" in script
    assert "PYINSTALLER_RESET_ENVIRONMENT" in script
    assert "procedure CurStepChanged(CurStep: TSetupStep);" in script
    assert "(CurStep = ssPostInstall) and HasExplicitRelaunchRequest()" in script


def test_release_package_contains_no_separate_updater_or_powershell_helper() -> None:
    resources = PROJECT_ROOT / "src" / "lolmanager" / "resources"

    assert not list(resources.rglob("*.ps1"))
    assert not list(resources.rglob("*updater*.exe"))
    assert not (PROJECT_ROOT / "installer" / "installer-managed.marker").exists()


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
    assert "LOLMANAGER_RELAUNCH=1" in script
    assert "LOLMANAGERUPDATEMODE" not in script
    assert "업데이트 installer가 원본 LOLManager 종료 전에 대기하지 않았습니다." in script
    assert "잔여 GUI가 보존되지 않았습니다" in script
    assert "InstallLocation 등록값 불일치" in script
    assert "Close-LolManagerInstance" in script
    assert "Get-TaskOwnedLolManagerProcesses" in script
    assert "$updateBootstrapProcess.Id" in script
    assert "Start-UpdateInstallerWithStalePyInstallerEnvironment" in script
    assert "_PYI_ARCHIVE_FILE" in script
    assert "_PYI_PARENT_PROCESS_LEVEL" in script
    assert "PYINSTALLER_RESET_ENVIRONMENT" in script
    assert "Stop-Process -Name 'LOLManager'" not in script
    assert "legacy installer marker를 정리하지 못했습니다." in script
    assert "update mode waited for bootstrap exit, preserved residual GUI, and relaunched: yes" in script
    assert "settings preserved after reinstall/uninstall: yes" in script
