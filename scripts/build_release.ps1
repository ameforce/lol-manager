[CmdletBinding()]
param(
    [string]$Version,
    [string]$InnoCompiler
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = [IO.Path]::GetFullPath((Join-Path $scriptRoot '..'))
$pyprojectPath = Join-Path $repoRoot 'pyproject.toml'
$releaseDir = Join-Path $repoRoot 'dist\release'
$buildRoot = Join-Path $repoRoot 'build\release'

if (-not $Version) {
    $projectText = [IO.File]::ReadAllText($pyprojectPath, [Text.Encoding]::UTF8)
    $versionMatch = [regex]::Match(
        $projectText,
        '(?ms)^\[project\].*?^version\s*=\s*"(?<version>\d+\.\d+\.\d+)"'
    )
    if (-not $versionMatch.Success) {
        throw 'pyproject.toml에서 [project].version을 찾지 못했습니다.'
    }
    $Version = $versionMatch.Groups['version'].Value
}

if ($Version -notmatch '^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)$') {
    throw "릴리스 버전은 X.Y.Z 형식이어야 합니다: $Version"
}

$versionQuad = "$Version.0"
$portableName = "LOLManager-v$Version.exe"
$setupName = "LOLManager-Setup-v$Version.exe"
$portablePath = Join-Path $releaseDir $portableName
$setupPath = Join-Path $releaseDir $setupName
$checksumPath = Join-Path $releaseDir 'SHA256SUMS.txt'
$generatedPackageDir = Join-Path $buildRoot 'generated\lolmanager\core'
$buildVersionFile = Join-Path $generatedPackageDir '_build_version.py'
$versionInfoFile = Join-Path $buildRoot 'version_info.txt'
$pyInstallerWork = Join-Path $buildRoot 'pyinstaller'
$specDir = Join-Path $buildRoot 'spec'

foreach ($path in @($releaseDir, $generatedPackageDir, $pyInstallerWork, $specDir)) {
    [IO.Directory]::CreateDirectory($path) | Out-Null
}

$utf8NoBom = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText(
    $buildVersionFile,
    "BUILD_VERSION = `"$versionQuad`"`r`n",
    $utf8NoBom
)

$major = [int]$Matches['major']
$minor = [int]$Matches['minor']
$patch = [int]$Matches['patch']
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($major, $minor, $patch, 0),
    prodvers=($major, $minor, $patch, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'ameforce'),
        StringStruct('FileDescription', 'LOLManager'),
        StringStruct('FileVersion', '$versionQuad'),
        StringStruct('InternalName', 'LOLManager'),
        StringStruct('OriginalFilename', 'LOLManager.exe'),
        StringStruct('ProductName', 'LOLManager'),
        StringStruct('ProductVersion', '$Version')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
[IO.File]::WriteAllText($versionInfoFile, $versionInfo, $utf8NoBom)

$uv = Get-Command uv -ErrorAction Stop
$pyInstallerArgs = @(
    '--noconfirm',
    '--clean',
    '--windowed',
    '--onefile',
    '--name', "LOLManager-v$Version",
    '--icon', (Join-Path $repoRoot 'src\lolmanager\resources\assets\lolmanager.ico'),
    '--paths', (Join-Path $repoRoot 'src'),
    '--add-data', "$buildVersionFile;lolmanager\core",
    '--add-data', "$(Join-Path $repoRoot 'src\lolmanager\resources');lolmanager\resources",
    '--collect-all', 'ttkbootstrap',
    '--version-file', $versionInfoFile,
    '--distpath', $releaseDir,
    '--workpath', $pyInstallerWork,
    '--specpath', $specDir,
    (Join-Path $repoRoot 'src\lolmanager\cli\entrypoint.py')
)

Write-Host "[BUILD] portable EXE: $portableName"
& $uv.Source run --with pyinstaller pyinstaller @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 빌드 실패(exit=$LASTEXITCODE)"
}
if (-not (Test-Path -LiteralPath $portablePath -PathType Leaf)) {
    throw "portable EXE가 생성되지 않았습니다: $portablePath"
}

if (-not $InnoCompiler) {
    $isccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($isccCommand) {
        $InnoCompiler = $isccCommand.Source
    }
}
if (-not $InnoCompiler) {
    $innoCandidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    $InnoCompiler = $innoCandidates | Where-Object {
        $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
    } | Select-Object -First 1
}
if (-not $InnoCompiler) {
    throw 'Inno Setup 6 ISCC.exe를 찾지 못했습니다. winget install JRSoftware.InnoSetup을 실행하세요.'
}

$installerScript = Join-Path $repoRoot 'installer\LOLManager.iss'
Write-Host "[BUILD] installer: $setupName"
& $InnoCompiler "/DMyAppVersion=$Version" "/DMyAppVersionQuad=$versionQuad" "/DMySourceExe=$portablePath" $installerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup 빌드 실패(exit=$LASTEXITCODE)"
}
if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
    throw "installer가 생성되지 않았습니다: $setupPath"
}

$checksumLines = foreach ($artifact in @($setupPath, $portablePath)) {
    $hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToUpperInvariant()
    "$hash  $([IO.Path]::GetFileName($artifact))"
}
[IO.File]::WriteAllLines($checksumPath, $checksumLines, $utf8NoBom)

Write-Host '[BUILD] release artifacts:'
Get-Item -LiteralPath $setupPath, $portablePath, $checksumPath |
    Select-Object Name, Length, LastWriteTime
