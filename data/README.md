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

## Rules

- `data/synthetic/` (created in roadmap phase P5) holds generated data only. File names start with `SYNTHETIC_`.
- Client data never goes in this folder. It lives in the git-ignored `workspace/`.
