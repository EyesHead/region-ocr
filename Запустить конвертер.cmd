@echo off
cd /d "%~dp0"
if exist "%~dp0DocumentConverter.exe" (
  start "" "%~dp0DocumentConverter.exe" %*
  exit /b 0
)
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" -s -B "%~dp0portable_runtime.py" --open-browser %*
  if errorlevel 1 pause
  exit /b
)
python "local_web\server.py" --open-browser
if errorlevel 1 pause
