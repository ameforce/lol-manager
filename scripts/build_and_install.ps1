[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $PSCommandPath
$repoRoot = [IO.Path]::GetFullPath((Join-Path $scriptRoot '..'))
$pyprojectPath = Join-Path $repoRoot 'pyproject.toml'
$releaseBuilder = Join-Path $scriptRoot 'build_release.ps1'
$logDirectory = Join-Path $repoRoot 'logs'
$logPath = Join-Path $logDirectory 'build_and_install_last.log'
$installerLogPath = Join-Path $logDirectory 'build_and_install_installer_last.log'
$utf8NoBom = New-Object Text.UTF8Encoding($false)

[IO.Directory]::CreateDirectory($logDirectory) | Out-Null
[IO.File]::WriteAllText(
    $logPath,
    "[INFO] started: $([DateTime]::Now.ToString('o'))`r`n",
    $utf8NoBom
)

function Write-BuildInstallLog {
    param([Parameter(Mandatory)][string]$Message)

    $line = '[{0}] {1}' -f [DateTime]::Now.ToString('s'), $Message
    [Console]::WriteLine($line)
    [IO.File]::AppendAllText($logPath, "$line`r`n", $utf8NoBom)
}

function Get-ProjectVersion {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "pyproject.toml을 찾지 못했습니다: $Path"
    }

    $projectText = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    $versionMatch = [regex]::Match(
        $projectText,
        '(?ms)^\[project\].*?^version\s*=\s*"(?<version>\d+\.\d+\.\d+)"'
    )
    if (-not $versionMatch.Success) {
        throw 'pyproject.toml에서 [project].version을 찾지 못했습니다.'
    }

    return $versionMatch.Groups['version'].Value
}

function Find-InnoCompiler {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @()
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'
    }

    return $candidates | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf
    } | Select-Object -First 1
}

function Assert-ShortcutTarget {
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$ExpectedTarget
    )

    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
        throw "설치 바로가기를 찾지 못했습니다: $ShortcutPath"
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath($shortcut.TargetPath),
        [IO.Path]::GetFullPath($ExpectedTarget),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "설치 바로가기 대상 불일치: $ShortcutPath -> $($shortcut.TargetPath)"
    }
}

try {
    Write-BuildInstallLog "repository: $repoRoot"

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw 'uv를 찾지 못했습니다. https://docs.astral.sh/uv/ 의 Windows 설치 안내를 완료한 뒤 다시 실행하세요.'
    }
    Write-BuildInstallLog "uv: $($uv.Source)"

    $innoCompiler = Find-InnoCompiler
    if (-not $innoCompiler) {
        throw 'Inno Setup 6 ISCC.exe를 찾지 못했습니다. Inno Setup 6을 설치한 뒤 다시 실행하세요.'
    }
    Write-BuildInstallLog "Inno Setup compiler: $innoCompiler"

    if (-not (Test-Path -LiteralPath $releaseBuilder -PathType Leaf)) {
        throw "공통 릴리스 빌더를 찾지 못했습니다: $releaseBuilder"
    }

    $version = Get-ProjectVersion $pyprojectPath
    $versionQuad = "$version.0"
    $releaseDirectory = Join-Path $repoRoot 'dist\release'
    $setupPath = Join-Path $releaseDirectory "LOLManager-Setup-v$version.exe"
    $installDirectory = Join-Path $env:LOCALAPPDATA 'Programs\LOLManager'
    $installedExe = Join-Path $installDirectory 'LOLManager.exe'
    $desktopShortcut = Join-Path (
        [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
    ) 'LOLManager.lnk'
    $startMenuShortcut = Join-Path (
        [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
    ) 'LOLManager\LOLManager.lnk'

    Write-BuildInstallLog "building release version $version through scripts\\build_release.ps1"
    & $releaseBuilder -Version $version -InnoCompiler $innoCompiler
    Write-BuildInstallLog '[BUILD] release artifacts completed'

    if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
        throw "빌드 후 installer를 찾지 못했습니다: $setupPath"
    }

    $installerArguments = @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        '/TASKS=desktopicon',
        ('/DIR="{0}"' -f $installDirectory),
        ('/LOG="{0}"' -f $installerLogPath)
    )
    Write-BuildInstallLog "installing per-user to $installDirectory"
    $installerProcess = Start-Process -FilePath $setupPath -ArgumentList $installerArguments -Wait -PassThru
    if ($installerProcess.ExitCode -ne 0) {
        throw "installer 실행 실패(exit=$($installerProcess.ExitCode)): $installerLogPath"
    }

    if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
        throw "설치된 LOLManager.exe를 찾지 못했습니다: $installedExe"
    }
    $installedVersion = [string](Get-Item -LiteralPath $installedExe).VersionInfo.FileVersion
    if ($installedVersion -ne $versionQuad) {
        throw "설치된 EXE 버전 불일치: expected=$versionQuad, actual=$installedVersion"
    }
    Assert-ShortcutTarget $desktopShortcut $installedExe
    Assert-ShortcutTarget $startMenuShortcut $installedExe

    Write-BuildInstallLog "[SUCCESS] installed EXE: $installedExe"
    Write-BuildInstallLog "[SUCCESS] installed file version: $installedVersion"
    Write-BuildInstallLog "[SUCCESS] desktop shortcut: $desktopShortcut"
    Write-BuildInstallLog "[SUCCESS] Start Menu shortcut: $startMenuShortcut"
    [pscustomobject]@{
        InstalledExe = $installedExe
        FileVersion = $installedVersion
        DesktopShortcut = $desktopShortcut
        StartMenuShortcut = $startMenuShortcut
        LogPath = $logPath
    }
}
catch {
    Write-BuildInstallLog "[ERROR] $($_.Exception.Message)"
    exit 1
}
