# Investigation Workflow, Cases and Evidence (P15)

| | |
|---|---|
| Code | `ledgerspy/investigations/` (model, refs, service), `ledgerspy/pdfstatements/corrections.py` |
| API | `ledgerspy/api/routes/investigations.py` (+ two PDF correction routes) |
| UI | Investigations tab (`frontend/js/investigations-ui.js`) |
| Schema | migration 8 |
| Acceptance record | [`p15-acceptance.md`](p15-acceptance.md) |

**Terminology** (used consistently in the API and UI):
- **Finding / indicator.** An automated output that is a lead requiring review, never proof of fraud. Examples: a
  P9 review-risk row or entity, a P7/P8 detector signal, a P13 graph cycle or signal, a P14 reconciliation
  finding.
- **Assessment / decision.** An analyst's recorded decision with a factual rationale. It never changes the
  automated output, a P9 score, a detector, a model, a match or source data.
- **Conclusion.** The analyst's documented outcome of a case: `concerns_substantiated`, `no_issue_identified`,
  `inconclusive` or `referred`. No case is forced into a fraud / no-fraud verdict.

## Schema (migration 8)

Migration 8 creates seven tables, with indexes and triggers; it rewrites no existing row (tested from v7).

| Table | Mutability |
|---|---|
| `investigations` | status, texts and priority change (each change is an event); no delete |
| `investigation_links` | immutable (reference, snapshot, `snapshot_sha256`, scope key) |
| `investigation_link_removals`, `investigation_notes`, `investigation_decisions` | append-only |
| `investigation_events` | append-only; `audit_seq` points at the audit-log entry |
| `ocr_corrections` | append-only |

## 1. Cases and scope

An **investigation case** (`IV-…`) lives inside an **engagement case** (`C-…`, the P2 container that owns
datasets, master files and reconciliation runs).
- **Scope:** the engagement case is the investigation's scope. A link may reference only records of that case;
  anything else is refused with `cross_scope_reference`.
- **Fields:** title, factual description, priority (`low` / `medium` / `high` / `critical`), status, and outcome +
  conclusion + closure rationale when supplied, with creation and last-update metadata.
- **Assignment:** there is no investigator assignment. The application is single-user and local: the identity is
  the OS user the server runs as (`app.state.actor`), and no other identity is invented.

## 2. Lifecycle

```
open ──────► in_review ──────► concluded ──────► closed
 │  ▲          │   ▲  │            │               │
 │  └──────────┘   │  └► awaiting_evidence         │
 │                 └──────────────────────────────┘ (re-open: concluded/closed → in_review)
 └──────────────────────────────────────────────────► closed (e.g. opened in error)
```

| From | Allowed to |
|---|---|
| open | in_review, awaiting_evidence, closed |
| in_review | awaiting_evidence, concluded, open |
| awaiting_evidence | in_review |
| concluded | closed, in_review |
| closed | in_review |

**What each transition needs:**
- **Concluding:** an outcome and a conclusion of at least 10 characters.
- **Closing:** a closure rationale of at least 10 characters.
- **Re-opening** a concluded or closed case: a reason of at least 10 characters.

**What is enforced:**
- Any other transition is refused with `invalid_transition` (409), which lists the allowed ones.
- A **closed** case is read-only: links, notes, decisions and edits are refused (`investigation_closed`) until it
  is re-opened.
- Dismissing every linked finding never closes a case. Only an explicit transition does.

## 3. Evidence links (references, never copies)

A link records:
- the type and the dataset, run or record identifiers;
- a small **snapshot** of what was linked, and the **SHA-256 of the full referenced object** at link time;
- the reason, an optional annotation, the actor and the time.

Records are not copied into case tables. On demand (link detail, `verify`) the reference is resolved again:
- `current`: the record is identical;
- `changed`: it differs from what was linked;
- `stale`: a reconciliation run whose inputs changed, e.g. after an OCR correction;
- `missing`: the record no longer exists.

| Type | Reference | Resolved through |
|---|---|---|
| `transaction` | dataset + txn ID (ledger or bank) | canonical row (immutable) |
| `risk_transaction`, `risk_entity`, `signal` | dataset + txn / `E-` / `S-` ID | P9 result (read-only) |
| `entity`, `entity_pair` | dataset + `CE-` / `P-` | P12 resolution |
| `graph_node`, `graph_edge`, `graph_cycle`, `graph_signal` | dataset + `CE-` / `G-` / `C-` / `S-` | P13 graph |
| `reconciliation_run`, `reconciliation_result`, `reconciliation_finding`, `duplicate_group` | `RR-` + `RM-` / `S-` / `RD-` | P14 run register |
| `evidence_file` | SHA-256 | evidence store (must belong to the engagement case) |
| `pdf_page`, `pdf_item` | dataset + page number / `PX-` | P14b extraction |

**Validation:**
- IDs are matched by strict patterns. Free-text transaction IDs exclude control characters and path separators.
- They are looked up as values. There are no file paths, and no SQL is built from input.
- **Errors:** unknown records give 404; malformed identifiers give `invalid_reference`; references from another
  engagement case give `cross_scope_reference`; linking the same record twice gives `duplicate_link` (409).

**Unlinking** writes an append-only removal (reason, actor, time). Only the case link is removed: the referenced
record and the evidence are never touched, and the removed link stays in the history. Re-linking creates a new
link.

## 4. Notes and decisions

**Notes** (≤ 10,000 characters) are append-only, optionally attached to a link, and stored verbatim. The UI
renders them as text (an HTML payload stays text).

**Decisions** are append-only.
- **Contents:** the subject (the case or one link), the action, a factual rationale (required, ≥ 3 characters),
  supporting and contradicting evidence (active links of the same case), a payload, and whether the subject
  remains unresolved.
- **Current assessment:** the latest decision on a subject. Earlier decisions stay.

| Subject kind | Actions |
|---|---|
| any | `reviewed_no_issue`, `further_investigation_required`, `evidence_insufficient` |
| finding (P9 rows / entities, signals, graph cycles / signals, reconciliation findings) | + `finding_under_review`, `finding_confirmed`, `finding_dismissed`, `finding_escalated` → finding status new / under review / confirmed / dismissed / escalated |
| entity / entity pair | + `entity_link_accepted`, `entity_link_rejected` (recorded only: P12 never merges on an analyst decision) |
| duplicate group | + `duplicate_confirmed`, `duplicate_rejected` (shown on the P14 duplicates view; nothing is deleted) |
| reconciliation result | + `match_accepted` / `match_rejected` (only matches), `ambiguity_resolved` (only `ambiguous` results; `payload.chosen` must be one of the result's competing candidates) |
| PDF extraction row | + `ocr_row_confirmed`, `ocr_row_excluded`, `ocr_row_corrected` |

**Where decisions are visible.** Decisions on reconciliation results and duplicate groups appear read-only in the
P14 result detail (`analyst_decisions`) and duplicates page. The computed run, its statuses and its result hash
are unchanged. `ocr_row_confirmed` / `ocr_row_excluded` before confirmation also record the P14b review decision.
After confirmation an exclusion is refused (the canonical row is immutable).

## 5. OCR / extraction corrections

A correction is an explicit, append-only **overlay**:
- `ocr_corrections` holds the field, the **original extracted value**, the corrected value, the rationale, the
  page and source text, the reviewer, the time and, optionally, the investigation.
- The PDF and the stored extraction never change.
- The value must be valid for the field: a date in a configured format, or an amount in the statement's decimal
  convention.

**Before confirmation.** The extracted table handed to the P3 confirmation takes the corrected value.
- The row becomes `reviewer_corrected`, and its `pdf_corrections` provenance keeps original and corrected values.
- A review-required row still needs its decision.
- PDFs extracted before P15 have no `pdf_corrections` column and must be re-extracted first
  (`reextract_required`).

**After confirmation.** The corrected value is written to the canonical field it maps to (date, narration,
reference or amount) as **one logged P4 transformation**: a fix proposal with transform `ocr_correction`, an
accepted decision and an applied transformation.
- **Unchanged:** the canonical row.
- **Changed:** the dataset's normalised view, and its data version with it.
- **Effect on reconciliation:** every recorded reconciliation run that used the statement becomes **stale**.
  - `GET /api/reconciliation/runs` marks it `stale`.
  - `get_run` refuses it, even from cache (`inputs_changed`, 409).
  - Links to its results verify as `stale`.
  - A new run must be started. Matches computed from the uncorrected value are never left valid.
- **Not correctable after confirmation:** balance, value date and Dr/Cr indicator are provenance-only fields.
  Amount corrections must be positive; a change of direction is refused.
- **Reversible:** the correction can be reverted through the existing P4 revert, which is logged.

## 6. Audit semantics

Every write runs in one transaction with:
- an **audit-log entry** (`investigation.*`, `ocr.corrected`) in the hash-chained P2 log;
- an `investigation_events` row pointing at that entry (`audit_seq`).

The activity view shows the events with their audit sequence numbers and hashes. Links, removals, notes, decisions,
events and corrections are protected by append-only SQLite triggers. Only an investigation's own status, texts
and priority change, and each change is an event.

**What `GET /api/audit/verify` proves.** It recomputes the hash chain and (since P20) compares every trigger
in the database with the set the migrations create.
- **Detects:** an edited, inserted, reordered or deleted entry; a missing, altered or extra trigger on any table.
- **Does not detect:**
  - truncation of the newest entries;
  - an edit made while a trigger was dropped and then restored exactly;
  - a rewrite of the whole chain or the tables by someone who controls the database file and the code.
- **Hardening:** record `head_seq` and `head_hash` outside the database and pass them back as `anchor_seq` /
  `anchor_hash`: truncation or a rewrite up to that point is then detected (P20).
- **Not tamper-proof:** the log is tamper-evident against edits through the database alone, not against an
  administrator who can rewrite the file.

**Evidence.** The manifest re-hashes every evidence file behind the case's links. A modified file is reported as
`evidence_tampered`; a missing one as `evidence_missing`.

## 7. API (token-protected; `size` ≤ 500; deterministic order; unknown body fields refused)

| Method | Path |
|---|---|
| POST / GET | `/api/investigations` (create with optional initial links; list by case / status / priority) |
| GET / PATCH | `/api/investigations/{IV-…}` |
| POST | `/api/investigations/{id}/transitions` |
| GET / POST | `/api/investigations/{id}/links`; GET `/links/{LK-…}` (resolved again, `verify`); POST `/links/{LK-…}/unlink` |
| GET / POST | `/api/investigations/{id}/notes`, `/decisions` |
| POST | `/api/investigations/{id}/ocr-corrections` (links the row, corrects, records `ocr_row_corrected`) |
| GET | `/api/investigations/{id}/activity`, `/summary` (deterministic, `summary_sha256`), `/manifest` (`manifest_sha256` + evidence verification) |
| GET | `/api/investigations/vocabulary`, `/api/audit/verify` |
| POST / GET | `/api/datasets/{id}/pdf/items/{PX-…}/corrections`, `/api/datasets/{id}/pdf/corrections` |

## 8. Retention

Nothing in P15 deletes anything:
- Investigations cannot be deleted.
- Unlinking keeps the link and its removal.
- Corrections keep the original values.
- Dismissed findings stay with their reason. Excluding them from reports is P16.

## 9. Limitations

- **Identity:** a single local identity. There is no multi-user assignment, roles, or second-person approval.
- **Ambiguity resolutions and accepted entity links** are recorded assessments. They do not feed back into P14
  matching or P12 merging; that would be a separately validated change.
- **No run comparison.** The roadmap mentions run comparison; it is not built.
- **Rationales are not verified as true.** They must exist and are length-checked, but the system cannot check
  their factual content.
- **Snapshots are small summaries.** Full reproduction relies on the deterministic engines and the evidence;
  `changed` / `stale` / `missing` are reported when that is no longer possible.
- **No fine-grained access control.** Authorization is the session token: anyone with it has full access.
- **Trigger coverage of `verify`.** Since P20 every trigger is checked against the migrations (see
  [`p20-security-acceptance.md`](p20-security-acceptance.md) §4 for what remains undetectable).
