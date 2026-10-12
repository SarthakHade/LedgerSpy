# Release Verification (P22)

One command runs every required check of a LedgerSpy release and records exactly what happened:

```bat
.venv\Scripts\python tools\verify.py                 :: foreground
.venv\Scripts\python tools\verify.py --offline       :: plus the optional offline bundle check (Windows)
.venv\Scripts\python tools\verify.py --detach        :: detached: survives a closed terminal / session
.venv\Scripts\python tools\verify.py status verify-results\<run> --wait   :: follow a detached run
.venv\Scripts\python tools\verify.py package-check --bundle dist\<bundle>.zip   :: one bundle only
```

The full run takes about 25 minutes, almost all of it the backend suite. Use `--detach` for unattended runs:
the checks keep running if the terminal or the remote session ends, and `status … --wait` picks the result up
later.

## 1. Checks

| Check | Required | What runs | Source of the counts |
|---|---|---|---|
| `ruff-lint` | yes | `ruff check .` | exit code + Ruff's summary |
| `ruff-format` | yes | `ruff format --check .` | exit code + Ruff's summary |
| `backend` | yes | `pytest tests/backend` (the whole suite; its current count is in the run's report) | **JUnit XML** written by pytest during this run: total / passed / failed / errors / skipped, with the skip reasons |
| `node-harness` | yes | `node tests/frontend/harness.js` | its JSON report (`tests/frontend/out/harness-results.json`), accepted only if written during this run |
| `browser` | yes | `tests/frontend/browser_check.py` (Playwright, headless Chromium) | its JSON report, same freshness rule |
| `p4-panel` | yes | `tests/frontend/integrity_panel_check.py` | its JSON report, same freshness rule |
| `package` | yes | `tools/verify.py package-check` on the newest `dist/*-offline.zip` (or `--bundle`) | its JSON report (§3) |
| `offline-bundle` | optional (`--offline`) | `packaging/offline_check.py`: installs the bundle in a temporary folder under the network guard and drives every feature (P21) | its JSON report. The temporary installation is deleted afterwards; its report and install logs are kept |

**How a check passes.** It needs exit code 0 **and** a trustworthy report showing at least one test and no
failure or error.

| Situation | Status |
|---|---|
| A report from an earlier run, a missing report, or "exit 0 but nothing ran" | `incomplete` |
| A failing count, even with exit code 0 | `failed` |
| A check that exceeds its timeout | `failed`; its process tree is killed |

**Optional checks** are reported like the others, but they never decide the outcome. A missing optional
prerequisite is listed as `unavailable`, not hidden.

**Skipped tests** inside the backend suite are listed with their reason in the summary. An example is the OCR
tests, which are skipped without a local Tesseract. A skip is not a pass: the summary prints both counts.

## 2. Results

Each run writes to `verify-results/<UTC time>/` (git-ignored, never inside tracked files):

| File | Content |
|---|---|
| `run.json` | The machine-readable record. Run level: source commit and dirty flag, Python, platform, bundle. Per check: status, exit code, start / end, duration, counts, skip reasons, detail, log path. Rewritten atomically after each step |
| `<check>.log` | The check's complete stdout and stderr |
| `backend-junit.xml`, `*-results.json`, `package.json`, `offline-*.json` / `.log` | The reports the counts came from |
| `orchestrator.log` | Only with `--detach`: the orchestrator's own output |

**Exit code and `run.json` status.**

| Exit | Status | Meaning |
|---|---|---|
| 0 | `passed` | Every required check passed |
| 1 | `failed` | At least one required check failed |
| 2 | `incomplete` / `interrupted` / `running` | A required check could not run (missing tool or bundle), produced no trustworthy report, was interrupted, or the run never finished |
| 3 | — | Usage error |

**Completion marker.** `run.json` carries `"complete": true` and an `exit_code` only when the orchestrator
finished. If it is killed, the file stays `"complete": false`, and `status` then reports `interrupted` (its
process is gone) or `running`, never `passed`. Ctrl+C stops the current check (its process tree is killed),
marks it `interrupted` and the remaining checks `not_run`.

## 3. Package validation (`package`)

`package-check` validates a bundle without changing it. Every item is recorded in `package.json`:
- one top-level folder; `MANIFEST.sha256` lists exactly the bundle's files, and every SHA-256 matches;
- the P21 content scan passes: no session token, database, workspace, cache, ground truth or user-specific path;
- the required files are present;
- `BUILD-INFO.json` names a commit that exists in the repository, and the tree was clean when it was built;
- **provenance:** every application file equals that commit's version (`git show`; `INSTALL.md` =
  `docs/installation.md`, the `.cmd` files with CRLF), and every wheel's hash is in that commit's
  `requirements.lock`. A file edited after the build is caught even if the manifest was regenerated;
- **reproducibility,** only when the bundle's commit is the current, clean `HEAD`: the bundle is rebuilt in a
  temporary folder from `HEAD`, using the bundle's own wheels, and the two zips must be byte-identical. The
  temporary folder is removed. Otherwise the record says why it was not checked.

`matches_head` reports whether the bundle was built from the commit being verified. A bundle from an earlier
commit can still pass every item above; for a release, rebuild it from the release commit (P21 guide §8).

**The previous artefact is preserved.** `packaging/build_bundle.py` never overwrites a different earlier bundle:
the old file is renamed `…offline.<sha256[:12]>.zip` beside the new one.

## 4. Prerequisites

| For | Needs |
|---|---|
| Everything | The development `.venv` (`pip install -e .[dev]`) and git |
| `node-harness` | Node.js ≥ 18 on PATH |
| `browser`, `p4-panel` | `pip install playwright` and its Chromium (`playwright install chromium`, done once while connected) |
| `package` | A built bundle in `dist/` (P21 guide §8) or `--bundle` |
| OCR tests inside `backend`, and `offline-bundle` | A local Tesseract 5 with `eng` (found through `pdf.ocr.executable_candidates`). Without it the OCR tests are skipped and listed as skipped |
| `offline-bundle` | Windows, a 64-bit CPython 3.12 for `install.cmd`, and about 300 MB of free temporary space |

No check needs internet access. The only network step in the project is preparing the wheelhouse on the build
machine (P21).

## 5. Isolation

- Every backend test runs in pytest temporary folders. `pytest-socket` limits connections to 127.0.0.1, and no
  test writes to the repository, the user's workspace or the environment.
- The browser and P4 panel checks start `ledgerspy serve` on a fresh temporary workspace and delete it at the end
  (P22). The offline check uses a temporary installation that the orchestrator deletes after keeping its report.
- No check builds, moves or deletes release artefacts, and none touches `%LOCALAPPDATA%\LedgerSpy` or any real
  workspace.

## 6. Troubleshooting

| Symptom | What to do |
|---|---|
| `backend` `incomplete` with "no report" | The pytest process died before writing JUnit XML: read `backend.log` (the tail shows where it stopped) |
| `… predates this run (stale)` | The check exited before writing its report: read its log |
| `node-harness` `unavailable` | Install Node.js |
| `browser` / `p4-panel` `unavailable` | Install Playwright and Chromium (§4) |
| `package` `unavailable` | Build a bundle or pass `--bundle` |
| `package` FAIL "every file matches its source commit" | The bundle was modified after it was built, or built from uncommitted files: rebuild from a clean commit |
| Skipped OCR tests in the summary | Install Tesseract (P21 guide §5) to cover them; otherwise they remain an uncovered platform gap |
| A run shows `interrupted` | The orchestrator or a check was killed: rerun, preferably with `--detach` |

## 7. End-to-end acceptance of a release bundle (P24)

`tools/e2e_acceptance.py` is a release step, separate from `verify.py`. It installs a bundle by the documented
procedure in an empty folder, then runs the documented workflow against the **packaged** application:
- import, master data (by the documented command), data quality and analysis;
- entities and graph, reconciliation, PDF / OCR review and correction;
- investigation, the four report types, integrity checks and invalid inputs;
- a restart with reproduced results, and a public ledger.

```bat
.venv\Scripts\python tools\e2e_acceptance.py --bundle dist\<bundle>.zip --work <empty folder>
```

**How it checks.**
- **Expected values** are derived from the fixture files, or from the P5 ground truth, which is evaluation-only.
- **Network:** every installed Python process runs under the network guard, and the installation's processes
  are sampled with `netstat`.
- **Output:** `<work>\e2e-results.json` lists each check as PASS, FAIL or NOT TESTED.
- **Not tested here:** the disconnected-machine run is always NOT TESTED; it is the user's manual step.
- **Exit codes:** 0 if no check failed, 1 otherwise.

## 8. Limitations

- **Windows-first.** The offline check and the bundle target Windows x64. The orchestrator itself, Ruff, the
  backend suite and the Playwright checks are portable but are verified here only on Windows 11.
- **Inferred counts for Ruff.** Ruff has no machine-readable summary in this setup, so its counts are taken from
  its fixed summary line together with its exit code.
- **Detached runs need a live machine.** `--detach` survives the terminal or session, not a reboot or a log-off
  that ends the user's processes.
- **Not checked here:** test coverage percentage (no coverage tool is installed) and 1M-row performance. See
  `docs/p22-acceptance.md`.
