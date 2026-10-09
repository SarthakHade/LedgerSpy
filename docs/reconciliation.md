# Bank Statement Reconciliation (P14)

Ledger ↔ bank statement reconciliation, built on P3 ingestion, P4 normalised views and P12 entity resolution.
Code: `ledgerspy/reconciliation/` (records, engine, duplicates, balances, findings, service);
`ledgerspy/ingest/adapters/bank_statement.py`; API `ledgerspy/api/routes/reconciliation.py`; UI
`frontend/js/recon-ui.js` (Bank Recon tab). Configuration: `reconciliation:` in `config/default.yaml`.
Acceptance record: [`p14-acceptance.md`](p14-acceptance.md).

**Ground rules.**
- Every result is derived from imported rows. Nothing is fabricated: no transactions, balances, currencies or
  outcomes.
- Canonical rows are never modified. Results reference rows by ID and carry their source evidence.
- Reconciliation findings are a separate `reconciliation` evidence family. They do not change P9 scores
  (`weight: null`, `fused_in_p9: false`); integration is a later, separately validated change (P9 / P16).
- Evidence scores rank candidates. They are not probabilities and are not calibrated.
- Different currencies are never compared or added. Rows are never matched across bank accounts.

## 1. Data contract and ingestion

A bank statement is a dataset of kind `bank_statement` (migration 6 extends the `datasets.kind` CHECK; the table
is rebuilt with identical columns, rows and triggers). It is imported with the P3 flow and safety limits:

```
POST /api/cases/{case_id}/files?filename=…&kind=bank_statement     (raw CSV / XLSX body; size / row limits)
PUT  /api/datasets/{dataset_id}/mapping                             (adapter "bank_statement", confirmed `bank`)
```

The `bank_statement` adapter produces the same canonical `Transaction` as every adapter (no second schema):

| Statement value | Canonical field / place |
|---|---|
| Source line ID (if unique) | `txn_id` (else derived `T-…` from evidence hash + row) |
| Row reference | `source_row`, `dataset_id`, evidence SHA-256 |
| Account number | `account` (shown masked: last 4 digits, plus a stable `account_key`) |
| Transaction date | `posted_at` |
| Description / narration | `description` |
| Reference / UTR / cheque no. | `reference` |
| Debit / credit, or signed amount | `amount_minor` (positive, minor units) + `direction` `in` / `out` |
| Currency (explicit only) | `currency` (column, or `options.currency` declared for the file; never inferred) |
| Running balance, statement ID, value date, … | verbatim in `extra`; the balance / statement columns are named in `bank` |

**Direction semantics are confirmed, never assumed** (`Mapping.bank`, account holder's view):
- **Debit/credit column pair, or a debit/credit indicator column.** `debit_means` is `outflow` or `inflow`. It is
  proposed only when the headers state it (Withdrawal / Deposit, Paid out / Paid in). With "Debit" / "Credit"
  headers it must be confirmed.
- **One signed amount column.** `amount_sign` is `positive_inflow` or `positive_outflow`, and must be confirmed.
- **In/out indicators, or `options.direction` `in` / `out`.** Used as they are.

Nothing is reinterpreted:
- A zero signed amount gets no direction (issue `zero_amount_no_direction`).
- A negative value in a directional column leaves amount and direction empty (issue
  `negative_in_directional_column`).
- A missing currency stays missing. The proposal asks for a declaration, and such rows are reported as
  insufficient data.

**Kinds are distinct.**
- A `ledger` dataset cannot use the bank adapter, and a `bank_statement` dataset must.
- A ledger upload that looks like a statement gets a note suggesting `kind=bank_statement`.
- Bank statements are hidden from the ledger analysis picker. Any other kind is refused.

**PDF statements (text or scanned / OCR) are not supported in P14.** They come in **P14b**. The upload error and
the UI say so.

## 2. Scope: accounts, currencies, directions

A run compares rows only inside a **block**: the same scope (one bank account ↔ the ledger rows of that
account), the same currency and the same flow (`in` / `out` of the account).

- **Scopes are confirmed by the analyst.** Each scope is `{account_key, ledger_column, ledger_value}`. The ledger
  filter is a canonical text field or a source column.
- `GET /api/reconciliation/setup` proposes scopes only from evidence. Today that evidence is the case's
  group-company master (P12 import): its bank account matches a statement account, and a ledger column holds its
  code.
  - On P5 it proposes `…6964 ↔ Company Code = LVE` and `…2401 ↔ Company Code = LVC`.
- **Several accounts with no scope** are refused (`scopes_required`).
- **One account with no scope:** every ledger row is assumed to belong to it, and the run states this as a
  limitation.
- **Rows outside every scope** are `not_applicable`. **Rows matching two scopes** are `insufficient_data`
  (`ambiguous_scope`).
- **Ledger debit semantics** (`ledger_debit_means`) are required whenever the ledger uses debit/credit.
  - Setup proposes a value only when one party is the payer on at least `direction_proposal_min_share` (90 %) of
    debit rows and the payee on 90 % of credit rows, in every proposed scope.
  - On P5, the group company pays on 100 % of debit rows, so a debit is an outflow.

## 3. Normalisation (`records.py`)

Deterministic and read-only. Original values sit beside the comparison forms, and the P4 overlay
(`normalized_fields`) is kept.

| Field | Rule |
|---|---|
| Date | Calendar date of `posted_at` (time of day not compared) |
| Amount | `amount_minor` integer, positive; negative → insufficient, zero → not applicable |
| Currency | ISO code as ingested; missing → insufficient (`missing_currency`) |
| Flow | Bank `in` / `out`; ledger debit/credit → in/out only through `ledger_debit_means` |
| Account | Letters and digits, upper-cased; `A-<sha256[:12]>` key; masked label `…1234` |
| Reference | Letters and digits, upper-cased; `shape` = runs of letters/digits with lengths (`SYNR230403000003` → `A4N12`) |
| Counterparty (ledger) | Payee of an outflow, payer of an inflow; identified by its P12 canonical entity |
| Counterparty (bank) | Known parties **named** in the description (see below) |

**Names in descriptions** (`NameIndex`):
- **Known names** are the ledger's payer / payee names. With P12 they also include the master names (employees,
  vendors, customers) under their canonical entity, so aliases count as one party.
- **Normalisation.** Descriptions are split into segments at `/`, `|`, `;` and `\`. Each segment is normalised
  with the P12 name rules (`QUOR-VEN CARTON L.L.P.` → `quorven carton llp`).
- **A name is found** when its core tokens (legal form removed) appear consecutively in one segment.
  - Truncated narrations count too: the segment may end inside the name after at least `min_prefix_chars` (12)
    matching characters, with the cut token a prefix.
- **What is never searched.** Names shorter than `min_name_chars` (6). When one found name lies inside a longer
  found name, only the longer one counts.
- **Counterparty status:**
  - `agree`: the ledger counterparty's entity is named.
  - `conflict`: other known parties are named (not the ledger row's own side) and the counterparty is not.
  - `unknown`: otherwise.
- **Reference status:**
  - `agree`: equal, or one side's reference (≥ `reference.min_chars`) appears in the other's description.
  - `conflict`: two references of the same shape that differ.
  - `unknown`: otherwise.

## 4. Matching (`engine.py`)

All weights, windows and limits are configuration (`reconciliation.*`). Candidates come from indexes: equal amount
by hash map, partial amounts by bisect on sorted amounts, grouping by counterparty and date. There is no
all-pairs comparison.

| Stage | Candidates | Status |
|---|---|---|
| 1 / 2 one-to-one | Equal amount; bank date − ledger date in [−`bank_before_days` (2), +`bank_after_days` (5)]; no reference or counterparty conflict | `exact_match` if reference or counterparty agrees; `probable_match` if the evidence score ≥ `probable_min_score` (0.6) |
| 3 grouped | One line on one side = sum of a **date-contiguous run** of 2…`max_group_size` (6) rows of the same counterparty on the other side, within `window_days` (15) | `grouped_match` (both orientations: one bank line settles several ledger rows, or one ledger row is settled by several bank lines) |
| 4 partial | Amount difference ≤ min(`max_abs_minor`, `max_relative` × larger amount) (5,000.00 / 5 %) and / or a date in the extended window [−7, +31] — both only with an agreeing reference or counterparty; or equal amount and date with a conflicting reference / counterparty | `partial_match` with `discrepancies` ⊆ {amount, date, reference, counterparty}; never counted as reconciled |

- **Evidence score.** The weighted mean of the components available for a pair: amount, date, and reference /
  counterparty when they can be compared. Weights are amount 0.35, date 0.25, reference 0.2, counterparty 0.2.
- **Date component.** `1 − |Δdays| / (window + 1)`, using the window on that side of zero.
- **Partial amount component.** `1 − |Δ| / (tolerance + 1)`.

**Selection and ambiguity.** In stages 1/2 and 4 a pair is accepted only when each row is the other's best
candidate by more than `ambiguity_margin` (0.1). Accepted pairs are removed and selection repeats until nothing
changes.
- Rows left with comparable candidates form one `ambiguous` result per connected group, listing every competing
  pair with its score.
- In stage 3, overlapping candidate groups, or several runs for one settlement, are `ambiguous`.
- The first candidate is never picked.

**Ordering.** Candidates are sorted by stable keys, and a tie never selects. Shuffled input therefore gives
identical results.

**No combinatorial search.** Grouping uses contiguous runs only, never subsets, so splits that are not
contiguous in date are not found.
- At most `max_candidates` (60) rows are considered per settlement, and `max_candidates_per_row` (50) one-to-one
  / partial candidates per row.
- Reaching a limit is reported in `truncation`, and the summary carries a limitation.

**Every row ends in exactly one result.** Status `exact_match`, `probable_match`, `grouped_match`,
`partial_match`, `ambiguous`, `duplicate_candidate`, `unmatched_ledger`, `unmatched_bank`, `insufficient_data`
or `not_applicable`.
- An unmatched row whose clearing window falls outside the dates the other side covers is `insufficient_data`
  (`outside_statement_coverage`), not unmatched.
- An unmatched row that duplicates a matched row is `duplicate_candidate` (§5).

**Result fields:**
- `result_id`, `status`, `stage`.
- `scope`: scope ID, account key, masked account, currency, flow.
- Ledger and bank transaction IDs, with row summaries: date, amount, currency, flow, reference, counterparty /
  description, P4-normalised fields.
- `amount_difference_minor` (bank − ledger) and `date_difference_days` (a min / max range for groups).
- `evidence_score` with `score_components`, `discrepancies`, and `link` (reference / counterparty status and the
  parties named in the description).
- `components` (grouped) or `candidates` (ambiguous), `duplicate_group_ids`, `reasons`.
- `explanation`, `limitations`, and `detector` (name, version).

`…/results/{id}` adds the source evidence:
- Dataset, evidence SHA-256, file name and source row.
- The canonical values and the P4 normalised values.
- The confirmed balance / statement columns of a bank line (no other raw columns).

## 5. Duplicates (`duplicates.py`)

Duplicates are candidates only. Nothing is excluded or deleted, `confirmed` is always `false`, and confirmation
is an analyst decision (P15).

| Side | Type | Rule |
|---|---|---|
| bank | `exact` | Same account, date, amount, direction, description and reference |
| bank | `near` | Same account, amount, direction and counterparty signature (named parties, else description without digit tokens), dates chained within `near_window_days` (3) |
| ledger | `same_reference` | Same counterparty, amount, direction and non-empty reference |
| ledger | `near` | Same counterparty, amount and direction, dates chained within 3 days |

Each group lists its rows, basis and member statuses. An unmatched member of a group whose other member is matched
becomes `duplicate_candidate`. Identical candidates for one row are `ambiguous`, never silently assigned.

## 6. Balances (`balances.py`)

Balances are checked only where the statement supports it. That needs the confirmed running-balance column
(`bank.balance_column`) and in/out directions.
- **Order.** Per account and currency, rows are taken in file order. The order is used only if
  `balance[i] = balance[i−1] ± amount[i]` holds for ≥ `balance.min_order_share` (90 %) of adjacent transitions.
  Newest-first files are recognised in reverse. Otherwise the check is `unavailable`, with the measured shares.
- **What is reported:**
  - The implied opening balance: the first row's balance minus its own movement (derived, never invented).
  - Total movement, the closing balance, and whether opening + movement = closing.
  - Every discontinuity: the two rows, expected vs stated balance, and the difference.
  - Rows without a balance, and dates out of order.
- **Statement periods.** With a confirmed `bank.statement_column`, each period's opening and closing balance is
  reported, together with whether it continues the previous period's closing balance. Without one this is
  `not_applicable`.
- **Unavailable cases.** No balance column, no currency, or fewer than two usable adjacent rows each give
  `unavailable` with the reason.

## 7. Findings (`findings.py`, family `reconciliation`)

| Rule | Subject | Severity |
|---|---|---|
| `recon_unmatched_ledger` | ledger row | medium (outflow) / low |
| `recon_unmatched_bank` | bank line | high (outflow) / medium |
| `recon_amount_mismatch` | partial result | medium |
| `recon_date_discrepancy` | partial result | low |
| `recon_counterparty_mismatch` | partial result | high |
| `recon_reference_conflict` | partial result | medium |
| `recon_duplicate_bank_posting` | bank `exact` group | high |
| `recon_repeated_settlement` | bank `near` group | medium |
| `recon_duplicate_ledger_posting` | ledger group | medium |
| `recon_balance_discontinuity` | balance break | high |

Each finding has the P7 / P13 shape: a deterministic `signal_id` and the ledger / bank transaction IDs. It also
carries the result ID, metrics, evidence, explanation and limitations, with `weight: null` and
`fused_in_p9: false`.

## 8. Runs, determinism and persistence (`service.py`)

**Identity.**
- The run ID is `RR-` plus a hash of the run's identity: the engine version, and both datasets with their IDs, P4
  data versions and mapping hashes.
- It also covers the confirmed scopes and ledger semantics, the reconciliation configuration, the minor-unit
  configuration and the P12 resolution hash.
- The same inputs give the same run ID and the same `result_sha256`.

**Register.** A run is recorded once in `reconciliation_runs` and audit-logged as `reconciliation.run`.
- The table is append-only and holds the parameters, identity, result hash and a brief summary.
- After a restart, results are recomputed from the stored parameters and verified against the hash.
- If the inputs changed since the run (a new P4 overlay, or a different configuration), the API returns
  `inputs_changed` and the old run stays in the register.

**Same case.** Ledger and statement must belong to the same case (`different_cases`), and dataset kinds are
checked (`wrong_dataset_kind`).

**Summary.** Every aggregate is computed from the row-level results, and a test checks it against them:
- Counts by status, and rows per side by status.
- Amounts per side, currency and status. Matched, partial, ambiguous and unmatched amounts are reported per
  currency and never summed across currencies.
- Matched-row share = (exact + probable + grouped rows) / rows that could be reconciled.
- Duplicate groups by type, balance status per account, findings by rule.
- Truncation, limitations and P4 readiness.

## 9. API

All endpoints are token-protected (Bearer), Host / Origin checked, and loopback only. IDs are validated by
pattern, lists are paginated with `size` ≤ `limits.max_page_size` (500), no endpoint accepts a path, and unknown
body fields are refused.

| Method | Path | |
|---|---|---|
| GET | `/api/reconciliation/setup?ledger_dataset_id=&bank_dataset_id=` | Accounts, scope / semantics proposals with basis, what must be confirmed, readiness |
| POST | `/api/reconciliation/runs` | Body `{ledger_dataset_id, bank_dataset_id, scopes[], ledger_debit_means}` → 201 summary |
| GET | `/api/reconciliation/runs?ledger_dataset_id=&bank_dataset_id=` | Recorded runs |
| GET | `/api/reconciliation/runs/{RR-…}` | Summary |
| GET | `/api/reconciliation/runs/{RR-…}/results` | `status`, `account`, `currency`, `from`, `to`, `q`, `page`, `size` |
| GET | `/api/reconciliation/runs/{RR-…}/results/{RM-…}` | Result + source evidence + findings + duplicate groups |
| GET | `/api/reconciliation/runs/{RR-…}/duplicates` | `side`, pages |
| GET | `/api/reconciliation/runs/{RR-…}/balances` | Balance report |
| GET | `/api/reconciliation/runs/{RR-…}/findings` | `rule`, `severity`, pages |

## 10. UI (Bank Recon tab)

The tab shows only what the API returns:
- **Setup.**
  - Choose a ledger, then a bank statement of the same case, or import one as CSV / XLSX.
  - Confirm its direction semantics, and declare a currency only if the statement states it.
  - Confirm or edit scopes (proposal basis shown) and the ledger debit meaning, then run.
- **Results.**
  - Status counters and per-currency amounts, with the matched-row share as defined above.
  - A result table with status / account / currency / date / ID filters.
  - A detail view: explanation, differences, source evidence, components, competing candidates.
  - Balance check, duplicate candidates and findings.
- **Unavailable states** are stated explicitly.
- **PDF / OCR** is labelled "coming in P14b".

## 11. Limitations

- **Narration-based counterparty evidence.** It depends on names appearing in descriptions.
  - A payee mismatch is detected only when the bank names a KNOWN other party (a ledger party or, with P12
    masters, a master record).
  - On P5 without the employee master, the S-S-07 payee mismatch is a probable match: the beneficiary is not a
    known party.
- **References.** References of different kinds (invoice vs UTR) are never compared, and narrations are not
  parsed for bank-specific reference formats.
- **Grouping.** Only date-contiguous runs of one counterparty are grouped. Unnamed or non-contiguous batch
  settlements remain unmatched or partial for review.
- **Identical duplicates.** Identical duplicate payments matched to identical bank lines are `ambiguous` by
  design: the evidence cannot tell which pairing is right.
- **No aging analysis.** "Unreconciled items by age" is not built.
- **No FX.** Different currencies are never matched.
- **Scope proposals** need a group-company master. Otherwise scopes are entered by the analyst.
- **No PDF / OCR** (P14b).
