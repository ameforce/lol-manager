@echo off
setlocal EnableExtensions

set "LM_NAME=LOLManager"

set "LM_DESKTOP=%USERPROFILE%\Desktop"
set "LM_DESKTOP_LNK=%LM_DESKTOP%\%LM_NAME%.lnk"
if exist "%LM_DESKTOP_LNK%" del /f /q "%LM_DESKTOP_LNK%" >nul 2>&1

set "LM_PROGRAMS=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "LM_START_DIR=%LM_PROGRAMS%\%LM_NAME%"
set "LM_START_LNK=%LM_START_DIR%\%LM_NAME%.lnk"
if exist "%LM_START_LNK%" del /f /q "%LM_START_LNK%" >nul 2>&1

if exist "%LM_START_DIR%" (
    set "LM_HAS_ITEMS="
    for /f "delims=" %%A in ('dir /b /a "%LM_START_DIR%" 2^>nul') do set "LM_HAS_ITEMS=1"
    if not defined LM_HAS_ITEMS rmdir /q "%LM_START_DIR%" >nul 2>&1
)

set "LM_ROOT=%~dp0.."
for %%I in ("%LM_ROOT%") do set "LM_ROOT=%%~fI"

if exist "%LM_ROOT%\scripts\run_lolmanager_gui.vbs" del /f /q "%LM_ROOT%\scripts\run_lolmanager_gui.vbs" >nul 2>&1
if exist "%LM_ROOT%\scripts\run_lolmanager_gui.pyw" del /f /q "%LM_ROOT%\scripts\run_lolmanager_gui.pyw" >nul 2>&1

exit /b 0
