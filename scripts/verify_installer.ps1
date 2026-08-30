[CmdletBinding()]
param(
    [string]$Version,
    [switch]$UseDefaultInstallPath
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = [IO.Path]::GetFullPath((Join-Path $scriptRoot '..'))
$pyprojectPath = Join-Path $repoRoot 'pyproject.toml'
$projectText = [IO.File]::ReadAllText($pyprojectPath, [Text.Encoding]::UTF8)
$versionMatch = [regex]::Match(
    $projectText,
    '(?ms)^\[project\].*?^version\s*=\s*"(?<version>\d+\.\d+\.\d+)"'
)
if (-not $Version) {
    if (-not $versionMatch.Success) {
        throw 'pyproject.toml에서 [project].version을 찾지 못했습니다.'
    }
    $Version = $versionMatch.Groups['version'].Value
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "검증할 버전은 X.Y.Z 형식이어야 합니다: $Version"
}
$releaseDir = Join-Path $repoRoot 'dist\release'
$setupPath = Join-Path $releaseDir "LOLManager-Setup-v$Version.exe"
$versionQuad = "$Version.0"

if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
    throw "installer가 없습니다: $setupPath"
}
if (Get-Process -Name 'LOLManager' -ErrorAction SilentlyContinue) {
    throw '검증 시작 전에 실행 중인 LOLManager.exe가 있습니다.'
}

$verificationBase = Join-Path $env:LOCALAPPDATA 'Programs\LOLManager-Installer-Verification'
$verificationRoot = Join-Path $verificationBase ([guid]::NewGuid().ToString('N'))
$verificationRootFull = [IO.Path]::GetFullPath($verificationRoot)
$verificationBaseFull = [IO.Path]::GetFullPath($verificationBase)
if (-not $verificationRootFull.StartsWith(
    $verificationBaseFull + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "검증 경로가 허용된 per-user 임시 루트 밖입니다: $verificationRootFull"
}

$defaultInstallDir = Join-Path $env:LOCALAPPDATA 'Programs\LOLManager'
if ($UseDefaultInstallPath) {
    if (Test-Path -LiteralPath $defaultInstallDir) {
        throw "기본 설치 경로가 이미 존재해 안전한 검증을 시작할 수 없습니다: $defaultInstallDir"
    }
    $installDir = $defaultInstallDir
}
else {
    $installDir = Join-Path $verificationRootFull 'LOLManager'
}
$testAppData = Join-Path $verificationRootFull 'AppData\Roaming'
    $settingsDir = Join-Path $testAppData 'LOLManager'
    $settingsMarker = Join-Path $settingsDir 'installer-preserve-check.txt'
    $legacyInstallerMarker = Join-Path $installDir '.lolmanager-installer-managed'
$backupDir = Join-Path $verificationRootFull 'shortcut-backup'
$installLog1 = Join-Path $verificationRootFull 'install-first.log'
$installLog2 = Join-Path $verificationRootFull 'install-reinstall.log'
$updateInstallLog = Join-Path $verificationRootFull 'install-update-mode.log'
$residualUpdateInstallLog = Join-Path $verificationRootFull 'install-update-residual.log'
$uninstallLog = Join-Path $verificationRootFull 'uninstall.log'

$desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
$programs = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
$desktopShortcut = Join-Path $desktop 'LOLManager.lnk'
$startShortcut = Join-Path $programs 'LOLManager\LOLManager.lnk'
$shortcutPaths = @($desktopShortcut, $startShortcut)
$shortcutBackups = @{}
$originalAppData = $env:APPDATA
$launchedProcess = $null
$windowProcess = $null
$residualUpdateWindowProcess = $null
$verificationSucceeded = $false

function Invoke-Installer([string]$LogPath) {
    $process = Start-Process -FilePath $setupPath -ArgumentList @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        "/DIR=$installDir",
        '/TASKS=desktopicon',
        "/LOG=$LogPath"
    ) -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "installer 실행 실패(exit=$($process.ExitCode)): $LogPath"
    }
}

function Start-UpdateInstallerWithStalePyInstallerEnvironment(
    [int]$WaitPid,
    [string]$LogPath
) {
    $environmentNames = @(
        '_PYI_ARCHIVE_FILE',
        '_PYI_PARENT_PROCESS_LEVEL',
        '_PYI_APPLICATION_HOME_DIR',
        '_PYI_SPLASH_IPC',
        'PYINSTALLER_RESET_ENVIRONMENT'
    )
    $originalEnvironment = @{}
    foreach ($name in $environmentNames) {
        $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }

    try {
        [Environment]::SetEnvironmentVariable('_PYI_ARCHIVE_FILE', $installedExe, 'Process')
        [Environment]::SetEnvironmentVariable('_PYI_PARENT_PROCESS_LEVEL', '2', 'Process')
        [Environment]::SetEnvironmentVariable('_PYI_APPLICATION_HOME_DIR', $verificationRootFull, 'Process')
        [Environment]::SetEnvironmentVariable('_PYI_SPLASH_IPC', '0', 'Process')
        [Environment]::SetEnvironmentVariable('PYINSTALLER_RESET_ENVIRONMENT', $null, 'Process')

        return Start-Process -FilePath $setupPath -ArgumentList @(
            '/VERYSILENT',
            '/SUPPRESSMSGBOXES',
            '/NORESTART',
            "/DIR=$installDir",
            '/LOLMANAGER_RELAUNCH=1',
            "/LOLMANAGERWAITPID=$WaitPid",
            "/LOG=$LogPath"
        ) -PassThru
    }
    finally {
        foreach ($name in $environmentNames) {
            [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process')
        }
    }
}

function Assert-ShortcutTarget([string]$ShortcutPath, [string]$ExpectedTarget) {
    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
        throw "바로가기가 생성되지 않았습니다: $ShortcutPath"
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath($shortcut.TargetPath),
        [IO.Path]::GetFullPath($ExpectedTarget),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "바로가기 대상 불일치: $ShortcutPath -> $($shortcut.TargetPath)"
    }
}

function Get-TaskOwnedLolManagerProcesses() {
    $ownedPrefix = $verificationRootFull + [IO.Path]::DirectorySeparatorChar
    return @(
        Get-CimInstance Win32_Process -Filter "Name='LOLManager.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ExecutablePath -and $_.ExecutablePath.StartsWith(
                    $ownedPrefix,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
}

function Get-LolManagerInstanceRootPid([int]$WindowPid) {
    $processes = @{}
    foreach ($process in Get-TaskOwnedLolManagerProcesses) {
        $processes[[string]$process.ProcessId] = $process
    }

    $currentPid = $WindowPid
    while ($processes.ContainsKey([string]$currentPid)) {
        $parentPid = [int]$processes[[string]$currentPid].ParentProcessId
        if (-not $processes.ContainsKey([string]$parentPid)) {
            break
        }
        $currentPid = $parentPid
    }
    return $currentPid
}

function Test-LolManagerInstanceTreeRunning([int]$RootPid) {
    $processes = @(Get-TaskOwnedLolManagerProcesses)
    $remaining = [System.Collections.Generic.HashSet[int]]::new()
    [void]$remaining.Add($RootPid)
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $processes) {
            if ($remaining.Contains([int]$process.ParentProcessId) -and
                $remaining.Add([int]$process.ProcessId)) {
                $changed = $true
            }
        }
    }
    return @($processes | Where-Object { $remaining.Contains([int]$_.ProcessId) }).Count -gt 0
}

if (-not ('LolManagerNativeWindow' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class LolManagerNativeWindow {
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool PostMessage(
        IntPtr hWnd,
        uint message,
        IntPtr wParam,
        IntPtr lParam);
}
'@
}

function Close-LolManagerInstance([Diagnostics.Process]$WindowProcess, [string]$FailureMessage) {
    $WindowProcess.Refresh()
    if ($WindowProcess.MainWindowHandle -eq 0) {
        throw "종료할 LOLManager GUI 창을 찾지 못했습니다: $($WindowProcess.Id)"
    }
    $rootPid = Get-LolManagerInstanceRootPid $WindowProcess.Id
    if (-not [LolManagerNativeWindow]::PostMessage(
        $WindowProcess.MainWindowHandle,
        0x0010,
        [IntPtr]::Zero,
        [IntPtr]::Zero
    )) {
        throw "LOLManager GUI 종료 요청을 보낼 수 없습니다: $($WindowProcess.Id)"
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (Test-LolManagerInstanceTreeRunning $rootPid) {
        if ([DateTime]::UtcNow -ge $deadline) {
            throw $FailureMessage
        }
        Start-Sleep -Milliseconds 250
    }
}

try {
    [IO.Directory]::CreateDirectory($backupDir) | Out-Null
    [IO.Directory]::CreateDirectory($settingsDir) | Out-Null
    [IO.File]::WriteAllText($settingsMarker, 'preserve-me', [Text.Encoding]::UTF8)
    $env:APPDATA = $testAppData

    foreach ($shortcutPath in $shortcutPaths) {
        if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
            $backupPath = Join-Path $backupDir ([guid]::NewGuid().ToString('N') + '.lnk')
            Copy-Item -LiteralPath $shortcutPath -Destination $backupPath
            $shortcutBackups[$shortcutPath] = $backupPath
        }
    }

    Invoke-Installer $installLog1
    $installedExe = Join-Path $installDir 'LOLManager.exe'
    if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
        throw "설치된 EXE가 없습니다: $installedExe"
    }
    $installedVersion = (Get-Item -LiteralPath $installedExe).VersionInfo.FileVersion
    if ($installedVersion -ne $versionQuad) {
        throw "설치된 EXE 버전 불일치: $installedVersion"
    }
    $innoUninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{F1E18E34-A5B3-4DE8-8E91-74DC33D66D15}_is1'
    $registeredInstallLocation = (Get-ItemProperty -LiteralPath $innoUninstallKey -ErrorAction Stop).InstallLocation
    $trailingDirectorySeparators = [char[]]@([char]0x5c, [char]'/')
    $registeredInstallLocationFull = [IO.Path]::GetFullPath($registeredInstallLocation).TrimEnd($trailingDirectorySeparators)
    $installDirFull = [IO.Path]::GetFullPath($installDir).TrimEnd($trailingDirectorySeparators)
    if (-not [string]::Equals(
        $registeredInstallLocationFull,
        $installDirFull,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Inno InstallLocation 등록값 불일치: $registeredInstallLocation"
    }
    if (Get-Process -Name 'LOLManager' -ErrorAction SilentlyContinue) {
        throw '일반 silent installer가 명시적 relaunch 없이 LOLManager를 시작했습니다.'
    }
    Assert-ShortcutTarget $desktopShortcut $installedExe
    Assert-ShortcutTarget $startShortcut $installedExe

    $launchedProcess = Start-Process -FilePath $installedExe -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $running = @(Get-Process -Name 'LOLManager' -ErrorAction SilentlyContinue)
        $windowProcess = $running | Where-Object {
            $_.Refresh()
            $_.MainWindowHandle -ne 0
        } | Select-Object -First 1
    } while (-not $windowProcess -and [DateTime]::UtcNow -lt $deadline)
    if (-not $running) {
        throw '설치된 앱이 실행 직후 종료됐습니다.'
    }
    if (-not $windowProcess) {
        throw '설치된 앱의 GUI 창이 30초 안에 나타나지 않았습니다.'
    }
    $windowTitle = $windowProcess.MainWindowTitle

    [IO.File]::WriteAllText($legacyInstallerMarker, 'legacy-installer-marker', [Text.Encoding]::UTF8)
    if (-not (Test-Path -LiteralPath $legacyInstallerMarker -PathType Leaf)) {
        throw '업그레이드 정리 검증용 legacy installer marker를 만들지 못했습니다.'
    }
    Invoke-Installer $installLog2
    if (Get-Process -Name 'LOLManager' -ErrorAction SilentlyContinue) {
        throw '재설치가 실행 중 LOLManager.exe를 종료하지 못했습니다.'
    }
    if (Test-Path -LiteralPath $legacyInstallerMarker) {
        throw '재설치가 legacy installer marker를 정리하지 못했습니다.'
    }
    if (-not (Test-Path -LiteralPath $settingsMarker -PathType Leaf)) {
        throw '재설치 중 사용자 설정 marker가 삭제됐습니다.'
    }
    Assert-ShortcutTarget $desktopShortcut $installedExe
    Assert-ShortcutTarget $startShortcut $installedExe

    function Wait-ForLolManagerWindow([string]$FailureMessage, [int]$ExcludePid = 0) {
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        $candidate = $null
        do {
            Start-Sleep -Milliseconds 250
            $taskOwnedWindowProcesses = @(
                Get-TaskOwnedLolManagerProcesses |
                    Where-Object { [int]$_.ProcessId -ne $ExcludePid } |
                    ForEach-Object {
                        Get-Process -Id ([int]$_.ProcessId) -ErrorAction SilentlyContinue
                    } |
                    Where-Object {
                        [void]$_.Refresh()
                        $_.MainWindowHandle -ne 0
                    }
            )
            $errorWindow = $taskOwnedWindowProcesses |
                Where-Object { $_.MainWindowTitle -eq 'Error' } |
                Select-Object -First 1
            if ($errorWindow) {
                throw '업데이트 재실행이 LOLManager GUI 대신 Error 창을 표시했습니다.'
            }
            $candidate = $taskOwnedWindowProcesses |
                Where-Object { $_.MainWindowTitle -like 'LOLManager*' } |
                Select-Object -First 1
        } while (
            (-not $candidate -or $candidate.MainWindowHandle -eq 0) -and
            [DateTime]::UtcNow -lt $deadline
        )
        if (-not $candidate -or $candidate.MainWindowHandle -eq 0) {
            throw $FailureMessage
        }
        $candidateVersion = (Get-Item -LiteralPath $candidate.Path).VersionInfo.FileVersion
        if ($candidateVersion -ne $versionQuad) {
            throw "업데이트 재실행 EXE 버전 불일치: $candidateVersion"
        }
        return $candidate
    }

    # First prove the safe residual branch. The installer must wait for the
    # initiating GUI, then fail without force-terminating the second GUI.
    $updateBootstrapProcess = Start-Process -FilePath $installedExe -PassThru
    $updateWindowProcess = Wait-ForLolManagerWindow '업데이트 모드 검증용 GUI 창이 30초 안에 나타나지 않았습니다.'
    Start-Process -FilePath $installedExe | Out-Null
    $residualUpdateWindowProcess = Wait-ForLolManagerWindow '업데이트 모드 검증용 잔여 GUI 창이 30초 안에 나타나지 않았습니다.' $updateWindowProcess.Id

    $residualUpdateProcess = Start-Process -FilePath $setupPath -ArgumentList @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        "/DIR=$installDir",
        '/LOLMANAGER_RELAUNCH=1',
        "/LOLMANAGERWAITPID=$($updateBootstrapProcess.Id)",
        "/LOG=$residualUpdateInstallLog"
    ) -PassThru
    Start-Sleep -Milliseconds 750
    $residualUpdateProcess.Refresh()
    if ($residualUpdateProcess.HasExited) {
        throw '업데이트 installer가 원본 LOLManager 종료 전에 대기하지 않았습니다.'
    }
    Close-LolManagerInstance $updateWindowProcess '원본 LOLManager GUI가 30초 안에 정상 종료되지 않았습니다.'
    if (-not $residualUpdateProcess.WaitForExit(60000)) {
        Stop-Process -Id $residualUpdateProcess.Id -Force -ErrorAction SilentlyContinue
        throw '잔여 GUI가 실행 중일 때 업데이트 installer가 60초 안에 안전하게 중단되지 않았습니다.'
    }
    if ($residualUpdateProcess.ExitCode -eq 0) {
        throw "잔여 GUI가 실행 중인데 업데이트 installer가 성공했습니다: $residualUpdateInstallLog"
    }
    if (-not (Get-Process -Id $residualUpdateWindowProcess.Id -ErrorAction SilentlyContinue)) {
        throw '잔여 GUI가 보존되지 않았습니다.'
    }
    Close-LolManagerInstance $residualUpdateWindowProcess '잔여 LOLManager GUI가 30초 안에 정상 종료되지 않았습니다.'

    # Then prove a single initiating GUI performs the direct native update and
    # relaunches only through the explicit Inno [Run] contract.
    $updateBootstrapProcess = Start-Process -FilePath $installedExe -PassThru
    $updateWindowProcess = Wait-ForLolManagerWindow '업데이트 성공 경로 검증용 GUI 창이 30초 안에 나타나지 않았습니다.'
    $updateProcess = Start-UpdateInstallerWithStalePyInstallerEnvironment `
        $updateBootstrapProcess.Id `
        $updateInstallLog
    Start-Sleep -Milliseconds 750
    $updateProcess.Refresh()
    if ($updateProcess.HasExited) {
        throw '업데이트 installer가 원본 LOLManager 종료 전에 대기하지 않았습니다.'
    }
    Close-LolManagerInstance $updateWindowProcess '업데이트 원본 LOLManager GUI가 30초 안에 정상 종료되지 않았습니다.'
    if (-not $updateProcess.WaitForExit(60000)) {
        Stop-Process -Id $updateProcess.Id -Force -ErrorAction SilentlyContinue
        throw '업데이트 installer가 원본 LOLManager 종료 뒤 60초 안에 완료되지 않았습니다.'
    }
    if ($updateProcess.ExitCode -ne 0) {
        throw "업데이트 installer 실행 실패(exit=$($updateProcess.ExitCode)): $updateInstallLog"
    }

    $relaunchedProcess = Wait-ForLolManagerWindow '업데이트 installer가 성공 뒤 LOLManager를 다시 시작하지 않았습니다.' $updateWindowProcess.Id
    Close-LolManagerInstance $relaunchedProcess '업데이트 후 재시작된 LOLManager GUI가 30초 안에 정상 종료되지 않았습니다.'

    $uninstaller = Join-Path $installDir 'unins000.exe'
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw "uninstaller가 없습니다: $uninstaller"
    }
    $uninstallProcess = Start-Process -FilePath $uninstaller -ArgumentList @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        "/LOG=$uninstallLog"
    ) -Wait -PassThru
    if ($uninstallProcess.ExitCode -ne 0) {
        throw "uninstaller 실행 실패(exit=$($uninstallProcess.ExitCode))"
    }
    if (Test-Path -LiteralPath $installedExe) {
        throw '제거 후 설치 EXE가 남아 있습니다.'
    }
    if (Test-Path -LiteralPath $installDir) {
        throw '제거 후 설치 경로가 남아 있습니다.'
    }
    if (Test-Path -LiteralPath $desktopShortcut) {
        throw '제거 후 installer가 만든 바탕 화면 바로가기가 남아 있습니다.'
    }
    if (Test-Path -LiteralPath $startShortcut) {
        throw '제거 후 installer가 만든 시작 메뉴 바로가기가 남아 있습니다.'
    }
    if (-not (Test-Path -LiteralPath $settingsMarker -PathType Leaf)) {
        throw '제거 중 사용자 설정 marker가 삭제됐습니다.'
    }

    $verificationSucceeded = $true
    Write-Host '[VERIFY] installer lifecycle passed'
    Write-Host "[VERIFY] install dir: $installDir"
    Write-Host "[VERIFY] launched PID: $($windowProcess.Id), window: $windowTitle"
    Write-Host "[VERIFY] file version: $installedVersion"
    Write-Host '[VERIFY] shortcuts: desktop + Start Menu'
    Write-Host '[VERIFY] reinstall closed running app: yes'
    Write-Host '[VERIFY] update mode waited for bootstrap exit, preserved residual GUI, and relaunched: yes'
    Write-Host '[VERIFY] settings preserved after reinstall/uninstall: yes'
}
finally {
    Get-TaskOwnedLolManagerProcesses |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $uninstaller = Join-Path $installDir 'unins000.exe'
    if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
        Start-Process -FilePath $uninstaller -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
        ) -Wait | Out-Null
    }
    foreach ($shortcutPath in $shortcutPaths) {
        if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
            Remove-Item -LiteralPath $shortcutPath -Force
        }
        if ($shortcutBackups.ContainsKey($shortcutPath)) {
            $parent = Split-Path -Parent $shortcutPath
            [IO.Directory]::CreateDirectory($parent) | Out-Null
            Copy-Item -LiteralPath $shortcutBackups[$shortcutPath] -Destination $shortcutPath
        }
    }
    $env:APPDATA = $originalAppData
    if ($verificationSucceeded -and (Test-Path -LiteralPath $verificationRootFull)) {
        Remove-Item -LiteralPath $verificationRootFull -Recurse -Force
    }
    elseif (Test-Path -LiteralPath $verificationRootFull) {
        Write-Host "[VERIFY] retained failure evidence: $verificationRootFull"
    }
}
