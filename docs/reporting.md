# Audit Reporting and Evidence-Backed Memos (P16)

| | |
|---|---|
| Code | `ledgerspy/reports/` (model, build, render, store) |
| API | `ledgerspy/api/routes/reports.py` |
| UI | Reports tab (`frontend/js/reports-ui.js`) |
| Schema | migration 9 (`reports` register) |
| Acceptance record | [`p16-acceptance.md`](p16-acceptance.md) |

**What a report is.** A report is a read-only view of stored records, assembled by deterministic code and
templates. It never computes an analysis of its own: it reads the P15 investigation records, the P14 run
register (and the run's own row-level results), P14b extraction rows, and the P2 evidence store. It never
changes a case, finding, score, match, extraction or evidence file. No language model is involved, local or
remote.

**Terminology** (the same as P15):
- **Finding / indicator:** an automated output and a lead requiring review, never proof of fraud.
- **Assessment:** an analyst's recorded decision with its rationale.
- **Conclusion:** only what an analyst recorded for an investigation; a report never draws one.
- **Review-risk score:** P9's 0–100 review priority. It is not a probability and not an amount.
- **Gross value:** the sum of listed amounts per dataset and currency. It is not a loss and not a confirmed
  exposure.

## 1. Report types

| Type | Subject | Contents |
|---|---|---|
| `case_investigation` | one investigation | Engagement and investigation metadata, scope (datasets with evidence verification, reconciliation runs with stale status), automated findings, linked records, analyst assessments (with supporting **and** contradicting evidence), analyst notes, open questions, conclusion (only if recorded), values of linked transactions, removed links, data quality of the references, activity history with audit hashes, limitations |
| `bank_reconciliation` | one reconciliation run | Run identity and sources, then for a **current** run: results and rows by status, account × currency × status totals, matched / partial / ambiguous / unmatched values per side and currency, exceptions with row-level detail and analyst decisions, duplicate candidates, balance check, extraction (OCR) provenance, limitations, recommendations. A **stale** run shows only its recorded summary, labelled superseded |
| `audit_memo` | 1–20 investigations of one engagement case | Draft memo in fixed sections (§4) plus the full case reports as appendix |
| `evidence_manifest` | 1–20 investigations of one engagement case | Every reference (active and removed) with verification state and source location, every evidence file behind them with SHA-256, original names, size and verification |

## 2. Claims and traceability

Every material statement is a **claim**. Each claim records:
- `claim_id`, a deterministic hash of the claim's subject;
- `claim_type`: `automated_finding`, `linked_record`, `reconciliation_result`, `extraction_value`,
  `analyst_decision`, `analyst_note` or `case_conclusion`;
- `origin`: `automated` (an engine's output), `analyst` (recorded in the workflow) or `computed` (counts and
  totals made by the report);
- `references`: each linked record with link ID, type, dataset / run / record IDs, the snapshot hash taken at
  link time, the snapshot then and the record now, its **verification** (`current` / `changed` / `stale` /
  `missing` / `unavailable`), its **source locations** (file, source row, PDF page and extraction row, review
  state), and the **evidence files** behind it with their SHA-256 and re-verification result;
- `contradicting`: for decisions, the contradicting links recorded by the analyst;
- `limitations`.

**Traceability** is one of:
- `traceable`: every reference resolves to the current record and its evidence file verifies. This shows where
  a claim comes from, **not that it is true**.
- `unverified`: a reference is stale, changed, missing or unavailable, or its evidence does not verify. The
  memo excludes such findings from its material findings and lists them under limitations.
- `no_reference`: the claim has no linked record (e.g. a case-level decision); it rests on the analyst's
  rationale alone.

Supporting and contradicting evidence are always both shown; nothing is suppressed.

## 3. Source-of-truth and aggregation rules

| Figure | Source | Rule |
|---|---|---|
| Linked transaction values | `transactions` (canonical rows) + `normalized_values` | The P4-normalised value is used where a logged transformation applies (the override and its transformation ID are shown beside the original). Distinct `(dataset, transaction)` pairs only: a transaction linked as a transaction **and** as a P9 risk row counts once. Totals per dataset and currency; datasets and currencies are never added together. |
| Reconciliation counts and totals | the run's row-level results (`get_run`) | Every bank and ledger row belongs to exactly one result (the engine asserts it), so grouped matches count each row once. The report rebuilds account × currency × status totals from the rows and **checks them against the run's own per-currency totals**; a mismatch refuses the report (`totals_mismatch`). |
| Matched / unmatched / partial values | the run summary | Labelled as amounts the matching could or could not pair: not losses. |
| Risk figures | P9 result (re-resolved) | Score and tier as stored, always with "review priority, not a probability". |
| Extraction values | P14b extraction rows | Shown with their review state. A value whose row is not confirmed by a reviewer is labelled "not a confirmed financial fact" and never enters a total. |
| Balances | the run's balance check | Opening / closing balances are reported only where the statement supports a balance check; otherwise they are stated as unavailable, with the reason. |
| Memo counts and totals | the case reports | Collected in the memo's `facts` block (see §4). |

## 4. Memo composition

The memo is composed from the case reports with fixed templates. Its sections are:
1. engagement and investigation scope;
2. executive summary;
3. methodology and data sources;
4. material findings and evidence;
5. amounts and transaction counts;
6. counterparty and relationship indicators;
7. reconciliation exceptions (exceptions first; matched results linked for context are listed after them);
8. supporting and contradictory evidence;
9. limitations, assumptions and open questions;
10. analyst decisions and conclusion;
11. recommended next steps, clearly labelled as recommendations.

**Rules.**
- **Material findings** are automated findings that are `traceable` and not dismissed by the analyst. They are
  ordered confirmed → escalated → under review → new.
- **Dismissed findings** are listed separately, with the analyst's decision, never silently dropped.
- **Unverified findings** are excluded from the material findings and listed under limitations.
- **Recommendations** are generated from the case state: open questions, unreviewed findings, stale or changed
  records, missing conclusions, investigations awaiting evidence. They are suggestions, not facts.
- **Number cross-check.** Every number in the memo's narrative must occur in its report model: the case
  reports, the engagement, or the memo's `facts` block (counts and totals it derived from those case reports).
  Generation fails with `memo_number_unsupported` otherwise. Identifiers such as `P9`, `IV-…` or a SHA-256 are
  not numbers for this check. The templates themselves contain no figures (tested).
- **Draft only.** The memo's status is always `draft` and its approval `not_reviewed`. LedgerSpy has no
  report-approval workflow, so no endpoint approves or signs off a memo, and none is implied.

## 5. Stale and unconfirmed data

- **Stale reconciliation run** (its ledger or bank data changed after it was recorded, e.g. by an OCR
  correction):
  - the reconciliation report shows `run_state: stale`, only the summary recorded at the time (labelled
    superseded) and a recommendation to re-run; no current totals, exceptions or results;
  - references to the run's results verify as `stale`, their claims are `unverified`, and the memo recommends a
    new run.
- **Unreproducible run** (the configuration changed): `run_state: not_reproducible`, handled the same way.
- **Unconfirmed OCR / extraction row:** `extraction_value` claims carry the review state; unconfirmed values are
  never totals and are listed under data quality.

## 6. Output formats and artefact safety

| Format | Purpose |
|---|---|
| JSON | The complete record: `{envelope, content}`. Canonical source for every other rendering |
| HTML | Self-contained, printable page. Every value escaped with `html.escape`; a CSP meta tag allows no script, network, images, forms or base URL |
| Markdown | Editable text of the memo (or headline sections of other reports). HTML-significant characters become entities and Markdown control characters are escaped, so hostile text stays text |

**JSON schema (format version 1).**
- **Top level:** exactly `{envelope, content}`.
- **`envelope`:** `report_id`, `report_type`, `version`, `status` (`generated`, or `draft` for memos),
  `subject_key`, `case_id`, `params`, `generated_at`, `generated_by`, `previous_report_id`,
  `same_content_as_previous`, `content_sha256`, `schema_version`, `format_version`, `config_sha256`.
- **`content`, every type:** `report_type`, `title`, `format_version`, `schema_version`, `config_sha256`,
  `provenance` (`case_id`, `investigation_ids`, `run_ids`, `dataset_ids`, `link_ids`, `evidence_sha256`) and
  `content_sha256`. The last is the SHA-256 of the canonical JSON of the content without that key.
- **`content`, per type:**
  - `case_investigation`: `engagement`, `investigation`, `scope`, `automated_findings`, `linked_records`,
    `analyst_assessments`, `analyst_notes`, `unresolved`, `conclusion`, `conclusion_status`, `linked_values`,
    `removed_links`, `data_quality`, `activity`, `limitations`, `terminology`.
  - `bank_reconciliation`: `engagement`, `run`, `sources`, `run_state`, `run_state_reason`, `terminology`.
    - **Current run:** also `scopes`, `status_counts`, `totals`, `exceptions`, `duplicate_candidates`,
      `balance_check`, `extraction`, `findings`, `truncation`, `readiness`, `limitations`, `recommendations`.
    - **Stale or not reproducible:** instead `recorded_summary_superseded`, `results: null`, `limitations`,
      `recommendations`.
  - `audit_memo`: `status`, `approval`, `engagement`, `investigation_ids`, `facts`, `sections` (each `id`,
    `title`, `paragraphs`, and optionally `items` and `findings`), `case_reports`, `numbers_check`.
  - `evidence_manifest`: `engagement`, `investigations`, `references`, `evidence_files`, `counts`, `note`.
- **Claims:** at least `claim_id`, `claim_type`, `origin`, `text`, `references`, `traceability`,
  `limitations`.
- **References:** `link_id`, `link_type`, `dataset_id`, `run_id`, `ref_id`, `active`, `snapshot_sha256`,
  `snapshot_at_link`, `current`, `verification`, `verification_reason`, `locations`, `evidence` (each
  `{sha256, verification}`).

**Deferred formats.** PDF and DOCX are **not built**: no PDF or DOCX library is installed, and P16 does not add
dependencies (the roadmap's ReportLab is deferred). The HTML rendering prints to PDF from a browser. No CSV
export exists, so formula injection does not arise; the P2 export rule applies if one is added.

**Storage.**
- **Location:** artefacts are written to `<workspace>/reports/<report_id>.<json|html|md>`. Names come from the
  generated report ID only, so no caller text reaches a path.
- **No overwrite:** files are created exclusively (an existing file is never replaced) and made read-only.
- **Register:** each set is recorded in the append-only `reports` table with its SHA-256 and size per format,
  plus one audit entry (`report.generated`). The table refuses UPDATE and DELETE by trigger.
- **Versioning:** regenerating creates version n + 1 with a new report ID, `previous_report_id` and
  `same_content_as_previous`; earlier versions stay.
- **Reads:** every read re-hashes the file: `report_tampered` or `report_missing` (409) if it no longer matches.
- **Size bound:** a rendering over 20 MB is refused (`report_too_large`, 413) with nothing written. Section
  listings are bounded at 500 rows; totals always cover every row, and truncation is stated.

**Provenance recorded with each report:**
- generation time and user;
- schema version, report format version and configuration SHA-256;
- the selected investigation IDs or run ID;
- the content's case, investigation, run, dataset and link IDs, and every evidence SHA-256.

**Determinism.** The content excludes the generation time and lists everything in a fixed order, so identical
source state and configuration give the identical `content_sha256`. The envelope (ID, version, time) differs
per generation.

## 7. API (token-protected; unknown body fields refused; IDs validated by pattern)

| Method | Path | |
|---|---|---|
| GET | `/api/reports/vocabulary` | Types, formats, deferred formats, origins, traceability, limits, `approval_supported: false` |
| POST | `/api/reports/preview` | Content only, nothing stored |
| POST | `/api/reports` | Generate and store a new version (201) |
| GET | `/api/reports?case_id&report_type&page&size` | Stored reports, newest first (`size` ≤ 500) |
| GET | `/api/reports/{RP-…}` | Envelope + content (hash-checked) |
| GET | `/api/reports/{RP-…}/verify` | Re-hash every format |
| GET | `/api/reports/{RP-…}/download?format=json\|html\|md` | Attachment with a safe file name and `X-Content-SHA256` |

The evidence manifest is the `evidence_manifest` report type; P15's `/api/investigations/{id}/manifest`
remains as it was.

## 8. Ground truth and synthetic data

Report code reads no `labels` table, ground-truth file or scenario metadata. A test runs every report type
under a SQLite authorizer that denies any read of `labels` and any write outside `reports` and `audit_log`, and
a source scan forbids those names in `ledgerspy/reports/`. Datasets flagged synthetic at import are labelled
"flagged synthetic" in the memo's data sources.

**When nothing is declared** (generic ledgers and bank statements do not declare their origin; only the PaySim
adapter does):
- **JSON:** the dataset's `synthetic` field is `null`.
- **HTML:** the scope table shows "not declared".

Before P24 such datasets were reported as `false` / "no", an assertion the system cannot make.

## 9. Limitations

- **Approval:** none. Memos stay drafts; there is no reviewer sign-off workflow.
- **Formats:** no PDF or DOCX output (see §6).
- **Narrative:** the memo is template-based. It reproduces analyst text verbatim and does not paraphrase or
  judge it, and it cannot verify that a rationale is true.
- **Values:** linked-transaction values cover transactions linked directly (transaction and P9 risk-row links).
  Values inside linked reconciliation results are reported in the reconciliation context, not added to them.
- **Matching inputs:** reconciliation reports describe one run; there is no comparison between runs.
- **Truncation:** listings stop at 500 rows per section (stated); a report with more exceptions should be read
  in the Bank Recon tab or the JSON export.
- **Stale runs:** a stale run's recorded summary is shown for the record; its row-level results are not
  reconstructed.
