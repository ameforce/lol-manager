@echo off
setlocal EnableExtensions

set "LM_NAME=LOLManager"
set "LM_SCRIPT_DIR=%~dp0"
set "LM_DRY_RUN=0"
if /i "%~1"=="--dry-run" (
    set "LM_DRY_RUN=1"
    shift
)
if not "%~1"=="" (
    echo [ERROR] scripts\install_shortcuts.bat does not accept arguments except --dry-run.
    exit /b 1
)

set "LM_ROOT=%LM_SCRIPT_DIR%.."
for %%I in ("%LM_ROOT%") do set "LM_ROOT=%%~fI"

set "LM_EXE=%LM_ROOT%\%LM_NAME%.exe"
if not exist "%LM_EXE%" (
    if exist "%LM_ROOT%\dist\%LM_NAME%.exe" set "LM_EXE=%LM_ROOT%\dist\%LM_NAME%.exe"
)
if not exist "%LM_EXE%" (
    if exist "%LM_ROOT%\dist\%LM_NAME%\%LM_NAME%.exe" set "LM_EXE=%LM_ROOT%\dist\%LM_NAME%\%LM_NAME%.exe"
)
if "%LM_DRY_RUN%"=="0" if not exist "%LM_EXE%" (
    echo [ERROR] executable not found. run scripts\build_exe.bat first.
    exit /b 1
)

set "LM_ICON=%LM_ROOT%\src\lolmanager\resources\assets\lolmanager.ico"
if not exist "%LM_ICON%" set "LM_ICON=%LM_EXE%"

call "%LM_SCRIPT_DIR%shortcut_paths.bat"
if errorlevel 1 exit /b %ERRORLEVEL%

if "%LM_DRY_RUN%"=="1" (
    echo [DRY-RUN] LM_DESKTOP_LNK=%LM_DESKTOP_LNK%
    echo [DRY-RUN] LM_START_DIR=%LM_START_DIR%
    echo [DRY-RUN] LM_START_LNK=%LM_START_LNK%
    exit /b 0
)

if not exist "%LM_START_DIR%" mkdir "%LM_START_DIR%" >nul 2>&1

call :CREATE_SHORTCUT "%LM_DESKTOP_LNK%" "%LM_EXE%" "%LM_ROOT%" "%LM_ICON%"
if errorlevel 1 exit /b %ERRORLEVEL%

call :CREATE_SHORTCUT "%LM_START_LNK%" "%LM_EXE%" "%LM_ROOT%" "%LM_ICON%"
if errorlevel 1 exit /b %ERRORLEVEL%

exit /b 0

:CREATE_SHORTCUT
set "LM_LNK=%~1"
set "LM_TARGET=%~2"
set "LM_WORKDIR=%~3"
set "LM_ICONPATH=%~4"
for %%D in ("%LM_LNK%") do set "LM_LNK_DIR=%%~dpD"
if not exist "%LM_LNK_DIR%" mkdir "%LM_LNK_DIR%" >nul 2>&1

%LM_PS% -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $lnk=$env:LM_LNK; $target=$env:LM_TARGET; $work=$env:LM_WORKDIR; $icon=$env:LM_ICONPATH; try { if([string]::IsNullOrWhiteSpace($lnk)) { throw 'lnk argument is empty' }; $dir = Split-Path -Parent $lnk; if($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }; $ws=New-Object -ComObject WScript.Shell; $sc=$ws.CreateShortcut($lnk); $sc.TargetPath=$target; if($work){$sc.WorkingDirectory=$work}; if($icon -and (Test-Path -LiteralPath $icon)) {$sc.IconLocation=($icon + ',0')}; $sc.Save() } catch { [Console]::Error.WriteLine('[PS] ' + $_.Exception.Message); try { [Console]::Error.WriteLine(('[PS] HResult=0x{0:X8}' -f $_.Exception.HResult)) } catch {}; [Console]::Error.WriteLine('[PS] lnk=' + $lnk); [Console]::Error.WriteLine('[PS] target=' + $target); [Console]::Error.WriteLine('[PS] work=' + $work); [Console]::Error.WriteLine('[PS] icon=' + $icon); exit 1 }"
if errorlevel 1 (
    echo [ERROR] failed to create shortcut: "%LM_LNK%"
    exit /b 3
)
exit /b 0
