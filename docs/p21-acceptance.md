# P21 — Reproducible Offline Packaging: Acceptance Record

Date: 2026-10-11 · Scope: [`roadmap.md`](roadmap.md) §P21, on top of P20 (`56fa2b2`). P22 is not started.
User guide: [`installation.md`](installation.md).

## 1. What is delivered

| Item | Where | Notes |
|---|---|---|
| Dependency lock | `requirements.lock` | The complete runtime closure (19 packages: the 9 direct pins of `pyproject.toml` plus 10 transitive ones), exact versions, SHA-256 per wheel. The versions are those of the tested development environment (checked by a test) |
| Lock generator | `packaging/make_lock.py` | From a prepared wheelhouse; refuses if a direct pin is missing or differs |
| Bundle builder | `packaging/build_bundle.py` | Allow-listed, git-tracked (or new, not ignored) files plus the locked wheels, each re-checked against the lock. Reproducible zip (sorted entries, fixed timestamps). Refuses databases, lock files, workspaces, caches, ground truth, session tokens and user-specific paths. Writes `MANIFEST.sha256` and `BUILD-INFO.json`. Downloads nothing |
| Bundle | `ledgerspy-0.2.0-win_amd64-py312-offline.zip` (≈ 15 MB, 248 files) | Application, UI, config, docs (not `docs/audits/`), synthetic demo data **without ground truth**, `wheelhouse/` (19 wheels), `install.cmd`, `ledgerspy.cmd`, `doctor.cmd`, `INSTALL.md`. **Not** a standalone executable |
| Installer | `install.cmd` | Requires 64-bit CPython 3.12. Creates or reuses `.venv`, then installs with `--no-index --find-links wheelhouse --require-hashes --only-binary=:all:` and `PIP_NO_INDEX=1`, then runs `doctor` |
| Launcher | `ledgerspy.cmd` | `serve` with an explicit workspace: `%LEDGERSPY_WORKSPACE%`, default `%LOCALAPPDATA%\LedgerSpy\workspace` (outside the application folder) |
| Diagnostics | `ledgerspy doctor` / `doctor.cmd` | Checks Python, the locked dependencies, the UI and strict CSP, the configuration, the workspace (writable; warns if inside the app folder), the database (current / will upgrade / newer = fail / unreadable = fail, SQLite `quick_check`), the workspace lock, the port, the PDF renderer (renders a test page) and Tesseract (the configured candidates, version and languages). It **creates nothing** |
| Startup safety | `ledgerspy serve` | One server per workspace (OS lock; a crash leaves no stale lock); port check; one-line errors (exit 2 = configuration / database / UI, 3 = workspace or port in use); graceful Ctrl+C / Ctrl+Break / SIGTERM stop with exit code 0 and the lock released |
| Upgrade safety | `init_db` | Before migrating an existing database, a consistent read-only copy goes to `<workspace>/backups/ledgerspy-v<old>-before-v<new>-<time>.db`. The migration is one transaction. A newer database is refused, never downgraded. Nothing ever resets a workspace |
| Offline verification | `packaging/offline_check.py` + `packaging/netguard/` | §3 |
| Guide | `docs/installation.md` (= `INSTALL.md` in the bundle) | Prerequisites, install, run, OCR, upgrade, backup / recovery, building, troubleshooting |

**Dependency note.** FastAPI 0.142 hard-requires `opentelemetry-api`. It is the API package only: no SDK and no
exporter are installed, so its default tracer does nothing. The offline check confirms that no process attempts
a connection.

**Paths.** LedgerSpy runs from its application folder: `ledgerspy/paths.py` resolves `frontend/` and
`config/` next to the package. The bundle therefore installs only the **dependencies** into `.venv`, not
LedgerSpy itself as a wheel. Workspace, evidence, reports, models and backups live under the workspace given to
`serve` (the launcher's default is `%LOCALAPPDATA%\LedgerSpy\workspace`).

## 2. Supported platform

| | |
|---|---|
| Tested | Windows 11 x64, CPython 3.12.4 (python.org), Tesseract 5.4.0.20240606 (UB-Mannheim) at `C:\Program Files\Tesseract-OCR`, which is **not on PATH**: found through `pdf.ocr.executable_candidates` |
| Supported by the bundle | Windows 10 / 11 x64 with 64-bit CPython 3.12 (the wheels are `cp312-win_amd64` / `py3-none-win_amd64`) |
| Not bundled (prerequisites) | The Python 3.12 interpreter and Tesseract OCR (optional). Both must be installed from installers placed on the offline media |
| Not supported / not tested | Other Python versions, 32-bit Python, macOS / Linux. The code is portable, but these need their own wheelhouse and have not been tested |

## 3. Offline verification (evidence)

`packaging/offline_check.py --bundle <zip> --work <empty dir>` installs and exercises the **built bundle** in a
fresh folder:
- **Network guard:** every Python process of the installation (pip during install, the server, the PDF / OCR
  worker it spawns) runs with `packaging/netguard/sitecustomize.py` on `PYTHONPATH`. It refuses and logs any
  non-loopback DNS lookup or connection.
- **Netstat:** `netstat` samples the installation's processes about every second: every process started from
  its `.venv`, **all of their descendants**, and `tesseract.exe`. On Windows the venv's `python.exe` is a
  launcher whose child is the *base* interpreter that actually runs the server. A first version of the check
  sampled only the launcher; this was found and fixed. The check now also verifies that each server process
  (its PID from its own log line) was among the processes sampled.
- **Leftovers:** after each stop, every recorded process (PID plus creation time, so PID reuse cannot hide
  one) must be gone.

Final run, on a bundle built after every code change of this phase (§5):

Run: 26 passed, 0 failed, 67 s. Installed Python: Python 3.12.4. Netstat: 67 samples over 23 processes of the installation (including the 3 server processes). Bundle built from a working tree with uncommitted changes (base `56fa2b2`); the release bundle is rebuilt from the commit and compared with this build file by file (`MANIFEST.sha256`): only this document and `BUILD-INFO.json` may differ.

| # | Step | Result | Detail |
|---|---|---|---|
| 1 | bundle manifest | pass | 248 files |
| 2 | install.cmd run 1 | pass | RESULT: ready |
| 3 | install.cmd run 2 (re-run reuses .venv) | pass | RESULT: ready |
| 4 | workspace not created by install/doctor | pass | <work>\offline1\workspace |
| 5 | start via ledgerspy.cmd on a fresh workspace | pass | http://127.0.0.1:8791 |
| 6 | import + P4 integrity | pass | dataset D-01M4MRFRAG23SF9A84385Q1DHP |
| 7 | P7/P8/P9 analysis | pass | 7271 rows |
| 8 | P12 entity resolution | pass |  |
| 9 | P13 graph | pass | 5 cycles |
| 10 | P14 reconciliation (CSV) | pass | {"exact_match": 7104, "probable_match": 145, "grouped_match": 2, "partial_match": 7, "ambiguous": 3, "unmatched_ledger": 3, "unmatched_bank": 3} |
| 11 | P14b text PDF extraction | pass | 60 rows |
| 12 | P14b OCR (Tesseract) + review | pass | pages ['ocr']; 12 reviewed |
| 13 | page image rendering | pass | 58405 bytes |
| 14 | P15 investigation workflow | pass | IV-01M4MRG5HSDG4EG2C4N80BNHF4 |
| 15 | P16 reports JSON/HTML/MD | pass | RP-01M4MRG5NE1NPKNBT5X0WWEFSZ |
| 16 | audit chain + triggers verify | pass | 32 entries |
| 17 | UI loads and every tab opens | pass | 10 tabs, 27 requests |
| 18 | UI: no non-loopback request, no CSP violation | pass | external [], csp [] |
| 19 | second server on the same workspace refused | pass | ERROR: another LedgerSpy server is already using the workspace <work> |
| 20 | stop of ledgerspy.cmd with Ctrl+Break: no process of the installation left, lock released | pass | exit 0 (cmd.exe), remaining pids [], lock free True |
| 21 | restart on the same workspace: data intact, audit verifies | pass | 4 datasets, 32 audit entries |
| 22 | graceful stop of the server with Ctrl+Break: exit code 0, 'stopped cleanly' | pass | exit 0 |
| 23 | doctor on an older workspace announces the upgrade | pass |  |
| 24 | existing v8 workspace upgraded with a backup; data kept | pass | schema v9, backup ['ledgerspy-v8-before-v9-20261011T055606814419Z.db'] |
| 25 | no guarded network attempt (pip, server, PDF/OCR worker) | pass |  |
| 26 | no non-loopback TCP connection by the installation (netstat) | pass | 67 samples over 23 processes of the installation; the 3 server processes were sampled: True; foreign [] |

**What this does and does not prove.**
- **Proven:**
  - pip installed from the bundled wheelhouse alone;
  - no Python process of the installation attempted a non-loopback lookup or connection;
  - no process of the installation, Tesseract included, held a non-loopback TCP connection while every feature
    ran;
  - the UI made no non-loopback request.
- **Not done:** the workstation's network adapter was **not** disabled. That requires changing system settings,
  which this work does not do. The guard covers Python processes, and `netstat` covers the rest of the
  installation.
- **Final acceptance step:** install from the zip on the target laptop with Wi-Fi off and the cable unplugged,
  as the roadmap asks ("verified … manually"). It remains to be performed by the user. The guide's §2–§4 steps
  are exactly what the check automates.
- **One transient failure:** during development one run of the check failed once with a loopback connect
  timeout (`WinError 10060`) on its first API call, after a successful health check. An identical rerun passed
  all steps, and the failure was not reproduced afterwards. It is recorded here, not hidden; it was not traced
  to a cause.

## 4. Tests (`tests/backend/test_packaging_p21.py`, 15)

| Test | What it proves |
|---|---|
| `test_invalid_configuration_is_refused_with_a_clear_error` | Non-loopback host refused by the schema; malformed YAML gives `doctor` FAIL and `serve` exit 2 with "ERROR: configuration" |
| `test_doctor_reports_without_creating_anything` | All ten checks reported; the dependencies match; the strict CSP holds; a missing workspace is reported, not created |
| `test_doctor_explains_missing_ocr_and_stays_ready` | Missing Tesseract → `warn` with the reason and the consequence; no `fail` |
| `test_doctor_flags_a_newer_or_older_database` | A newer schema is a `fail` |
| `test_one_server_per_workspace` | A second lock is refused; a real second `serve` process exits 3 before touching the database; the lock is reusable once released |
| `test_port_in_use_is_reported_before_startup` | Exit 3, clear message, no database created |
| `test_a_newer_database_stops_startup_cleanly` | Exit 2, clear message, lock released |
| `test_upgrading_an_existing_workspace_backs_it_up_first` | v8 → v9: one read-only backup holding the v8 schema and the data, the data kept; no second backup when already current |
| `test_a_fresh_workspace_needs_no_backup` | No backup for a new database |
| `test_requirements_lock_is_complete_hashed_and_matches_the_tested_environment` | Every pin hashed; direct pins = `pyproject.toml`; every locked version = the version installed in the test environment |
| `test_bundle_contains_only_the_application_and_its_metadata` | **Present:** required files and the CRLF batch files using `--no-index --require-hashes`. **Absent:** tests, archive, packaging, ground truth, caches, databases, workspace, raw data, `docs/audits`. **Manifest:** covers every file with correct hashes |
| `test_bundle_is_reproducible` | Two builds → identical zip SHA-256 |
| `test_bundle_scan_refuses_secrets_paths_and_data` | A token, Windows and Unix user paths, a database, a `.pyc` and ground truth are each refused; `%LOCALAPPDATA%` / `Program Files` text is allowed |
| `test_bundle_refuses_a_wheel_that_does_not_match_the_lock` | A wheel whose hash differs from the lock stops the build |
| `test_the_offline_checks_network_guard_blocks_and_logs` | The guard blocks and logs a non-loopback connect and a DNS lookup, and allows loopback |

## 5. Commands and results (final code)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider -rs tests/backend
node tests/frontend/harness.js
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py
.venv/Scripts/python packaging/build_bundle.py --wheelhouse <wheelhouse> --out <dist>
PYTHONIOENCODING=utf-8 .venv/Scripts/python packaging/offline_check.py --bundle <zip> --work <empty dir>
```

| Check | Result |
|---|---|
| Backend suite | 1212 passed, 0 failed, 0 skipped, 0 errors (exit 0; 1164 s; P20 baseline 1197 + 15 new) |
| P21 tests | 15 passed (in the run above) |
| Node harness | 13 passed, 0 failed |
| Browser check | 22 passed, 0 failed (A4: 189 requests, none external, no CSP violation) |
| P4 integrity panel check | 6 passed, 0 failed |
| Ruff | check clean; format clean (267 files) |
| Offline check | 26 passed, 0 failed (§3) |

## 6. Behaviour changes

- `ledgerspy serve`:
  - refuses a workspace already served, or a busy port (exit 3), and reports configuration, database and UI
    errors in one line (exit 2) instead of a traceback;
  - backs the database up before a schema upgrade;
  - on Ctrl+C / Ctrl+Break / SIGTERM, exits 0 after uvicorn's graceful shutdown. Previously, Ctrl+Break ended
    the process with code 3 after shutdown, and the workspace lock (new in P21) would have been released by
    the OS rather than by `serve`.
- New command `ledgerspy doctor`.
- P9: no change to configuration, features or analytics code. Risk results are unchanged.

## 7. Remaining limitations

- **Interpreter not bundled:** Python 3.12 (and optionally Tesseract) must be installed from the offline media;
  `install.cmd` checks the version and architecture.
- **Windows x64 only** for the built bundle; other platforms need their own wheelhouse and are untested.
- **No standalone executable:** PyInstaller was not used.
- **No memory cap** on the PDF / OCR worker or Tesseract (timeouts, page and pixel limits only).
- **Hard kills (not tested):** if the server is killed hard (Task Manager) during a PDF extraction, the worker
  subprocess may keep running until its own timeout (15 min). SQLite and the evidence store stay consistent, and nothing is
  registered for an unfinished extraction.
- **`pip-audit`:** vulnerability auditing of the locked versions is a build-machine step and was not run here
  (network).
- **Network test:** the offline test isolated the installation's processes, not the machine (§3). The final
  disconnected-machine check is the user's to run.
