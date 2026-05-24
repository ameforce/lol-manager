@echo off
setlocal EnableExtensions

set "LM_EXIT_CODE=0"
set "LM_EXE=LOLManager.exe"
set "LM_RESTART_AFTER_BUILD=0"
set "LM_KILLED_OK=0"

set "LM_AUTO_UTF8=0"
if defined VSCODE_PID set "LM_AUTO_UTF8=1"
if defined WT_SESSION set "LM_AUTO_UTF8=1"
if /i "%LM_NO_UTF8%"=="1" set "LM_AUTO_UTF8=0"
if /i "%LM_FORCE_UTF8%"=="1" set "LM_AUTO_UTF8=1"

if "%LM_AUTO_UTF8%"=="1" (
    for /f "tokens=2 delims=:" %%A in ('chcp') do set "LM_ORIG_CP=%%A"
    set "LM_ORIG_CP=%LM_ORIG_CP: =%"
    if not "%LM_ORIG_CP%"=="65001" chcp 65001 >nul 2>&1
)

set "LM_ROOT=%~dp0.."
for %%I in ("%LM_ROOT%") do set "LM_ROOT=%%~fI"
set "LM_LOG_DIR=%LM_ROOT%\logs"
if not exist "%LM_LOG_DIR%" md "%LM_LOG_DIR%" >nul 2>&1
set "LM_LOG=%LM_LOG_DIR%\build_exe_last.log"
del /f /q "%LM_LOG%" >nul 2>&1

echo [INFO] log: "%LM_LOG%"
echo [INFO] started: %DATE% %TIME%>"%LM_LOG%"
>>"%LM_LOG%" echo [INFO] log: "%LM_LOG%"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

call :MAIN %*
set "LM_EXIT_CODE=%ERRORLEVEL%"

if "%LM_EXIT_CODE%"=="0" (
    if "%LM_RESTART_AFTER_BUILD%"=="1" (
        echo [INFO] restarting %LM_EXE% ...
        start "" "%CD%\%LM_EXE%"
    )
) else (
    echo [INFO] see log: "%LM_LOG%"
    if "%LM_RESTART_AFTER_BUILD%"=="1" (
        if "%LM_KILLED_OK%"=="1" (
            if exist "%CD%\%LM_EXE%" start "" "%CD%\%LM_EXE%"
        )
    )
)

if defined LM_ORIG_CP (
    if /i "%LM_RESTORE_CP%"=="1" (
        if not "%LM_ORIG_CP%"=="65001" chcp %LM_ORIG_CP% >nul 2>&1
    )
)

endlocal & exit /b %LM_EXIT_CODE%

:MAIN
if not "%~1"=="" (
    echo [ERROR] scripts\build_exe.bat does not accept any arguments.
    >>"%LM_LOG%" echo [ERROR] scripts\build_exe.bat does not accept any arguments.
    exit /b 1
)

cd /d "%~dp0.."

where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv not found in PATH
    >>"%LM_LOG%" echo [ERROR] uv not found in PATH
    exit /b 2
)

call :STOP_IF_RUNNING
if errorlevel 1 exit /b %ERRORLEVEL%

call :BUILD_EXE
if errorlevel 1 exit /b %ERRORLEVEL%

call :REINSTALL_SHORTCUTS
if errorlevel 1 exit /b %ERRORLEVEL%

exit /b 0

:STOP_IF_RUNNING
call :IS_PROCESS_RUNNING "%LM_EXE%"
if errorlevel 1 exit /b 0

echo [INFO] %LM_EXE% is running. trying to terminate...
>>"%LM_LOG%" echo [INFO] %LM_EXE% is running. trying to terminate...
set "LM_RESTART_AFTER_BUILD=1"

taskkill /IM "%LM_EXE%" >nul 2>&1

call :WAIT_FOR_EXIT "%LM_EXE%" 5
if errorlevel 1 (
    echo [INFO] forcing terminate...
    >>"%LM_LOG%" echo [INFO] forcing terminate...
    taskkill /F /IM "%LM_EXE%" >nul 2>&1

    call :WAIT_FOR_EXIT "%LM_EXE%" 5
    if errorlevel 1 (
        echo [ERROR] failed to terminate %LM_EXE%. aborting.
        >>"%LM_LOG%" echo [ERROR] failed to terminate %LM_EXE%. aborting.
        exit /b 3
    )
)

echo [INFO] terminated.
>>"%LM_LOG%" echo [INFO] terminated.
set "LM_KILLED_OK=1"
exit /b 0

:BUILD_EXE
echo [INFO] building onefile exe via PyInstaller...
>>"%LM_LOG%" echo [INFO] building onefile exe via PyInstaller...
if exist "dist\%LM_EXE%" del /f /q "dist\%LM_EXE%" >nul 2>&1

uv run --with pyinstaller pyinstaller ^
  --noconfirm ^
  --windowed ^
  --onefile ^
  --name "LOLManager" ^
  --icon "src\lolmanager\resources\assets\lolmanager.ico" ^
  --paths "src" ^
  --add-data "src\lolmanager\resources;lolmanager\resources" ^
  --collect-all "ttkbootstrap" ^
  "src\lolmanager\cli\entrypoint.py" >>"%LM_LOG%" 2>&1

if errorlevel 1 (
    echo [ERROR] pyinstaller failed
    >>"%LM_LOG%" echo [ERROR] pyinstaller failed
    exit /b 5
)

if not exist "dist\%LM_EXE%" (
    echo [ERROR] built exe not found: "dist\%LM_EXE%"
    >>"%LM_LOG%" echo [ERROR] built exe not found: "dist\%LM_EXE%"
    exit /b 6
)

if exist "%LM_EXE%" del /f /q "%LM_EXE%" >nul 2>&1
move /y "dist\%LM_EXE%" "%LM_EXE%" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] failed to move exe to project root
    >>"%LM_LOG%" echo [ERROR] failed to move exe to project root
    exit /b 7
)

echo [INFO] build ok: "%CD%\%LM_EXE%"
>>"%LM_LOG%" echo [INFO] build ok: "%CD%\%LM_EXE%"
exit /b 0

:REINSTALL_SHORTCUTS
>>"%LM_LOG%" echo [INFO] uninstall_shortcuts.bat ...
call "scripts\uninstall_shortcuts.bat" >>"%LM_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] uninstall_shortcuts failed
    >>"%LM_LOG%" echo [ERROR] uninstall_shortcuts failed
    exit /b 8
)

>>"%LM_LOG%" echo [INFO] install_shortcuts.bat ...
call "scripts\install_shortcuts.bat" >>"%LM_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] install_shortcuts failed
    >>"%LM_LOG%" echo [ERROR] install_shortcuts failed
    exit /b 9
)

exit /b 0

:IS_PROCESS_RUNNING
tasklist /FI "IMAGENAME eq %~1" 2>nul | find /I "%~1" >nul
exit /b %ERRORLEVEL%

:WAIT_FOR_EXIT
call :IS_PROCESS_RUNNING "%~1"
if errorlevel 1 exit /b 0

for /L %%I in (1,1,%~2) do (
    timeout /t 1 /nobreak >nul
    call :IS_PROCESS_RUNNING "%~1"
    if errorlevel 1 exit /b 0
)

exit /b 1
