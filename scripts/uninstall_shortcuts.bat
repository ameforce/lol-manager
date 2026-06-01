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
    echo [ERROR] scripts\uninstall_shortcuts.bat does not accept arguments except --dry-run.
    exit /b 1
)

call "%LM_SCRIPT_DIR%shortcut_paths.bat"
if errorlevel 1 exit /b %ERRORLEVEL%

if "%LM_DRY_RUN%"=="1" (
    echo [DRY-RUN] LM_DESKTOP_LNK=%LM_DESKTOP_LNK%
    echo [DRY-RUN] LM_START_DIR=%LM_START_DIR%
    echo [DRY-RUN] LM_START_LNK=%LM_START_LNK%
    exit /b 0
)

if exist "%LM_DESKTOP_LNK%" del /f /q "%LM_DESKTOP_LNK%" >nul 2>&1

if exist "%LM_START_LNK%" del /f /q "%LM_START_LNK%" >nul 2>&1

if exist "%LM_START_DIR%" (
    set "LM_HAS_ITEMS="
    for /f "delims=" %%A in ('dir /b /a "%LM_START_DIR%" 2^>nul') do set "LM_HAS_ITEMS=1"
    if not defined LM_HAS_ITEMS rmdir /q "%LM_START_DIR%" >nul 2>&1
)

set "LM_ROOT=%LM_SCRIPT_DIR%.."
for %%I in ("%LM_ROOT%") do set "LM_ROOT=%%~fI"

if exist "%LM_ROOT%\scripts\run_lolmanager_gui.vbs" del /f /q "%LM_ROOT%\scripts\run_lolmanager_gui.vbs" >nul 2>&1
if exist "%LM_ROOT%\scripts\run_lolmanager_gui.pyw" del /f /q "%LM_ROOT%\scripts\run_lolmanager_gui.pyw" >nul 2>&1

exit /b 0
