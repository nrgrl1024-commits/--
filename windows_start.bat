@echo off
rem Fengzai video pipeline - keep this window open (minimize is fine).
cd /d "%~dp0"
title fengzai-video
if not exist ".venv\Scripts\python.exe" (
  echo Please double-click windows_install.bat first.
  pause
  exit /b 1
)
set PYTHONUTF8=1
:loop
".venv\Scripts\python.exe" -m fengzai_video watch --interval 120
echo Stopped unexpectedly. Restarting in 30 seconds...
timeout /t 30 >nul
goto loop
