# LedgerSpy User Guide

For the auditor or analyst who operates LedgerSpy. It describes the application as built (version 0.2.0).
Installation: [`installation.md`](installation.md). Operations and troubleshooting: [`operations.md`](operations.md).
Security and limitations: [`security.md`](security.md).

> **Indicators, not conclusions.** LedgerSpy's scores, flags, matches and findings are machine-generated
> **indicators that require review**. Nothing LedgerSpy computes is an audit conclusion or a finding of fraud.
> Only what *you* record (decisions, notes and an investigation's conclusion) is an analyst assessment. Reports
> keep the two apart and label each claim with its origin.

## 1. Before you start

- **Data handling.** Work on the auditor's laptop. Keep the workspace on an encrypted disk in your user profile,
  never on a synced or shared folder (§11).
- **Starting.** Start LedgerSpy with `ledgerspy.cmd` (installation §3). It prints
  `Open: http://127.0.0.1:8765/?token=…` and opens the browser.
- **The link.** The link is valid for this run only; a new token is issued at every start. If the page says
  *LedgerSpy is locked*, open the full `Open:` link from the LedgerSpy window again.
- **Opening the file directly.** If `frontend/index.html` is opened as a plain file, the page computes nothing.
  It says the local service is required.

The page has a status line, a dataset picker with **Analyse dataset**, and these tabs:
- **Overview**
- **Data Integrity**
- **Findings**
- **Bank Recon**
- **Trends**
- **Anomalies**
- **Relationships**
- **Explainability**
- **Investigations**
- **Reports**

Below the tabs are the transaction table, a tier filter and a search box.

## 2. Import a ledger

1. **Choose the file.** Use the upload area (*Import a .csv / .xlsx ledger*):
   - **Formats:** CSV (encoding and delimiter are detected) or XLSX.
   - **XLSX:** cached values only; formulas, macros and external links are never executed.
   - **Limits:** 100 MiB and 1,000,000 data rows (`ingest` in `config/default.yaml`).
   - **Header:** the header must be the first non-blank row.
2. **What happens on upload.** The file is stored unchanged as read-only **evidence** with its SHA-256. Each
   import creates a new engagement case named `Import <file name>`.
3. **Confirm the column mapping.** The *Confirm column mapping* card shows the proposed mapping:
   - **Proposed mapping:** canonical field → source column, and the adapter (generic ledger or PaySim).
   - **Label columns** (e.g. `isFraud`) are stored separately and never used for analysis.
   - **Unmapped columns** are kept verbatim but not analysed.
   - **Required confirmations:** for example the direction of every row when the file has no debit / credit
     column.

   Click **Confirm mapping and analyse**, or **Cancel**. Once confirmed, a dataset's mapping is final: to map it
   differently, import the file again.
4. **What follows.** LedgerSpy runs the integrity check and then the analysis.

Problems are reported with a clear reason and nothing is half-imported. Examples: a malformed file, a size or row
limit, or an ambiguous delimiter.

**Master data (vendor, employee, customer and group-company files).** These are imported with a command, not in
the browser (installation §4). The Relationships tab's *Entity Similarity Matrix* card shows the case ID the
command needs. Without masters, entities are resolved from ledger names only.

## 3. Check data quality (Data Integrity tab)

- **Data Readiness** is reported **separately from risk**. A poor readiness score never raises a risk score.
- **Backend Integrity Report** (click **Run integrity check** to re-run it) shows:
  - the readiness score and its decomposition;
  - categorised issues with severities: missing values, invalid values, unparseable dates, exact and near
    duplicates, negative or zero amounts, out-of-period dates, balance continuity, unmapped columns and more;
  - the transaction and source row of every issue.

  A check that cannot run on your data says `not_available` and why.
- **Fixes.** Proposed fixes are applied only after you **Accept** them (or **Reject**; **Accept all …** is
  offered for safe fixes). Then click **✨ Auto-Fix: apply …**.
  - Each fix is a logged, reversible transformation; **Revert** undoes it.
  - The evidence file never changes.
  - Some fixes ask you to choose. For example, `05/04/2024` could be 5 April or 4 May.
- **What is never fixed automatically.** Rows that are duplicated or missing required values are not deleted or
  invented. Treat them in an investigation (§8).
- **Ledger rows marked out of period.** Out-of-period dates are judged only when the case has a period. Cases
  created by the UI import have none, so that check reports `not_available`.

## 4. Run and read the analysis

Choose the dataset and click **Analyse dataset**. The local service computes these steps, then every tab shows
the result:
1. features;
2. forensic rules and Benford;
3. anomaly detection;
4. risk fusion.

**The review-risk score (0–100)** is a **review priority, not a probability of fraud**, and it is not calibrated.
- **Each detector family contributes points.** Correlated evidence is capped, so one family cannot dominate.
- **Tiers need corroboration** (`risk.tiers` in the configuration):

  | Tier | Rule |
  |---|---|
  | medium | score ≥ 25 |
  | high | score ≥ 50 from at least 2 independent sources |
  | critical | score ≥ 75 from at least 3 independent sources |

  One strong detector alone cannot make a row *high*. Each row states its tier reason.
- **Insufficient data.** A row no detector could assess is `insufficient_data` and has no score. That means "not
  assessed", not "low risk".
- **Review-volume guard.** If more than 2% of scored rows are high or critical (critical: 0.5%), a calibration
  warning is shown. Scores and tiers are never altered to hide it. The review queue holds the first 250 rows of
  medium or above; rows beyond it are marked `over_capacity`.

**Where to look:**

| Tab | What it shows |
|---|---|
| Overview | Tier counts, risk distribution and summary figures from the API |
| Transaction table | Rows in review order. Filter by tier and search by transaction ID, payer, payee or reference. Expand a row for its **exact contribution breakdown** (the contributions add up to the score), the evidence behind each signal, its limitations and **Open investigation from this finding** |
| Findings | Detector status and coverage, dataset-level findings (counterparty and subgroup evidence) and the Benford results ("investigative lead, never proof") |
| Trends | Monthly tier counts (real posting dates only), the Benford first-digit chart (with sample size, MAD conformity band and χ² p-value) and top counterparties by exposure. The Monte Carlo card says **Not available yet** (no cash-flow simulation is built) |
| Anomalies | Score versus amount scatter (click a point for its explanation) and the label-free anomaly detectors. "Unusual" is relative to this dataset, never "fraudulent" |
| Explainability | How the score is built: families, caps, tiers and how often each family leads |

**Detectors that cannot run** are listed with the reason, never simulated. On PaySim, for example, corporate
fields are missing. The structuring rule and the holiday feature need approval limits and a holiday calendar.
These come only from a configuration file passed with `--config` ([`configuration.md`](configuration.md)); the UI has no
setting for them.

## 5. Entities and the relationship graph (Relationships tab)

- **Relationship Graph** ("evidence for review, not proof"):
  - nodes are resolved entities and edges are payments or shared identifiers;
  - node size is the payment exposure, opacity is the PageRank percentile, colour is the entity's risk tier;
  - select a node, edge or **cycle** to see the transactions behind it;
  - employee–vendor evidence and ghost-vendor *verification leads* are listed below the graph, never conclusions.

  A cycle has a status. `review` means time-ordered with value retained. Other statuses explain why it is weaker.
- **Entity Similarity Matrix.** It loads when the tab is opened.
  - **Matrix.** A cell is the name similarity of a candidate pair, coloured by its status. A blank cell means "not
    compared", not zero similarity.
  - **Filters.** Filter by status (**Apply**) and page through pairs.
  - **Pair detail.** Click a cell or row for the evidence, conflicts and shared payment patterns.
  - **Evidence score.** It measures identity evidence, not fraud probability.

  | Pair status | Meaning |
  |---|---|
  | confirmed match | Merged automatically on explicit evidence |
  | probable match, ambiguous | Review: never merged |
  | conflicting identity | Identity evidence contradicts itself, e.g. a look-alike vendor or a shared account |
  | shared attribute | Different entities share contact details, an address or an account |

- **Counterparty Risk** lists counterparties in review order with their identity status.

To record that two records are (or are not) the same entity, use an investigation decision (§8). This records your
assessment; it does **not** merge entities in the engine.

## 6. Reconcile a bank statement (Bank Recon tab)

1. **Choose the ledger**, then click **Import bank statement (CSV / XLSX / PDF)**. The statement joins the
   ledger's case.
2. **CSV / XLSX statements:** confirm the statement mapping, then click **Confirm statement mapping**. You must
   state, never have assumed:
   - what a debit means, or which amount sign means money in;
   - the date format or decimal separator when they are ambiguous;
   - optionally, the currency printed on the statement.
3. **PDF statements:** review the extraction first (§7), then **Continue to the statement mapping**.
4. **Scope.** Choose which ledger rows belong to each bank account (a ledger column and value, or all rows) and
   what a ledger debit means. Then click **Run reconciliation**. Rows are compared only within a scope, a currency
   and a direction.

| Status | Meaning |
|---|---|
| exact / probable / grouped match | Paired. A grouped match is one-to-many in either direction |
| partial match | Paired but with a discrepancy in amount, date, reference or counterparty. Never counted as reconciled |
| ambiguous | Several equally good candidates. **None is selected automatically**; the candidates are listed |
| duplicate candidate | Possible duplicate rows |
| unmatched ledger / unmatched bank | No counterpart found |
| insufficient data | Cannot be assessed: e.g. outside the other side's date coverage, or the row matches two scopes |
| not applicable | The row is outside every confirmed scope, so it was not compared |

- **Every row appears exactly once.** The **evidence score** ranks candidates; it is not a probability.
- **Amounts** are shown per currency and are never converted or added across currencies. Matched or unmatched
  amounts are not losses.
- **Balances, duplicates and findings** (balance checks only where the statement supports them, duplicate
  groups and reconciliation findings) form a separate evidence family. They do **not** change risk scores.
- **Recording a decision.** Click a result for its source rows and competing candidates, then **Open
  investigation from this finding** to resolve an ambiguity or accept a match. The computed result is never
  changed.
- **Stale runs.** Each run is recorded with its parameters and a result hash. If the ledger or statement data
  later changes (an accepted fix, an OCR correction), the run becomes **stale**: only its recorded summary can be
  shown, and reports label it superseded. Run the reconciliation again.

## 7. Review PDF / OCR statements

**Extraction.** After a PDF upload, the local service extracts each page in a time-limited background process:
- **Text pages** are read with PDFium.
- **Scanned pages** are read with the local Tesseract OCR, if installed. The Bank Recon tab says whether OCR is
  available. Without it, scanned pages are reported as *not processed*.
- **Large files** take a while.

**The *PDF extraction review* panel shows:**
- **Per page:** the method, characters, image coverage, OCR mean confidence, number of lines and notes.
- **Every line:** its kind, method, source text, extracted fields and review reasons.
- **Original page:** **Page n** draws the page image with the line outlined.
- **Statement checks:** whether the opening and closing balances on the PDF match.

**Review-required lines.** Every OCR line and every uncertain line needs a decision:
- For a transaction, click **Confirm** or **Exclude**.
- For anything else, click **Not a transaction**.

**Continue to the statement mapping** works only when the extraction is complete and every review-required line
is decided. **An unconfirmed extracted line is never matched.** **Re-run extraction** repeats the extraction.

**Correcting a misread value** (e.g. `UTROOO1` for `UTR0001`): link the PDF line to an investigation and use
**Record correction** (§8).
- The original extracted value is kept; the correction is an overlay.
- After the statement is confirmed, a correction becomes a logged transformation and makes dependent
  reconciliation runs stale.
- Balance, value-date and Dr/Cr fields cannot be corrected after confirmation; re-extract or exclude instead.

**Limits:**
- **Layouts:** only column tables with a recognisable header line are parsed. Other layouts give
  `layout_not_recognised` lines, never guessed rows.
- **Untested:** real bank layouts, non-English OCR and handwriting have not been tested.
- **Encrypted PDFs** are refused.

## 8. Investigations (Investigations tab)

An investigation belongs to one engagement case and may reference only that case's datasets, runs and evidence.

- **Create.** Click **New investigation**, or **Open investigation from this finding** in the transaction table or
  a reconciliation result. Enter a factual title, a description and a priority, then click **Create**.
- **Lifecycle.** **Valid next status** offers only allowed moves (`→ …`):

  | From | Can move to |
  |---|---|
  | open | in review, awaiting evidence, closed |
  | in review | awaiting evidence, concluded, open |
  | awaiting evidence | in review |
  | concluded | closed, in review |
  | closed | in review |

  - **Conclude** needs an outcome (*concerns substantiated*, *no issue identified*, *inconclusive* or
    *referred*) and a factual conclusion.
  - **Close** and **reopen** need a reason.
  - Nothing closes automatically.
- **Link records.** Use **Link** with a type, the IDs and a reason, or the buttons in other tabs.
  - **What a link stores.** A link is a reference with a snapshot hash, not a copy.
  - **Checking a link.** **Details** re-checks it: *current*, *changed*, *stale* or *missing*.
  - **Removing a link.** **Unlink** records a removal; the record itself is untouched.
- **Decide.** **Decide** (on a link) and **Decision on the case** record an analyst decision with a rationale
  (**Record decision**).
  - **Examples:** finding confirmed, dismissed, escalated or under review; reviewed with no issue; entity link
    accepted or rejected; duplicate confirmed; a reconciliation match accepted; an ambiguity resolved by choosing a
    listed candidate.
  - **The finding's status follows the latest decision:** New, Under review, Confirmed, Dismissed or Escalated.
  - **Automated outputs never change.** Scores, detectors, matches and source data stay as computed.
- **Notes.** **Add note** appends a note.
- **Append-only history.** Notes, decisions, links and removals cannot be edited or deleted; every action is in
  the hash-chained audit log under your local Windows identity.
- **Evidence manifest** lists the evidence files with their SHA-256 and their verification now.

## 9. Reports (Reports tab)

Choose the engagement case and the report type:

| Report | Built from |
|---|---|
| Case investigation report | One investigation |
| Bank reconciliation report | One reconciliation run. A stale run shows only its recorded summary, labelled superseded |
| Audit memo (draft) | Up to 20 investigations of the case |
| Evidence manifest | Up to 20 investigations |

- **Preview and store.** **Preview** builds the report from the current records without storing it.
  **Generate and store** saves a new version; earlier versions are never overwritten.
- **Stored reports.** They are listed with their content SHA-256:
  - **Open** shows a stored report;
  - **Verify** re-checks its hashes;
  - **JSON / HTML / MD** download it.
- **Formats.** JSON, self-contained escaped HTML and editable Markdown. **No PDF or DOCX file is produced.** To get
  a PDF, open the HTML report and print it to PDF in the browser; that PDF is not hashed or registered by
  LedgerSpy.
- **What reports contain.** Each claim shows its origin:
  - *automated*: an engine's indicator, a lead requiring review;
  - *analyst*: a recorded decision, note or conclusion;
  - *computed*: a count or total from the listed records.

  Each claim also shows its records and evidence hashes and its traceability: `traceable`, `unverified` or
  `no_reference`. *Traceable* means the claim has a reference; it does not mean the claim is true.
- **Totals.** Totals are per dataset and currency, each transaction counted once.
- **Flagged sources.** Stale runs, changed evidence and unconfirmed OCR values are flagged and never presented as
  current facts.
- **Dismissed findings** are left out of the memo's material findings and listed separately, with the decision.
- **The audit memo is always a draft.** There is **no approval or sign-off workflow**, and nothing in LedgerSpy
  marks a memo as approved. Review and edit the Markdown version under your firm's own procedures, and sign off
  outside LedgerSpy.

## 10. Interpreting what you see

| You see | It means | It does not mean |
|---|---|---|
| A high or critical tier | At least 2 (3) independent detector families agree, with this exact breakdown | Fraud, or a probability of fraud |
| `insufficient_data`, "not available", "unavailable" | The data cannot support that check, with the reason given | Low risk or a clean result |
| A Benford deviation | The digit distribution differs significantly, for a sample large enough to test | Fabricated amounts |
| A conflicting identity or shared attribute | Identity evidence to verify | Collusion |
| A ghost-vendor lead | Indicators to verify against master data and documents | A fictitious vendor |
| An ambiguous reconciliation result | Equally good candidates, none chosen | A match |
| A partial match | Paired with a stated discrepancy | Reconciled |
| A stale run or report section | The inputs changed after the run | Current results |
| A confirmed finding | An analyst substantiated the indicator | A fraud finding |
| Evidence score, similarity percentage, OCR confidence | Ranking and quality measures | Probabilities |

## 11. Handling evidence and sensitive data

- LedgerSpy processes everything locally and needs no network. Keep the laptop's networking disabled during an
  engagement if your policy requires it. The application cannot enforce that, but it never makes outbound
  connections.
- **The workspace** (`%LOCALAPPDATA%\LedgerSpy\workspace` by default) contains client data:
  - the database;
  - read-only copies of every imported file;
  - reports and backups.

  Protect it with full-disk encryption and your user account. **Never** place it in a synced, shared or network
  folder.
- **Treat the `Open:` link as a password for this session.** Anyone holding it can use LedgerSpy while it runs.
  - Do not paste it into chats or tickets.
  - Close the LedgerSpy window when you stop working; the token then becomes useless.
- **Copies of client data.** Downloaded reports and exports of case material are client data too; store them
  under the same controls.
- **Do not edit or delete files inside the workspace by hand.** Evidence and records are append-only by design.
  Back up the whole folder ([`operations.md`](operations.md) §5).
- **Demo data.** `SYNTHETIC_*` demo files are fictional, and the `paysim_sample_10k.csv` sample in the source
  repository is a public simulation. Neither is client data.
