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

## 4. Synthetic development data (to generate — Phase 5)

A seeded, documented generator (`ledgerspy synth`) produces a coherent fictional company:

| Output file | Content |
|---|---|
| `SYNTHETIC_vendor_master.csv` | Fictional vendors (generated names, fake-format GST/PAN that pass syntax but are flagged synthetic, addresses from a fictional address generator, bank accounts) |
| `SYNTHETIC_employee_master.csv` | Fictional employees with addresses, bank accounts, roles and approval limits |
| `SYNTHETIC_ledger.csv` | 2–3 years of AP/GL payments with realistic seasonality, amount distributions (log-normal per vendor), approval thresholds and posting times |
| `SYNTHETIC_bank_statement.csv` (+ optional rendered PDF) | Statement derived from ledger payments with realistic delays, fees, batching and narration formats |
| `SYNTHETIC_cashflow_monthly.csv` | Monthly inflows/outflows for the simulation |
| `SYNTHETIC_ground_truth.csv` | For every injected scenario: `scenario_id`, type, affected txn/entity ids |

Injected scenarios, each with ground truth so detectors can be measured:

- ghost vendor (no master data, paid via an employee-linked bank account)
- look-alike vendor ("Stationery Ltd" / "Stationery L.T.D")
- employee–vendor shared address or bank account
- circular trading ring (k = 3–6, temporally ordered, value retained)
- duplicate payments (exact and near)
- split payments under an approval limit
- round-amount fabrication by one vendor (Benford-distorting)
- after-hours, weekend and period-end postings
- ledger payments missing from the bank, and bank debits missing from the ledger
- payee mismatch between ledger and bank
- sudden spike for a dormant vendor

This **scenario benchmark** becomes the regression test suite for every detector (precision@k, recall per scenario).

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
