@echo off
cd /d "%~dp0"
if exist "%~dp0runtime\pythonw.exe" (
  start "" "%~dp0runtime\pythonw.exe" -s "%~dp0configure_report_telegram.py" %*
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%~dp0configure_report_telegram.py" %*
  exit /b 0
)
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 "%~dp0configure_report_telegram.py" %*
  exit /b 0
)
python "%~dp0configure_report_telegram.py" %*
if errorlevel 1 pause
