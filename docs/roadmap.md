# LedgerSpy — Implementation Roadmap

The ordering follows dependencies. Testing and documentation are continuous: every phase ships its own tests
and docs, and phases 22–23 only consolidate them. The design for each module is in
[`architecture.md`](architecture.md).

## Dependency graph

```
P0 Cleanup ✔
 └─ P1 Truthful baseline (existing UI)
     └─ P2 Foundation (package, config, schema, IDs, SQLite, audit log, API skeleton) ✔
         └─ P3 Ingestion + evidence ✔ ┬─ P4 Validation / cleaning / integrity dashboard
                                      └─ P5 Synthetic scenario data ◄─ (needs canonical schema from P2/P3)
                                          └─ P6 Feature registry
                                              ├─ P7 Forensic rules + Benford
                                              ├─ P8 Anomaly detection
                                              └─ P9 Risk engine v1 + Explainability v1 + UI wired to API  ◄── first end-to-end slice
                                                  ├─ P10 Supervised training/evaluation ─ P11 Model registry/inference
                                                  ├─ P12 Entity resolution ─ P13 Graph analysis
                                                  ├─ P14 Bank reconciliation (P14b PDF/OCR)  (uses P12 aliases)
                                                  └─ P15 Investigation workflow + case/evidence mgmt
                                                      └─ P16 Reports + local memo assistant
                                                          ├─ P17 Investigation assistant (optional)
                                                          ├─ P18 Monte Carlo cash-flow (data-gated)
                                                          └─ P19 Benchmarking (data-gated)
 P20 Security hardening · P21 Offline packaging · P22 Test consolidation · P23 Docs · P24 Final E2E validation
```

---

### P0 — Repository cleanup ✔ (done in Phase 2)
See [`cleanup-log.md`](cleanup-log.md).

### P1 — Truthful baseline (existing UI) ✔ (done; see [`p1-acceptance.md`](p1-acceptance.md))
- **Objective.** The current app stops displaying fabricated results and becomes safe enough to show, with no new analytics.
- **Why now.** "Never display fabricated results" is a project principle. The fix is cheap and has no dependencies. Every later phase builds on a UI with honest empty states.
- **Files.** `frontend/index.html`, `frontend/js/*.js`, `frontend/css/*.css`, new `tests/frontend/` harness.
- **Input / output.** Existing UI → the same UI with every number computed or explicitly "not available".
- Full scope and acceptance criteria are in §"First implementation phase" below.

### P2 — Architecture foundation ✔ (done; see [`p2-acceptance.md`](p2-acceptance.md))
- **Objective.** Create the `ledgerspy` package skeleton and its supporting infrastructure, so that all later logic lands in one place.
  - Config loader and schema validation, with config hashing.
  - Canonical pydantic models (Transaction, Entity, Signal, Score, Finding, Explanation).
  - ID scheme.
  - SQLite schema with `schema_version`.
  - Hash-chained audit log.
  - FastAPI app serving `frontend/` on 127.0.0.1 with a session token and security headers.
  - CLI: `ledgerspy serve`.
- **Why now.** Everything else needs one home, one config and one schema.
- **Files.** `pyproject.toml`, `ledgerspy/{config,cases,api}/`, `ledgerspy/cli.py`, `config/default.yaml`, `tests/`.
- **Dependencies.** P0.
- **Input / output.** — → a runnable empty app with health and case endpoints.
- **Tests.** Config validation; ID determinism (same file + row = same `txn_id`); audit-log chain verification and tamper detection; API binds only to loopback; token required; `pytest-socket` blocks outbound calls.
- **Acceptance.**
  - `ledgerspy serve` opens the UI from localhost with no external requests.
  - `ledgerspy audit verify` passes, and fails after a manual DB edit.
  - `ruff` and `pytest` pass.

### P3 — Data ingestion and evidence ✔ (done; see [`p3-acceptance.md`](p3-acceptance.md))
- **Objective.** Import CSV and XLSX safely through a single, explicit mapping step.
  - Detect encoding and delimiter; enforce size and row limits.
  - Hash the file and keep a read-only evidence copy.
  - Suggest a column mapping from alias tables; the user confirms it.
  - Detect label columns and split them off.
  - Adapters: PaySim and generic ledger.
  - Upload endpoint; the frontend upload goes to the backend.
- **Why now.** Every analytic needs canonical data.
- **Files.** `ledgerspy/ingest/*`, `api/routes/files.py`, `frontend/js` upload flow.
- **Dependencies.** P2.
- **Input / output.** CSV/XLSX → dataset with canonical rows, mapping and evidence record.
- **Tests.**
  - Quoted newlines, BOMs and `;` delimiters.
  - XLSX with formulas (cached values only) and a zip-bomb fixture being rejected.
  - Label column never present in canonical rows.
  - `paysim_sample_10k.csv` imports to 10,127 rows with correct IDs.
- **Acceptance.**
  - Both formats import.
  - Malformed files give clear errors.
  - The evidence hash in the DB equals `sha256` of the original.
  - No row shows a placeholder ID.

### P4 — Validation, cleaning and the Data Integrity Dashboard (R3, R4)
- **Objective.** A real integrity check suite with a documented readiness formula.
  - Checks cover nulls per required field, type/format mismatches, unparseable dates, exact/near duplicate rows, negative or zero amounts, out-of-period dates, balance continuity and unmapped columns.
  - Issues are categorised by severity.
  - Proposed fixes are accepted or rejected by the user, and applied as logged transformations.
- **Why now.** The PS requires clean data *before* analysis, and later detectors assume normalised input.
- **Files.** `ledgerspy/quality/*`, `ledgerspy/normalize/*`, Data Integrity tab rendering.
- **Dependencies.** P3.
- **Output.** Integrity report JSON, normalised dataset, transformation log.
- **Tests.** Fixture files with known injected issues give exact issue counts. The readiness score formula is unit-tested. Transformations are reversible and logged.
- **Acceptance.**
  - The dashboard shows only computed values.
  - Every issue links to its rows.
  - "Auto-fix" performs a real, logged transformation.

### P5 — Synthetic scenario data
- **Objective.** A seeded generator producing the fictional company (vendor and employee masters, ledger, bank statement, cash-flow) plus a ground-truth file for injected scenarios. Everything is marked SYNTHETIC.
- **Why now.** ER, graph, reconciliation, anomaly evaluation and the risk engine all need data with known answers. The existing dataset cannot provide it.
- **Files.** `ledgerspy/synth/` (or `cli synth`), `data/synthetic/` (generated, small reference set committed), `docs/data-strategy.md`.
- **Dependencies.** P2–P3 (canonical schema).
- **Tests.** Determinism (same seed gives the same hash); every scenario present in the ground truth; files pass P3 ingestion and P4 integrity checks with the expected injected issues only.
- **Acceptance.** The generated files are importable, clearly labelled, and cover every scenario in `data-strategy.md` §4.

### P6 — Feature engineering (single registry)
- **Objective.** One feature registry: name, definition, inputs, point-in-time rule and version. Implements the "Target definition" column of the feature review in `architecture.md` §6.
- **Why now.** Detectors and models consume features. One registry removes the Python/JS duplication.
- **Files.** `ledgerspy/features/*`.
- **Dependencies.** P3–P5.
- **Tests.**
  - Point-in-time: no feature uses rows dated after the transaction (property test).
  - Peer-group fallback.
  - Features are deterministic.
  - Every registry entry has a doc string and unit test.
- **Acceptance.** Feature matrix produced for PaySim and synthetic data; no constant features (variance check reported).

### P7 — Forensic rules and Benford (R5, R16)
- **Objective.** Rule signals (duplicates, roundness, structuring/splits, temporal, new-vendor-large-amount, balance continuity) and the Benford module (first digit, first two digits, last two digits, MAD/z/χ², subgroups, min-n gating).
- **Why now.** These are label-free, explainable signals and a quick path to real value.
- **Files.** `ledgerspy/detectors/rules.py`, `ledgerspy/forensics/benford.py`, `ledgerspy/forensics/duplicates.py`, `config/default.yaml` rules section.
- **Dependencies.** P6.
- **Tests.**
  - Benford: data drawn from a Benford distribution conforms (MAD ≤ 0.006); uniform data does not; n < min gives `insufficient_data`; values match a hand-computed example.
  - Rules: scenario recall on synthetic data.
- **Acceptance.** Benford output for the PaySim sample and synthetic ledger is backed by data; the injected round-amount vendor is detected in the subgroup analysis.

### P8 — Anomaly detection (R7)
- **Objective.** Isolation Forest plus robust peer z-scores, producing tail-percentile signals.
- **Why now.** This is the PS's core label-free detector, and it feeds the risk engine.
- **Files.** `ledgerspy/detectors/anomaly.py`.
- **Dependencies.** P6.
- **Tests.** Fixed-seed determinism; precision@k / recall@k on synthetic scenarios against a recorded baseline; PaySim labels as a proxy (PR-AUC reported, not optimised).
- **Acceptance.** Metrics recorded in a model card; the run completes on 100k synthetic rows in an acceptable time on a laptop (target < 60 s).

### P9 — Risk engine v1, Explainability v1, and UI wired to the API (R10, R12, R7 UI) — first end-to-end slice
- **Objective.**
  - Signal fusion with family caps, tiers, entity risk, the volume guard and an exact contribution breakdown.
  - Explanation objects.
  - API endpoints for summary, transactions, scatter and explanation.
  - Frontend rewired: table, scatter, hero and the XAI tab render API data.
  - **Delete the scoring and feature code in `frontend/js/ml-engine.js`.**
- **Why now.** It is the first moment the system is genuinely end-to-end on real logic. Later modules only add signals.
- **Files.** `ledgerspy/risk/*`, `ledgerspy/explain/*`, `api/routes/runs.py`, `frontend/js/*`.
- **Dependencies.** P7, P8.
- **Tests.**
  - Contributions sum to 100% and recompose the score exactly.
  - Removing a signal lowers or keeps the score (monotonicity).
  - The volume-guard warning fires on a deliberately mis-weighted config.
  - Frontend contains no scoring constants (grep test).
- **Acceptance.** On the synthetic set, the flagged volume is within the review budget, scenario recall is at or above the target recorded in the plan, and each flagged row shows a % breakdown plus evidence links.

### P10 — Supervised training and evaluation (R17)
- **Objective.** Rebuild the notebook work as `ledgerspy train/evaluate`, following the pipeline in `architecture.md` §5.2.
  - Split first; preprocessing inside the pipeline; class weights or in-fold SMOTE.
  - Compare LR vs HGB; calibrate; select the threshold from review capacity.
- **Why now.** It needs the feature registry and benefits from the risk engine as its consumer. It is optional for the PS core.
- **Files.** `ledgerspy/detectors/supervised.py`, `ledgerspy/cli.py train`, `models/<name>/model_card.json`.
- **Dependencies.** P6 (P9 to integrate).
- **Tests.** Leakage tests (label column absent from the feature matrix; no test rows seen in fit); reproducible metrics with a fixed seed; baselines reported.
- **Acceptance.** The model card shows PR-AUC with CI vs baselines; no accuracy headline; results are reproducible from the CLI.

### P11 — Model persistence and inference
- **Objective.** Model registry (`models/`), artefact SHA-256 verification on load, applicability gating, and the supervised signal plugged into fusion with SHAP attributions.
- **Dependencies.** P10, P9.
- **Tests.** A tampered artefact is refused; a non-applicable dataset gives `not_applicable`; SHAP values sum to the model output (additivity check).
- **Acceptance.** A run manifest lists model hashes; the UI explanation shows a SHAP sub-breakdown only for the supervised component.

### P12 — Entity resolution (R6)
- **Objective.** The pipeline in `architecture.md` §8, with a similarity matrix API, analyst confirm/reject, and employee–vendor linking.
- **Dependencies.** P3–P5 (masters), P6.
- **Tests.** Normalisation table cases ("Stationery L.T.D" ≡ "Stationery Ltd"); blocking recall ≥ target on synthetic pairs; precision/recall of match decisions on synthetic ground truth; analyst decisions persist across reruns.
- **Acceptance.** Injected look-alike and shared-attribute scenarios are found; the matrix UI shows per-feature evidence and shared payment patterns.

### P13 — Graph and relationship analysis (R9)
- **Objective.** Graph build from resolved entities, temporal cycles, reciprocity, fan-in/out percentiles, communities, centrality, concentration, shell/ghost indicators, and a Cytoscape view (node size = risk, glow = centrality).
- **Dependencies.** P12, P9.
- **Tests.** Known small graphs (cycle of length k found; non-temporal cycle rejected); synthetic ring recovered with the correct members; performance on 100k edges.
- **Acceptance.** The static SVG and hard-coded cards are deleted; every node, edge and finding traces to transactions.

### P14 — Bank statement reconciliation (R8)
- **Objective.** CSV/XLSX statements, normalisation, candidate generation, scoring, optimal assignment, one-to-many grouping, matched/partial/missing classification, and exception findings.
  - **P14b:** text-PDF via pdfplumber, then scanned statements via local Tesseract OCR with confidence gating.
- **Dependencies.** P3, P4, P12 (aliases), P9.
- **Tests.** Synthetic statement with known delays, fees, batching and missing items gives exact expected classification counts; tie-breaking is deterministic; OCR fixture with low-confidence lines is never auto-matched.
- **Acceptance.** `runMockBSR` is gone; the side-by-side view shows real statuses; exceptions feed the risk engine.

### P15 — Investigation workflow, case and evidence management (R13, R14)
- **Objective.** Case lifecycle, finding statuses (New / Under review / Confirmed / Dismissed with reason / Escalated), notes, server-side paging and filtering, an evidence panel, audit-log viewer, and run comparison.
- **Dependencies.** P2, P9 (P12–P14 enrich it).
- **Tests.** Every state change is audit-logged; dismissed findings are excluded from reports but retained; pagination is correct on 100k rows.
- **Acceptance.** An analyst can go from a flagged row to a confirmed finding with notes; the audit trail verifies.

### P16 — Reporting and local audit memo assistant (R11, R15)
- **Objective.** Report model, deterministic memo NLG with finding citations, JSON preview, ReportLab PDF with hashes, run manifest, methodology and limitations.
- **Dependencies.** P15.
- **Tests.** Every number in the memo exists in the report model (automated cross-check); sections with no data say so; the PDF opens and its hash is stored; golden-file test for the template.
- **Acceptance.** One-click memo and PDF generated from a real case, with no static text other than templates.

### P17 — Investigation assistant (optional, R20)
- **Objective.** The chat panel answers only through API queries (summary, top entities, why-flagged using explanation objects). It never generates numbers itself.
- **Dependencies.** P9, P15.
- **Acceptance.** Every answer is reproducible from API data; the threshold text comes from config.

### P18 — Monte Carlo cash-flow analysis (R18, data-gated)
- **Objective.** From supplied monthly cash-flow history: block-bootstrap (or fitted) simulations, an optional leakage scenario from confirmed findings, a 12-month survival curve and percentile bands.
- **Dependencies.** P3 (cash-flow import), P16 (report section).
- **Tests.** Deterministic seed; analytic sanity checks (zero-variance case gives a deterministic path).
- **Acceptance.** Without cash-flow data the module shows `not_available`; with data, results come from 10k simulations, with parameters shown.

### P19 — Benchmarking (R19, data-gated)
- **Objective.** Only if a genuine benchmark source exists: import a signed aggregate file and compare the case's anomaly/exception rates with its definition-matched rate.
  - "Privacy-preserving" means only aggregates (with a minimum cohort size) ever leave any client.
  - No zero-knowledge claims.
- **Acceptance.** Hidden when no benchmark file is present.

### P20 — Security hardening
- **Objective.**
  - Remove inline handlers and enable strict CSP.
  - Write a threat model doc.
  - Fuzz the parsers and add malicious-file fixtures.
  - Formula-injection-safe exports.
  - `pip-audit` on the build machine.
  - Review the token and Origin checks.
- **Dependencies.** P2–P16 (hardening pass; basics exist from P1–P3).
- **Acceptance.** CSP has no `unsafe-inline`; the malicious fixture suite passes; the threat model is documented.

### P21 — Offline packaging
- **Objective.**
  - Vendored fonts and JS.
  - Pinned, hashed wheelhouse and an offline install script.
  - Tesseract offline install notes.
  - Optional PyInstaller build.
- **Acceptance.** Fresh install on a machine with the network disabled; the full demo case runs; zero non-loopback connections (verified by test and manually).

### P22 — Test consolidation
- **Objective.** Coverage targets, the scenario-benchmark regression suite in CI-like local runs, Playwright E2E smoke (upload → run → finding → report), and performance tests (100k and 1M rows).
- **Acceptance.** One command runs the full suite; detector metrics are compared with recorded baselines.

### P23 — Documentation
- **Objective.** User guide (auditor workflow), methodology (each detector, its assumptions and limits), model cards, config reference, developer guide, and data provenance.
- **Acceptance.** A new developer can install offline and run the demo case from the docs alone.

### P24 — Final end-to-end validation
- **Objective.** On a network-disabled laptop, run the synthetic company and a real public ledger end to end.
  - Integrity → analysis → investigation → confirmed findings → memo/PDF.
  - Verify the audit log and reproduce the run from its manifest.
- **Acceptance.** All PS MUST requirements are demonstrably met with real computation; every displayed number is traceable; the reproduced run is identical.

---

## First implementation phase: P1 — Truthful baseline

**Scope (frontend only; no new analytics, no backend).**

1. **Label leakage.** Remove the `isFraud → ≥85` override in `predictTransaction`. Exclude label columns (`isFraud`, `isFlaggedFraud`, `fraud`, `label`, `target`, `flagged`) from all scoring paths.
2. **Honest naming.** Rename AI/ML/"calibrated LR"/"SHAP-style" wording to "heuristic preview (rule-based, not a trained model)". Add a visible banner stating the score is heuristic, with the flagged % shown.
3. **Remove fabricated values. Show computed values or an explicit "Not available yet" state.**
   - Hero defaults; donut (bind to real counts and fix the missing element IDs).
   - Quick-stat tiles: remove "Detection Precision 93.7%"; compute average high-risk amount and flagged-entity count.
   - Benchmark block: not available.
   - Benford chart: render observed vs expected from the computed digit counts; no significance claims until P7.
   - Monte Carlo: not available.
   - Trends axis: label as "time buckets (step)" or real dates when a date column is mapped; no "FY 2024".
   - Relationships: remove the static graph, cards, PMLA and loop depth; keep only the computed "top destinations by flagged amount" list.
   - Explainability defaults: removed. Post-scan view renamed "Rule contribution summary (heuristic)".
   - Memo: only computed sentences.
   - PDF button, Auto-Fix and bank recon: disabled with a "not yet implemented" note; `runMockBSR` deleted.
   - "Behavioral Anomaly Cluster #4": removed.
4. **XSS.** Add an `escapeHtml` helper. Convert every data-bearing `innerHTML` (table, recon, top vendors, chatbot, entity names, scatter panel) to `textContent` or escaped templates.
5. **Offline.** Remove the Google Fonts link and fall back to a system font stack (vendored fonts come in P21).
6. **IDs and mapping.** When no ID column exists, show `ROW-<n>` (1-based data row). Add the "date of transaction" alias. Restrict the file picker to `.csv` until P3.
7. **Consistency.** One shared thresholds constant used by both the engine and the chatbot text.
8. **Test harness.** `tests/frontend/harness.js` (Node, no dependencies) loads the engine and runs it on `data/raw/paysim_sample_10k.csv`, plus a malicious-CSV fixture.

**Acceptance criteria.**
- A1. A grep test over `frontend/` finds none of these literals: `2,45,00,000`, `93.7%`, `3.4x`, `zero-knowledge`, `68% chance`, `Stationery L.T.D`, `MG Road`, `PMLA`, `Cluster #4`, `Risk = 87%`, `Math.random()` in analytic code, `fonts.googleapis`.
- A2. The harness produces **identical risk vectors** for the PaySim sample with and without the `isFraud` column.
- A3. A CSV whose vendor/ID cells contain `<img src=x onerror=…>` renders as literal text in every view (verified in a browser), with no script execution.
- A4. Loading the page with networking disabled makes **zero non-local requests** (DevTools network log) and the layout still renders.
- A5. Per-tab checklist: every displayed number is either computed from the uploaded file or shown as "Not available yet — requires …". The donut segments equal the tier counts.
- A6. No row shows `TX-???`. The chatbot's "why is ROW-123 flagged?" resolves the correct row.
- A7. Regression smoke: upload → scan → filter → search → expand → add row → CSV export → chatbot all still work (manual checklist plus harness).
- A8. The heuristic banner shows the flagged percentage. For the PaySim sample it is expected to be very high, which is a known issue recorded for P9 and is not hidden.
- A9. The changes are committed as one reviewed commit with the checklist results in the message or in `docs/`.
