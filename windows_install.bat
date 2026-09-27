@echo off
rem Fengzai video pipeline - Windows installer. Double-click to run.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\install.ps1"
pause
