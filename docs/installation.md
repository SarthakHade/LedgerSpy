# Installing and Running LedgerSpy Offline (P21)

**What this bundle is.**
- **A folder application.** It contains the LedgerSpy source, its UI, the default configuration, the synthetic
  demo data, and a **wheelhouse** with every Python dependency as a pinned, hash-checked wheel.
- **Not a standalone executable.** It needs a locally installed Python.
- **No downloads after setup.** Nothing is downloaded during installation or at run time.
- **Offline operation as tested.** On the development machine, every feature ran with no non-loopback connection
  attempt, under a process-level network guard ([`p21-acceptance.md`](p21-acceptance.md)).
- **Not yet tested:** a fresh install and run on the target laptop with networking physically disabled. That
  step remains to be done.

**What is and is not in the bundle.**

| Bundled | Not bundled (install separately, from offline media) |
|---|---|
| The `ledgerspy` application, the UI (`frontend\`), `config\default.yaml` | **Python 3.12** (64-bit CPython from python.org) |
| The documentation (`docs\`, `README.md`, this guide as `INSTALL.md`) | **Tesseract 5** with `eng` data (optional, scanned PDFs only) |
| The synthetic demo company `data\synthetic\reference\` (no ground truth) | A browser |
| `wheelhouse\` (every locked dependency wheel) and `requirements.lock` | The release verification tools and tests (`tools\`, `tests\`): they are in the source repository only |
| `install.cmd`, `ledgerspy.cmd`, `doctor.cmd`, `BUILD-INFO.json`, `MANIFEST.sha256` | Any client data or workspace (never packaged) |

The bundle is built and verified for **Windows x64 only**. Other operating systems are **unverified**.

## 1. Supported platform and prerequisites

| Item | Requirement | Notes |
|---|---|---|
| Operating system | **Windows 10 / 11, 64-bit** (tested on Windows 11) | The wheelhouse is built for `win_amd64`. Other systems need a wheelhouse built for them (§8); they are not tested |
| Python | **CPython 3.12, 64-bit**, from the python.org Windows installer | **Not included in the bundle:** put the python.org installer on your offline media. Tested with 3.12.4. Other minor versions are refused by `install.cmd` (the wheels are `cp312`) |
| Disk | ~150 MB for the application and its `.venv`, plus your workspace | |
| Tesseract OCR (optional) | Tesseract 5 with the `eng` language data | Only for **scanned** PDF statements. Without it, text PDFs, CSV and XLSX work and scanned pages are reported as not processed. See §5 |
| Browser | A current Chromium-based browser, Firefox or Edge | The UI loads nothing from the internet |

## 2. Install (no network needed)

1. Install Python 3.12 (64-bit) with the python.org installer. Keep the "py launcher" option ticked.
2. Unzip `ledgerspy-<version>-win_amd64-py312-offline.zip` to a folder you can write to, e.g.
   `C:\LedgerSpy\ledgerspy-0.2.0`.
3. Recommended: check the zip's SHA-256 against the value published with the release, and every unzipped file
   against `MANIFEST.sha256`. The PowerShell commands are in [`operations.md`](operations.md) §10.
4. Double-click **`install.cmd`**, or run it in a terminal. It:
   - finds 64-bit Python 3.12 (`py -3.12`, else `python`) and refuses any other version;
   - creates `.venv\` inside the application folder (reused if it already exists);
   - runs `pip install --no-index --find-links wheelhouse --require-hashes -r requirements.lock`, so pip cannot
     contact any index, and a wheel whose hash differs from the lock is refused;
   - runs `ledgerspy doctor`, which reports what is ready. It never changes your data.

Installation never creates, resets or overwrites a workspace.

**Repeat installation** (same version, e.g. after an interrupted install or a damaged `.venv`):
- Run `install.cmd` again. It reuses `.venv\` and installs any missing locked package from the wheelhouse,
  with the same hash checks.
- If `doctor` still reports `FAIL dependencies`, delete `.venv\` (only that folder) and run `install.cmd` again.
- The workspace is not touched.

## 3. Run

- **Start:** double-click **`ledgerspy.cmd`**. It prints a line such as `Open: http://127.0.0.1:8765/?token=…`
  and opens your browser at it. The token is new at every start.
- **Workspace:** by default `%LOCALAPPDATA%\LedgerSpy\workspace`. It holds the database `ledgerspy.db`,
  `evidence\`, `reports\`, `models\` and `backups\`. Set the environment variable `LEDGERSPY_WORKSPACE` to use
  another folder; prefer one in your user profile on an encrypted disk, not a synced or shared folder.
- **Options:** `ledgerspy.cmd --port 8766` or `ledgerspy.cmd --no-browser`.
- **Stop:** press **Ctrl+C** in the LedgerSpy window, or close it. Any running PDF / OCR extraction has a
  timeout and is stopped with its job.

**What the launcher enforces.**
- The server binds to `127.0.0.1` only; any other address is refused.
- Every API call needs the per-launch token, and the UI runs under a strict Content-Security-Policy (P20).
- **One server per workspace.** A second `ledgerspy.cmd` on the same workspace stops with "another LedgerSpy
  server is already using the workspace" (exit code 3). A port already in use is reported the same way.
- **Database upgrades.** Starting a newer LedgerSpy on an older workspace upgrades its database. A read-only copy
  is first saved as `backups\ledgerspy-v<old>-before-v<new>-<time>.db`, and the upgrade itself is one
  transaction. A database newer than the application is refused, never downgraded.

**Startup errors** are one-line messages:

| Exit code | Meaning |
|---|---|
| 2 | Configuration invalid, database unreadable or newer, UI files missing or altered |
| 3 | Workspace already in use, or port in use |

## 4. Demo data

`data\synthetic\reference\` contains the synthetic company (no real data). Import `SYNTHETIC_ledger.csv` and
`SYNTHETIC_bank_statement.csv` from the UI to try every feature. The evaluation ground truth is deliberately not
shipped.

**Master data (vendors, employees, customers, group companies).** Master files such as
`SYNTHETIC_*_master.csv` are imported from a command prompt in the LedgerSpy folder.
- **Which case.** Use the case the UI shows: in the Relationships tab, the *Entity Similarity Matrix* card names
  it ("… for case C-…").
- **Which workspace.** Name the workspace that `ledgerspy.cmd` serves. Without `--workspace` the command uses
  `.\workspace` in the current folder, which is not the UI's workspace, so the case is not found.
- **The server can keep running.**

```bat
.venv\Scripts\python -m ledgerspy entities import-master --workspace "%LOCALAPPDATA%\LedgerSpy\workspace" --case-id C-… --file data\synthetic\reference\SYNTHETIC_vendor_master.csv --entity-type vendor --use-suggested
```

- `--entity-type` is `vendor`, `employee`, `customer` or `group_company`.
- `--use-suggested` accepts the suggested column mapping explicitly. Without it the command prints the suggestion
  and imports nothing; give the mapping with repeated `--map field=column` instead.
- If you set your own `LEDGERSPY_WORKSPACE`, pass that folder instead.
- `… entities import-master --help` lists every option. Open the Relationships tab again to see the result.

## 5. OCR (scanned PDF statements)

- **Search order.** LedgerSpy looks for Tesseract in the order given by `pdf.ocr.executable_candidates` in
  `config\default.yaml`: `tesseract` on PATH, then `C:\Program Files\Tesseract-OCR\tesseract.exe`, then common
  Unix paths. Tesseract does not need to be on PATH.
- **Installation.** Install Tesseract 5 offline from an installer you have obtained beforehand (e.g. the
  UB-Mannheim Windows build), including the `eng` language data. Then run `doctor.cmd`: the `ocr` line shows
  the executable, version and languages found, or the exact reason OCR is unavailable.
- **Without Tesseract.** Scanned pages are reported as *not processed*. Nothing is guessed, and there is no cloud
  fallback.
- **Limits.**
  - **Enforced:** 200 pages per PDF, 40 Mpx per rendered page, 120 s per OCR page, 15 min per extraction.
  - **Not enforced:** there is **no memory cap** on the extraction worker or on Tesseract. A very large scanned
    statement can use a lot of memory; split it if the machine struggles.

## 6. Upgrade

1. Stop LedgerSpy.
2. Optional: back up the workspace (§7).
3. Unzip the new bundle into a **new** folder, run its `install.cmd`, then start it with its `ledgerspy.cmd`.
   - The workspace lives outside the application folder, so the new version uses it unchanged.
   - Its database is upgraded after an automatic backup (§3).
4. Remove the old application folder when satisfied. Never delete the workspace to "clean up".

To go back, run the old version against a copy of the workspace restored from the pre-upgrade backup. An old
version refuses a database upgraded by a newer one.

## 7. Backup and recovery

- **Back up:** with LedgerSpy stopped, copy the whole workspace folder: `ledgerspy.db`, `evidence\`,
  `reports\`, `models\`, `backups\`. The evidence files are content-addressed and read-only; keep them
  together with the database.
- **Check a copy:** run `.venv\Scripts\python -m ledgerspy audit verify --workspace <copy>` and
  `… evidence verify --workspace <copy>`. They report the audit chain, trigger integrity, the head entry, and any
  missing or altered evidence file.
- **Anchor:** record the `head seq` and `hash` printed by `audit verify` somewhere outside the workstation.
  Passing them back later (`/api/audit/verify?anchor_seq=…&anchor_hash=…`) detects truncation or a rewrite of
  the log up to that point (P20 §4). The exact procedure and its limits: [`operations.md`](operations.md) §6.
- **Restore:** stop LedgerSpy, replace the workspace folder with the backup copy, start, run `doctor.cmd`.

## 8. Building the bundle (connected build machine)

Building is the only step that needs network access, and it runs on the build machine, never on the target:

```bat
py -3.12 -m venv .venv && .venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\python -m pip download --only-binary=:all: --platform win_amd64 --python-version 3.12 ^
    --implementation cp -d wheelhouse -r <runtime requirements> -c <constraints of the tested venv>
.venv\Scripts\python packaging\make_lock.py --wheelhouse wheelhouse
.venv\Scripts\python packaging\build_bundle.py --wheelhouse wheelhouse --out dist
```

- **Inputs:** `<runtime requirements>` are the exact pins of `pyproject.toml`; the constraints file is
  `pip freeze` of the tested development environment, so the transitive versions are exactly the tested ones.
- **Lock:** `make_lock.py` writes `requirements.lock` with the SHA-256 of every wheel.
- **Bundle:** `build_bundle.py` copies only allow-listed, git-tracked files and the locked wheels (re-checking
  every hash). It refuses databases, tokens, workspaces, caches and user-specific paths, and writes a
  reproducible zip with `MANIFEST.sha256` and `BUILD-INFO.json`.
- **Vulnerability audit:** `pip-audit` (or an equivalent advisory check) belongs here, on the connected machine.
  It is not part of the offline bundle.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `install.cmd`: "Python 3.12 64-bit was not found" / "needs 64-bit CPython 3.12" | Install the 64-bit python.org 3.12 installer; other versions cannot use the bundled wheels |
| `install.cmd`: "dependency installation failed" | A wheel is missing or altered: compare with `MANIFEST.sha256`; unzip the bundle again |
| `doctor`: `FAIL dependencies` | The `.venv` was changed by hand: delete `.venv\` and run `install.cmd` again |
| `doctor`: `WARN ocr` | Tesseract or its `eng` data is missing; see §5 (optional) |
| `doctor`: `WARN workspace … inside the application folder` | Use the default workspace or set `LEDGERSPY_WORKSPACE` outside the application folder |
| Start: "another LedgerSpy server is already using the workspace" | One is already running (another window); use it or stop it |
| Start: "127.0.0.1:8765 is already in use" | Another program uses the port: `ledgerspy.cmd --port 8766` |
| Start: "schema vN is newer than this LedgerSpy" | The workspace was opened with a newer version; use that version |
| Browser shows "LedgerSpy is locked" | Open the full `Open:` link printed in the window (the token changes at each start) |
