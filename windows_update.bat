@echo off
rem Update to the latest code. Keeps .env, config.yaml, fonts, music and work files.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\update.ps1"
pause
