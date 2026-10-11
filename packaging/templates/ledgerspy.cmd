@echo off
rem Start LedgerSpy (P21). Serves the UI and API on 127.0.0.1 only and prints the entry link.
rem Workspace (case data, evidence, reports): %LEDGERSPY_WORKSPACE%, default %LOCALAPPDATA%\LedgerSpy\workspace,
rem outside this folder so upgrades never touch it. Stop with Ctrl+C in this window.
rem Extra arguments are passed on, e.g.  ledgerspy.cmd --port 8766 --no-browser
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: LedgerSpy is not installed in this folder yet. Run install.cmd first.
  exit /b 2
)
if not defined LEDGERSPY_WORKSPACE set "LEDGERSPY_WORKSPACE=%LOCALAPPDATA%\LedgerSpy\workspace"
".venv\Scripts\python.exe" -m ledgerspy serve --workspace "%LEDGERSPY_WORKSPACE%" %*
exit /b %errorlevel%
