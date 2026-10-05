# LedgerSpy — Data Strategy

Rule: **nothing synthetic is ever presented as real.** Synthetic files carry `SYNTHETIC` in their name, carry an
`is_synthetic=true` column, and the UI shows a persistent "synthetic data" banner when one is loaded. No real
personal or company identity data is fabricated or committed.

## 1. Existing data

| File | What it is | Size | Labels | Usable for | Not usable for |
|---|---|---|---|---|---|
| `data/raw/paysim_sample_10k.csv` (originally `Datasets.csv`) | Sample of the public **PaySim** mobile-money *simulation* (step, type, amount, nameOrig/nameDest, balances), plus appended columns `branch`, `unusuallogin`, `Acct type`, `Date of transaction`, `Time of day` of unknown provenance (fraud rate is flat across them, so they look randomly assigned) | 10,127 rows × 17 cols; md5 `aff98f7c430c0a2b8c8c74b82cf8d5f0` | `isFraud`: 68 positive (0.67%), 2 missing; `isFlaggedFraud` all 0 | Ingestion/mapping tests, integrity checks (real NaNs exist), first-digit Benford test (n ≈ 10k), supervised-pipeline methodology, weak proxy for anomaly evaluation | Corporate-ledger forensics: no vendors/employees, IDs are codes (ER impossible), senders are near-unique (behaviour/velocity degenerate), no bank statements, no cash-flow series, ~3 weeks only |

Legacy derived files (`archive/hackathon-prototype/derived-data/`) are notebook outputs with the leakage problems
described in the Phase 1 audit. They are kept for history only and are **not** inputs to anything.

## 2. Data each module needs

| Module | Minimum fields | Better with |
|---|---|---|
| Ledger analytics (R5, R7, R16) | date, amount, payee/counterparty, (debit/credit), reference or voucher no. | posting time, document date, GL account, approver, user who posted, description |
| Entity resolution (R6) | vendor master: id, name | address, PAN/GST, bank account, phone, email, creation date, change log; employee master: id, name, address, bank account, phone, PAN |
| Graph (R9) | payer, payee, amount, date | resolved entities + master attributes (for attribute edges) |
| Reconciliation (R8) | bank statement: date, description, debit, credit (+ balance); ledger bank-account entries | value date, cheque/UTR references, bank account id |
| Cash-flow simulation (R18) | ≥ 24 months of monthly operating inflows/outflows, opening cash | committed payments, credit lines, seasonality |
| Benchmarking (R19) | a genuine sector-level reference rate with its definition and source | multiple peers' aggregates |
| Supervised model (R17) | labelled transactions of the **same domain/schema** | many more positives (≥ hundreds), timestamps for temporal split |

## 3. Additional datasets to obtain (real, public)

All are optional and must keep their source licences. None are in the repository today.

1. **Full PaySim dataset** (public, roughly 6.3M rows, published on Kaggle as "Synthetic Financial Datasets For Fraud
   Detection"). Gives credible supervised-model evaluation (thousands of positives, 30 days of steps for a temporal
   split). It is still *simulated* mobile money, so it is used only for the supervised known-pattern module.
2. **Real public payment ledgers**, for example government open-spending data (local-authority "payments over
   threshold" files, city or state "checkbook" datasets). These contain real payee names, amounts and dates, so
   Benford, duplicates, entity resolution, round-number and threshold analysis can be tested on realistic data.
   They have no fraud labels and are used for realism and false-positive-rate checks only.
3. Each candidate source must be checked for licence and personal data before use. Records naming private
   individuals are excluded or pseudonymised.

## 4. Synthetic development data (built in Phase 5)

`ledgerspy synth --out DIR [--seed N --start YYYY-MM-DD --years 2..5 --scale X --force]` generates a coherent,
fictional two-company group ("Larkspur Vale Components Pvt Ltd" and its subsidiary "Larkspur Vale Exports Pvt Ltd").
The generator is offline and deterministic: seeded sub-streams (SHA-256 of seed + component name) mean the same
seed always gives byte-identical files, and a different seed gives different files. Parameters (seed, start date,
years, scale, currency, approval limits, business hours) come from the `synth` section of `config/default.yaml`.
The committed reference dataset (seed 42, 2023-04-01 → 2026-03-31) is in `data/synthetic/reference/`. The tests
regenerate it and compare hashes. See `docs/synthetic-scenario-matrix.md` for the feature → scenario mapping.

| Output file | Content |
|---|---|
| `SYNTHETIC_group_entities.csv` | The 2 group companies, with addresses, bank accounts, approval limits from config, business hours and working days |
| `SYNTHETIC_vendor_master.csv` | 99 fictional vendors across 10 categories (no two firms share a name up to its legal form, so no unplanned look-alikes or shared e-mail domains). Names come from invented stems, cities are fictional, phones are `+91-0…` (not valid Indian numbers), bank accounts are `SYN…`, bank codes `SYNB0…`, tax IDs `SYN-TX-…` (deliberately not the GSTIN/PAN format), emails use `.example` |
| `SYNTHETIC_employee_master.csv` | 37 fictional employees with department, role, manager, approval limit, home address and bank account |
| `SYNTHETIC_customer_master.csv` | 26 fictional customers of the two companies |
| `SYNTHETIC_relationships.csv` | Declared relationships: parent/subsidiary, a declared employee–vendor interest, declared sister concerns |
| `SYNTHETIC_holiday_calendar.csv` | Fictional holiday calendar for the period |
| `SYNTHETIC_ledger.csv` | 3 years of AP, expense-claim, customer-receipt, payroll, statutory, bank-charge and intercompany postings. Per-vendor log-normal amounts, Oct–Dec seasonality, Tue/Fri payment runs, posting times within business hours, approval by threshold. 7,271 rows |
| `SYNTHETIC_bank_statement.csv` | One statement per company bank account, derived from the ledger. Most lines match exactly or shift 0–2 working days; the rest are fees, batches, one-to-many lines, ledger-only and bank-only lines, and delays. Running balances are continuous per account. 7,267 rows |
| `SYNTHETIC_cashflow_monthly.csv` | Monthly opening/inflow/outflow/closing per company, derived from the bank statement (72 rows) |
| `ground_truth/SYNTHETIC_ground_truth.json` and `.csv` | **Separate folder.** One record per scenario: `scenario_id`, type, control flag, severity, description, expected detection targets and phases, affected ledger/bank/vendor/employee/customer/group IDs, stages, injection parameters, start/end dates, explanation evidence, `also_part_of` (shared transactions). Also includes the exact P4 row issues the data must produce |
| `SYNTHETIC_manifest.json` | Parameters, per-file SHA-256/bytes/rows, and `dataset_sha256` over all files |

Every file has an `is_synthetic=true` column, or a top-level `synthetic: true` for the JSON files. The input files
contain no labels: nothing in the ledger, bank statement or masters says which rows belong to a scenario, and
entity IDs are assigned by onboarding/joining order, so IDs give nothing away either. A test scans every input file
for label-like column names and words, and the P3 pipeline test imports the inputs with the ground-truth folder
absent.

The reference dataset has **63 scenarios: 45 injected and 18 normal controls** (A–X; U = controls). Some scenarios
are isolated and some share transactions (`also_part_of`), for example the composite S-X-01. Injected behaviour
includes:

- amount anomalies and round-number fabrication, plus a Benford-distorting subgroup
- exact duplicates, re-paid invoices and near-duplicates (next day with a suffix, transposed digits)
- look-alike vendors
- a ghost vendor created by a buyer, sharing that buyer's address and bank account, with sequential invoices just under the CFO limit
- sudden new vendors, velocity spikes and **dormant-vendor reactivation (S-H-03)**
- a first-time high-value payment
- threshold splitting of claims and vendor payments
- weekend/holiday/night postings and **year-end late-night clustering (S-K-03)**
- small→large sequences
- an undeclared employee–vendor relationship
- shared attributes: address, phone, bank account
- circular trading rings with k = 3 and k = 4
- fan-in and fan-out concentration
- gradual risk escalation
- silent small fraud
- seven kinds of bank mismatch
- data-quality defects
- inconsistent GL/payment-mode combinations
- a composite geographic/new-vendor case

Each has a control that looks similar but is legitimate (rent, retainers, installments, seasonal rises, quarter-end
overtime, declared relationships, payroll batches and so on).

Not built in P5: the rendered PDF bank statement (deferred to P14b, PDF/OCR statements), and a UI banner for synthetic
datasets (the UI does not list synthetic datasets separately yet; every file and the P3 dataset name say SYNTHETIC).
Circular rings are k = 3–4 (not up to 6).

This **scenario benchmark** becomes the regression suite for every detector (precision@k, recall per scenario).

## 5. Optional real-world integrations (later)

| Integration | Mode (offline-compatible) |
|---|---|
| Tally / SAP / QuickBooks / Zoho exports | Importers for their standard export formats (file-based) |
| Bank formats | CSV/XLSX, PDF (text), scanned PDF/image via local OCR, MT940 / CAMT.053 parsers |
| Company registry (e.g., MCA21 director and company data) | Offline import of extracted files the auditor obtains separately; never live lookups |
| Sanctions / PEP lists | Offline list files, versioned and hashed |
| Industry benchmarks | Signed aggregate files from a trusted source, imported manually |

## 6. Data handling rules

- Client data lives only under `workspace/`, which is git-ignored, and is never committed.
- Label columns are stored separately and used only for evaluation.
- Every dataset records its provenance (`real-public`, `client`, `synthetic`) and licence. The UI and reports show it.
