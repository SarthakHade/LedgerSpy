# P22 — Test Consolidation and Release Verification: Acceptance Record

Date: 2026-10-11 · Scope: [`roadmap.md`](roadmap.md) §P22, on top of P21 (`1483d5f`). P23 / P24 are not started.
Workflow guide: [`verification.md`](verification.md).

## 1. What is delivered

| Item | Where |
|---|---|
| One verification command | `tools/verify.py`: runs `ruff-lint`, `ruff-format`, `backend`, `node-harness`, `browser`, `p4-panel`, `package` (required) and `offline-bundle` (optional, `--offline`). Each check is its own subprocess, reusing the existing scripts unchanged in what they test |
| Result capture | `verify-results/<run>/run.json` (atomic, incremental, completion marker), one log per check, plus the reports the counts came from: pytest JUnit XML, the frontend checks' JSON, `package.json` and the offline report |
| Honest outcomes | Exit 0 only if every required check passed with a fresh, non-empty report. Exit 1 if a required check failed. Exit 2 if one was unavailable, interrupted, produced no trustworthy report, or the run never finished. A timed-out check is killed (process tree) and failed. A killed orchestrator leaves `complete: false`, read back as `interrupted` |
| Session robustness | `--detach` plus `status <dir> --wait` (the long-running suite survives the terminal, as the P15 / P16 detached runs showed was necessary) |
| Package validation | `tools/verify.py package-check`: manifest coverage and hashes, the P21 content scan, required files, a clean source commit, **file-by-file provenance against that commit** (and its lock for the wheels), and a byte-for-byte rebuild when the bundle is from the clean `HEAD` |
| Artefact preservation | `packaging/build_bundle.py` renames a different earlier bundle to `…offline.<sha256[:12]>.zip` instead of overwriting it |
| Isolation fixes | The browser and P4 panel checks now delete their temporary workspace (with read-only evidence) at the end. **Before P22 each run left one behind** (about 400 accumulated in the developer's temp folder; they were not deleted by this phase) |
| Acceptance inventory | [`acceptance-inventory.json`](acceptance-inventory.json): P0–P21 with status, the acceptance records, the gap sections inside them, the test files and checks that are evidence, and the roadmap items each record states as unmet. It references; it does not restate results |
| Tests | `tests/backend/test_verify_p22.py` (21) |

## 2. Consolidated run on the final code

`python tools/verify.py --offline` (detached), source tree = the final P22 code:

Run `verify-results/p22-final` (git-ignored): **PASSED, exit 0**, complete True, started 2026-10-11T11:28:38+00:00, ended 2026-10-11T11:54:23+00:00, Python 3.12.4, source `1483d5f` + the uncommitted P22 changes (every code file last changed before the start).

| Check | Kind | Status | Exit | Duration | Counts |
|---|---|---|---|---|---|
| `ruff-lint` | required | **passed** | exit 0 | 0.2 s | failed 0 |
| `ruff-format` | required | **passed** | exit 0 | 0.2 s | passed 271, failed 0 |
| `backend` | required | **passed** | exit 0 | 1285.1 s | total 1233, passed 1233, failed 0, errors 0, skipped 0 |
| `node-harness` | required | **passed** | exit 0 | 0.2 s | total 13, passed 13, failed 0 |
| `browser` | required | **passed** | exit 0 | 119.3 s | total 22, passed 22, failed 0 |
| `p4-panel` | required | **passed** | exit 0 | 13.4 s | total 6, passed 6, failed 0 |
| `package` | required | **passed** | exit 0 | 15.0 s | total 8, passed 8, failed 0 |
| `offline-bundle` | optional | **passed** | exit 0 | 109.5 s | total 26, passed 26, failed 0 |

**Cross-check against the underlying tools.** The recorded counts are those of the tools' own reports:

- `backend`: pytest's own last line `1233 passed in 1277.17s`; JUnit `tests=1233 failures=0 errors=0 skipped=0`.
- `node-harness` / `browser` / `p4-panel`: their own summary lines `13 passed, 0 failed`, `22 passed, 0 failed`, `6 passed, 0 failed`.
- `package`: 8 PASS lines, 0 FAIL. The bundle is the P21 release (`1483d5f`), matching its source commit file by file; reproducibility was *not checked* in this run because the working tree had uncommitted P22 changes (it is checked after the commit, see the commit report).
- `offline-bundle`: `26 passed, 0 failed`; the temporary installation was removed (`temporary_install_removed: true`).
- Ruff: `All checks passed!` and `271 files already formatted`.
- **A dry run before the final code** (`verify-results/p22-run1`) also passed all checks (backend 1232: one workflow test was added afterwards). It exposed one tool bug, fixed and tested: `status --wait` gave up when started before the detached run had written `run.json`.

## 3. Tests (`tests/backend/test_verify_p22.py`, 21)

| Area | Tests |
|---|---|
| Exit codes | Success only when every required check passed; a failing check gives exit 1 with its exit code, counts and log, and later checks still run; an optional failure is reported but does not decide the outcome |
| Reports | Exit 0 with a stale report, a missing report or no tool summary is `incomplete` (exit 2); a report with a failure fails despite exit 0; JUnit aggregation (failures, errors, skips with their reasons); "no test ran" is `incomplete`; the real pytest report of one test file matches pytest's own summary |
| Availability | A missing Node.js, a missing bundle and a missing optional prerequisite are `unavailable`, and the run is `incomplete`, not passed |
| Timeouts / interruption | `status --wait` waits for a just-started detached run (regression test). A hung check is killed and failed. **A real orchestrator killed in the middle of a check** leaves `complete: false` with no exit code, and `status` returns `interrupted` (exit 2) |
| Package integrity | A pristine bundle passes the integrity items. **Detected:** a tampered file (hash mismatch); an unlisted database inside the zip (manifest coverage and content scan); a consistently re-hashed forgery (the manifest agrees, provenance against the source commit fails). CLI exit codes for a missing (2) and a tampered (1) bundle |
| Cleanup / artefacts | The offline check's temporary installation, including read-only files, is removed and its report kept. This test found and fixed an orchestrator bug: `rmtree(ignore_errors=True)` silently left read-only files. The UI checks' workspace removal handles read-only evidence. A different earlier bundle is preserved, an identical one is not duplicated |
| Inventory | Every phase P0–P21 is listed in order; the statuses agree with the roadmap's ✔; every referenced document, gap heading and test file exists; every backend test file is attributed to a phase |

## 4. Coverage and acceptance gaps (honest list)

Phase-level gaps through P21 are in [`acceptance-inventory.json`](acceptance-inventory.json) (`roadmap_gaps`).
They point into each phase's own deviation or limitation section. Gaps of this phase, against the roadmap's P22
entry:

| Roadmap P22 item | Status |
|---|---|
| "One command runs the full suite" | **Met:** `tools/verify.py` |
| "Detector metrics compared with recorded baselines" | **Partly met.** P8's detectors are compared with the baseline recorded in `p8-acceptance.md` (`test_anomaly_datasets.py`). P7's rules and P12 / P13 / P14 have exact expected results on the P5 data in their tests. There is no single baseline file for every detector, and P9's scenario recall has no recorded target (inventory, P9) |
| "Playwright E2E smoke (upload → run → finding → report)" | **Covered by the existing browser check:** A1 / A5 import and analysis, A13 finding → investigation, A14 report. No new E2E script was added |
| "Performance tests (100k and 1M rows)" | **100k: yes** (P8 timing guard; P15 pagination over 100k notes). **1M rows: not built** |
| "Coverage targets" | **Not measured:** no coverage tool is installed (adding `pytest-cov` would be a new dependency); no target is enforced |
| "Scenario-benchmark regression suite" | The P5 scenario tests and the P7 / P8 / P12–P14 dataset tests are this suite; they run inside `backend` |
| Optional platform checks | OCR tests (Tesseract) and `offline-bundle` (Windows) depend on the machine; when unavailable they are reported as skipped or unavailable |

## 5. Commands and results (final code)

| Check | Result |
|---|---|
| Backend suite | 1233 passed, 0 failed, 0 errors, 0 skipped (exit 0; P21 baseline 1212 + 21 new) |
| P22 tests (`test_verify_p22.py`) | 21 passed (in the run above) |
| Node harness | 13 passed, 0 failed |
| Browser check | 22 passed, 0 failed |
| P4 integrity panel check | 6 passed, 0 failed |
| Ruff | check clean; format clean (271 files) |
| Package validation (P21 bundle) | 8 / 8 items pass |
| Offline bundle check (optional) | 26 passed, 0 failed |

## 6. Limitations

- **Windows only so far:** the orchestrator is portable, but it is verified on Windows 11 only.
- **Ruff counts** come from Ruff's summary line together with its exit code; Ruff has no report file in this
  setup.
- **Detached runs** survive a closed terminal, not a reboot or a log-off.
- **Bundle vs. HEAD:** `package` validates a bundle against **its own** source commit. Whether that commit is
  the one being released is reported (`matches_head`) and must be checked by the releaser.
- **Old temporary workspaces** left by earlier runs of the UI checks were not removed by this phase; that is a
  decision for the machine's owner.
