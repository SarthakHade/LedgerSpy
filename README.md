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
- The original notebooks are in `archive/` for reference. Their evaluation had data leakage and must not be quoted.

See [`docs/requirements.md`](docs/requirements.md) for the requirement-by-requirement status.

## Repository layout

```
ledgerspy/     Python package: config/, canonical/, cases/ (SQLite + audit log), api/, cli.py
config/        default.yaml — every configurable value (validated, SHA-256 hashed)
frontend/      prototype UI (index.html, css/, js/) — served by `ledgerspy serve`
tests/         backend/ (pytest, P2) · frontend/harness.js (Node) + browser_check.py (optional, Playwright)
data/raw/      paysim_sample_10k.csv — sample of the public PaySim simulation (see data/README.md)
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
```

The workspace (`workspace/ledgerspy.db`) holds client data and is git-ignored.

## Running the tests

```bash
.venv/Scripts/python -m pytest                  # P2 backend suite; outbound network is blocked
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .
node tests/frontend/harness.js                  # engine-level P1 checks, no dependencies
python tests/frontend/browser_check.py          # optional in-browser checks (needs playwright + Chromium)
```

## Documentation

| Document | Purpose |
|---|---|
| [`docs/problem-statement.md`](docs/problem-statement.md) | Original PS (transcribed + image) |
| [`docs/requirements.md`](docs/requirements.md) | Final requirements, PS → current-system matrix, misleading-claims register |
| [`docs/architecture.md`](docs/architecture.md) | Target architecture, responsibilities, API, data model, ML strategy, forensic module designs, security, offline |
| [`docs/data-strategy.md`](docs/data-strategy.md) | Existing data, data needed, synthetic data plan, integrations |
| [`docs/roadmap.md`](docs/roadmap.md) | Phase-by-phase plan with dependencies and acceptance criteria |
| [`docs/p1-acceptance.md`](docs/p1-acceptance.md) | P1 Truthful Baseline: acceptance results A1–A9 |
| [`docs/p2-acceptance.md`](docs/p2-acceptance.md) | P2 Architecture Foundation: acceptance results and design decisions |
| [`docs/cleanup-log.md`](docs/cleanup-log.md) | What was moved, archived or deleted in the cleanup, and why |
| [`docs/audits/phase-1-current-state-audit.md`](docs/audits/phase-1-current-state-audit.md) | Detailed audit of the prototype |

## Principles

1. One source of truth for features, models, scores, thresholds and explanations: the Python package.
2. No fabricated analytical output. Missing data gives an explicit "not available".
3. Every score is explainable and traceable to evidence; every run is reproducible.
4. Local only: no ledger data leaves the machine, no network needed at runtime.
