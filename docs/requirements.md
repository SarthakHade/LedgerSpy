# LedgerSpy — Final Requirements and Current-State Matrix

Authoritative source: [`problem-statement.md`](problem-statement.md) (the original PS).
Baseline: [`audits/phase-1-current-state-audit.md`](audits/phase-1-current-state-audit.md).
Code paths refer to the cleaned repository layout (`frontend/…`, `archive/hackathon-prototype/…`).

---

## 1. What the PS actually asks for

LedgerSpy is a **forensic audit tool for a company's own books**. A Chartered Accountant runs it on a
**laptop with networking disabled** against a client's **ledger, usually exported from Excel and spanning
several years**. The PS describes the detection target in the story: a **circular trading network
involving ghost vendors** draining funds. The tool must surface this **without labelled fraud data**,
**explain** every flag well enough to defend it, **reconcile against bank statements (including scanned
ones)**, and produce a **professional audit memo locally**.

Five consequences shape the whole design:

1. **The domain is corporate ledgers.** Entities are *vendors, employees, transactions, bank entries*. The
   current dataset is PaySim mobile money (customer → merchant / customer), which is a proxy, not the target domain.
2. **Unsupervised and forensic analytics are the core.** "Highlights unusual transactions without needing
   labeled fraud data" is explicit. A supervised fraud classifier is an *extension*, usable only where labels exist.
3. **Offline is a hard constraint.** The story's stakes are data leakage. Air-gapped operation is a product
   requirement, not a slogan.
4. **Every flag must be defensible.** That requires traceable evidence, reproducible runs and honest explanations.
5. **A "glowing graph" and "beautiful dashboard" are UI expectations.** They are not analytical requirements.
   The existing UI shell already satisfies most of the visual intent.

## 2. Previous audit vs actual PS

| Category | Items |
|---|---|
| **Correctly identified** | Benford / digital frequency; fuzzy vendor matching; anomaly and outlier scatter; bank reconciliation; relational graph; explainability; audit memo; data integrity dashboard; Monte Carlo; industry benchmarking; air-gapped operation |
| **Missed** | Bank statements are **scanned** (needs PDF/OCR ingestion). Anomaly detection must work **without labels**. The graph must include **employees** (employee–vendor links). Integrity must cover **duplicates** and **format mismatches** with **categorised** issues. Fuzzy matching must show a **similarity matrix** with **shared payment patterns** and **per-pair risk**. Graph node size and glow must encode **risk and centrality**. **Ghost vendors** are a named target. Ledgers span **3+ years**. Input arrives as **Excel**. |
| **Misunderstood** | "PS" was not only the About-modal narrative: the PS *is* Piyush's story plus a feature list. **The chatbot is not a PS requirement.** The PS asks for a *memo assistant* (one-click professional summary); the chatbot is now an optional extension. **Trends tab** and **transaction table** are supporting UI, not PS features. A **supervised classifier is not required**; the existing notebook model is an extension. **Auto-cleaning** is not in the PS. The PS says *ensure data is clean before analysis*: inspect, report, then apply explicit, logged normalisation. |
| **More detailed than understood** | Benford: highlight *significant* deviations (needs a statistical test). Explainability: a per-item % breakdown ("65% due to unusual timing + high amount"), so the score must decompose exactly. Reconciliation: three explicit states, green matched / yellow partial / red missing evidence. Graph: "gatekeepers" means centrality. |
| **No longer relevant as framed** | "Brownie points" and judging criteria. "Court-ready" as a marketing claim: replaced by *defensible, traceable, reproducible* output. "Glowing" aesthetics as a requirement. |
| **Real-world extensions** | Case and evidence management, evidence hashing, tamper-evident audit log, reproducible runs. Duplicate-payment detection. Structuring / approval-threshold evasion. Temporal controls (weekend, after-hours, period-end). Vendor-master hygiene (missing GST/PAN/address, bank-account changes). A supervised known-pattern model where labels exist. PDF report file generation. Analyst feedback (confirm/dismiss) on matches and findings. Optional investigation assistant over structured results. |

## 3. Final requirement list

**MUST** = core product. **SHOULD** = important for real use. **COULD** = optional, data-dependent.

| ID | Requirement | Priority | Source |
|---|---|---|---|
| R1 | Fully local / air-gapped operation on a laptop; no ledger data leaves the machine; no network needed at runtime | MUST | PS story |
| R2 | Ingest ledgers from CSV and **XLSX**, multi-year, tens to hundreds of thousands of rows, with column mapping | MUST | PS story ("Excel rows", "entire ledger") |
| R3 | Data Integrity Dashboard: Readiness Score %, categorised issues (nulls, duplicates, format mismatches, …), visualisations | MUST | PS |
| R4 | Explicit, logged cleaning and normalisation (no silent coercion) | MUST | Real-world (implied by R3) |
| R5 | Digital Frequency Profiling: first-digit Benford chart, statistical significance, highlighted deviations, interpretation | MUST | PS |
| R6 | Fuzzy Entity Reconciliation: vendor and employee entity resolution, interactive similarity matrix, shared payment patterns, per-pair risk | MUST | PS |
| R7 | Anomaly & Outlier Detection **without labels**; interactive scatter; clickable outliers with full context | MUST | PS |
| R8 | Bank Statement Reconciliation: ledger vs bank (CSV/XLSX first, then PDF/scanned via local OCR); matched / partial / missing; side-by-side | MUST | PS |
| R9 | Relational Risk Mapping: employee–vendor–transaction graph; cycles (circular trading), fan-in/out, shell-like and ghost vendors; node size = risk, glow = centrality | MUST | PS |
| R10 | Explainable Risk Insights: per flagged item, exact % contribution breakdown plus supporting evidence | MUST | PS |
| R11 | Local Audit Memo Assistant: one-click memo in standard audit tone, generated only from actual findings, copyable | MUST | PS |
| R12 | Unified risk score for transactions and entities, with transparent fusion and bounded false-positive volume | MUST | Needed by R7, R9, R10 |
| R13 | Investigation workflow: filter, search, drill-down, finding status, analyst notes | SHOULD | Real-world |
| R14 | Case and evidence management: cases, evidence hashes, audit trail, reproducible runs | SHOULD | Real-world (defensibility) |
| R15 | Report export as an actual PDF file | SHOULD | Real-world (R11 extension) |
| R16 | Duplicate payments, structuring/threshold, temporal and vendor-master checks | SHOULD | Real-world forensic standard |
| R17 | Supervised known-pattern fraud model where labelled data exists, with honest evaluation | COULD | Extension of existing work |
| R18 | Monte Carlo going-concern simulation (only when cash-flow history is supplied) | COULD | PS brownie |
| R19 | Privacy-preserving industry benchmarking (only with a genuine benchmark source) | COULD | PS brownie |
| R20 | Investigation assistant (chat) over structured results | COULD | Existing work |

## 4. PS → current system matrix

Legend: 🟢 working · 🟡 partial · 🟠 heuristic/approximation · 🔴 mock/static/simulation · ⚫ not implemented

| ID | Status | Files / functions | What works | What doesn't | What must change |
|---|---|---|---|---|---|
| R1 Offline | 🟡 | `frontend/index.html` (Google Fonts `<link>`); notebooks (`!pip install shap`) | Scoring runs in-browser; no ledger upload | External font request; no offline install path; notebook needs internet | Vendor fonts and assets locally; local backend bound to 127.0.0.1; offline wheelhouse; network-guard tests |
| R2 Ingestion | 🟡 | `frontend/js/data-pipeline.js` `fileLoaded`, `parseCsvText`, `splitCsvLine`, `autoMapIdx`; `ml-engine.js` `findMLColIdx` | Simple CSV parsed; header aliases | `.xlsx` accepted but ignored; naive quoting; two inconsistent mappers; partial-match bugs; IDs `TX-???` on own dataset | Python ingestion (CSV and XLSX) with limits, encoding detection, one mapping step confirmed by the user, deterministic IDs |
| R3 Integrity dashboard | 🟡 | `data-pipeline.js` `runDataIntegrityCheck`; `index.html` `#tab-di` | Counts blank/non-numeric amounts and blank vendors; ring and score UI | No duplicate check, no date/format checks per column, arbitrary score (floor 60, ×10), sampled extrapolation | Full rule-based check suite with a defined readiness formula, categorised issues, per-column visuals |
| R4 Cleaning | 🔴 | `#di-clean-btn` → `alert()` | — | Nothing is cleaned; `parseNum` silently coerces bad values to 0 | Explicit transformation steps, logged, reversible, reported |
| R5 Benford | 🟠 / 🔴 | `ml-engine.js` `engineerFeatures` (digitCounts, `benford_flag`); static bars in `#tab-tr`; notebook digit-7/8/9 rule | Real first-digit counts computed in JS | Chart hard-coded ("+128%", "2.28×"); per-row flag from dataset-level deviation; no test, no sample-size rule | `ledgerspy.forensics.benford`: χ², MAD with conformity bands, per-digit z, min-n gating, subgroup analysis, chart from data |
| R6 Fuzzy ER | 🟠 | `ml-engine.js` `stringSimilarity`, `fuzzy_flag`, `self_transfer`; notebook `SequenceMatcher` | Some similarity computed | Compares sender vs its own receiver; bag-of-characters metric; no vendor master; memo pair "Stationery L.T.D" static | ER pipeline (normalise → block → score → cluster) over vendor/employee names and attributes; similarity matrix UI |
| R7 Anomaly / outlier | 🟠 | `ml-engine.js` `predictTransaction`; `ui-core.js` `buildScatter` | Scatter renders, clickable, details panel | No label-free model; hand-tuned sigmoid flags 84%; random jitter and sampling | Isolation Forest + robust peer statistics; scatter from backend results; deterministic layout |
| R8 Bank recon | 🔴 | `data-pipeline.js` `runMockBSR` | Side-by-side layout | `Math.random()` statuses, fake " INC", amount × 0.9; no bank input | Bank ingestion, normalisation, staged matching, assignment, exception findings; OCR later |
| R9 Relational graph | 🔴 / 🟠 | `index.html` `#rel-static-graph`, entity cards; `ml-engine.js` `updateRelationshipsTab`, `circular_flag`, `many_to_one_flag`, `multi_receiver_flag` | Fan-in/out counts computed; top flagged destinations aggregated | Graph is a static pentagon; addresses, directors, "Loop depth 5–6", "PMLA 2002" are static; 1-hop reciprocity only; no employees | networkx graph from resolved entities; temporal cycles, centrality, communities, shell/ghost indicators; Cytoscape view |
| R10 Explainability | 🟡 / 🔴 | `predictTransaction` reasons; `explainPrediction`; `updateExplainabilityTab`; notebook SHAP | Per-row fired-rule reasons | Global bars use a different weight table on averaged features; default bars static; no % breakdown per item; SHAP offline only | Exact contribution decomposition from the risk engine + real SHAP for model components + evidence links |
| R11 Memo assistant | 🔴 | `index.html` `.report-doc`; `updateDashboardWithML` (one paragraph) | One sentence uses real counts | Findings, "87% SEVERE", recommendations hard-coded | Template-based memo from confirmed findings; every sentence cites finding IDs |
| R12 Risk engine | 🟠 | `predictTransaction` (JS); `predict_transaction` (notebook) | Produces scores and tiers | Two incompatible engines; invented weights; label leakage (`isFraud → ≥85`); 84% flagged | One Python risk engine, config-driven, signal fusion with bounded review volume |
| R13 Investigation | 🟢 / 🟡 | `renderT`, `filt`, `smartSearch`, `tgRow`, `ltAddRow`, `ltDownloadCsv` | Filter, search, expand, add row, export | 500-row cap; added rows not scored; no statuses or notes | Server-side paging and filtering; finding statuses and notes |
| R14 Case / evidence | ⚫ | — | — | No persistence, hashes or audit log | SQLite case store, evidence hashing, hash-chained audit log, run manifests |
| R15 PDF report | 🔴 | "Download PDF" → `alert()` | — | No file produced | ReportLab renderer from the report model |
| R16 Additional forensic checks | 🟠 | `threshold_flag`, `round_amount`, `odd_time`, notebook `is_duplicate` | Simple flags exist | Fixed bands (4800–5100); `step % 24` "hours"; duplicates unused | Config-driven duplicates, structuring, temporal, vendor-master checks |
| R17 Supervised model | 🟡 | `archive/hackathon-prototype/notebooks/core_model*.ipynb`, `shap_explainability.ipynb` | LR trains; SHAP plots | SMOTE before split; transductive features; accuracy on 55 rows; constant features; artefacts missing; not connected | Proper pipeline (§ ML strategy in `architecture.md`), model registry, applicability gating |
| R18 Monte Carlo | 🔴 / 🟠 | `#tab-tr` static SVG; notebook `monte_carlo_simulation` | Notebook function runs | Inputs invented; app shows hand-drawn result ("68% insolvency") | Cash-flow input; bootstrap simulation; disabled when no data |
| R19 Benchmarking | 🔴 | `#bench-*` static; notebook `np.random.normal(1.5, 0.5)` | — | Fabricated industry values; "zero-knowledge proofs" claim | Only with a real benchmark source; otherwise hidden |
| R20 Chat assistant | 🟡 | `frontend/js/chatbot-ui.js` `processQuery`; `archive/.../chatbot.py` | Keyword answers over live results | Contradictory thresholds; keyword-order bug | Later: answers from the API over structured results only |

## 5. Misleading claims register

Decision codes: **1** = make it real later (hide or mark as unavailable until then) · **2** = rewrite truthfully · **3** = remove.

| Claim / display | Location | Decision |
|---|---|---|
| "AI-powered", "AI Model Inference Breakdown", "scored by ML engine", "Logistic regression weights — calibrated" | About modal, `#tab-ex`, status text, `ml-engine.js` comment | 2 now ("heuristic preview"), 1 later |
| "SHAP-style feature contribution" | `ml-engine.js` §5 | 3 (replace with exact rule contributions; real SHAP later) |
| "Detection Precision 93.7%" | Overview quick stats | 3 (only show precision measured against labels, with method) |
| "Total Fraud Exposure ₹2,45,00,000", "Vendors Flagged 14 Entities" | Overview quick stats | 3 (compute or hide) |
| Industry Avg 1.2%, "3.4x higher", "SaaS/Tech", "zero-knowledge proofs" | Overview benchmark | 3 now; 1 only if a real benchmark source exists |
| Monte Carlo chart, "10,000 iterations", "68% chance of insolvency by Month 9" | Trends | 3 now; 1 when cash-flow data is supported |
| Benford chart values "+128%", "2.28×", "13.2% vs 5.8%" | Trends | 1 (render from data) |
| "Monthly … FY 2024" axis (actually `step % 24` bins) | Trends | 2 (label honestly) → 1 (real dates) |
| Static entity graph, addresses, directors, "Loop Depth 5–6", "PMLA 2002 classification", "Circular Trading" banner | Relationships | 3 now; 1 via graph module |
| Memo findings (140% digit 7, 94% similarity, 65% at 11 PM–2 AM, "87% SEVERE", "Ring of 5", recommendations) | Insights memo | 3 now; 1 via report generator |
| "Download PDF Report" (alert) | Insights | 3 now; 1 via PDF renderer |
| "Auto-Fix Formatting Issues" (alert) | Data Integrity | 3 now; 1 via cleaning step |
| Bank recon "Launch Recon Engine" (random) | Bank Recon | 3 now; 1 via reconciliation module |
| "Behavioral Anomaly Cluster #4" | Table detail row | 3 |
| Default hero values 142 / 864 / 47,285, "₹2.45 Cr … 6 entities" | Hero panel | 3 (empty state) |
| Default XAI bars 65/20/10/5%, "Risk = 87%" | Explainability | 3 (empty state) |
| "court-ready forensic memos" | About modal, chatbot | 2 ("traceable, evidence-referenced memo") |
| "100% Air-Gapped … no telemetry", "100% Offline operation. System Secured." | About, footer | 2 now ("processes data locally"); 1 after offline packaging and network-guard tests |
| ".xlsx / .csv" upload prompt | Hero | 2 (CSV only) until XLSX ingestion exists |
| README "Explainable AI", "Behavioral analysis" | archived README | Superseded by new README |
