# LedgerSpy Developer Guide

For someone changing LedgerSpy itself.
- **Architecture and design rationale:** [`architecture.md`](architecture.md).
- **Phase plan:** [`roadmap.md`](roadmap.md).
- **Proof of each phase:** the acceptance records (`p*-acceptance.md`).

## 1. Set up

Windows (the verified platform):

```bat
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\ledgerspy doctor
.venv\Scripts\ledgerspy serve
```

- **Python.** `pyproject.toml` allows Python ≥ 3.11, but 3.12 is the tested and packaged version.
- **Node.js ≥ 18** runs the frontend harness.
- **Playwright** with its Chromium (`playwright install chromium`, once, while connected) runs the browser and
  P4-panel checks.
- **Tesseract 5** (`eng`) runs the OCR tests; without it they are skipped and reported as skipped.
- **Workspace.** The development workspace defaults to `.\workspace` (git-ignored). Never commit a workspace,
  database, `dist\` or `verify-results\`.

## 2. Where things are

| Path | Content |
|---|---|
| `ledgerspy/` | The single source of truth for every computation. Packages per phase, e.g. `ingest/`, `quality/`, `features/`, `detectors/` + `forensics/`, `anomaly/`, `risk/`, `resolution/`, `graph/`, `reconciliation/`, `pdfstatements/`, `investigations/`, `reports/`, `api/`, `cli.py` |
| `config/default.yaml` | Every configurable value ([`configuration.md`](configuration.md)) |
| `frontend/` | The UI. It renders API results only and computes nothing |
| `tests/backend/` | pytest suite. Outbound network is blocked (`pytest-socket`, loopback only) |
| `tests/frontend/` | `harness.js` (Node), `browser_check.py`, `integrity_panel_check.py` (Playwright) |
| `packaging/` | `make_lock.py`, `build_bundle.py`, `offline_check.py`, launcher templates |
| `tools/verify.py` | The release-verification orchestrator |
| `data/` | The PaySim sample and the synthetic reference; provenance in `data/README.md` and [`data-strategy.md`](data-strategy.md) |

## 3. Rules the code and tests enforce

**Evidence and analytics**
- **No fabricated output.** If something cannot be computed, say `not_available` and why. The frontend computes
  no score, tier or statistic; static tests and the Node harness enforce this.
- **Labels never reach features, detectors, scores or reports.** Ground truth is evaluation-only and never
  shipped.
- **P9 stability.** Do not change P9 scores, weights or thresholds without a validated, documented change. Many
  tests check that later phases leave P9 results unchanged.

**Data and schema**
- **Append-only records.** Database changes are new migrations in `ledgerspy/cases/db.py`, never edits of old
  ones. `audit verify` compares the live triggers with the migrations, and recent migrations have
  upgrade tests.
- **Invariants.** Runtime invariants use `ledgerspy.errors.require`, never `assert`: `python -O` removes asserts.
  A test scans the package for this.

**Security and dependencies**
- **Strict CSP.** No inline `<script>`, `<style>`, `style=""` or `on…=` in `frontend/index.html`; the server
  refuses to start otherwise. Set styles through `el(..., {style})` or `data-style`, and insert data with
  `textContent` (`el()`), never `innerHTML`.
- **No new runtime dependency** unless it can be locked as an offline wheel (`requirements.lock`, P21) and is
  justified.

**Style and files**
- **Style.** Ruff, line length 110 (`ruff check .`, `ruff format --check .`).
- **Line endings.** New files use LF; `*.cmd` files use CRLF (`.gitattributes`). Test fixtures and the synthetic
  reference are byte-exact (`-text`).
- **Documentation.** Commands, endpoints and links in the user-facing documents are checked by
  `tests/backend/test_docs_p23.py`. The acceptance inventory is checked by `tests/backend/test_verify_p22.py`.

## 4. Test and verify

```bat
.venv\Scripts\python -m pytest
node tests\frontend\harness.js
.venv\Scripts\python tests\frontend\browser_check.py
.venv\Scripts\python tools\verify.py --offline --detach
.venv\Scripts\python tools\verify.py status verify-results\<run> --wait
```

- **Each command:**
  - `pytest` runs the backend suite (about 40 minutes on the development laptop);
  - `harness.js` runs without a browser;
  - `browser_check.py` drives the real UI against a temporary workspace;
  - `verify.py` runs every check with honest exit codes; `status --wait` follows a detached run.
- **Counts.** Use the counts in `verify-results\<run>\run.json` and the reports beside it, not numbers quoted in
  documents.
- **Details:** [`verification.md`](verification.md).

## 5. Build a release bundle

On the connected build machine, from a clean commit (installation guide §8):

```bat
.venv\Scripts\python packaging\make_lock.py --wheelhouse wheelhouse
.venv\Scripts\python packaging\build_bundle.py --wheelhouse wheelhouse --out dist
.venv\Scripts\python tools\verify.py package-check --bundle dist\ledgerspy-0.2.0-win_amd64-py312-offline.zip
```

- **Output.** The build prints the zip's SHA-256; publish that value with the release.
- **Reproducibility.** `package-check` re-validates the manifest and the provenance of every file against the
  source commit, and rebuilds the zip byte for byte when the bundle is from the clean `HEAD`.
- **Earlier bundles.** A different earlier bundle is kept, renamed `…offline.<sha256[:12]>.zip`.
