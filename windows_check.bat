@echo off
rem Check that Feishu, Doubao and ffmpeg are configured correctly.
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Please double-click windows_install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m fengzai_video check
pause
