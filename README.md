# LedgerSpy

An offline forensic audit tool for company ledgers. LedgerSpy is meant to run on an auditor's laptop with
networking disabled. It checks data integrity, profiles digit frequencies (Benford), resolves look-alike vendors,
detects anomalies without labelled data, maps relationships between employees, vendors and transactions,
reconciles the ledger against bank statements, explains every risk score, and drafts an evidence-referenced
audit memo.

The original problem statement is in [`docs/problem-statement.md`](docs/problem-statement.md).

## Current status (honest)

This repository is being rebuilt from a hackathon prototype into a working system. **Today:**

- `frontend/` is the prototype UI. It parses a CSV in the browser and scores rows with a **hand-written heuristic
  formula, not a trained model**, and says so on screen. Since **P1 (Truthful Baseline)**:
  - every number shown is computed from the loaded file;
  - modules that don't exist yet (bank reconciliation, relationship graph, Monte Carlo, benchmarking, PDF) show "Not available yet";
  - label columns such as `isFraud` are ignored;
  - file content is rendered safely;
  - the page makes no network requests.

  See [`docs/p1-acceptance.md`](docs/p1-acceptance.md).
- The Python package `ledgerspy/` (the future single source of truth for all analytics) has its **P2 foundation**:
  validated and hashed config, canonical models, the ID scheme, a versioned SQLite workspace, a hash-chained audit
  log, and a local FastAPI server that serves `frontend/` on 127.0.0.1 behind a per-launch session token. It does
  **no analysis yet**; the UI still uses its in-browser heuristic until P9. See [`docs/p2-acceptance.md`](docs/p2-acceptance.md).
- **P3 ingestion** works through the API:
  - CSV/XLSX are read safely, within configured limits; formulas, macros and external links are never executed.
  - Every accepted file is kept as read-only evidence with its SHA-256.
  - Column mapping is explicit: the upload only proposes it, and the user confirms it.
  - PaySim and generic-ledger adapters produce the same canonical transactions.
  - Label columns go to a separate table.

  See [`docs/p3-acceptance.md`](docs/p3-acceptance.md).
- **P4 data quality** runs on the canonical data:
  - 17 integrity checks; a check that can't run says `not_available` and why.
  - Config-driven severities and a decomposed readiness score.
  - Every issue links to its transaction and source row.
  - Fixes are applied only after an analyst accepts them, as logged and reversible transformations; the evidence never changes.
  - The Data Integrity tab shows the backend report when the page is opened through `ledgerspy serve`.

  See [`docs/p4-acceptance.md`](docs/p4-acceptance.md).
- **P5 synthetic data:**
  - `ledgerspy synth` generates a deterministic fictional company (seeded; same seed gives the same hash).
  - It includes vendor/employee/customer masters, a 3-year ledger, bank statement and cash flow.
  - 63 scenarios: 45 injected anomalies and 18 legitimate look-alike controls.
  - The ground truth lives in a separate `ground_truth/` folder and never appears in the inputs.
  - The reference set is in `data/synthetic/reference/` and passes P3 ingestion and P4 with exactly the planned issues.

  See [`docs/p5-acceptance.md`](docs/p5-acceptance.md) and [`docs/synthetic-scenario-matrix.md`](docs/synthetic-scenario-matrix.md).
- **P6 feature registry** (`ledgerspy/features/`):
  - Every feature is defined once: 62 implemented, 10 registered as not available yet with the reason.
  - Each has a version, documented meaning, point-in-time rule, missing-data policy, peer fallback and unit test.
  - Feature matrices are built for PaySim (29 legitimately supported features) and the synthetic company (60).
  - Each build comes with a manifest and a feature-quality report.
  - Features never read labels, unmapped columns or ground truth.

  See [`docs/feature-registry.md`](docs/feature-registry.md) and [`docs/p6-acceptance.md`](docs/p6-acceptance.md).
- **P7 forensic rules + Benford** (`ledgerspy/detectors/rules.py`, `ledgerspy/forensics/`):
  - Ten deterministic, config-driven rules over P6 features: exact and near duplicates, round amounts, structuring, off-hours timing, new vendor + large amount, balance continuity (from P4), dormancy, period-end clusters and velocity.
  - Benford first-digit, first-two and last-two tests with MAD/z/χ², min-n gating and subgroups.
  - Every signal carries its evidence, the feature values used and an explanation.
  - Rules that the data cannot support say why.
  - `POST /api/datasets/{id}/forensics` returns the read-only report.
  - Signals are review leads, not findings; risk fusion is P9.

  See [`docs/forensics.md`](docs/forensics.md) and [`docs/p7-acceptance.md`](docs/p7-acceptance.md).
- The original notebooks are in `archive/` for reference. Their evaluation had data leakage and must not be quoted.

See [`docs/requirements.md`](docs/requirements.md) for the requirement-by-requirement status.

## Repository layout

```
ledgerspy/     Python package: config/, canonical/, cases/ (SQLite, audit log, evidence), ingest/, quality/,
               normalize/, synth/, features/, detectors/, forensics/, api/, cli.py
config/        default.yaml — every configurable value (validated, SHA-256 hashed)
frontend/      prototype UI (index.html, css/, js/) — served by `ledgerspy serve`
tests/         backend/ (pytest, P2) · frontend/harness.js (Node) + browser_check.py (optional, Playwright)
data/raw/      paysim_sample_10k.csv — sample of the public PaySim simulation (see data/README.md)
data/synthetic/reference/  SYNTHETIC_* inputs + ground_truth/ + manifest (seed 42, generated by `ledgerspy synth`)
docs/          problem statement, requirements, architecture, data strategy, roadmap, cleanup log, audits
archive/       legacy hackathon prototype (notebooks, scripts, derived data, screenshots) — reference only
```

Target layout (from phase P2 on) is described in [`docs/architecture.md`](docs/architecture.md) §16.

## Setup and running

Requires Python ≥ 3.11 (Node ≥ 18 only for the P1 harness).

```bash
py -3.12 -m venv .venv                          # or: python3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]" # .venv/bin/... on macOS/Linux
.venv/Scripts/ledgerspy serve                   # opens http://127.0.0.1:8765/?token=… (loopback only)
.venv/Scripts/ledgerspy audit verify            # verify the workspace audit-log hash chain
.venv/Scripts/ledgerspy evidence verify         # re-hash every evidence file against the database
.venv/Scripts/ledgerspy synth --out out/synth --seed 42   # generate a synthetic company (offline, deterministic)
```

The workspace (`workspace/ledgerspy.db`) holds client data and is git-ignored.

## Running the tests

```bash
.venv/Scripts/python -m pytest                  # P2 backend suite; outbound network is blocked
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .
node tests/frontend/harness.js                  # engine-level P1 checks, no dependencies
python tests/frontend/browser_check.py          # optional in-browser checks (needs playwright + Chromium)
python tests/frontend/integrity_panel_check.py  # optional: P4 dashboard against a live `ledgerspy serve`
```

## Documentation

| Document | Purpose |
|---|---|
| [`docs/problem-statement.md`](docs/problem-statement.md) | Original PS (transcribed + image) |
| [`docs/requirements.md`](docs/requirements.md) | Final requirements, PS → current-system matrix, misleading-claims register |
| [`docs/architecture.md`](docs/architecture.md) | Target architecture, responsibilities, API, data model, ML strategy, forensic module designs, security, offline |
| [`docs/data-strategy.md`](docs/data-strategy.md) | Existing data, data needed, synthetic data (as built), integrations |
| [`docs/roadmap.md`](docs/roadmap.md) | Phase-by-phase plan with dependencies and acceptance criteria |
| [`docs/p1-acceptance.md`](docs/p1-acceptance.md) | P1 Truthful Baseline: acceptance results A1–A9 |
| [`docs/p2-acceptance.md`](docs/p2-acceptance.md) | P2 Architecture Foundation: acceptance results and design decisions |
| [`docs/p3-acceptance.md`](docs/p3-acceptance.md) | P3 Data Ingestion and Evidence: acceptance results, PaySim run, deviations |
| [`docs/p4-acceptance.md`](docs/p4-acceptance.md) | P4 Validation, Cleaning and Integrity Dashboard: results, PaySim report, deviations |
| [`docs/p5-acceptance.md`](docs/p5-acceptance.md) | P5 Synthetic Scenario Data: hashes, scenario inventory, P3→P4 demo, deviations |
| [`docs/feature-registry.md`](docs/feature-registry.md) | P6 feature registry: rules, point-in-time semantics, peer groups, every feature |
| [`docs/p6-acceptance.md`](docs/p6-acceptance.md) | P6 Feature Registry: inventory, dataset support, quality reports, leakage/determinism demos |
| [`docs/forensics.md`](docs/forensics.md) | P7 forensic rules and Benford: inputs, thresholds, availability, evidence, limitations |
| [`docs/p7-acceptance.md`](docs/p7-acceptance.md) | P7 acceptance: scenario diagnostics, PaySim validation, Benford results, deviations |
| [`docs/synthetic-scenario-matrix.md`](docs/synthetic-scenario-matrix.md) | Feature → scenario → fields → records → detector phase |
| [`docs/cleanup-log.md`](docs/cleanup-log.md) | What was moved, archived or deleted in the cleanup, and why |
| [`docs/audits/phase-1-current-state-audit.md`](docs/audits/phase-1-current-state-audit.md) | Detailed audit of the prototype |

## Principles

1. One source of truth for features, models, scores, thresholds and explanations: the Python package.
2. No fabricated analytical output. Missing data gives an explicit "not available".
3. Every score is explainable and traceable to evidence; every run is reproducible.
4. Local only: no ledger data leaves the machine, no network needed at runtime.
