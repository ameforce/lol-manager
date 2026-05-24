@echo off
setlocal
rem 스크립트 위치를 기준으로 uv 실행
cd /d "%~dp0"

rem 상시 GUI 실행(권장)
uv run lolmanager %*
