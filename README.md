# LedgerSpy

An offline forensic audit tool for company ledgers. LedgerSpy is meant to run on an auditor's laptop with
networking disabled. It checks data integrity, profiles digit frequencies (Benford), resolves look-alike vendors,
detects anomalies without labelled data, maps relationships between employees, vendors and transactions,
reconciles the ledger against bank statements, explains every risk score, and drafts an evidence-referenced
audit memo.

The original problem statement is in [`docs/problem-statement.md`](docs/problem-statement.md).

## Current status (honest)

This repository is being rebuilt from a hackathon prototype into a working system. **Today:**

- `frontend/` is the UI, served by `ledgerspy serve`. Since **P9** it **renders the backend's results only**: the
  in-browser heuristic scorer of the prototype is deleted, and every score, tier, count, chart and explanation comes
  from the local API. Opened as a plain file it says the local service is required. P1's guarantees still hold:
  - modules that don't exist yet (Monte Carlo, benchmarking, PDF reports) show "Not available yet";
  - label columns such as `isFraud` are never used;
  - file content is rendered safely;
  - the page makes no non-local requests.

  See [`docs/p1-acceptance.md`](docs/p1-acceptance.md) and [`docs/p9-acceptance.md`](docs/p9-acceptance.md).
- The Python package `ledgerspy/` (the future single source of truth for all analytics) has its **P2 foundation**:
  validated and hashed config, canonical models, the ID scheme, a versioned SQLite workspace, a hash-chained audit
  log, and a local FastAPI server that serves `frontend/` on 127.0.0.1 behind a per-launch session token. It does
  no analysis itself (later phases add it). See [`docs/p2-acceptance.md`](docs/p2-acceptance.md).
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
- **P8 statistical anomaly detection** (`ledgerspy/anomaly/`):
  - Three label-free detectors over P6 features: a deterministic pure-Python Isolation Forest, robust peer-group z-scores (counterparty → account / type) and tail percentiles per flow.
  - Feature eligibility, missing values, warm-up and insufficient data are reported explicitly; thresholds follow review capacity.
  - `POST /api/datasets/{id}/anomaly` returns subject-level scores, triggered `model_anomaly` signals and model metadata.
  - "Unusual" is never "fraudulent"; risk fusion is P9.

  See [`docs/anomaly-model-card.md`](docs/anomaly-model-card.md) and [`docs/p8-acceptance.md`](docs/p8-acceptance.md).
- **P9 risk engine v1 + explainability + first end-to-end slice** (`ledgerspy/risk/`, `frontend/js/risk-ui.js`):
  - P7 rule signals and P8 anomaly signals are fused into a bounded **0–100 review-risk score** (a review priority,
    not a probability of fraud), with family and group caps so correlated evidence cannot dominate.
  - Every score has an exact contribution breakdown by signal; tiers need corroboration by independent sources; a
    review-volume guard warns when too many rows are high; counterparty (entity) risk aggregates its transactions.
  - Data quality is reported beside the risk, never as risk.
  - `/api/datasets/{id}/risk…` serves the summary, paginated transactions, explanations, entities, detectors and
    the scatter from one cached analysis; the UI imports, maps, analyses and renders through these endpoints.

  See [`docs/risk-engine.md`](docs/risk-engine.md) and [`docs/p9-acceptance.md`](docs/p9-acceptance.md).
- **P10 supervised experiments** (`ledgerspy/supervised/`, `ledgerspy evaluate`), an optional and experimental layer:
  - Logistic regression and histogram gradient boosting are trained on P6 features with a versioned contract.
  - Labels come only through a dedicated adapter (PaySim `isFraud`, or a P5 ground-truth file given explicitly).
  - Random, chronological and entity/scenario-grouped splits; thresholds and calibration on validation only.
  - Baselines (prevalence, amount, P8, P9), bootstrap intervals and a PaySim ablation.
  - Outputs are model ranking scores, not fraud probabilities. Nothing is persisted, and P9 and the UI are
    unchanged.

  See [`docs/supervised-model-card.md`](docs/supervised-model-card.md) and [`docs/p10-acceptance.md`](docs/p10-acceptance.md).
- **P11 model artefacts and inference** (`ledgerspy model …`, `/api/models…`):
  - Fitted P10 models persist as deterministic JSON artefacts with a canonical SHA-256. A strict loader never
    executes content and refuses tampered, malformed, unsupported or inconsistent files.
  - Feature contracts must match exactly; preprocessing is the persisted training state.
  - A local, audit-logged registry has a lifecycle (experiment → candidate → approved → retired) and an approval
    gate. PaySim and synthetic models stay experimental by policy.
  - Batch inference is deterministic and returns an uncalibrated ranking score with full metadata. It never
    changes P9 review risk, and the UI is unchanged.

  See [`docs/model-lifecycle.md`](docs/model-lifecycle.md) and [`docs/p11-acceptance.md`](docs/p11-acceptance.md).
- **P12 entity resolution** (`ledgerspy/resolution/`, `ledgerspy entities …`, `/api/datasets/{id}/resolution…`):
  - Vendor / customer / employee / group-company masters are imported as evidence with an explicit mapping.
  - Names and identifiers are normalised deterministically (raw kept). Identifiers decide first; several name
    metrics feed explicit rules; blocking keeps comparisons small.
  - Only confirmed matches merge, and conflicts are always surfaced (look-alike vendors, shared bank accounts,
    employee–vendor links). Clusters are cannot-link-safe and have stable canonical IDs.
  - Seven P6 features; P9 groups counterparties by resolved entity with transaction scores unchanged.
  - Validation on P5: 0 false merges.

  See [`docs/entity-resolution.md`](docs/entity-resolution.md) and [`docs/p12-acceptance.md`](docs/p12-acceptance.md).
- **P13 relationship graph** (`ledgerspy/graph/`, `/api/datasets/{id}/graph…`, Relationships tab):
  - Nodes are P12 canonical entities. Edges are evidence: payments (payer → payee, per currency, every
    transaction ID kept) and shared bank accounts / tax IDs / contacts / addresses from P12 pair evidence.
  - Bounded, deterministic cycle detection with time-ordered, value-retention assessment.
  - Documented network metrics (degree, flow, reciprocity, concentration, PageRank, exact betweenness within
    limits, components).
  - Employee–vendor evidence and ghost-vendor verification leads (never conclusions).
  - Graph signals as a separate evidence family; P9 scores are unchanged.
  - The UI draws only API data.

  See [`docs/relationship-graph.md`](docs/relationship-graph.md) and [`docs/p13-acceptance.md`](docs/p13-acceptance.md).
- **P14 bank reconciliation** (`ledgerspy/reconciliation/`, `/api/reconciliation…`, Bank Recon tab):
  - CSV / XLSX bank statements are imported through the P3 flow (`kind=bank_statement`); debit/credit or sign
    semantics are confirmed, never assumed, and currencies are never invented.
  - Ledger ↔ bank matching within confirmed account scopes, per currency and direction: exact, probable, grouped
    (both orientations), partial (amount / date / reference / counterparty), ambiguous (never resolved by order),
    duplicate candidates, unmatched, insufficient data. Every row lands in exactly one traceable result.
  - Balance checks only where the statement supports them; reconciliation findings as a separate evidence family
    (P9 scores unchanged); deterministic runs recorded in an append-only register.
  - PDF statements: see P14b below.

  See [`docs/reconciliation.md`](docs/reconciliation.md) and [`docs/p14-acceptance.md`](docs/p14-acceptance.md).
- **P14b PDF statements and OCR** (`ledgerspy/pdfstatements/`, `/api/datasets/{id}/pdf…`, Bank Recon tab):
  - PDF bank statements are uploaded through the P3 flow, stored byte-for-byte as evidence and extracted
    locally: embedded text via PDFium (pypdfium2), scanned pages via the local Tesseract executable, mixed PDFs
    page by page, in a time-limited subprocess.
  - Every line is kept with its class, page, position and source text. Text, OCR and parse confidences are
    reported separately; every OCR row and every uncertain row is review-required.
  - Confirmation (the P3 mapping) is refused until the extraction is complete and every review-required row is
    confirmed or excluded; confirmed rows then reconcile in P14 unchanged, with their PDF provenance.
  - Offline install of the dependencies and Tesseract: [`docs/pdf-extraction.md`](docs/pdf-extraction.md) §1.

  See [`docs/pdf-extraction.md`](docs/pdf-extraction.md) and [`docs/p14b-acceptance.md`](docs/p14b-acceptance.md).
- **P15 investigation workflow** (`ledgerspy/investigations/`, `/api/investigations…`, Investigations tab):
  - Investigation cases inside an engagement case with a documented lifecycle; invalid transitions are refused,
    conclusions and closure need a reason, `inconclusive` is a valid outcome and nothing auto-closes.
  - Evidence links are scope-checked references (never copies) with a snapshot hash, re-verified on demand.
  - Append-only notes and analyst decisions. A finding is a lead requiring review; decisions never change
    P9 scores, detectors, matches or source data.
  - OCR corrections as explicit overlays that keep the original value; after confirmation they invalidate
    dependent reconciliation runs.
  - Every action is audit-logged under the local identity; `GET /api/audit/verify` checks the hash chain
    (tamper-evident, not tamper-proof against someone who controls the database file).

  See [`docs/investigations.md`](docs/investigations.md) and [`docs/p15-acceptance.md`](docs/p15-acceptance.md).
- **P16 audit reports** (`ledgerspy/reports/`, `/api/reports…`, Reports tab):
  - Case investigation reports, bank reconciliation reports, draft audit memos and evidence manifests, built
    read-only from the stored records by deterministic templates.
  - Every claim keeps its origin (automated indicator / analyst record), its records, source locations and
    evidence hashes; stale runs and unconfirmed OCR values are flagged and never presented as current facts.
  - Memos are always drafts; every number they state is cross-checked against the report model.
  - JSON, escaped HTML and Markdown artefacts, versioned and hash-checked. PDF / DOCX are not built.

  See [`docs/reporting.md`](docs/reporting.md) and [`docs/p16-acceptance.md`](docs/p16-acceptance.md).
- **P20 security hardening:**
  - Strict CSP (no inline script, handler or style).
  - Bounded request bodies and pagination.
  - Safe evidence file names.
  - A trigger-integrity check and an external anchor for the audit log.
  - Explicit errors for malformed XLSX archives.
  - A written threat model, including what is *not* protected (a local administrator, memory caps).

  See [`docs/p20-security-acceptance.md`](docs/p20-security-acceptance.md).
- **P21 offline packaging:**
  - A reproducible Windows x64 / Python 3.12 bundle with a hash-locked wheelhouse.
  - `install.cmd` installs with no index access; `ledgerspy.cmd` runs with a workspace outside the
    application folder.
  - `ledgerspy doctor` diagnoses an installation.
  - One server per workspace; database backup before upgrades.
  - A network-guarded offline check of every feature.

  See [`docs/installation.md`](docs/installation.md) and [`docs/p21-acceptance.md`](docs/p21-acceptance.md).
- **P22 release verification:** `python tools/verify.py` runs every required check (Ruff, backend suite, Node
  harness, browser and P4 panel checks, bundle validation) with honest exit codes and a machine-readable record.
  See [`docs/verification.md`](docs/verification.md) and [`docs/p22-acceptance.md`](docs/p22-acceptance.md).
- The original notebooks are in `archive/` for reference. Their evaluation had data leakage and must not be quoted.

See [`docs/requirements.md`](docs/requirements.md) for the requirement-by-requirement status.

## Repository layout

```
ledgerspy/     Python package: config/, canonical/, cases/ (SQLite, audit log, evidence), ingest/, quality/,
               normalize/, synth/, features/, detectors/, forensics/, anomaly/, risk/, supervised/, resolution/, graph/,
               reconciliation/, pdfstatements/, investigations/, reports/, api/, cli.py
config/        default.yaml — every configurable value (validated, SHA-256 hashed)
frontend/      UI (index.html, css/, js/) — served by `ledgerspy serve`; renders API results only
tests/         backend/ (pytest, P2) · frontend/harness.js (Node) + browser_check.py (optional, Playwright)
data/raw/      paysim_sample_10k.csv — sample of the public PaySim simulation (see data/README.md)
data/synthetic/reference/  SYNTHETIC_* inputs + ground_truth/ + manifest (seed 42, generated by `ledgerspy synth`)
docs/          problem statement, requirements, architecture, data strategy, roadmap, cleanup log, audits
archive/       legacy hackathon prototype (notebooks, scripts, derived data, screenshots) — reference only
```

Target layout (from phase P2 on) is described in [`docs/architecture.md`](docs/architecture.md) §16.

## Setup and running

Requires Python ≥ 3.11 (Node ≥ 18 only for the frontend harness).

**Target laptop (offline):** install from the P21 bundle instead: `install.cmd`, then `ledgerspy.cmd`. See [`docs/installation.md`](docs/installation.md). The commands below are for development.

```bash
py -3.12 -m venv .venv                          # or: python3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]" # .venv/bin/... on macOS/Linux
.venv/Scripts/ledgerspy serve                   # opens http://127.0.0.1:8765/?token=… (loopback only)
.venv/Scripts/ledgerspy doctor                  # P21: check the installation (changes nothing)
.venv/Scripts/ledgerspy audit verify            # verify the workspace audit-log hash chain
.venv/Scripts/ledgerspy evidence verify         # re-hash every evidence file against the database
.venv/Scripts/ledgerspy synth --out out/synth --seed 42   # generate a synthetic company (offline, deterministic)
.venv/Scripts/ledgerspy evaluate --dataset-id D-… --labels paysim   # P10 supervised experiment (local JSON report)
.venv/Scripts/ledgerspy model train --dataset-id D-… --labels paysim  # P11: persist one model as an experiment artefact
.venv/Scripts/ledgerspy model list                                    # registry; also inspect / verify / infer / promote …
.venv/Scripts/ledgerspy entities validate                             # P12: entity-resolution validation on P5 (false merges)
```

The workspace (`workspace/ledgerspy.db`) holds client data and is git-ignored.

## Running the tests

Release verification (every check, one command): `python tools/verify.py`; see [`docs/verification.md`](docs/verification.md).

```bash
.venv/Scripts/python -m pytest                  # P2 backend suite; outbound network is blocked
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .
node tests/frontend/harness.js                  # frontend checks without a browser (no dependencies)
python tests/frontend/browser_check.py          # optional: the UI against a live `ledgerspy serve` (playwright + Chromium)
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
| [`docs/anomaly-model-card.md`](docs/anomaly-model-card.md) | P8 model card: purpose, features, reference population, thresholds, leakage controls, results, failure modes |
| [`docs/p8-acceptance.md`](docs/p8-acceptance.md) | P8 acceptance: scenario validation, PaySim proxy, determinism, performance, deviations |
| [`docs/risk-engine.md`](docs/risk-engine.md) | P9 risk engine: score formula, caps, tiers, volume guard, entities, explanations, API, limitations |
| [`docs/p9-acceptance.md`](docs/p9-acceptance.md) | P9 acceptance: synthetic and PaySim validation, frontend rewire, security, deviations |
| [`docs/supervised-model-card.md`](docs/supervised-model-card.md) | P10 model card: data, features, splits, leakage controls, metrics, calibration, findings |
| [`docs/p10-acceptance.md`](docs/p10-acceptance.md) | P10 acceptance: experiments, baselines, leakage checks, deviations |
| [`docs/model-lifecycle.md`](docs/model-lifecycle.md) | P11 artefact schema, integrity, compatibility, registry, lifecycle, approval gate, inference, security |
| [`docs/p11-acceptance.md`](docs/p11-acceptance.md) | P11 acceptance: round trips, tampering, compatibility, P9 isolation, performance, deviations |
| [`docs/entity-resolution.md`](docs/entity-resolution.md) | P12 entity resolution: normalisation, identifiers, similarity, blocking, decision rules, clusters, integration, limitations |
| [`docs/p12-acceptance.md`](docs/p12-acceptance.md) | P12 acceptance: P5 validation (false merges / splits), adversarial cases, P9 before/after, performance |
| [`docs/relationship-graph.md`](docs/relationship-graph.md) | P13 graph: schema, identity, edge provenance, cycles, metrics, signals, API, limits |
| [`docs/p13-acceptance.md`](docs/p13-acceptance.md) | P13 acceptance: P5 findings, PaySim and scale limits, P9 isolation, UI, deviations |
| [`docs/reconciliation.md`](docs/reconciliation.md) | P14 reconciliation: data contract, scope, normalisation, matching stages, duplicates, balances, findings, API |
| [`docs/p14-acceptance.md`](docs/p14-acceptance.md) | P14 acceptance: P5 classification and precision, PaySim and scale, determinism, P9 isolation, UI, deviations |
| [`docs/pdf-extraction.md`](docs/pdf-extraction.md) | P14b PDF / OCR: dependencies and offline install, per-page methods, layout strategy, confidences, review gate, API, limits |
| [`docs/p14b-acceptance.md`](docs/p14b-acceptance.md) | P14b acceptance: genuine OCR tests, accuracy and runtime measured, review gate, failure handling, deviations |
| [`docs/investigations.md`](docs/investigations.md) | P15 investigations: lifecycle, evidence links, decisions, OCR corrections, audit semantics, API, limitations |
| [`docs/p15-acceptance.md`](docs/p15-acceptance.md) | P15 acceptance: lifecycle, scope and security tests, correction invalidation, P9 isolation, pagination, UI |
| [`docs/reporting.md`](docs/reporting.md) | P16 reporting: report types, claims and traceability, aggregation rules, memo, stale data, formats, storage, API, limitations |
| [`docs/p16-acceptance.md`](docs/p16-acceptance.md) | P16 acceptance: traceability, totals, stale / unconfirmed data, memo cross-check, versioning, artefact safety, P9 isolation |
| [`docs/p20-security-acceptance.md`](docs/p20-security-acceptance.md) | P20 threat model, controls, fixes, audit-trail guarantees and limits, remaining risks |
| [`docs/installation.md`](docs/installation.md) | P21 offline installation: prerequisites, install, run, OCR, upgrade, backup / recovery, building the bundle, troubleshooting |
| [`docs/p21-acceptance.md`](docs/p21-acceptance.md) | P21 acceptance: lock and bundle, offline check evidence, startup safety, migrations, limits |
| [`docs/verification.md`](docs/verification.md) | P22 release verification: the single command, checks, results, package validation, troubleshooting |
| [`docs/p22-acceptance.md`](docs/p22-acceptance.md) | P22 acceptance: consolidated run, workflow tests, coverage and acceptance gaps |
| [`docs/acceptance-inventory.json`](docs/acceptance-inventory.json) | P0–P21 acceptance inventory (references, evidence, gaps), validated by a test |
| [`docs/synthetic-scenario-matrix.md`](docs/synthetic-scenario-matrix.md) | Feature → scenario → fields → records → detector phase |
| [`docs/cleanup-log.md`](docs/cleanup-log.md) | What was moved, archived or deleted in the cleanup, and why |
| [`docs/audits/phase-1-current-state-audit.md`](docs/audits/phase-1-current-state-audit.md) | Detailed audit of the prototype |

## Principles

1. One source of truth for features, models, scores, thresholds and explanations: the Python package.
2. No fabricated analytical output. Missing data gives an explicit "not available".
3. Every score is explainable and traceable to evidence; every run is reproducible.
4. Local only: no ledger data leaves the machine, no network needed at runtime.
