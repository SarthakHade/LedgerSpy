# LedgerSpy — Current-State Audit (read-only analysis)

> **Historical document (Phase 1, 2026-10-04).** File paths refer to the layout *before* the Phase 2 cleanup
> (e.g. `Frontend_Hack/` is now `frontend/`, `Datasets.csv` is now `data/raw/paysim_sample_10k.csv`).
> See `docs/cleanup-log.md` for the mapping.

Scope: every file in `G:\SE HACK`, read without changing anything. Python notebooks were parsed cell by cell, including saved outputs. The datasets were profiled with pandas. The browser engine (`ml-engine.js` + `data-pipeline.js`) was run headlessly in Node against the project's own `Datasets.csv` to measure what it actually outputs.

---

## 1. EXECUTIVE SUMMARY

LedgerSpy is two separate projects that never connect:

1. **A Python/Jupyter ML prototype** (`core_model.ipynb`, `model_2.ipynb`). It is trained on a 10k-row sample of a PaySim-style mobile-money dataset, uses a Logistic Regression and adds a rule boost on top. It runs offline only. It has no API, and the files it saves (`fraud_model.pkl`, `features.json`) are **not in the upload**. `main.py` imports a `model` module that **does not exist**.
2. **A static browser front-end** (`Frontend_Hack/ledgerspy.html` + 4 JS files). It parses a CSV in the browser and scores every row with a **hand-written JavaScript formula**: invented "logistic" weights, a sigmoid, then additive rule boosts. It does not load, call or reproduce the trained Python model.

On the project's own dataset (10,127 rows, 68 real frauds = 0.67%), the browser engine labels **8,493 rows (83.9%) as "Fraud"**, 23 as Suspicious and 1,611 as Safe. That is a **precision of 0.8%**, and it reports "₹104 Cr exposure". Your own screenshots show the same behaviour (79.3% high-risk). The engine also **reads the `isFraud` label column and forces labelled rows to ≥85% risk**, which is ground-truth leakage into the displayed result.

About half of the "smart" features are **static HTML or simulations**: Bank Reconciliation (`Math.random()`), Monte Carlo (hard-coded SVG and "68% insolvency" text), Industry Benchmark (hard-coded 1.2%, "zero-knowledge proofs" claim), the Benford chart, the risk donut, the "Detection Precision 93.7%" tile, most of the Audit Memo, the PDF download (`alert()`), Auto-Fix (`alert()`) and the entity addresses and directors.

**No Problem Statement document exists in the upload.** The only thing labelled "PS" is *"PS — Piyush's Story"*, a narrative inside the About modal. The requirements in §6 are therefore reconstructed from what the project itself claims: the About modal, the `HACKATHON REQUIREMENT` and `HACKATHON BROWNIE POINT` code comments, the README and the tab names. **Please send the real PS if you have one.** The matrix should be re-checked against it.

---

## 2. WHAT LEDGERSPY CURRENTLY DOES

**Intended problem** (from the About modal and README): an *air-gapped, AI-powered forensic audit tool for internal auditors, fraud investigators and compliance teams*. It takes a raw transaction ledger and surfaces fraud: fabricated amounts (Benford), look-alike vendors (fuzzy matching), circular or shell-company trading, odd-hour payments, round amounts and structuring. It then explains each flag, reconciles against the bank statement, and produces a "court-ready" audit memo.

**Intended users:** internal auditors, forensic investigators, compliance teams (persona: "Piyush", junior auditor with 80,000 rows).

**What it actually does today:**

| Area | Reality |
|---|---|
| Input accepted | One `.csv` file in the browser (`.xlsx` and `.xls` are accepted by the file picker but **never parsed**). Python side: the hard-coded path `C:/Users/gaura/Downloads/Datasets.csv`. |
| Processing | Browser: naive CSV split → header alias mapping → ~30 engineered features → hand-tuned sigmoid + rules → 3-tier label. Python: cleaning → feature engineering → undersample + SMOTE → Logistic Regression → rule boost. |
| ML models | Python: `LogisticRegression(max_iter=1000)` (used) and `DecisionTreeClassifier(max_depth=5)` (trained, never evaluated or used). Browser: no ML model; a fixed formula. |
| Rules / heuristics | Python: 5 rules (+5 to +15). JS: 19 sigmoid weights + 20 additive rules + label override. |
| Explanations | Python: rule-reason strings, plus real SHAP plots in `model_2.ipynb` (offline only). JS: rule-reason strings (real per row) and a "SHAP-style" bar chart that is **not SHAP** (§9). |
| Frontend shows | Hero panel, 7 analysis tabs, a transaction investigation table, a chatbot and an audit memo. Each is a mix of computed and static content (§10). |
| Backend / API | **None.** No server, no `fetch`, no model loading. |
| What talks to what | `ledgerspy.html` ↔ 4 JS files (all global variables). The Python notebooks only talk to CSV files on one developer's machine. `chatbot.py`, `main.py` and `test_model.py` are connected to nothing. |

---

## 3. COMPLETE PROJECT ARCHITECTURE (as it exists)

```
G:\SE HACK
├── Datasets.csv                 raw data (10,127 × 17)               ← source of truth
├── archive.zip / Datasets.csv.zip   two zipped copies of Datasets.csv
├── cleaned_fraud_data.csv       notebook output (10,106 × 10)
├── featured_fraud_data.csv      notebook output (10,106 × 38)
├── smote_balanced_data.csv      notebook output (272 × 14)
├── core_model.ipynb             training notebook v1 (51 cells)
├── core_model (2).ipynb         training notebook v2 (52 cells, diverges)
├── model_2.ipynb                SHAP notebook (9 cells)
│
├── fraud-detection-project/     ← byte-identical copies of the notebooks + 3 CSVs, plus:
│   ├── main.py                  imports `from model import predict_transaction` → module missing
│   ├── requirements.txt         pandas, scikit-learn, numpy (incomplete)
│   ├── README.md                claims "Explainable AI", run `python main.py`
│   └── image.png / image-1.png / WhatsApp…jpeg   UI screenshots
│
└── Frontend_Hack/
    ├── ledgerspy.html           883 lines, single page, the whole UI
    ├── css/main.css, css/chatbot.css
    ├── js/data-pipeline.js      CSV parse, table, search, add-row, CSV export, data integrity, MOCK bank recon
    ├── js/ml-engine.js          feature engineering + JS scoring + dashboard updaters
    ├── js/ui-core.js            cursor, hex background, tabs, scatter plot
    ├── js/chatbot-ui.js         keyword chatbot (JS port of chatbot.py)
    ├── chatbot.py               standalone keyword function (unused)
    ├── test_model.py            reads missing test_transactions.csv, imports missing module
    ├── core_model (3).ipynb, model_2 (2).ipynb   duplicates
    └── dnv kvc.png, sdfcsdv.png older-UI screenshots
```

**Runtime architecture:** a single static HTML page opened from disk. All state lives in globals (`tableData`, `_csvRawRows`, `window._mlResults`, `window._mlSummary`). Script load order is `data-pipeline.js → ui-core.js → ml-engine.js → chatbot-ui.js`, and the files call each other's globals. There is no build step, module system, backend, database or persistence. A refresh loses everything.

**Missing artefacts referenced by code:** `model.py`, `fraud_model.pkl`, `features.json`, `shap_values.csv`, `test_transactions.csv`.

---

## 4. CURRENT END-TO-END DATA FLOW

### 4a. Browser application (what a user actually experiences)

| Stage | What really happens | Real / Demo |
|---|---|---|
| **Input** | `<input type=file>` → `fileLoaded()`. Only `.csv/.txt` are read (`FileReader.readAsText`). `.xlsx` is silently ignored. | Real (CSV only) |
| **Ingestion** | `parseCsvText()`: delimiter guessed from the first line (`;`, tab, else `,`), split on `\n`, quote handling by toggling. Quoted newlines and escaped quotes break it. The whole file is held in memory. | Real, fragile |
| **Data integrity** | `runDataIntegrityCheck()` samples ≤5,000 rows and counts blank or non-numeric amounts and blank vendor cells. It extrapolates, and score = `max(60, 100 − issue% × 10)`. | Real but crude. Floor of 60 and ×10 are arbitrary. "Auto-Fix" button = `alert()` |
| **Cleaning** | None. `parseNum()` turns unparsable values into `0`. | Missing |
| **Column mapping** | Two independent mappers. `findMLColIdx()` (ML fields, with partial-substring fallback) and `autoMapIdx()` (display fields id/date/vendor/amount/risk/reason, exact only). On the project's own `Datasets.csv` the display mapper finds **no id, date or vendor**, so every row shows `TX-???` and `—`. | Real, fragile |
| **Feature engineering** | `engineerFeatures()`: per-sender counts and averages, per-destination counts and sender sets, dataset-level Benford deviation, sort by sender + step, then ~30 features per row (§9). | Real computation; several definitions are weak (§8, §9) |
| **"ML model"** | `predictTransaction()`: `z = −8 + Σ wᵢ·fᵢ` with **19 hand-picked weights** → `sigmoid(z)×100` → add 20 rule boosts → **if `isFraud==1` then max(score, 85)** → clamp 0–100. | **Heuristic, not ML.** Not derived from the trained model |
| **Risk tiers** | ≥70 Fraud / ≥40 Suspicious / else Safe. The chatbot text says ≥50, which is inconsistent. | Real |
| **Explanation** | Per row: the list of fired rules (real). Global: `explainPrediction()` on the *average* feature vector of flagged rows, using a different weight table. | Per-row real; global pseudo-XAI |
| **Dashboard** | `updateDashboardWithML()` updates hero counts, gauge (= mean risk), exposure (= sum of amounts of "Fraud" rows), insights, part of the memo, and the Trends, Explainability and Relationships tabs. Donut, quick-stats, benchmark, Benford chart and Monte Carlo are not updated. | Mixed |
| **Investigation** | Table (first 500 rows), risk filter, column-scoped text search, expandable rule reasons, scatter of amount vs per-sender count, keyword chatbot over the in-memory results. | Real (UI over heuristic scores) |
| **Reporting** | Audit memo: only the executive-summary sentence is dynamic. "Download PDF" = `alert()`. "Download CSV" (Add-Tx modal) really exports the raw rows. | Mostly demo |

### 4b. Python prototype (offline, developer-only)

`Datasets.csv` → select 10 columns, `dropna` (10,127 → 10,106) → engineer ~28 features on the **full** dataset → one-hot `type` (drop CASH_IN) → feature list of 13 (v1) or 14 (v2, adds `step`) → undersample non-fraud to 136 + 68 fraud = 204 rows → **SMOTE on all 204 before splitting** → 272 rows → random 80/20 split (55 test rows, not stratified, no seed) → LogisticRegression → **accuracy only** (0.945 v1 / 0.927 v2) → `predict_transaction()` adds rule points → `joblib.dump` → `model_2.ipynb` loads it and runs SHAP. Separately: a random-normal Monte Carlo and a random-normal "industry benchmark". Nothing flows to the browser.

---

## 5. FILE-BY-FILE ROLE SUMMARY

| File | Role | Status |
|---|---|---|
| `Datasets.csv` | Raw PaySim-style sample + extra columns | Real data |
| `archive.zip`, `Datasets.csv.zip` | Zipped copies of `Datasets.csv` (archive.zip verified to contain it) | Duplicate |
| `cleaned_fraud_data.csv` | 10 selected cols, NaNs dropped | Generated |
| `featured_fraud_data.csv` | 38 cols incl. engineered features and dummies, sorted by `nameOrig` | Generated |
| `smote_balanced_data.csv` | 272 SMOTE rows (136/136), 13 features + label | Generated, mostly synthetic |
| `core_model.ipynb` | Main pipeline v1, plus Monte Carlo and benchmark cells | Prototype |
| `core_model (2).ipynb` | v2: adds `step` feature and `class_weight='balanced'`, trains on pre-SMOTE data then **retrains** on SMOTE data | Divergent duplicate |
| `model_2.ipynb` | Loads `fraud_model.pkl`, SHAP bar and waterfall, coefficient plot, exports `shap_values.csv` | Prototype; inputs and outputs missing |
| `fraud-detection-project/*.ipynb, *.csv` | Byte-identical copies (md5 verified) | Duplicate |
| `fraud-detection-project/main.py` | Demo call to `predict_transaction` | **Broken**: `model` module missing |
| `fraud-detection-project/requirements.txt` | pandas, scikit-learn, numpy | Incomplete: missing imbalanced-learn, joblib, shap, matplotlib |
| `fraud-detection-project/README.md` | Short description and screenshots | Overclaims |
| `Frontend_Hack/ledgerspy.html` | Entire UI, much static content | Main app |
| `js/data-pipeline.js` | CSV parsing, table render, filters, search, add-row, export, data integrity, **mock** bank recon | Mixed |
| `js/ml-engine.js` | Feature engineering, heuristic scorer, pseudo-XAI, all dashboard updaters | Core of the app; heuristic |
| `js/ui-core.js` | Custom cursor, animated hex canvas, tab switching, scatter plot | Real UI |
| `js/chatbot-ui.js` | Keyword-routed assistant over in-memory results | Real, rule-based |
| `css/main.css`, `css/chatbot.css` | Styling | Real |
| `chatbot.py` | Python keyword function; not imported anywhere | Orphan |
| `test_model.py` | Loop over `test_transactions.csv` (missing). Builds `balance_error` from the receiver balance, wrong formula. No `type_*` keys, so it would throw `KeyError` | **Broken**, not a test |
| `core_model (3).ipynb`, `model_2 (2).ipynb` | Copies | Duplicate |
| `*.png`, `*.jpeg` | Screenshots (two from an older 5-tab UI) | Docs |

---

## 6. PROBLEM STATEMENT REQUIREMENTS (reconstructed — no PS file found)

Sources: the About modal "Core Capabilities", the code comments `HACKATHON REQUIREMENT` / `HACKATHON BROWNIE POINT`, the README features, the tab names and the memo sections.

1. Ingest raw ledgers (CSV/XLSX) locally
2. Data integrity / readiness check and cleaning *(marked HACKATHON REQUIREMENT)*
3. ML anomaly / fraud risk scoring per transaction
4. Benford's Law / digital frequency profiling
5. Fuzzy entity reconciliation (look-alike vendors)
6. Relational risk mapping / entity graph / circular trading / shell entities
7. Behavioural analysis: velocity, odd timing, spikes, fan-in and fan-out
8. Outlier / anomaly visualisation
9. Explainable AI (reasons, feature attribution)
10. Bank statement reconciliation *(marked HACKATHON REQUIREMENT)*
11. Trend / time-series analysis
12. Interactive investigation (search, filter, vouching, add records, export)
13. Audit assistant / chatbot
14. Automated audit memo / downloadable report
15. Monte Carlo going-concern stress test *(BROWNIE POINT)*
16. Privacy-preserving industry benchmarking *(BROWNIE POINT)*
17. 100% air-gapped / offline processing

---

## 7. PS REQUIREMENT IMPLEMENTATION MATRIX

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Ledger ingestion | 🟡 PARTIAL | CSV parsed in `parseCsvText`. `.xlsx/.xls` allowed by `accept=` but `fileLoaded` only handles `.csv/.txt`. Naive parser. |
| 2 | Data integrity + cleaning | 🟡 / 🔴 | Checks are real (null and non-numeric amount, blank vendor). Score formula arbitrary. "Auto-Fix" = `alert('Auto-clean applied…')`, no cleaning. |
| 3 | ML fraud scoring | 🟠 HEURISTIC (app) / 🟡 (notebook) | App uses `predictTransaction()` with invented weights. Notebook LR exists but isn't connected and has a flawed evaluation. |
| 4 | Benford's Law | 🟠 / 🔴 | JS computes the first-digit distribution and flags rows whose digit deviates >8 pp. **Chart is static HTML** ("+128%", "2.28×"). Notebook flag = "first digit ∈ {7,8,9}", which is not Benford. No χ²/MAD test. |
| 5 | Fuzzy entity reconciliation | 🟠 | Only compares a row's *sender vs its own receiver* (not vendor vs vendor master). JS similarity is a character-bag overlap, not the SequenceMatcher it claims to mirror. On synthetic IDs it gives 509 false "self-transfers". Memo's "Stationery L.T.D 94%" is static. |
| 6 | Relational mapping / circular trading | 🔴 MOCK (visual) / 🟠 (flags) | Graph is a **static SVG pentagon**. JS only overwrites its 5 labels with the top "Fraud" destinations. `circular_flag` = dest previously sent to orig (fires on 1 row). Addresses, directors, "Loop depth 5–6" and the "PMLA 2002" classification are hard-coded. The banner says "circular pattern" whenever any Fraud row exists. |
| 7 | Behavioural analysis | 🟠 | Velocity, rapid, spike, fan-out and many-to-one are computed. On this data senders are ~unique (10,121 of 10,127), so per-sender behaviour is degenerate (§8). |
| 8 | Outlier visualisation | 🟡 | Real scatter of amount vs per-sender tx-count, coloured by heuristic tier, with random jitter and random down-sampling. No statistical outlier method (z-score, IQR, Isolation Forest). |
| 9 | Explainable AI | 🟡 (per-row reasons) / 🔴 (global "SHAP-style") | Per-row fired-rule reasons are genuine. Global bars use a different weight table from the scorer, applied to averaged features. Default bars 65/20/10/5% are static. Real SHAP only in `model_2.ipynb`. |
| 10 | Bank reconciliation | 🔴 MOCK | `runMockBSR()`: `Math.random()` gives 85% "EXACT", 10% "VALUE DIFF" (amount × 0.9), 5% "NOT IN STATEMENT". Vendor + " INC". Labelled "(Simulated)". No bank file input. |
| 11 | Trends | 🔴 / 🟠 | "Monthly" bars = `step % 24` binned into 12 buckets labelled Apr–Mar, "FY 2024". No real dates used (the dataset has a `Date of transaction` column that is ignored). |
| 12 | Investigation table | 🟢 / 🟡 | Filter, search, expand, add row and CSV export work. Added rows are **not scored** (risk comes from a dropdown, default "suspicious"). Only the first 500 rows are rendered. IDs show `TX-???` on the project dataset. |
| 13 | Chatbot | 🟡 | Real keyword router over real in-memory results. No NLP or LLM. Some text contradicts the engine (thresholds, Benford). Ordering bug: "why is this fraud?" hits the *fraud list* branch before the *why* branch. |
| 14 | Audit memo / report | 🔴 MOSTLY STATIC | Only the exec-summary sentence is dynamic. Findings (140% digit 7, 94% fuzzy, 65% 11PM–2AM), "87% SEVERE", "Circular Trading Ring of 5" and recommendations are hard-coded. PDF = `alert()`. |
| 15 | Monte Carlo going concern | 🔴 MOCK (app) / 🟠 (notebook) | App: hand-drawn SVG paths plus the static text "68% chance of insolvency by Month 9". Notebook: generic random walk with made-up defaults (balance 100,000, μ 5,000, σ 20,000), not linked to ledger data. |
| 16 | Privacy-preserving benchmark | 🔴 MOCK | App: "Industry Avg 1.2%", "3.4x higher", "SaaS/Tech", "zero-knowledge proofs", all static (the "Your Ledger 4.1%" bar is never updated). Notebook: `np.random.normal(1.5, 0.5)` invented "industry" samples. No privacy mechanism. |
| 17 | Air-gapped | 🟡 | Scoring is genuinely in-browser with no data upload. But the page loads **Google Fonts** from the internet, the notebooks run `!pip install shap`, and there's no packaging for offline install. |

---

## 8. ML PIPELINE AUDIT

**Dataset (`Datasets.csv`)**
- 10,127 rows × 17 columns: `[index], step, type, branch, amount, nameOrig, oldbalanceOrg, newbalanceOrig, nameDest, oldbalanceDest, newbalanceDest, unusuallogin, isFlaggedFraud, Acct type, Date of transaction, Time of day, isFraud`.
- The core columns are the PaySim mobile-money simulator schema. `branch` (135 countries, Spanish names), `unusuallogin` (0–20), `Acct type`, `Date of transaction` (≈ Jan 2018 if D/M/Y) and `Time of day` look like appended columns. The fraud rate is flat across Time of day (25/19/24), which suggests they were randomly assigned.
- **Target `isFraud`:** 68 fraud (0.67%), 10,057 non-fraud, 2 NaN.
- **Fraud occurs only in TRANSFER (33) and CASH_OUT (35).** PAYMENT, CASH_IN and DEBIT have zero fraud, so `type` is close to a label proxy.
- 95.6% of fraud rows drain the sender exactly (`old − new == amount`). This is a PaySim signature.
- `isFlaggedFraud` is all 0. `step` only ranges 1–7. Small NaN counts exist in 13 columns (2–10 each).
- `nameOrig` is unique on 10,121 of 10,127 rows. **Each sender effectively appears once.**
- None of `branch`, `unusuallogin`, `Acct type`, `Date`, `Time of day` is used by any model.

**Preprocessing:** column subset + `dropna` (→10,106). No type validation, deduplication or outlier treatment.

**Feature engineering (notebook)**, all computed on the **full dataset before any split**:
- `amount_ratio`, `round_amount`, `balance_error`. These are good signals, but `balance_error` largely encodes the PaySim drain signature.
- `transaction_count`, `avg_amount_user`, `amount_deviation`, `time_gap`, `velocity`, `rapid_txn_flag`, `prev_amount`, `sudden_spike`, `unique_receivers`, `multi_receiver_flag` are **degenerate** because senders are unique. Measured: velocity = 1.0 for every row, transaction_count = 1 for all, multi_receiver_flag = 0 for all, fuzzy_flag = 0 for all.
- `odd_time = step%24 <6 or >22`. With step 1–7 this just means "step ≤ 5", a proxy for step and not hour-of-day. The real `Time of day` column is ignored.
- `is_new_dest` = not in the top-100 destinations. `new_vendor_risk` = dest seen <5 times. Both are computed transductively over all rows.
- `benford_flag` = first digit ∈ {7,8,9}. This is not a Benford test.
- `name_similarity` / `fuzzy_flag` compare sender ID vs receiver ID (synthetic codes like `C123…`), so this is meaningless on this data.
- `threshold_flag` (4900–5000), `is_duplicate` and `self_transfer` are computed but not used in the model.

**Encoding:** `get_dummies(type, drop_first=True)`. **Scaling: none.** Amounts range to 10⁷ and ratios to 10⁶, which produces the lbfgs ConvergenceWarning in v2.

**Model features (v1):** amount, amount_ratio, balance_error, amount_deviation, velocity, odd_time, new_vendor_risk, fuzzy_flag, multi_receiver_flag + type_CASH_OUT/DEBIT/PAYMENT/TRANSFER (13). v2 adds `step`. Three of these are constants and one is ~0.

**Sampling:** non-fraud undersampled to 2× fraud (136) + 68 fraud = 204 rows. **SMOTE applied to all 204 before the split**, giving 272.

**Split:** `train_test_split(test_size=0.2)`, no `stratify`, no `random_state`. Test set = 55 rows, a mix of real and **synthetic SMOTE points interpolated from training neighbours**.

**Models:** `LogisticRegression(max_iter=1000)`, default C and L2. v2 first fits with `class_weight='balanced'` on the 204-row set, then **refits** on the SMOTE split. `DecisionTreeClassifier(max_depth=5)` is fit on all SMOTE data, then never evaluated or used.

**Evaluation:** accuracy only (0.945 / 0.927). No precision, recall, F1, ROC-AUC, PR-AUC, confusion matrix, cross-validation, or evaluation at the real 0.67% prevalence. The "test" is 55 balanced, partly synthetic rows, so the number says nothing about real-world performance.

**Leakage and methodology problems:**
1. SMOTE before the split. Synthetic test points are built from training points.
2. Transductive features (group stats, top-100 dests, dest counts) computed on all data, including test rows.
3. `type` makes fraud trivially separable in this sample. High accuracy mainly means "is it TRANSFER/CASH_OUT with a drained balance".
4. Constant and degenerate features inflate apparent sophistication.
5. A tiny, unseeded test set means results aren't reproducible.
6. `predict_transaction()` adds rule points *on top of a probability*. The "High velocity" rule (`velocity > 0.5`) **fires for every transaction** because velocity is always 1, so every score gets +10.
7. Decision thresholds (70/50) are not calibrated against any metric.
8. Benchmark cell uses `df.isFraud.mean()` (the *labels*), not predictions, as "company fraud rate". Cell 49 invents `fraud_rate = risk/100*5`.

**Persistence:** `joblib.dump(model,"fraud_model.pkl")` and `features.json` were written in the developer's working directory. **Neither is in the upload.**

**Prediction pipeline:** only inside the notebook. `main.py` → `from model import predict_transaction` fails because there is no `model.py`. Even if it existed, `main.py`'s input lacks `step` (needed by v2), and `test_model.py` lacks the `type_*` keys, so the `X.columns` selection would `KeyError`.

**Explainability:** `model_2.ipynb` uses real `shap.Explainer(model, X)` (linear) on the full featured data, with bar and waterfall plots and a coefficient bar chart. Valid as offline analysis. **Not used by the app.**

**Is the trained model used by the application? No.**
- `ml-engine.js` comments say "Logistic regression weights — calibrated" and "mirrors Python SequenceMatcher". Neither is true.
- The weights (e.g. `self_transfer 18`, `odd_time 10`, `amount_ratio 0.8`, bias −8) are hand-picked and unrelated to the notebook's `coef_`.
- The JS feature definitions differ from Python on: odd-hour window (0–5 vs <6 or >22), fuzzy threshold (0.72 vs 0.8), fan-out (>4 vs >5), new-vendor (<3 vs <5), threshold band (4800–5100 vs 4900–5000), Benford definition, velocity default (gap 999 vs fillna 0), and tier thresholds (40 vs 50).

**Measured JS engine behaviour on `Datasets.csv`** (run headlessly against the project's own code):
- Fraud **8,493 (83.9%)**, Suspicious 23, Safe 1,611. Mean risk 85. "Exposure" ₹104 Cr.
- Versus labels: TP 68, FP 8,425, FN 0. **Precision 0.8%**, recall 100%. The recall is trivial when 84% of rows are flagged.
- Main causes:
  - `amount_ratio × 0.8` is unbounded. 2,453 rows have zero opening balance, so the ratio ≈ the amount, and 3,115 rows have ratio >10. Ratio, balance error and amount alone push 3,894 rows past z>0.
  - `odd_time` fires on 5,504 rows.
  - `new_vendor_risk` fires on 6,217 rows.
  - `many_to_one` fires on 3,601 rows.
  - The character-bag "fuzzy" similarity fires on 509 rows.

---

## 9. FEATURE-BY-FEATURE IMPLEMENTATION AUDIT

| Feature | Verdict | Detail |
|---|---|---|
| Benford / digital frequency | Heuristic + static | JS: dataset-level first-digit % vs expected. A row is flagged if its digit's global deviation >8 pp, so *every* row with that digit is flagged. No χ², MAD, Z-stat, second-digit or per-vendor test. Chart is hard-coded HTML. Chatbot claims "7,8,9" (that's the notebook's rule, not the JS rule). |
| Fuzzy entity reconciliation | Heuristic, misapplied | Sender vs its own receiver only. JS algorithm = greedy character-multiset overlap (`"abc"` vs `"cba"` = 1.0). No vendor master, no normalisation (Ltd/L.T.D), no blocking or clustering. |
| Anomaly detection | Heuristic | No unsupervised model (Isolation Forest, LOF, autoencoder). "Anomaly" = heuristic tier. |
| Outlier detection | Visual only | Scatter plot. No statistical test. |
| Bank reconciliation | Simulated | `Math.random()`. No second file input. |
| Relational risk mapping | Static visual | Fixed SVG nodes and edges. Only labels change. |
| Network / graph analysis | Not implemented | No graph built, no cycle detection, no centrality or communities. `circular_flag` is a 1-hop "has dest ever paid orig" check. |
| Behavioural analysis | Partial | Per-sender stats computed correctly in JS, but useless on single-transaction senders. |
| Transaction velocity | Partial / degenerate | `1/(gap+1)` on `step`. Python: always 1. JS: 0.001 for first-of-sender. |
| Circular transactions | Minimal | 1-hop reciprocity only (1 hit in the dataset). Multi-hop cycles not detected. UI claims "5–6 hop loops" statically. |
| Many-to-one / one-to-many | Partial | Distinct-sender count >3 per dest; distinct-receiver count >4 per sender. Real counts, arbitrary thresholds, no time window. |
| Explainable AI | Partial | Per-row rule reasons are real. Global "inference breakdown" = second weight table × averaged features, normalised to 100%. That is not attribution, and not tied to the scorer's actual weights. |
| SHAP | Offline only | Real in `model_2.ipynb`. Absent from the app. |
| Risk scoring | Heuristic | Sigmoid of invented weights + additive boosts + label override. Gauge = mean score. |
| Audit memo | Mostly static | 1 dynamic sentence. |
| Data integrity | Partial | Real null and format counts for amount and vendor only. Arbitrary score. Fake auto-fix. |
| Monte Carlo | Static (app) / generic (notebook) | See §7. |
| Industry benchmarking | Fabricated | See §7. |
| Privacy-preserving benchmarking | Not implemented | "Zero-knowledge proofs" is a claim only. |
| Chatbot | Rule-based, real data | Keyword `if` chain. Uses live `_mlSummary` and `tableData`. |
| Report generation | Not implemented | PDF = `alert()`. Only CSV export of the ledger works. |
| Air-gapped processing | Mostly | No data egress. External font CDN; no offline packaging. |

---

## 10. DEMO / MOCK / HARDCODED FUNCTIONALITY

| # | Where | What | What it would take to make it real |
|---|---|---|---|
| 1 | `data-pipeline.js` `runMockBSR()` | Random EXACT/VALUE DIFF/MISSING; vendor + " INC"; amount × 0.9 | Bank-statement input, a matching engine (amount/date/reference tolerance, one-to-many), and real exception categories |
| 2 | `ledgerspy.html` Data Integrity "Auto-Fix" | `alert('Auto-clean applied…')` | Real cleaning rules plus a before/after diff |
| 3 | Insights "Download PDF Report" | `alert('Starting secure download of Audit_Memo_FY24.pdf…')` | PDF generation from computed findings |
| 4 | Audit memo §2–§5 | Static findings, "87% SEVERE", "Circular Trading Ring of 5", static recommendations | Generate every section from computed results |
| 5 | Overview donut | `stroke-dasharray` fixed at 84/12/4%. JS looks for `#donut-high/#donut-susp/#donut-safe`, which **don't exist** | Bind to the summary |
| 6 | Overview quick-stats | "Avg High-Risk Amount ₹0", "Total Fraud Exposure ₹2,45,00,000", **"Detection Precision 93.7%"**, "Vendors Flagged 14 Entities" never updated | Compute, or remove precision unless it is measured against labels |
| 7 | Privacy benchmark | Your Ledger 4.1% (never updated), Industry 1.2%, "3.4x higher", "SaaS/Tech", "zero-knowledge proofs" | Real benchmark data plus a privacy mechanism, or relabel as illustrative |
| 8 | Notebook benchmark | `np.random.normal(1.5,0.5,1000)` "industry" | Real external data |
| 9 | Benford chart (Trends) | Hard-coded bar heights, "+128%", "2.28×", "13.2% vs 5.8%" | Render from the computed `digitCounts` plus a statistical test |
| 10 | Monte Carlo (Trends) | Hand-drawn SVG paths, "10,000 iterations", "68% insolvency by M9", "M9 (Insolvency Break)" | Cash-flow inputs plus a simulation run in code |
| 11 | Notebook Monte Carlo | Arbitrary defaults, not data-linked | Parameterise from real cash-flow history |
| 12 | Trends "Monthly" chart | `step%24` bins relabelled Apr–Mar "FY 2024". The (old) screenshot alert text "September spike 52 vs avg 8" was static | Use real dates; aggregate by actual month |
| 13 | Relationships graph | Static pentagon SVG; only 5 labels swapped | Build a graph from orig→dest edges; detect cycles |
| 14 | Entity cards | "14 MG Road, Delhi", "SCO 44, Gurugram", "Same Director", "ADDR MATCH", "MAIN HUB" static | Vendor master data with addresses and directors |
| 15 | Relationship stats | "Loop Depth 5–6", "PMLA 2002 classification", "Circular Trading" | Compute from the graph |
| 16 | Relationship banner/conclusion | Says "circular pattern…shell entities" for any Fraud vendors | Only assert when a cycle is detected |
| 17 | Hero panel defaults | 142 / 864 / 47,285, 4.1% / 11.8%, "₹2.45 Cr… Circular Trading across 6 entities" (replaced after scan) | Show an empty state before a scan |
| 18 | Explainability defaults | 65/20/10/5%, "Risk = 87%", "2024 ledger" | Empty state |
| 19 | Explainability post-scan | Separate weight table, averaged features | Real attribution from the actual scorer |
| 20 | Table detail | "Algorithm confident match: Behavioral Anomaly Cluster #4" for every non-safe row | Real pattern or cluster ID, or remove |
| 21 | `predictTransaction()` | Hand-picked weights presented as "calibrated LR" | Use trained, evaluated model parameters |
| 22 | `predictTransaction()` label override | `isFraud==1 → ≥85` | Never use the label at inference |
| 23 | Scatter plot | `Math.random()` jitter (±9 px) and random down-sampling | Deterministic layout |
| 24 | Chatbot methodology text | "≥50% Suspicious" (engine uses 40), "20+ features", Benford "7,8,9" | Generate from config |
| 25 | `main.py` | Hard-coded demo input; broken import | — |
| 26 | `test_model.py` | Not a test; missing data and module | Real test suite |
| 27 | Duplicated implementations | Feature engineering (Py vs JS), chatbot (Py vs JS), 3× notebooks, 2× CSVs, 2× zips | Single source of truth |
| 28 | Footer / About | "100% Offline operation. System Secured.", "court-ready", "No cloud API calls" (Google Fonts is one) | Make true, or reword |

---

## 11. ARCHITECTURAL PROBLEMS

- **Two disconnected brains.** The Python model and the JS scorer share nothing: different features, thresholds and weights. Any accuracy claimed for the notebook does not apply to what the user sees.
- **No backend, API, persistence or audit trail.** Results vanish on refresh. There are no case notes, reviewer sign-off or history, which a forensic tool needs.
- **Global-variable coupling.** Four scripts depend on load order and on each other's globals (`tableData`, `_csvRawHeaders`, `window._ml*`).
- **DOM-scraping updates.** The dashboard is updated with brittle selectors like `div[style*="font-size:3rem"]`, `p:nth-of-type(2)` and `querySelector('text')`. Any markup tweak silently breaks a binding, which already happened with the donut.
- **Two column mappers** with different alias lists. `findMLColIdx`'s partial fallback matches substrings: `'to'` matches "total" and "Date of transaction", and an empty header `""` matches *every* alias. So an unmatched field can bind to the pandas index column.
- **Schema assumptions.** Built around PaySim columns (`step`, balances, `nameOrig/nameDest`). A typical GL or AP ledger (date, vendor, invoice, account, debit/credit) has no balances or sender, so most features become 0 or degenerate.
- **Index desync risk.** `_mlResults` is re-sorted by sender while `tableData` follows `_mlResults`. Rows added via the modal go into `tableData` and `_csvRawRows` but not into `_mlResults`. Scatter clicks use `tableData[idx]` against `_mlResults` indices, which drift after an add.
- **Performance.** The whole file is held in memory. `Math.max(...allAmounts)` will throw "Maximum call stack size exceeded" at roughly 10⁵–10⁶ rows. Per-row `userAmounts.filter` is O(n·k). Only 500 DOM rows are shown and there is no pagination. Scoring runs on the main thread with no worker.
- **Validation and error handling.** No schema validation. Bad numbers become 0 silently. No user feedback when required columns are missing. `.xlsx` fails silently.
- **Dependencies.** `requirements.txt` is incomplete. No pinned versions. Notebook paths are hard-coded to `C:/Users/gaura/…`. No `package.json` (fine for static, but no tooling).
- **Repository hygiene.** Triplicated notebooks, duplicated data and zips, no `.gitignore`, no version control evidence, and model artefacts missing.

---

## 12. DATASET / DATA GAPS

**A. Genuinely implementable with the existing dataset**
- Supervised transaction-level fraud classifier with honest evaluation (stratified split before resampling, PR-AUC and recall at k). Caveat: 68 positives is very small, and `type` plus the drain pattern dominate.
- Balance-consistency checks (sender and receiver).
- Amount outliers per type and per destination.
- Dataset-level Benford test on `amount` (10k rows is enough for a first-digit test; PaySim amounts may not conform naturally).
- Destination-level aggregation: fan-in (e.g. `C985934102` receives 62 transactions), merchant (`M…`) vs customer (`C…`) destinations.
- Data-integrity profiling (real NaNs exist in 13 columns; ambiguous date format).
- Real time-of-day analysis using the `Time of day` column, and daily trends using `Date of transaction` (≈3 weeks of data, not a fiscal year).
- A directed graph of orig→dest. Expect very few cycles, because senders are near-unique.
- SHAP on a real model.

**B. Requires additional datasets or files (none present)**
- Bank statements (for reconciliation).
- Vendor master and employee master (names, addresses, PAN/GST, bank accounts, directors). Needed for fuzzy vendor duplicates, shell detection and employee-vendor conflicts.
- Human-readable entity names (current IDs are synthetic codes, so fuzzy matching has nothing to work on).
- GL / chart of accounts, invoices, POs, GRNs (for three-way match and duplicate invoices).
- Company cash position and cash-flow history (Monte Carlo going concern).
- Industry fraud and anomaly rates (benchmarking).
- Multi-transaction sender histories (for real velocity and behaviour).

**C. Can be generated synthetically for development and testing**
- A bank statement derived from the ledger with injected timing differences, amount mismatches and missing items.
- A vendor master with injected near-duplicate names, shared addresses and shared bank accounts.
- Injected circular rings and fan-in/fan-out structures with known ground truth.
- Repeat-sender sequences for velocity testing.
- Synthetic cash-flow series for the simulation.

**D. Requires real-world external integrations**
- Bank feeds or statement formats (PDF, CSV, MT940, CAMT.053).
- ERP and accounting exports (Tally, SAP, QuickBooks, Zoho).
- Company registries (e.g. MCA21) for directors and addresses; sanctions and PEP lists.
- An industry benchmark consortium. True privacy preservation would need secure aggregation, differential privacy or federated methods, which conflicts with "air-gapped" unless done via offline exchanged aggregates.

---

## 13. SECURITY / PRIVACY / AIR-GAP GAPS

- **Stored XSS via CSV.** Vendor, ID, amount and reason values from the uploaded file are injected with `innerHTML` in `renderT`, the bank-recon tables, top-vendors, the chatbot and the entity cards. A crafted ledger cell like `<img src=x onerror=…>` executes.
- **External network call:** Google Fonts `<link>`. This leaks the user's IP and timing, and the page renders differently offline. It contradicts "No cloud API calls".
- Notebook `!pip install shap` requires network access.
- No authentication, roles, access logging, evidence hashing or chain of custody (despite the "court-ready" and "strictly confidential" claims).
- No data-at-rest handling: exported CSVs are unencrypted. (Acceptable for local use, but undocumented.)
- `joblib`/pickle model loading would be an arbitrary-code-execution risk if models are ever exchanged.

## 14. TESTING / RELIABILITY GAPS

- Zero automated tests (Python or JS). `test_model.py` is a broken script.
- No reproducibility: no random seeds on split or undersampling order, no pinned environment, missing artefacts.
- No validation dataset or metrics tracking. Metrics are accuracy on 55 partly-synthetic rows.
- No regression check that JS and Python agree.
- No input-schema tests, no large-file tests, no cross-browser tests.
- UI bindings are verified by nothing. The broken donut binding went unnoticed.

## 15. CRITICAL BUGS OR RISKS

1. **84% of transactions labelled Fraud** on the project's own data (precision 0.8%). The headline output is not usable.
2. **Label leakage at inference** (`isFraud==1 → ≥85`).
3. **Trained model not used.** Claims of ML-driven scoring are inaccurate.
4. **Static numbers presented as findings** ("Detection Precision 93.7%", benchmark, Monte Carlo, memo findings). For a forensic tool these could mislead a user or a judge.
5. **Leaky ML evaluation** (SMOTE before split; synthetic test rows).
6. **XSS from uploaded CSV.**
7. `main.py` / `test_model.py` crash (missing module and data; feature mismatch).
8. **Donut never updates** (missing IDs).
9. **Every row shows `TX-???`** for the project dataset. The chatbot "why is TX-123 flagged" lookup cannot work.
10. `.xlsx` silently unsupported.
11. Double currency symbol in Trends ("₹₹…") because `formatAmtShort` already prefixes ₹.
12. Chatbot routing: "fraud" keyword shadows "why".
13. Rows added via the modal are never scored and desync indices.
14. `Math.max(...array)` stack overflow on large files.
15. Partial-match column mapping can bind to the wrong column, including the empty-header index column.
16. Python "High velocity" rule fires on 100% of rows.

## 16. WHAT IS ALREADY GOOD AND SHOULD BE PRESERVED

- **The UI shell and information architecture:** hero → 7-tab analysis engine → investigation table → chatbot → audit memo. It's a coherent auditor workflow, visually polished, with a consistent design system (CSS variables, mono/sans pairing).
- **Client-side, no-upload processing model.** A good foundation for air-gapped operation.
- **Header auto-mapping concept** (alias tables): needs hardening, not replacement.
- **Feature catalogue** (balance error, amount/balance ratio, drain, round amounts, structuring band, fan-in/fan-out, reciprocity, Benford digit, odd hour, new destination). The right forensic vocabulary.
- **Per-row human-readable reason strings** and the decision/action tiers (Block / Review / Allow).
- **Investigation table UX:** risk filter, column-scoped search, expandable reasoning, add row, CSV export with original columns preserved (`_raw`).
- **Chatbot intent structure** and its grounding in live results (not canned).
- **Data-integrity dashboard concept** and real null/format counting.
- **Notebook groundwork:** the feature-engineering code, the `predict_transaction` output contract (`decision`, `risk_score`, `reasons`, `action`), real SHAP usage, and the Monte Carlo function skeleton.
- **The dataset itself** is useful for transaction-level detection.

## 17. WHAT NEEDS TO BE REBUILT OR REPLACED (what, not how)

- The browser scoring engine (`predictTransaction`) and the global "explainability" calculation.
- The ML training and evaluation methodology, and the missing model artefacts.
- The connection between trained model and app (currently nonexistent).
- Bank reconciliation (entirely).
- Relationship/graph analysis and the static graph and entity cards.
- Benford analysis (statistical test plus a chart bound to data).
- Fuzzy entity matching (algorithm and what it's applied to).
- Monte Carlo and benchmarking (inputs are fabricated).
- Audit memo and PDF generation.
- Data cleaning ("Auto-Fix").
- Trends time axis (real dates).
- CSV/XLSX ingestion robustness and column mapping.
- All hard-coded tiles (donut, quick stats, benchmark, defaults).
- `main.py`, `test_model.py`, `requirements.txt`; duplicate files.
- Persistence, input validation, XSS-safe rendering, offline asset bundling, tests.

## 18. CURRENT PROJECT READINESS ASSESSMENT

**Working functionality** (does what it says, on real data):
- CSV upload and parsing for simple, well-formed files.
- In-browser feature computation (balance error, ratios, per-destination counts, digit distribution, etc.).
- Per-row heuristic score, tier and fired-rule reasons. Mechanically correct, but poorly calibrated.
- Transaction table: filter, search, expand, add row, export updated CSV.
- Scatter plot of scored rows.
- Top-vendors-by-exposure aggregation and exposure totals (on heuristic tiers).
- Keyword chatbot answering from live results.
- Null and format counts in Data Integrity.
- Offline notebook: LR training, real SHAP plots (artefacts not present).

**Partial functionality:**
- Data integrity (score arbitrary, no cleaning).
- Benford (per-row flag, no test, static chart).
- Fuzzy matching (wrong algorithm and wrong comparison target).
- Behavioural, velocity, fan-in/out and reciprocity flags (degenerate on this data).
- Explainability (per-row real, global pseudo).
- Relationships tab (real names in a fake graph).
- Audit memo (1 dynamic sentence).
- Trends (real counts on a fake time axis).
- Air-gap (except fonts and pip).

**Demo functionality:**
- Bank reconciliation, Monte Carlo (app), industry benchmark and ZKP claim.
- Donut chart, quick-stat tiles incl. "93.7% precision".
- Benford chart, entity addresses and directors, loop depth / PMLA labels.
- Default hero and XAI values, PDF download, Auto-Fix.
- Notebook benchmark (random), notebook Monte Carlo (not data-linked).

**Missing functionality:**
- A formal Problem Statement document (in the upload).
- Backend/API or model serving; use of the trained model in the app.
- Persistence, case management, audit trail; authentication.
- XLSX parsing; bank-statement input; vendor/employee master input; real graph and cycle analysis.
- Statistical Benford test; real report/PDF generation; real cleaning.
- Automated tests; reproducible training; model artefacts; offline packaging.
