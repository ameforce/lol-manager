@echo off
rem Resolves shortcut paths for install_shortcuts.bat and uninstall_shortcuts.bat.
rem Caller may set LM_NAME before calling this script.

if not defined LM_NAME set "LM_NAME=LOLManager"

call :RESOLVE_POWERSHELL
if errorlevel 1 exit /b %ERRORLEVEL%

rem Fallback paths when SpecialFolder lookup is unavailable.
set "LM_DESKTOP=%USERPROFILE%\Desktop"
set "LM_PROGRAMS=%APPDATA%\Microsoft\Windows\Start Menu\Programs"

for /f "usebackq delims=" %%A in (`%LM_PS% -NoLogo -NoProfile -Command "[Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)" 2^>nul`) do set "LM_DESKTOP=%%A"
for /f "usebackq delims=" %%A in (`%LM_PS% -NoLogo -NoProfile -Command "[Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)" 2^>nul`) do set "LM_PROGRAMS=%%A"

set "LM_DESKTOP_LNK=%LM_DESKTOP%\%LM_NAME%.lnk"
set "LM_START_DIR=%LM_PROGRAMS%\%LM_NAME%"
set "LM_START_LNK=%LM_START_DIR%\%LM_NAME%.lnk"
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
