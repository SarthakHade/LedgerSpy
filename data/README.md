# Data

## `raw/paysim_sample_10k.csv`

- **Original filename:** `Datasets.csv`. md5 `aff98f7c430c0a2b8c8c74b82cf8d5f0`. 10,127 rows × 17 columns.
- **What it is:** a sample of the public **PaySim** mobile-money *simulation* (columns `step, type, amount, nameOrig,
  oldbalanceOrg, newbalanceOrig, nameDest, oldbalanceDest, newbalanceDest, isFlaggedFraud, isFraud`), with appended
  columns `branch, unusuallogin, Acct type, Date of transaction, Time of day` of unknown provenance.
- **Provenance class:** `real-public` (synthetic by origin: PaySim is a simulator).
- **Labels:** `isFraud` has 68 positives (0.67%) and 2 missing values; fraud occurs only in TRANSFER and CASH_OUT.
  `isFlaggedFraud` is all 0. **Labels are for evaluation only and must never be used as features or in scoring.**
- **Limits:** no vendors or employees, names are ID codes, senders are near-unique, there are no bank statements, and it covers roughly 3 weeks.
  It is **not** a corporate ledger. See `docs/data-strategy.md`.

## `synthetic/reference/` (P5)

- **What it is:** the seed-42 output of `ledgerspy synth`. It describes a fictional two-company group from
  2023-04-01 to 2026-03-31. Every name, address, phone (`+91-0…`), bank account (`SYN…`), bank code (`SYNB0…`),
  tax ID (`SYN-TX-…`) and email (`*.example`) is invented.
- **Provenance class:** `synthetic`. Every row carries `is_synthetic=true`.
- **Inputs (no labels):**
  - `SYNTHETIC_ledger.csv` and `SYNTHETIC_bank_statement.csv`: transaction files, importable through P3.
  - Master and reference files: vendor, employee and customer masters, group entities, relationships, holiday calendar, monthly cash flow.
- **Ground truth:** `ground_truth/SYNTHETIC_ground_truth.{json,csv}` covers 63 scenarios (45 injected, 18 controls) and the
  expected P4 issues. It is **for evaluation only**: never load it into a case or use it as a feature.
- **Integrity:** `SYNTHETIC_manifest.json` records parameters, per-file SHA-256 and `dataset_sha256`. The tests
  regenerate the set and require byte equality. Regenerate with
  `ledgerspy synth --out data/synthetic/reference --seed 42 --force` only when the generator changes on purpose.

## Rules

- `data/synthetic/` (roadmap phase P5) holds generated data only. File names start with `SYNTHETIC_`.
- Client data never goes in this folder. It lives in the git-ignored `workspace/`.
