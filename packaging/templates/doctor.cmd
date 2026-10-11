@echo off
rem Check this LedgerSpy installation (P21). Changes nothing.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: LedgerSpy is not installed in this folder yet. Run install.cmd first.
  exit /b 2
)
if not defined LEDGERSPY_WORKSPACE set "LEDGERSPY_WORKSPACE=%LOCALAPPDATA%\LedgerSpy\workspace"
".venv\Scripts\python.exe" -m ledgerspy doctor --workspace "%LEDGERSPY_WORKSPACE%" %*
exit /b %errorlevel%
