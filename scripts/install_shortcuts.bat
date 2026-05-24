@echo off
setlocal EnableExtensions

set "LM_NAME=LOLManager"
set "LM_ROOT=%~dp0.."
for %%I in ("%LM_ROOT%") do set "LM_ROOT=%%~fI"

set "LM_EXE=%LM_ROOT%\%LM_NAME%.exe"
if not exist "%LM_EXE%" (
    if exist "%LM_ROOT%\dist\%LM_NAME%.exe" set "LM_EXE=%LM_ROOT%\dist\%LM_NAME%.exe"
)
if not exist "%LM_EXE%" (
    if exist "%LM_ROOT%\dist\%LM_NAME%\%LM_NAME%.exe" set "LM_EXE=%LM_ROOT%\dist\%LM_NAME%\%LM_NAME%.exe"
)
if not exist "%LM_EXE%" (
    echo [ERROR] executable not found. run scripts\build_exe.bat first.
    exit /b 1
)

set "LM_ICON=%LM_ROOT%\src\lolmanager\resources\assets\lolmanager.ico"
if not exist "%LM_ICON%" set "LM_ICON=%LM_EXE%"

call :RESOLVE_POWERSHELL
if errorlevel 1 exit /b %ERRORLEVEL%

rem 기본값(폴백)
set "LM_DESKTOP=%USERPROFILE%\Desktop"
set "LM_PROGRAMS=%APPDATA%\Microsoft\Windows\Start Menu\Programs"

rem Windows 표준 SpecialFolder 경로를 우선 사용(OneDrive/리다이렉션 환경 대응)
for /f "usebackq delims=" %%A in (`%LM_PS% -NoLogo -NoProfile -Command "[Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)" 2^>nul`) do set "LM_DESKTOP=%%A"
for /f "usebackq delims=" %%A in (`%LM_PS% -NoLogo -NoProfile -Command "[Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)" 2^>nul`) do set "LM_PROGRAMS=%%A"

set "LM_DESKTOP_LNK=%LM_DESKTOP%\%LM_NAME%.lnk"

set "LM_START_DIR=%LM_PROGRAMS%\%LM_NAME%"
set "LM_START_LNK=%LM_START_DIR%\%LM_NAME%.lnk"

if not exist "%LM_START_DIR%" mkdir "%LM_START_DIR%" >nul 2>&1

call :CREATE_SHORTCUT "%LM_DESKTOP_LNK%" "%LM_EXE%" "%LM_ROOT%" "%LM_ICON%"
if errorlevel 1 exit /b %ERRORLEVEL%

call :CREATE_SHORTCUT "%LM_START_LNK%" "%LM_EXE%" "%LM_ROOT%" "%LM_ICON%"
if errorlevel 1 exit /b %ERRORLEVEL%

exit /b 0

:RESOLVE_POWERSHELL
set "LM_PS=powershell"
where powershell >nul 2>&1
if not errorlevel 1 exit /b 0
where pwsh >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PowerShell not found.
    exit /b 2
)
set "LM_PS=pwsh"
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
