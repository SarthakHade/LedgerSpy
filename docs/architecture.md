# LedgerSpy — Target Architecture

Status: **design**. Implemented so far:
- **P2 (foundation):** package skeleton, config, canonical models, IDs, SQLite schema, audit log, API skeleton and
  local security (see [`p2-acceptance.md`](p2-acceptance.md)).
- **P3 (ingestion and evidence):** CSV/XLSX ingestion, evidence store, explicit mapping, PaySim and generic-ledger
  adapters, labels split off (see [`p3-acceptance.md`](p3-acceptance.md)).
- **P4 (validation, cleaning, integrity dashboard):** 17 integrity checks, config-driven severity, a decomposed readiness
  score, row-traceable issues, analyst-decided fixes applied as logged and reversible transformations, and the
  backend panel in the Data Integrity tab (see [`p4-acceptance.md`](p4-acceptance.md)).
- **P5 (synthetic scenario data):** `ledgerspy.synth` and `ledgerspy synth` produce a deterministic, fictional
  two-company group (masters, ledger, bank statement, cash flow) with 63 scenarios (45 injected, 18 normal controls).
  The ground truth is kept in a separate folder. A committed seed-42 reference set passes P3 and P4 with exactly the
  planned issues (see [`p5-acceptance.md`](p5-acceptance.md), [`synthetic-scenario-matrix.md`](synthetic-scenario-matrix.md)).
- **P6 (feature registry):** `ledgerspy.features` holds one definition per feature: 62 implemented (43 transaction-level,
  19 entity-level; P7 added three) and 10 registered as not available yet.
  - Each feature has a version, metadata, a point-in-time class, a missing-data policy and a unit test.
  - Point-in-time history means strictly earlier rows; one peer hierarchy with a reported basis serves every peer feature.
  - A feature manifest and a feature-quality report accompany each build. Features read only canonical fields of the normalised view.
  - See [`feature-registry.md`](feature-registry.md) and [`p6-acceptance.md`](p6-acceptance.md).
- **P7 (forensic rules + Benford):** `ledgerspy.detectors.rules` holds ten config-driven rules over P6 features:
  exact/near duplicates, round amounts, structuring, temporal, new vendor + large amount, balance continuity (from P4),
  dormancy, period-end clusters and velocity.
  - `ledgerspy.forensics.benford` runs first-digit, first-two and last-two tests with MAD/z/χ², min-n gating and subgroups.
  - They emit explainable signals that convert to the canonical `Signal`. `POST /api/datasets/{id}/forensics` returns the
    read-only dataset report.
  - No fusion yet (P9). See [`forensics.md`](forensics.md) and [`p7-acceptance.md`](p7-acceptance.md).

Everything else is not implemented yet unless stated.
Requirement IDs (R1…R20) refer to [`requirements.md`](requirements.md).

---

## 1. Design principles

1. **One source of truth.** All analytics live in one Python package, `ledgerspy`. The frontend never
   computes features, scores, thresholds or explanations. It only renders API responses.
2. **Configuration, not constants.** Thresholds, rule weights, tier cut-offs and explanation templates live in
   one versioned YAML config. Every analysis run records the config's SHA-256.
3. **No fabricated output.** If a module lacks the data it needs, the API returns an explicit
   `not_available` status with the reason. The UI shows that state, never a placeholder number.
4. **Defensible by construction.** Every score decomposes exactly into named signals. Every signal points
   at evidence (rows, entities, matches, cycles). Every run is reproducible from hashed inputs, config,
   model and code version.
5. **Local by default.** One process on the auditor's laptop serves the API and UI on `127.0.0.1`. No
   outbound network at runtime.
6. **Modular monolith.** One Python package, one SQLite database per workspace, one static frontend. No
   microservices, queues, containers or cloud.
7. **Labels are for evaluation only.** Ground-truth columns (`isFraud`, …) are split off at ingestion and
   are never visible to feature engineering, scoring or explanation.

## 2. System overview

```
┌──────────────────────────── Auditor's laptop (offline) ────────────────────────────┐
│                                                                                    │
│  Browser ── http://127.0.0.1:<port> (session token, same-origin, strict CSP) ──┐   │
│  frontend/  (existing LedgerSpy UI, vanilla JS ES modules, vendored assets)    │   │
│                                                                                ▼   │
│  ledgerspy.api  (FastAPI + uvicorn, single process)                                │
│      │                                                                             │
│      ▼                                                                             │
│  Analysis pipeline (ledgerspy.*)                                                   │
│   ingest → quality → normalize → features → detectors ─┬─ supervised (optional)    │
│                                                        ├─ anomaly (unsupervised)   │
│                                                        └─ forensic rules           │
│   forensics: benford · entity_resolution · graph · reconciliation · cashflow_sim   │
│   → risk (signal fusion) → explain → findings → reports (memo, PDF)                │
│      │                                                                             │
│      ▼                                                                             │
│  workspace/  (git-ignored)                                                         │
│   ├── ledgerspy.db          SQLite: cases, datasets, transactions, entities,       │
│   │                         runs, signals, scores, findings, notes, audit_log      │
│   ├── evidence/<sha256>.*   read-only copies of every imported file                │
│   └── reports/              generated PDFs (hashed)                                │
│  models/                    model artefacts + model cards (sha256-verified)        │
│  config/default.yaml        all thresholds/weights/templates (hashed per run)      │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Why this shape.**
- **A local Python server, not browser-only.** The PS needs XLSX, OCR for scanned statements, scikit-learn
  models, SHAP, graph algorithms, PDF generation and persistent cases. All of these are mature in Python and
  unavailable or painful in the browser.
- **Running in-browser via Pyodide was considered and rejected.** It is heavy, has no OCR, has weak persistence,
  and makes the browser the trust boundary for evidence.
- **The existing UI is preserved.** It already embodies the PS's visual intent. Its DOM-scraping updaters are
  replaced by render functions fed by JSON.

### Analysis pipeline stages

| Stage | Module | Input | Output |
|---|---|---|---|
| Ingest | `ledgerspy.ingest` | Uploaded file + declared kind (ledger / bank / vendor master / employee master / cash-flow) | Evidence record (sha256), raw table, suggested column mapping |
| Map | `ledgerspy.ingest.mapping` | Raw headers, user confirmation | Mapping to the canonical schema (stored per dataset) |
| Quality | `ledgerspy.quality` | Normalised view of the canonical dataset (P4) | Integrity report: categorised issues, readiness score |
| Normalize | `ledgerspy.normalize` | Canonical rows + accepted fixes | Normalised view (canonical rows + `normalized_values` overlay); transformation log |
| Features | `ledgerspy.features` | Canonical tables | Feature matrix (point-in-time) from one feature registry |
| Detect | `ledgerspy.detectors`, `ledgerspy.forensics` | Features, canonical tables, entity map, bank data | **Signals** (uniform objects, see §6) |
| Risk | `ledgerspy.risk` | Signals | Transaction and entity risk scores, tiers, exact contributions |
| Explain | `ledgerspy.explain` | Scores, signals, model attributions | Explanation objects |
| Findings | `ledgerspy.cases` | High/review items, forensic results | Findings (persisted, with status) |
| Report | `ledgerspy.reports` | Case snapshot | Memo text, report JSON, PDF file |

A **run** executes these stages over a fixed set of datasets with a fixed config and model set. Runs are
immutable. Re-analysing creates a new run.

## 3. Responsibilities

| Concern | Frontend (`frontend/`) | Backend (`ledgerspy.api`, `ledgerspy.cases`) | ML / Analytics (`ledgerspy.*` core) | Storage (`workspace/`, `models/`) |
|---|---|---|---|---|
| File upload | File picker, progress, mapping confirmation UI | Size/type/magic checks, hashing, evidence copy, parser dispatch | Parsing (CSV/XLSX/PDF/OCR), mapping suggestions | Evidence files (read-only), `evidence_files` table |
| Validation | Integrity dashboard rendering | Orchestrates checks, persists report | Check implementations, readiness formula | `integrity_runs`, `integrity_issues` (P4) |
| Cleaning | Shows proposed fixes; user accepts or rejects | Records decisions in the audit log | Transformation functions | Transformation log per dataset |
| Features / models | — (never) | Triggers runs, tracks status | Feature registry, model inference, anomaly detection | `models/` artefacts + cards |
| Forensics | Charts, matrix, graph, side-by-side recon | Exposes results via API | Benford, ER, graph, reconciliation, cash-flow sim | `signals`, `recon_matches`, `entities`, `entity_aliases` |
| Risk + explanations | Breakdown bars, evidence links | Serves them | Fusion, tiers, contributions, SHAP | `scores`, `signals` |
| Investigation | Filters, search, drill-down, scatter, status changes, notes | Paging, filtering, queries, validation, audit logging | — | `findings`, `notes` |
| Reporting | Memo preview, copy, download | Report generation endpoint | Report model, memo NLG, PDF renderer | `reports` table + files |
| Security | Safe rendering (`textContent`), no inline handlers, no external assets | 127.0.0.1 bind, session token, Origin/Host checks, CSP headers, limits | Safe parsers (no formula/macro execution), model hash verification | Evidence immutability, hash-chained audit log |

### API surface (initial contract)

All JSON. IDs are strings. List endpoints are paginated (`?page=&size=`).

```
POST  /api/cases                                  create case
GET   /api/cases/{case_id}
POST  /api/cases/{case_id}/files                  raw-body upload (P3: ?filename=&kind=ledger[&sheet=][&delimiter=]); other kinds later
GET   /api/datasets/{dataset_id}                  profile + suggested mapping
PUT   /api/datasets/{dataset_id}/mapping          confirm mapping → canonical rows (once; immutable afterwards)
GET   /api/datasets/{dataset_id}/transactions     P3: paged canonical rows (labels never included)
GET   /api/datasets/{dataset_id}/integrity        integrity report (R3) — latest run (P4: 404 not_run before the first)
POST  /api/datasets/{dataset_id}/integrity        P4: run the checks → new immutable report
GET   /api/datasets/{dataset_id}/integrity/issues P4: paged issues (filter check/severity), each with txn_id + source_row
GET   /api/datasets/{dataset_id}/fixes            P4: fix proposals and their status
POST  /api/datasets/{dataset_id}/fixes/decisions  P4: accept (value for ambiguous) / reject (reason) — all-or-nothing
POST  /api/datasets/{dataset_id}/normalize        apply accepted fixes (R4) → logged transformations, then re-run checks
GET   /api/datasets/{dataset_id}/transformations  P4: transformation log
POST  /api/datasets/{dataset_id}/transformations/{id}/revert   P4: restore the logged original value (reason)
GET   /api/datasets                               P4: dataset list with latest readiness (dashboard picker)
POST  /api/cases/{case_id}/runs                   start analysis → run_id
GET   /api/runs/{run_id}                          status, manifest (hashes, versions), module availability
GET   /api/runs/{run_id}/summary                  tier counts, exposure, flagged-volume check
GET   /api/runs/{run_id}/transactions             filter: tier, signal, entity, amount, date, text
GET   /api/runs/{run_id}/transactions/{txn_id}    record + signals + explanation + evidence
GET   /api/runs/{run_id}/anomalies/scatter        points for R7
GET   /api/runs/{run_id}/benford                  R5
GET   /api/runs/{run_id}/entities                 entities with risk
GET   /api/runs/{run_id}/entities/matches         similarity matrix / candidate pairs (R6)
POST  /api/entity-matches/{pair_id}/decision      confirm / reject (analyst feedback)
GET   /api/runs/{run_id}/graph                    nodes/edges + metrics (R9)
GET   /api/runs/{run_id}/reconciliation           side-by-side rows with status (R8)
GET   /api/runs/{run_id}/cashflow-sim             R18 (or not_available)
GET   /api/cases/{case_id}/findings
PATCH /api/findings/{finding_id}                  status, assignee, notes
POST  /api/cases/{case_id}/reports                generate memo + PDF → report_id
GET   /api/reports/{report_id}/file
GET   /api/cases/{case_id}/audit-log
```

Every analytic endpoint returns `{"status": "ok" | "not_available" | "insufficient_data", "reason": …, "data": …}`.

## 4. Canonical data model and IDs

### Canonical transaction (ledger or bank line)

| Field | Type | Notes |
|---|---|---|
| `txn_id` | str | Source ID if present, unique and stable; otherwise `T-` + first **16** hex of `sha256(evidence_sha256 + ":" + source_row)` (P2: widened from 12 hex; 48 bits gives a ~0.2% chance of some collision in 10⁶ rows of one file, 64 bits ~3·10⁻⁸). Depends only on the file and its 1-based row, never on processing order |
| `dataset_id`, `source_row` | str, int | Exact provenance (1-based data row) |
| `posted_at` | datetime? | Posting date (and time if available) |
| `document_date` | date? | Invoice/document date (enables back-dating checks) |
| `amount_minor` | int? | Amount in minor units (paise) to avoid float errors; `currency` (optional) alongside |
| `direction` | enum? | `debit` / `credit` (or `out` / `in` for bank) |
| `account` | str? | GL account / bank account |
| `payer_raw`, `payee_raw` | str? | Names as written in the source |
| `payer_entity_id`, `payee_entity_id` | str? | After entity resolution |
| `reference` | str? | Invoice no., cheque no., UTR, voucher no. |
| `description` | str? | Narration |
| `txn_type` | str? | Source type (PAYMENT, TRANSFER, JV, …) |
| `extra` | JSON | Unmapped columns, preserved verbatim |

Label columns (`isFraud`, `isFlaggedFraud`, `label`, …) are detected at mapping. They go to a separate
`labels` table and are **never** part of the canonical transaction.

**P3 decisions.**
- `posted_at`, `amount_minor`, `currency` and `direction` are nullable. Real files have gaps (the PaySim sample has
  blank `amount` and `type` cells), and ingestion never invents a value.
  - A blank or unconvertible mapped cell becomes `NULL` plus an `ingest_issues` row (row, field, column, code, raw value).
  - P4's integrity checks start from those rows.
  - A confirmed mapping that leaves more than `mapping.max_unconvertible_share` of rows unconvertible in a field is refused
    as a wrong mapping.
- **Canonical rows exist from P3.** Each row is the *typed mapping* of its source row: nothing is cleaned or corrected.
  P4 adds integrity checks and logged corrections on top; it never rewrites P3 rows silently.
- **`source_row`** is the record ordinal after the header, counting blank records (spreadsheet row = header row + `source_row`).
  Blank records produce no transaction.
- **Source IDs** become `txn_id` only if every value is present, printable, ≤ 128 characters and unique in the file.
  Otherwise the mapping is refused (map the column as `reference`, or use derived IDs). Rows are keyed by `(dataset_id, txn_id)`.
- **Adapters:** `generic_ledger` (alias-driven, user-confirmed) and `paysim` (fixed mapping) both emit the same `Transaction`.
  - PaySim: `posted_at = adapters.paysim.step_epoch (1970-01-01) + (step − 1) h`, a deliberately artificial clock.
    The dataset is recorded with `time_basis = simulation_step` and `synthetic = 1`.
  - PaySim `direction` comes from `type`, relative to the `nameOrig` account (`CASH_IN` → in, all other types → out),
    as verified on the sample's balance columns. There is no currency.
- **Mapping is explicit.**
  - The upload returns a proposal with each field's status (proposed / ambiguous / low_confidence / unmapped) and its basis,
    plus label columns, unmapped columns and the items that need confirmation.
  - Only exact alias matches whose values fit are proposed. Ambiguous date formats or decimal separators are never guessed.
  - Alias and label tables are in `config/default.yaml` (hashed).

### Entities
- `entity_id` = `E-` + ULID. Assigned at resolution and stable within a case.
- `kind` ∈ {vendor, employee, customer, bank_account, company_account, unknown}.
- `canonical_name` and `attributes` (address, PAN/GST, bank account, phone, email).
- `entity_aliases(raw_name, entity_id, method, confidence, decided_by)` maps every raw spelling to its
  entity. Analyst decisions override automatic ones.

### Other tables (SQLite)
`cases`, `evidence_files`, `datasets`, `transactions`, `labels`, `ingest_issues`, `runs` (manifest), `signals`, `scores`,
`findings`, `notes`, `recon_matches`, `reports`, `audit_log`, `schema_version`. (P3 added `transactions`, `labels` and
`ingest_issues`; `evidence_files` rows are immutable and canonicalised `datasets` rows are frozen by triggers.)
P4 added `integrity_runs`, `integrity_issues`, `fix_proposals`, `fix_decisions`, `transformations` and `normalized_values`.

### Data quality and normalisation (P4)
- **Where checks run.** Checks run on the *normalised view*: the canonical P3 row, overridden field by field by
  `normalized_values`.
  - P3 rows are never edited. A trigger blocks updates to their canonical columns and blocks deletes;
    the entity-ID columns stay writable for P12.
  - `normalized_values` is written only by a logged transformation.
  - This replaces "normalize writes canonical tables" (§2): the canonical rows stay the typed mapping of the evidence,
    and every difference from them is explained by the log.
- **Checks** (`ledgerspy/quality/checks.py`):
  - **Fields:** missing_required, missing_optional, field_coverage.
  - **Validity:** invalid_value, unparseable_date, implausible_date, malformed_identifier.
  - **Duplicates:** exact_duplicate, near_duplicate, duplicate_source_id.
  - **Amounts, period, balances:** negative_amount, zero_amount, out_of_period, balance_continuity.
  - **Source and housekeeping:** unmapped_columns, needs_normalization, ingestion_issues.
  - A check that lacks its inputs reports `not_available` with the reason (no payee column, no case period,
    simulated clock, no balance column, …), never a silent pass.
- **Severity** (config `quality.severity`) is assigned per check: the base severity, raised by prevalence escalation rules.
  For synthetic datasets an override replaces it and is final.
- **Readiness** (config `quality.readiness`):
  - Formula: `floor1dp(100 × Σ w·clean / Σ w)` over the assessed components (completeness, validity, uniqueness,
    consistency). Here `clean` = 1 − the share of rows with an issue of at least `min_severity`.
  - The score is capped at 99.9, and the status is `not_ready`, while any issue of at least `blocking_severity` remains.
  - Every component's points are reported.
- **Fixes.** Safe fixes (whitespace, identifier NFKC/zero-width removal, configured case) and reinterpretations (another
  configured date format or decimal convention) are only *proposed*.
  - Applying any fix needs an explicit accept; an ambiguous one also needs the chosen value. A rejection needs a reason.
  - Missing values, rounding and duplicate removal are never proposed.
  - Each application is a `transformations` row: txn, field, original, normalised, transform, reason, run, actor, time.
  - A revert restores the logged original exactly and appends its own row.
- **IDs.** Issue and fix IDs are deterministic hashes, so re-runs on the same data and config give identical IDs and an
  identical `report_sha256`.

- Large per-run tables (`signals`, `scores`) are keyed by `(run_id, subject_type, subject_id)`.
- SQLite comfortably handles ~10⁶ rows on a laptop. Parquet or DuckDB can be added later **only** if profiling shows a need.

### Configuration
- `config/default.yaml` (repository root) is the only place values are set; the schema (`ledgerspy/config/schema.py`) has no defaults, rejects unknown keys and is strict about types. `ledgerspy.config.load_config()` is the single interface. P2 defined `config_version`, `server` and `workspace`; P3 added `ingest` (size/row/column/cell limits, CSV encodings and delimiters, XLSX zip limits, minor units), `mapping` (aliases, label columns, date formats, thresholds) and `adapters`; P4 added `quality` (required/optional/expected fields, date bounds, near-duplicate window, identifier rules, balance, severity, readiness) and `normalize`; P5 added `synth` (seed, start date, years, scale, currency, approval limits, business hours); P6 added `features` (history minimums, baseline window, MAD floor, calendar, case approval thresholds, adapter profiles, report flags); each later phase adds its own section.
- Sections planned: `ingest` (limits), `quality`, `features`, `rules` (one entry per rule: code, family, parameters, weight, severity, explanation template), `risk` (fusion and tier thresholds), `benford`, `entity_resolution`, `graph`, `reconciliation`, `cashflow_sim`.
- A case may override values. The effective config is hashed and stored in the run manifest.

## 5. ML strategy

### 5.1 Two detector families, one risk score

| Family | Purpose (PS) | Data required | First model | Why this model |
|---|---|---|---|---|
| **Unsupervised anomaly detection** (core) | "Unusual transactions without labelled data" (R7) | Any ledger | **Isolation Forest** + **robust peer statistics** (median/MAD z within vendor, account and type) | No labels needed; handles mixed feature scales; scales to 10⁵–10⁶ rows; robust z-scores are explainable to auditors |
| **Supervised known-pattern model** (extension) | Transactions resembling *known* fraud patterns (R17) | Labelled data of the same schema/domain | **Logistic Regression** (baseline) and **HistGradientBoostingClassifier** (candidate) | LR is an interpretable baseline. HGB handles non-linearity and native missing values, is in scikit-learn (no extra dependency) and is supported by SHAP TreeExplainer |

Random Forest, LOF, autoencoders and LightGBM/XGBoost are **not** added unless a measured gap justifies them.
Each would add parameters, dependencies or slower explanation without a demonstrated benefit on this data.

**Applicability gating.** A supervised model only contributes when the dataset's schema and type
distribution match its model card's applicability domain. The PaySim model must never score a corporate
GL. Otherwise the run reports `supervised: not_applicable`.

### 5.2 Supervised pipeline (fixes every Phase 1 defect)

```
raw → validate → split FIRST (stratified; time-aware where timestamps allow) → fit preprocessing on train only
→ point-in-time features (aggregates use only data before each transaction) → imbalance handling inside CV
→ train candidates → evaluate (repeated stratified k-fold on train; untouched test set once at the end)
→ calibrate (Platt/sigmoid; isotonic only if ≥ ~1,000 positives) → choose operating threshold from review capacity
→ SHAP → serialize (artefact + model card + sha256) → production inference (hash-verified load)
```

- **Leakage controls.** Label columns are removed at ingestion. `isFlaggedFraud` is excluded.
  - Group and destination aggregates are computed inside the fit/transform boundary (sklearn `Pipeline`/`ColumnTransformer` with custom transformers).
  - No resampling happens before the split. SMOTE is allowed only inside the imblearn pipeline per CV fold, and only if it beats class weights.
- **PaySim specifics.**
  - Fraud occurs only in TRANSFER/CASH_OUT, so train and evaluate on those types and report the "type-only" baseline.
  - Balance-arithmetic features are known simulator artefacts. Report the model **with and without** them to show what is learnt.
  - The 10k sample has only 68 positives. Report repeated-CV means with bootstrap confidence intervals. Obtaining the full public PaySim set is recommended for credible numbers.
- **Metrics.** Primary: **PR-AUC** (average precision) at true prevalence.
  - Also report ROC-AUC, recall@top-k / top-1%, precision at the operating threshold, confusion matrix, Brier score and a calibration curve.
  - Baselines: prevalence, type-only, rules-only.
  - Accuracy is not reported as a headline metric.
- **Model card** (JSON, next to the artefact). Training-data hash, feature list and feature-registry version,
  applicability domain, metrics with CIs, threshold and its rationale, library versions, artefact SHA-256, date.

### 5.3 Unsupervised pipeline

- **Features.** Log amount; robust z of amount within payee, account and type peer groups; amount percentile; hour and weekday (only when real time is present); days since payee first seen (point-in-time); payee transaction count to date; roundness level; digit features; days to period end.
- **Model.** `IsolationForest(n_estimators≈200, contamination="auto", random_state fixed)`, fitted per run on the case's own data. This is transductive by nature, which is correct for audit analytics.
- **Score.** Convert to a within-dataset percentile `p`. The signal strength is `s = clip((p − p0)/(1 − p0), 0, 1)` with `p0` from config (e.g., 0.95), so only the tail contributes.
- **Validation.** No labels, so evaluate with (a) the synthetic scenario benchmark (injected anomalies → precision@k, recall@k) and (b) PaySim labels as a weak proxy. Results are published in the model card.
- **As built (P8).** Pure-Python Isolation Forest (100 trees, ψ = 256, seed from config), threshold = top tail of reference scores above 0.5 capped by review capacity (not `contamination`), plus robust peer z and tail percentiles; reference = the dataset after a 90-day warm-up, or a chronological split. See [`anomaly-model-card.md`](anomaly-model-card.md).

### 5.4 Signals and the unified risk score (R12)

Every detector emits **signals**:

```
Signal { subject: txn|entity|pair, subject_id, code, family, strength s∈[0,1], weight w∈(0,1],
         raw_value, evidence: {rows, entities, peers, match, cycle_path, …}, template_id }
```

Families: `amount`, `timing`, `duplication`, `structuring`, `digit`, `entity`, `relationship`,
`reconciliation`, `model_anomaly`, `model_supervised`, `data_quality`.

**Fusion (noisy-OR with family caps).**
- Within a family, take the strongest signal plus diminishing returns (config), so correlated signals do not stack.
- Across families: `risk = 1 − Π_f (1 − w_f · s_f)`.
- Contribution of family `f`: `c_f = −ln(1 − w_f s_f) / Σ_g −ln(1 − w_g s_g)`. This is an exact decomposition of the
  score in log-survival space, and it produces the PS's "65% due to unusual timing + high amount".
- Contributions inside a family are split the same way across its signals.

**Tiers.**
- `high` if `risk ≥ τ_high` **and** at least 2 independent families fire, or if a configured *critical* signal fires (e.g., a large payment with no bank evidence).
- `review` if `risk ≥ τ_review`. Otherwise `low`.

**Volume guard.**
- Every run reports the flagged percentage.
- If it exceeds the configured review budget (e.g., 5%), the run is marked `calibration_warning`. That prevents the current 84%-flagged failure mode from going unnoticed.

**Entity risk.** Noisy-OR of the entity's top-k transaction risks, exposure-weighted, plus entity-level signals:
ER near-duplicate of another vendor, ghost-vendor indicators, cycle membership, centrality inside a suspicious community.

**Weights.**
- Initial weights are expert-set, documented in config with a rationale, then tuned against the synthetic scenario benchmark.
- They are never fitted on the evaluation set.
- If real labelled outcomes accumulate (analyst confirm/dismiss), a stacked logistic calibration can be fitted later and versioned like a model.

### 5.5 Explainability (R10)

Three layers, each named honestly:

1. **Score breakdown (exact).** Family and signal contributions from §5.4, plus the template sentence for each signal
   ("Amount ₹4,98,000 is 7.2 MADs above this vendor's median of ₹41,500").
2. **Model attributions.**
   - Supervised: **SHAP** values (TreeExplainer for HGB, LinearExplainer for LR) for the model component, labelled "SHAP".
   - Isolation Forest: SHAP TreeExplainer if performance allows; otherwise robust per-feature deviations labelled "feature deviation" (not SHAP).
3. **Evidence.** Links to source rows (`dataset_id`, `source_row`), peer comparison (vendor history), ER match
   pair, cycle path, reconciliation diff, Benford subgroup.

The explanation object is produced by the backend and rendered verbatim. The frontend never composes explanation text.

## 6. Forensic feature review

| Feature (current) | Verdict | Target definition |
|---|---|---|
| Amount anomalies (`amount_norm`, `amount_deviation`, `>50,000` rule) | **Redesign** | Robust z = (x − median)/(1.4826·MAD) within peer group (payee, account, type) on log scale; global percentile; minimum peer size from config, falling back to a broader group |
| Balance anomalies (`balance_error`, `balance_drain`) | **Correct + scope** | Keep for schemas with running balances (PaySim, bank statements: balance continuity check). Mark as a *data-integrity / simulator-specific* signal. For a GL, replace with "journal entries must net to zero" and "bank running balance continuity" |
| Benford (`benford_flag`, digit-7/8/9 rule) | **Replace** | Dataset and subgroup tests (§7). No per-row Benford flag; rows only inherit "belongs to a non-conforming subgroup" as weak context |
| Round numbers (`round_amount`) | **Correct** | Roundness level (multiple of 10/100/1,000/10,000). Test whether a payee's proportion of round amounts exceeds the population rate (binomial test). Weak signal on its own |
| Threshold / structuring (`threshold_flag` 4800–5100) | **Redesign** | Thresholds from config (client approval limits / delegation of authority; regulatory cash limits). Flag amounts within x% below a threshold, and *split patterns*: same payee, window ≤ N days, ≥2 payments each below threshold, sum above it |
| Velocity (`velocity`, `rapid_txn_flag`) | **Redesign** | Rolling-window counts and sums per entity (1d/7d/30d) on real timestamps, compared with the entity's own history (point-in-time). Disabled when timestamps are absent |
| Behavioural profile (`avg_amount_user`, `sudden_spike`) | **Redesign** | Entity profile (amount distribution, weekday mix, counterparties). Deviation scores need ≥ N past transactions, otherwise "insufficient history" |
| Fan-in / fan-out (`many_to_one_flag`, `multi_receiver_flag`) | **Move to graph** | Windowed distinct counterparties, judged against the population distribution (percentile), not fixed >3/>4 |
| Circular transactions (`circular_flag`, 1-hop) | **Replace** | Temporal cycle detection (§9) |
| Fuzzy matching (`stringSimilarity`, `fuzzy_flag`, `self_transfer`) | **Replace** | Entity-resolution pipeline (§8). Sender-vs-own-receiver similarity is removed |
| `is_new_dest` (top-100), `new_vendor_risk` (<3/<5) | **Replace** | Vendor age in days at transaction time (point-in-time) + "new vendor, large amount" rule + vendor-master creation date if available |
| Odd time (`step % 24`) | **Replace** | Real posting time: after-hours (config business hours), weekend/holiday (config calendar), period-end clustering, back-dating (posting date − document date) |
| Duplicates (notebook `is_duplicate`, unused) | **New / correct** | Exact duplicate (payee, amount, date, reference); near duplicate (same payee and amount within N days; reference similarity after normalisation); cross-vendor duplicate (same invoice reference/amount paid to ER-linked vendors) |
| Suspicious sequences | **New** | Vendor created → first payment within N days → escalating amounts; payment shortly after a vendor bank-account change (needs master change log); sequential invoice numbers from one vendor; dormant-then-active vendors |
| Outlier detection (scatter only) | **Replace** | Isolation Forest + robust z (§5.3). Scatter shows real scores with deterministic axes (amount vs anomaly score, or peer z) |
| Type one-hot (`type_*`) | **Retain (PaySim adapter only)** | Inputs to the supervised PaySim model |
| `transaction_count`, `unique_receivers` | **Retain inside profiles** | As point-in-time counts |
| `amount_ratio` (amount / balance) | **Retain, bounded** | Only when balances exist; use `log1p` and cap; never unbounded linear weight |

## 7. Benford analysis (R5)

1. **Extraction.** Take absolute amounts ≥ 10 (configurable) and exclude zeros, refunds/reversals if
   flagged, and *assigned* numbers (IDs, invoice numbers). First digit d₁ ∈ 1..9; first-two digits 10..99 for the
   first-two-digits test; last-two digits for a rounding/fabrication test.
2. **Expected.** `P(d) = log10(1 + 1/d)` (first digit) and `log10(1 + 1/dd)` (first two).
3. **Deviation measures.**
   - **MAD** = mean |observed − expected|, using Nigrini's conformity bands. First digit: ≤0.006 close, ≤0.012 acceptable, ≤0.015 marginal, >0.015 non-conformity. First-two digits: ≤0.0012, ≤0.0018, ≤0.0022, >0.0022.
   - **Per-digit z** with continuity correction: `z = (|p̂−p| − 1/(2n)) / sqrt(p(1−p)/n)`; highlight digits with |z| > 1.96, plus a stricter Bonferroni option.
   - **χ²** goodness-of-fit (8 / 89 df), reported with the caveat that it rejects almost everything at large n. This is why MAD is the primary measure.
4. **Sample size.** Fewer than ~300 values gives `insufficient_data` for the first-digit test (warn below 1,000). The first-two-digits test needs ~1,000+. Subgroups (per vendor, account or year) are tested only when they meet the minimum. Results carry n and confidence notes.
5. **Applicability check.** Warn when data is unlikely to be Benford-distributed: narrow range (max/min < 100), many
   capped or threshold-bound amounts, assigned values.
6. **Output.** Observed vs expected bars (digit highlighted when |z| significant), MAD and its band, χ², n, the
   subgroups ranked by MAD, and an interpretation template. Wording: "deviation is an investigative signal,
   not evidence of fraud". Rows in non-conforming subgroups whose digits drive the deviation are listed for sampling.

## 8. Entity resolution (R6)

```
raw names (+ attributes) → normalise → tokenise → aliases → block → score pairs → classify → cluster → canonical entities
```

- **Normalise.**
  - Unicode NFKC, case-fold, strip punctuation and extra whitespace.
  - Canonicalise legal suffixes via a config table: `l.t.d|ltd|limited → ltd`, `pvt|private`, `inc|incorporated`, `llp`, `co|company`, `&|and`.
  - Strip honorifics for people.
  - Transliteration is out of scope initially.
- **Tokenise.** Word tokens + character 3-grams. Remove a config list of generic words ("enterprises", "traders", "services") from *blocking* keys but not from scoring.
- **Aliases.** A user-maintained alias table (known trading names) is applied before scoring.
- **Blocking** (to avoid O(n²)). Union of keys: first significant token; sorted-token prefix; phonetic key (Double Metaphone on the first token); and shared attribute values (GST/PAN, bank account, phone, address postcode).
- **Pairwise features.**
  - Name similarity: Jaro-Winkler, token-set ratio, char-3-gram TF-IDF cosine.
  - Attribute agreements (exact GST/PAN, bank account, phone, email domain, address similarity).
  - Behavioural overlap ("shared payment patterns"): same approver, similar amount profiles, interleaved invoice numbers, overlapping active periods.
- **Scoring.** A transparent weighted score (Fellegi–Sunter-style log-likelihood weights from config) → confidence ∈ [0,1].
  - Thresholds: **match**, **possible** (needs review), **non-match**.
  - Weights can later be fitted on analyst-labelled pairs.
- **Clustering.** Union-find over *match* pairs, with a conflict check: a cluster cannot contain two entities with different verified GST/PAN.
  - Possible pairs stay as review items. Analyst confirm/reject is stored in `entity_aliases` and re-applied on reruns.
- **Employee–vendor conflicts.** The same pipeline runs across the employee and vendor masters, keyed on bank account, address, phone and PAN.
- **Output.** Similarity matrix (top candidate pairs with per-feature breakdown and shared payment stats), clusters with
  canonical names, and signals `entity.near_duplicate_vendor`, `entity.employee_vendor_link`.
- **Data required.** Vendor master and employee master (names, address, PAN/GST, bank account, phone, email, creation date).
  The current dataset only has synthetic codes (`C123…`/`M123…`), which have no linguistic content. ER is therefore developed on
  explicitly synthetic masters and optionally on real public payee lists (see `data-strategy.md`).
- **Library.** `rapidfuzz` (fast, offline, C++ backed) for string metrics; `jellyfish` for phonetic keys.

## 9. Relationship and graph analysis (R9)

- **Graph model** (networkx `MultiDiGraph`, aggregated to `DiGraph` for metrics).
  - **Nodes:** resolved entities (vendor, employee, customer, company bank account).
  - **Transaction edges:** payer → payee, aggregated per pair with count, sum, first/last date, and the list of `txn_id`s.
  - **Attribute edges** (undirected, typed): shared bank account, address, phone, PAN/GST, ER match. These make employee–vendor links visible.
- **Detections.**
  - Fan-in / fan-out: windowed distinct counterparties vs population percentile.
  - Reciprocal transfers: A→B and B→A within a window with similar amounts.
  - **Cycles:** restrict to strongly connected components, then bounded `simple_cycles(length_bound=k)` (k from config, e.g., 6). Keep only **temporally ordered** cycles (each hop after the previous one within the window) with a value-retention ratio inside bounds (money comes back largely intact = circular trading).
  - Multi-hop: k-hop paths from flagged entities.
  - Communities: Louvain (`networkx.community.louvain_communities`, fixed seed).
  - Centrality: weighted PageRank, betweenness (sampled for large graphs) → "gatekeepers".
  - Concentration: spend HHI and the top-N share per account and approver.
  - **Shell-like / ghost-vendor indicators:** recently created; few transactions of high value; missing master data (no GST/PAN/address); pass-through behaviour (receives and forwards similar amounts quickly); attribute-linked to an employee; no bank-statement evidence of payment.
- **Output.**
  - Nodes carry `risk` (entity risk → node size) and `centrality` (→ glow), as the PS specifies.
  - Edges carry aggregates and flags. Cycle and community findings carry their path and member IDs.
- **Frontend.** Cytoscape.js (vendored), which is purpose-built for interactive network views, replaces the static SVG.
- **Scale.** Aggregated graphs from 10⁵–10⁶ transactions are typically ≤10⁴–10⁵ nodes. This is fine for networkx with SCC pruning and bounded cycles.

## 10. Bank statement reconciliation (R8)

**Inputs**
- Ledger entries for the bank GL account(s): date, amount, direction, counterparty, reference, narration.
- Bank statement: transaction date, value date, description, debit, credit, running balance, cheque/UTR reference.

**Pipeline**
1. **Ingest.** CSV/XLSX first. Then text-based PDF (`pdfplumber` table extraction). Then **scanned** PDF/image via
   local **Tesseract OCR** (`pytesseract`), with a per-line confidence score. Low-confidence lines are marked and never auto-matched.
2. **Normalise.** Amounts to minor units with sign by direction. Dates parsed with the declared format.
   - Extract references from narration with regex (cheque no., UTR/IMPS/NEFT ref, invoice no.).
   - Counterparty name cleaned and mapped through ER aliases.
   - Check balance continuity (opening + Σ = closing). Breaks are integrity findings.
3. **Candidate generation.** For each ledger line, bank lines with the same direction, |Δdate| ≤ tolerance (config, e.g., 0–5 days) and amount equal or within fee tolerance. Indexed by amount for speed.
4. **Scoring.** Weighted evidence: amount equality, date distance, reference equality/similarity, counterparty similarity.
5. **Assignment.** One-to-one optimal assignment on the score matrix per amount/date block (`scipy.optimize.linear_sum_assignment`), then
   **one-to-many / many-to-one** grouping for the leftovers. This is bounded subset-sum within a date window and max group size, e.g., one bank debit = three ledger invoices.
6. **Classification.**
   - 🟢 **Matched:** amount exact, reference or counterparty match, date in tolerance.
   - 🟡 **Partial:** amount within tolerance or grouped match, or date outside tolerance, or reference mismatch.
   - 🔴 **Missing evidence:** a ledger entry with no bank counterpart (possible fictitious payment), or a bank entry not in the ledger (unrecorded transaction).
7. **Exceptions → findings and signals.** `reconciliation.missing_in_bank` (critical when the amount is material), `reconciliation.amount_mismatch`, `reconciliation.unrecorded_bank_txn`, `reconciliation.payee_mismatch` (ledger says vendor A, bank shows a different beneficiary).

**Output**
- Side-by-side rows with status, score, diffs, and the matched IDs on both sides.
- Summary: matched %, value of unreconciled items by age.

## 11. Audit memo and reporting (R11, R15)

- **Report model.** A structured object built from a case snapshot (one run, findings with status ≠ dismissed). Sections:
  - Executive summary.
  - Scope and data: files, hashes, periods, row counts, integrity score.
  - Methodology: modules run, config hash, model versions and model-card metrics.
  - Findings, each with its own evidence: high-risk transactions, entity findings, Benford results, reconciliation exceptions, relationship findings, model findings.
  - Explanations for each cited item.
  - Recommendations.
  - Limitations (modules `not_available`, data gaps).
  - Appendix: evidence references, run manifest, audit-trail extract, generation timestamp.
- **Memo assistant (local).** Deterministic natural-language generation: per finding type, sentence templates in standard audit
  tone (config). Numbers come only from the report model. Every sentence carries the finding IDs it is based on.
  - No LLM is required.
  - An optional local LLM rephrasing step (offline runtime) can come later behind a flag. Its output must keep every number and ID, verified by a post-check.
- **Renderers.** The same report model goes to JSON (UI preview and copy) and to PDF via **ReportLab** (pure-Python wheels, works
  offline on Windows without system libraries). The PDF's SHA-256 is stored in `reports`.
- **Never fabricated.** Sections with no findings say so explicitly ("No reconciliation performed: no bank statement supplied").

## 12. Case, investigation and evidence management (R13, R14)

- **Case:** name, client, period, status (Open → In review → Closed), created_by (local user name).
- **Evidence:**
  - Every imported file is copied to `workspace/evidence/<sha256>.<ext>`, marked read-only, recorded with original name, size, kind, import time, parser and version.
  - Analyses always read the stored copy.
  - P3: the SHA-256 is computed over the bytes as they stream in, with the size limit enforced per chunk.
    - Identical files are stored once.
    - The stored copy is re-hashed before every canonicalisation.
    - `ledgerspy evidence verify` re-hashes all evidence and checks it is read-only.
    - Rejected uploads are deleted; an `upload.rejected` audit entry is kept.
- **Findings.**
  - Generated per run; deduplicated across runs by a stable key (type + subject).
  - Status: New → Under review → Confirmed exception | Dismissed (reason required) | Escalated.
  - Analyst notes and attachments (hashed).
- **Audit log.** Append-only table; each entry has `seq, ts, actor, action, object, details, prev_hash, hash = sha256(prev_hash || canonical_json(entry))`.
  - P2: `entry = {seq, ts, actor, action, object, details}` with `details` as its stored canonical-JSON text; genesis `prev_hash` is 64 zeros. SQLite triggers refuse UPDATE/DELETE, and `verify` also fails if those triggers are missing or altered.
  - Limitation: deleting entries from the *end* of the chain is undetectable from the database alone. `verify` prints the head hash and count so they can be anchored outside the DB (e.g. in reports, P16).
  - Verifiable by a CLI command (`ledgerspy audit verify`).
  - Logged actions: imports, mapping changes, cleaning decisions, runs, status changes, report generation.
- **Reproducibility.** The run manifest holds input file hashes, mapping hash, config hash, model artefact hashes, the code version (package version + git commit) and library versions.
  - Re-running the manifest reproduces scores bit-for-bit (fixed seeds).

## 13. Security strategy

| Area | Control |
|---|---|
| Network exposure | Bind to `127.0.0.1` only. Random per-launch session token required in a header. Reject requests whose `Host`/`Origin` aren't the local origin (defends against DNS rebinding and CSRF from other sites in the same browser) |
| HTML rendering | Never insert data with `innerHTML`. Use `textContent` / DOM APIs; a single `escapeHtml` helper for the rare template. Strict CSP: `default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'`. That requires removing inline `onclick` handlers (planned) |
| Uploaded files | Allow-list of extensions **and** magic-byte checks. Size limit (config). Row/column limits. Encoding detection with UTF-8 fallback rules. Filenames never used as paths (stored by hash). Processing in a temp dir |
| CSV | Python `csv` module (RFC 4180 quoting). No evaluation of content. **Formula-injection protection on export:** prefix cells starting with `= + - @ \t \r` with `'` |
| XLSX | `openpyxl` read-only, `data_only=True` (cached values, no formula evaluation). Reject `.xlsm`/macros. Zip-bomb protection: check uncompressed size and ratio before parsing. Reject external links. P3 also refuses `vbaProject.bin`/macro content types, ActiveX, embedded OLE objects and any DTD/ENTITY in workbook XML; the row width is bounded so a bogus sheet dimension cannot inflate rows |
| PDF / OCR | Parse with `pdfplumber` (no JavaScript execution). Page and size limits. OCR runs locally |
| Model artefacts | Loaded only from `models/`. SHA-256 must match the model card and the registry. Pickle/joblib files are never loaded from user uploads |
| Evidence integrity | Hash on import. Read-only copies. Hashes printed in reports. Hash-chained audit log |
| Local storage | Workspace in the user profile with OS permissions. Recommend OS full-disk encryption (BitLocker/FileVault) rather than custom crypto. No browser `localStorage` for case data |
| Dependencies | Pinned versions. Offline wheelhouse with hashes (`pip install --require-hashes`). Periodic `pip-audit` on a connected build machine |
| Authentication | Single local user: OS login + session token. Multi-user auth is out of scope unless the deployment model changes |

**As implemented in P2** (`ledgerspy/api/security.py`, `ledgerspy/api/app.py`):
- `ledgerspy serve` creates a random token per launch and prints `http://127.0.0.1:<port>/?token=…`. Opening it sets an `HttpOnly; SameSite=Strict` cookie and redirects to `/`, so the token leaves the address bar. Without that cookie, `/` shows a "locked" page.
- The served page carries the token in `<meta name="ledgerspy-session-token">` (injected at serve time; `frontend/` files are unchanged) for the UI's later API calls.
- Every `/api/*` path except `/api/health` requires `Authorization: Bearer <token>`. The cookie and query string never authorise API calls (CSRF). Deny-by-default: new routes are protected automatically. `/docs` and `/redoc` are disabled (they load a CDN); `/api/openapi.json` needs the token.
- `Host` must be `127.0.0.1:<port>` or `localhost:<port>`. `Origin`, when present, must be that origin. `Sec-Fetch-Site` must be `same-origin` or `none`.
- Headers on every response: CSP, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, COOP/CORP `same-origin`, `Permissions-Policy`, `Cache-Control: no-store`. uvicorn runs with `proxy_headers=False`, no `Server` banner and no access log (the entry URL carries the token).
- **CSP interim (until P20).** `script-src 'self' 'unsafe-hashes' <sha256 of each inline handler in index.html>`, computed at startup. There is no `'unsafe-inline'` for scripts, so injected markup cannot run script while the existing `onclick=` handlers keep working. `style-src` still allows `'unsafe-inline'` for the 161 inline `style=` attributes. P20 removes both.

## 14. Offline / air-gapped strategy (R1)

**Definition.** LedgerSpy is *air-gapped capable* when:
- (a) installation can be done entirely from an offline bundle;
- (b) at runtime the process opens **no outbound connections** and the UI loads **no external resources**;
- (c) all features work with networking disabled;
- (d) no telemetry exists;
- (e) updates arrive as files (a new bundle), verified by hash.

**How.**
- **Python.** `pyproject.toml` with pinned dependencies. `pip download` builds a wheelhouse for the target OS and Python on a connected build machine, and `pip install --no-index --find-links wheelhouse --require-hashes` installs it offline. A PyInstaller single-folder build is an optional later convenience.
- **Frontend assets.**
  - Fonts: Sora and DM Mono are OFL-licensed and self-hosted as `.woff2` under `frontend/assets/fonts/` with `@font-face`. Until vendored, use a system font stack.
  - Cytoscape.js is vendored as a file. No CDNs.
- **Tesseract.** Bundled installer or documented offline install. Language data shipped locally.
- **Verification.**
  - Automated test: the full pipeline and API run with sockets blocked except loopback (`pytest-socket`).
  - Browser test: page load records zero non-loopback requests.
  - A manual acceptance run is done with the network adapter disabled.

## 15. Technology choices (each with a reason)

| Technology | Reason |
|---|---|
| Python 3.11+ | The analytics ecosystem the project already uses |
| pandas, numpy | Tabular processing (already used) |
| scikit-learn | LR, HistGradientBoosting, IsolationForest, calibration, metrics, pipelines: one library |
| shap | Real model attributions (already used in the notebook) |
| scipy | χ² test, `linear_sum_assignment` for reconciliation |
| networkx | Cycles, SCC, centrality, Louvain without a graph DB |
| rapidfuzz, jellyfish | Fast offline string similarity and phonetic keys for ER |
| openpyxl | Safe XLSX reading (PS: Excel ledgers) |
| pdfplumber, pytesseract (+ Tesseract) | Bank statements, including scanned (later phase) |
| FastAPI + uvicorn + pydantic | Local API with typed contracts and generated OpenAPI docs; lightweight |
| SQLite (stdlib `sqlite3`) | Zero-install, single-file persistence for cases and results |
| PyYAML | Human-editable config |
| ReportLab | PDF generation without system libraries (Windows-friendly, offline) |
| Vanilla JS (ES modules), Cytoscape.js | Keeps the existing UI with no build toolchain; Cytoscape for the real graph |
| pytest, pytest-socket, ruff | Tests, network-guard tests, lint/format |

Explicitly **not** used (no current need): React or another SPA framework, Docker, Celery/Redis, PostgreSQL, Kubernetes,
cloud services, LLM APIs.

## 16. Target repository structure

```
LedgerSpy/
├── ledgerspy/                 # the single source of truth (Python package)
│   ├── config/                # loader, schema validation, hashing (values live in /config/default.yaml)
│   ├── canonical/             # pydantic models (Transaction, Entity, Signal, Score, Finding, Explanation) + ID scheme
│   ├── ingest/                # csv, xlsx, (pdf, ocr), mapping, adapters (paysim, generic_ledger, bank)
│   ├── quality/               # integrity checks + readiness score
│   ├── normalize/             # explicit, logged transformations
│   ├── features/              # P6 feature registry: spec, data (canonical rows only), history (point in time, peers),
│   │                          #   transaction/entity features, deferred, matrix + manifest, report, registry_lock.json
│   ├── detectors/             # rules.py (P7), supervised.py
│   ├── anomaly/               # P8: eligibility, isolation_forest, robust_stats, evaluation, models, service
│   ├── forensics/             # models.py, duplicates.py, benford.py, service.py (P7); entity_resolution.py, graph.py,
│   │                          #   reconciliation.py, cashflow_sim.py (later)
│   ├── risk/                  # signal fusion, tiers, volume guard
│   ├── explain/               # explanation objects, SHAP wrappers
│   ├── cases/                 # sqlite store, evidence, audit log, findings
│   ├── reports/               # report model, memo NLG, PDF renderer
│   ├── synth/                 # P5 deterministic synthetic company + scenarios + separate ground truth
│   ├── api/                   # FastAPI app, routes, schemas
│   ├── hashing.py, paths.py   # canonical JSON + SHA-256; repository paths
│   └── cli.py                 # ledgerspy serve | train | evaluate | audit verify | synth
├── config/default.yaml        # all configurable values (hashed)
├── frontend/                  # existing UI (served by the API)
├── models/                    # artefacts + model cards (binaries git-ignored; cards committed)
├── data/
│   ├── raw/                   # public/sample data (committed, small)
│   └── synthetic/reference/   # generated (seed 42), explicitly marked SYNTHETIC; ground_truth/ kept apart
├── notebooks/                 # exploration only; never imported by the package
├── tests/
├── docs/
├── archive/                   # legacy hackathon prototype (read-only reference)
├── pyproject.toml
├── README.md
└── .gitignore                 # ignores workspace/, wheelhouse/, model binaries, caches
```

There is no separate `backend/`, `ml/` or `analytics/` top level. One package with sub-modules avoids
cross-package duplication, which was the root problem of the old project.
