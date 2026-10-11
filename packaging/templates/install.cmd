@echo off
rem LedgerSpy offline installer (P21). Installs ONLY from the bundled wheelhouse: pip is run with --no-index,
rem so it cannot contact any package index. Safe to re-run: an existing .venv is reused, and no workspace
rem (case data) is touched.
setlocal
cd /d "%~dp0"
set "PY="
py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
  echo ERROR: Python 3.12 64-bit was not found. Install it from the python.org installer included with your
  echo        offline media ^(see INSTALL.md^), then run install.cmd again.
  exit /b 2
)
%PY% -c "import struct, sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 1)"
if errorlevel 1 (
  echo ERROR: this bundle needs 64-bit CPython 3.12 ^(its wheels are cp312 / win_amd64^). Found:
  %PY% -c "import sys, platform; print(sys.version, platform.architecture()[0])"
  exit /b 2
)
if exist ".venv\Scripts\python.exe" (
  echo Reusing the existing .venv
) else (
  echo Creating .venv ...
  %PY% -m venv .venv || ( echo ERROR: could not create .venv & exit /b 2 )
)
set "PIP_NO_INDEX=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PIP_NO_CACHE_DIR=1"
".venv\Scripts\python.exe" -m pip install --no-index --find-links wheelhouse --require-hashes --only-binary=:all: -r requirements.lock
if errorlevel 1 (
  echo ERROR: dependency installation failed. Nothing was downloaded; check that the wheelhouse folder is complete
  echo        ^(compare it with MANIFEST.sha256^).
  exit /b 2
)
if not defined LEDGERSPY_WORKSPACE set "LEDGERSPY_WORKSPACE=%LOCALAPPDATA%\LedgerSpy\workspace"
echo.
echo Checking the installation ^(nothing is changed^):
".venv\Scripts\python.exe" -m ledgerspy doctor --workspace "%LEDGERSPY_WORKSPACE%"
exit /b %errorlevel%
