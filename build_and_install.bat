@echo off
setlocal EnableExtensions

if not "%~1"=="" (
    echo [ERROR] build_and_install.bat does not accept arguments.
    set "LM_EXIT_CODE=2"
    goto :FINISH
)

set "LM_ROOT=%~dp0"
where powershell.exe >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Windows PowerShell was not found. This Windows-only launcher cannot continue.
    set "LM_EXIT_CODE=3"
    goto :FINISH
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LM_ROOT%scripts\build_and_install.ps1"
set "LM_EXIT_CODE=%ERRORLEVEL%"

if "%LM_EXIT_CODE%"=="0" (
    echo [SUCCESS] Build and per-user installation completed.
) else (
    echo [ERROR] Build and installation failed with exit code %LM_EXIT_CODE%.
    echo [INFO] See logs\build_and_install_last.log for details.
)

:FINISH
if /I not "%LOLMANAGER_NO_PAUSE%"=="1" (
    echo.
    pause
)

endlocal & exit /b %LM_EXIT_CODE%
