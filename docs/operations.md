# LedgerSpy Operations and Troubleshooting

For whoever installs, runs, backs up and verifies LedgerSpy. It covers the offline Windows bundle (P21) and
the source repository's release verification (P22).
- **Install and upgrade:** [`installation.md`](installation.md).
- **Daily use:** [`user-guide.md`](user-guide.md).
- **Security model:** [`security.md`](security.md).

Paths are relative to the LedgerSpy application folder (where `install.cmd` is). `<workspace>` is the workspace
folder: `%LOCALAPPDATA%\LedgerSpy\workspace` unless `LEDGERSPY_WORKSPACE` is set.

## 1. `ledgerspy doctor`: is this installation ready?

Run `doctor.cmd`, or `.venv\Scripts\python -m ledgerspy doctor --workspace "<workspace>"`. It changes nothing.

| Line | Checks |
|---|---|
| `python` | The interpreter in use |
| `dependencies` | Every installed package matches `requirements.lock` |
| `frontend` | The UI files are present and contain no inline script or style (strict CSP) |
| `configuration` | The configuration loads and validates; its SHA-256; the server address |
| `workspace` | Exists (or will be created); warns if it is inside the application folder |
| `database` | Readable; its schema version, or that it will be created or upgraded |
| `workspace lock` | Whether a server holds the workspace |
| `port` | 127.0.0.1:<port> is free |
| `pdf renderer` | PDFium and Pillow render a test page |
| `ocr` | Tesseract: executable, version and languages, or why OCR is unavailable |

**Result.**
- **Lines:** each line is `OK`, `WARN` or `FAIL`, then `RESULT: ready` or `RESULT: NOT READY — fix the FAIL lines above`.
- **Exit code:** 0 means ready (warnings allowed, e.g. no OCR); 1 means a check failed.
- **Machine-readable:** `--json` prints the checks as JSON.

## 2. Start, stop and conflicts

| Action | How |
|---|---|
| Start | `ledgerspy.cmd` (options `--port N`, `--no-browser`). It prints `Open: http://127.0.0.1:<port>/?token=…` |
| Stop | **Ctrl+C** (or Ctrl+Break) in the LedgerSpy window, or close it. A graceful stop exits with code 0 and releases the workspace lock |
| Use another workspace | Set `LEDGERSPY_WORKSPACE` before `ledgerspy.cmd`, or pass `--workspace` to the CLI |

| Startup error | Exit | Cause | Fix |
|---|---|---|---|
| "another LedgerSpy server is already using the workspace" | 3 | A server already runs on this workspace (one per workspace, OS file lock) | Use the running one, or stop it |
| "127.0.0.1:<port> is already in use" | 3 | Another program uses the port | `ledgerspy.cmd --port 8766` |
| Configuration invalid; database unreadable; "schema vN is newer than this LedgerSpy"; UI files missing or with inline markup | 2 | — | Fix the configuration; use the newer version; reinstall the application folder |

- **Stale locks.** A crash never leaves a stale lock: the OS releases it when the process ends.
- **Tokens.** The token changes at every start. An old browser tab shows *LedgerSpy is locked*; open the new
  `Open:` link.

## 3. Logs

- **The console window is the only output.** LedgerSpy writes **no log file**. Startup messages, the entry link
  and errors appear in the LedgerSpy console window.
  - HTTP access logging is switched off, because the entry link carries the session token.
  - To keep a record of a session's console output, copy it from the window. It contains the token, which is
    worthless after the server stops.
- **What is recorded where:**

  | Record | Where |
  |---|---|
  | Every write (imports, fixes, runs, investigation actions, reports) | The **hash-chained audit log** in `ledgerspy.db` (§6) |
  | Reconciliation runs | The run register |
  | Reports | The report register |
  | PDF extraction results and warnings | Stored with each extraction and shown in the Bank Recon tab |

## 4. Workspace contents

| Path | Content |
|---|---|
| `ledgerspy.db` | SQLite database: cases, datasets, canonical rows, issues, transformations, investigations, registers, audit log |
| `evidence\` | Read-only copy of every imported file, named by its SHA-256 |
| `reports\` | Stored report artefacts (JSON / HTML / Markdown), read-only, hash-checked on every read |
| `models\`, `experiments\`, `inference\` | Supervised model artefacts and records (CLI only, optional) |
| `backups\` | Read-only database copies made before schema upgrades |
| `tmp\` | Upload staging. A `.part` file exists only while a file is being received |
| `.ledgerspy.lock` | Lock file of the running server |

**Do not edit, rename or delete files in the workspace by hand.** Database triggers make records append-only, and
`audit verify` reports a changed schema.

## 5. Backup, restore and migrations

**Back up.**
1. Stop LedgerSpy.
2. Copy the **whole** workspace folder. Evidence must stay with the database that records it.
3. Check the copy:
   ```bat
   .venv\Scripts\python -m ledgerspy audit verify --workspace "D:\backup\workspace"
   .venv\Scripts\python -m ledgerspy evidence verify --workspace "D:\backup\workspace"
   ```

**What the checks print.**
- **`audit verify`** prints `OK: … N entries verified; T triggers intact; head seq S hash H`. Exit codes: 0 OK,
  1 failure, 2 database not found or unreadable.
- **`evidence verify`** prints `OK: … N evidence files match their recorded SHA-256 and are read-only`, or a
  `FAIL` line per missing or altered file. It uses the same exit codes.

**Restore.**
1. Stop LedgerSpy.
2. Move the damaged workspace aside. Do not delete it.
3. Copy the backup into place.
4. Run `doctor.cmd`, `audit verify` and `evidence verify`.

**Migrations.**
- **Automatic backup.** A newer LedgerSpy upgrades an older workspace database on its first start. It first
  writes a read-only copy `<workspace>\backups\ledgerspy-v<old>-before-v<new>-<UTC time>.db`.
- **Atomic.** The upgrade is one transaction: a failure leaves the database unchanged.
- **No downgrades.** A database newer than the application is refused, never downgraded. Backups are never
  deleted by LedgerSpy.
- **Going back to the old version after an upgrade.** Use a full workspace backup taken before the upgrade.
  - The `backups\…db` file holds only the database, not files added to `evidence\` or `reports\` later.
  - To use it anyway: copy the workspace, replace the copy's `ledgerspy.db` with a writable copy of the backup
    file, and run the old version on that copy.
  - Files added after the backup are then unregistered (§7).

## 6. Audit trail and the external anchor

**What the audit log does.** Every write appends an entry whose hash covers the previous entry.

**What `audit verify` and `GET /api/audit/verify` check:**
- the whole chain;
- every database trigger against the ones the migrations create.

**Detected:** an edited, inserted, reordered or deleted entry, and a missing, altered or extra trigger.

**Not detected from inside the database:**
- truncation of the newest entries;
- a consistent rewrite of the whole chain by someone who controls the file and the code;
- an edit made while a trigger was dropped and then restored exactly.

**The anchor procedure** (detects truncation or a rewrite up to the anchor):
1. **Record the anchor.** After important work (e.g. when an engagement's analysis is complete), run
   `audit verify`. Record its `head seq` and `hash` **outside the workstation**: in the engagement file, on
   paper, or in an e-mail to yourself.
2. **Check against it later.** With the server running, take the token from the `Open:` link and call the API
   from PowerShell:
   ```powershell
   $token = "<token from the Open: link>"
   Invoke-RestMethod -Headers @{ Authorization = "Bearer $token" } `
     "http://127.0.0.1:8765/api/audit/verify?anchor_seq=<seq>&anchor_hash=<hash>"
   ```
3. **Read the result.**
   - `ok` and `anchor.matches` are true when the anchored entry still exists with the same hash and the chain
     verifies.
   - Otherwise `anchor.reason` says whether the entry is missing (truncated) or differs (rewritten).

**Limits of the anchor procedure:**
- The anchor check exists **only in the API**: there is no CLI option or UI control for it.
- It proves nothing about entries added after the anchor.
- An anchor kept on the same machine can be changed together with the database.

## 7. Recovering from interrupted operations

| Situation | What happens | What to do |
|---|---|---|
| Server stopped or crashed during an upload | The upload is not registered; nothing is half-imported. A hard kill can leave `<workspace>\tmp\upload-….part` | With LedgerSpy stopped, delete leftover `.part` files in `tmp\` (they are never evidence) and import again |
| Hard kill exactly while an import was being committed | Rarely, an evidence file without a database record remains. Importing the same file again then fails with `evidence_conflict` ("An unregistered file already occupies …") | Stop LedgerSpy and move (do not delete) the named file from `evidence\` to a folder outside the workspace. Import again |
| Server stopped during a PDF / OCR extraction | Nothing is registered for an unfinished extraction. After a hard kill (e.g. Task Manager) the worker process may run until its own timeout (15 min) | Wait or end the worker process, then **Re-run extraction** or import again |
| Extraction failed or timed out | The extraction records the reason | **Re-run extraction**; split very large scanned PDFs |
| Machine short of memory during OCR | There is no memory cap on the worker or Tesseract | Close other programs; split the PDF |
| Detached release verification interrupted | `run.json` stays `complete: false` and `status` reports `interrupted` (exit 2) | Run it again (§8) |

## 8. Missing OCR components

**Without Tesseract:**
- `doctor` shows `WARN ocr` with the reason, and the Bank Recon tab shows *Local OCR UNAVAILABLE*.
- Text PDFs, CSV and XLSX still work.
- Scanned pages are reported as *not processed*. Nothing is guessed and there is no cloud fallback.

**To enable OCR:**
1. Install Tesseract 5 with the `eng` language data from an installer obtained beforehand (installation §5).
2. Run `doctor.cmd` and check the `ocr` line.
3. **Re-run extraction** for statements imported while OCR was missing.

## 9. Release verification (source repository)

The checks below run from a **development checkout** of the source repository, not from the offline bundle,
which contains neither `tools\` nor the tests:
```bat
.venv\Scripts\python tools\verify.py --offline --detach
.venv\Scripts\python tools\verify.py status verify-results\<run> --wait
.venv\Scripts\python tools\verify.py package-check --bundle dist\<bundle>.zip
```

**What runs.**
- **Required:** Ruff (lint and format), the backend test suite, the Node frontend harness, the browser check, the
  P4 integrity-panel check, and validation of the newest `dist\*-offline.zip`.
- **Optional (`--offline`):** an installation of the bundle in a temporary folder, under a network guard.

**Exit codes.**

| Exit | Meaning |
|---|---|
| 0 | Every required check passed |
| 1 | A required check failed |
| 2 | Incomplete or interrupted: a check could not run, produced no fresh report, or the run did not finish |
| 3 | Usage error |

**Results** go to `verify-results\<UTC time>\`:
- `run.json` (status, counts, completion marker);
- one log per check;
- the JUnit XML and JSON reports the counts were read from.

Details: [`verification.md`](verification.md).

## 10. Verifying a package and keeping earlier releases

**The release zip.**
1. Compare its SHA-256 with the value published with the release (`build_bundle.py` prints it):
   ```powershell
   (Get-FileHash -Algorithm SHA256 .\ledgerspy-0.2.0-win_amd64-py312-offline.zip).Hash.ToLower()
   ```
2. After unzipping, check every file against `MANIFEST.sha256`, in PowerShell from the application folder:
   ```powershell
   $bad = Get-Content MANIFEST.sha256 | ForEach-Object { $h, $p = $_ -split '  ', 2; if ((Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLower() -ne $h) { $p } }
   if ($bad) { "MISMATCH: $bad" } else { "OK: every file matches MANIFEST.sha256" }
   ```

   This detects an altered or missing listed file. It cannot detect a manifest regenerated together with the
   files, so the zip's own published hash (step 1) is the anchor. The source repository's
   `tools\verify.py package-check` additionally compares every file with its source commit and rebuilds the zip
   byte for byte.
3. `BUILD-INFO.json` names the version, the source commit, and whether the build tree was clean.

**Earlier releases.**
- **Keep previous release zips** with their published hashes.
- **The builder does not overwrite.** When `build_bundle.py` writes a bundle whose name exists with different
  content, it renames the earlier file to `…offline.<first 12 hex of its SHA-256>.zip`.
- **Install each release in its own folder.** The workspace is shared and outside the folder (installation §6).
