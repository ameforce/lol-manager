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
$backupDir = Join-Path $verificationRootFull 'shortcut-backup'
$installLog1 = Join-Path $verificationRootFull 'install-first.log'
$installLog2 = Join-Path $verificationRootFull 'install-reinstall.log'
$updateInstallLog = Join-Path $verificationRootFull 'install-update-mode.log'
$updateResultPath = Join-Path $verificationRootFull 'update-result.json'
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

    Invoke-Installer $installLog2
    if (Get-Process -Name 'LOLManager' -ErrorAction SilentlyContinue) {
        throw '재설치가 실행 중 LOLManager.exe를 종료하지 못했습니다.'
    }
    if (-not (Test-Path -LiteralPath $settingsMarker -PathType Leaf)) {
        throw '재설치 중 사용자 설정 marker가 삭제됐습니다.'
    }
    Assert-ShortcutTarget $desktopShortcut $installedExe
    Assert-ShortcutTarget $startShortcut $installedExe

    Start-Process -FilePath $installedExe | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $updateWindowProcess = $null
    do {
        Start-Sleep -Milliseconds 250
        $updateWindowProcess = Get-Process -Name 'LOLManager' -ErrorAction SilentlyContinue |
            Where-Object {
                [void]$_.Refresh()
                $_.MainWindowHandle -ne 0
            } |
            Select-Object -First 1
    } while (
        (-not $updateWindowProcess -or $updateWindowProcess.MainWindowHandle -eq 0) -and
        [DateTime]::UtcNow -lt $deadline
    )
    if (-not $updateWindowProcess -or $updateWindowProcess.MainWindowHandle -eq 0) {
        throw '업데이트 모드 검증용 GUI 창이 30초 안에 나타나지 않았습니다.'
    }

    $updateProcess = Start-Process -FilePath $setupPath -ArgumentList @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        '/LOLMANAGERUPDATEMODE',
        "/LOLMANAGERWAITPID=$($updateWindowProcess.Id)",
        "/LOLMANAGERRESULT=$updateResultPath",
        "/LOLMANAGERTARGETVERSION=$Version",
        "/LOG=$updateInstallLog"
    ) -PassThru
    Start-Sleep -Milliseconds 750
    $updateProcess.Refresh()
    if ($updateProcess.HasExited) {
        throw '업데이트 installer가 원본 LOLManager 종료 전에 대기하지 않았습니다.'
    }
    Stop-Process -Id $updateWindowProcess.Id -Force
    if (-not $updateProcess.WaitForExit(60000)) {
        Stop-Process -Id $updateProcess.Id -Force -ErrorAction SilentlyContinue
        throw '업데이트 installer가 원본 LOLManager 종료 뒤 60초 안에 완료되지 않았습니다.'
    }
    if ($updateProcess.ExitCode -ne 0) {
        throw "업데이트 installer 실행 실패(exit=$($updateProcess.ExitCode)): $updateInstallLog"
    }
    if (-not (Test-Path -LiteralPath $updateResultPath -PathType Leaf)) {
        throw '업데이트 installer 성공 결과 파일이 없습니다.'
    }
    $updateResult = Get-Content -LiteralPath $updateResultPath -Raw | ConvertFrom-Json
    if ($updateResult.status -ne 'success' -or $updateResult.target_version -ne $Version) {
        throw '업데이트 installer 성공 결과가 올바르지 않습니다.'
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $relaunchedProcess = $null
    do {
        Start-Sleep -Milliseconds 250
        $relaunchedProcess = Get-Process -Name 'LOLManager' -ErrorAction SilentlyContinue |
            Where-Object {
                [void]$_.Refresh()
                ($_.Id -ne $updateWindowProcess.Id) -and
                ($_.MainWindowHandle -ne 0)
            } |
            Select-Object -First 1
    } while (
        (-not $relaunchedProcess -or $relaunchedProcess.MainWindowHandle -eq 0) -and
        [DateTime]::UtcNow -lt $deadline
    )
    if (-not $relaunchedProcess -or $relaunchedProcess.MainWindowHandle -eq 0) {
        throw '업데이트 installer가 성공 뒤 LOLManager를 다시 시작하지 않았습니다.'
    }
    Stop-Process -Id $relaunchedProcess.Id -Force

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

    Write-Host '[VERIFY] installer lifecycle passed'
    Write-Host "[VERIFY] install dir: $installDir"
    Write-Host "[VERIFY] launched PID: $($windowProcess.Id), window: $windowTitle"
    Write-Host "[VERIFY] file version: $installedVersion"
    Write-Host '[VERIFY] shortcuts: desktop + Start Menu'
    Write-Host '[VERIFY] reinstall closed running app: yes'
    Write-Host '[VERIFY] update mode waited for bootstrap exit and relaunched: yes'
    Write-Host '[VERIFY] settings preserved after reinstall/uninstall: yes'
}
finally {
    Get-Process -Name 'LOLManager' -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
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
    if (Test-Path -LiteralPath $verificationRootFull) {
        Remove-Item -LiteralPath $verificationRootFull -Recurse -Force
    }
}
