# P14 — Bank Statement Reconciliation: Acceptance Record

Date: 2026-10-10 · Scope: [`roadmap.md`](roadmap.md) §P14 (R8), [`architecture.md`](architecture.md) §10 ·
Methodology: [`reconciliation.md`](reconciliation.md).

**What P14 adds.**
- **Ingestion.** CSV / XLSX bank statements through the P3 pipeline: dataset kind `bank_statement` and a
  `bank_statement` adapter whose direction semantics must be confirmed.
- **Matching.** A deterministic, staged ledger ↔ bank engine: exact, probable, grouped (both orientations),
  partial (amount / date / reference / counterparty), ambiguous, duplicate candidate, unmatched,
  insufficient-data and not-applicable statuses.
- **Checks and evidence.** Statement-supported balance checks, and reconciliation findings as an independent
  `reconciliation` evidence family.
- **Persistence.** An append-only, audit-logged run register.
- **Interfaces.** Nine token-protected endpoints, and a Bank Recon tab drawn only from API data.

**What it leaves alone.**
- No P9 score changes, and P11 stays isolated.
- Labels and ground truth are read only by tests, to score results.
- PDF / OCR is deferred to P14b and labelled as such.

## Commands and results (final state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # All checks passed
.venv/Scripts/python -m pytest                                       # 1088 passed, 0 failed, 0 skipped (1043 P0–P13 + 45 P14), 18 min 46 s
node tests/frontend/harness.js                                       # 13 passed, 0 failed
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 16 passed, 0 failed (14 earlier + 2 P14)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed
```

| P14 test file | Tests | What it proves |
|---|---|---|
| `test_recon_units.py` | 31 | **Matching:** exact by counterparty and by reference; probable without either; no match outside the window; ambiguous candidates never resolved by order; a clear winner beats a weaker candidate; one bank line → several ledger rows and one ledger row → several bank lines; competing groupings ambiguous; grouping truncation reported; partial amount / date / counterparty with the difference stated; out-of-tolerance or unlinked amounts unmatched; a conflicting reference blocks matching. **Isolation:** no matching across accounts or currencies, and currencies never added. **Data:** insufficient / not-applicable rows kept with reasons; ledger debit semantics applied, not assumed; coverage. **Duplicates:** exact bank duplicate → ambiguous, then `duplicate_candidate` once its twin is matched; ledger duplicates by reference and window. **Integrity:** every row in exactly one result and amount totals reconcile; shuffled input → identical result hash; inputs not mutated; findings unfused. **Names:** truncated and P12-normalised names found, the shorter nested name dropped, short names not searched. **Balances:** a break located with its difference; newest-first order; unavailable cases; statement-period continuity. **Ingestion:** Withdrawal / Deposit proposal; Debit / Credit and signed amounts require confirmation and are applied as confirmed; zero and negative values not reinterpreted; kind / adapter must agree; ledger mapping hash unchanged; PDF → P14b. **Schema:** migration 6 keeps rows, triggers and FK integrity; run register append-only. **Isolation (static):** reconciliation imports no supervised, risk or synthetic-generator code and reads no labels or ground truth; P6–P13 and P9 never import reconciliation |
| `test_recon_datasets.py` | 14 | **P5:** evidence-backed scope / semantics proposals and masked accounts; exact classification counts; all seven S-S scenarios classified; every pairing equals the generator's true ledger → bank pairing (evaluation only); row and summary totals agree per currency; duplicates, balances and findings; determinism (same run ID and hash), recorded and audit-logged once, recomputed and verified after the cache is cleared; canonical tables unchanged; P9 `result_sha256` unchanged. **Validation:** scopes, kinds, cases, semantics; a one-account scope. **PaySim:** no currency → never reconciled. **Scale:** a generated 39,500-row ledger / 37,000-line statement with known counts. **API:** token required on all 9 endpoints (missing and wrong token); run, pages, filters (status / account / window / currency), detail with source evidence, duplicates, balances, findings, runs list; size limit, bad IDs, unknown body fields and wrong kinds refused |
| `browser_check.py` (+2, A11) | 2 | The Bank Recon tab shows exactly the API's status counts for the confirmed scopes; a partial match shows its discrepancy and source rows; PDF / OCR labelled P14b |

**Earlier code and tests updated (necessary only).**
- **DB:** migration 6 rebuilds `datasets` with `kind IN ('ledger', 'bank_statement')` (same columns, rows and
  triggers) and adds `reconciliation_runs`. `init_db` runs migrations with FK enforcement off for the
  connection and requires `PRAGMA foreign_key_check` to be empty before committing (the documented SQLite
  table-rebuild procedure).
- **Ingestion:**
  - `SUPPORTED_KINDS` gains `bank_statement`.
  - `suggest` / `validate_mapping` take the kind.
  - `Mapping` gains an optional `bank`; `mapping_json` omits it when absent, so ledger mapping hashes are
    unchanged.
  - The PDF error names P14b (same `unsupported_format` code).
- **Config:** a `reconciliation` section.
- **API:**
  - The app registers the router.
  - `GET /api/datasets` includes `kind`.
- **UI:**
  - `index.html`: the Bank Recon placeholder is replaced by the live view. The P14b card keeps a labelled
    `.na-card`, and "Bank statement reconciliation" stays in the page.
  - `recon-ui.js` is added.
  - `api.js`: `upload` takes an optional kind.
  - `risk-ui.js`: the ledger picker lists ledgers only.
  - A small CSS block.
- **Tests:**
  - `conftest.Ingest.upload` takes an optional `kind`.
  - `browser_check.py` gains the two A11 checks.
- **Unchanged:** no P0–P13 assertion was modified.

## Acceptance criteria

| # | Criterion | Evidence | Result |
|---|---|---|---|
| 1 | Bank statements via the P3 flow, CSV / XLSX | `kind=bank_statement`, evidence hashing, limits, mapping confirmation; PDF refused with P14b | ✅ |
| 2 | One canonical schema | `bank_statement` adapter → `Transaction`; balance / statement columns verbatim in `extra` | ✅ |
| 3 | Sign conventions confirmed, never assumed | `bank.debit_means` / `amount_sign` required unless headers state it; validation errors otherwise | ✅ |
| 4 | Currencies never invented or summed | Missing → insufficient; blocks per currency; amounts per currency | ✅ |
| 5 | Account scope respected | Confirmed scopes; evidence-backed proposals; no cross-account matches (test) | ✅ |
| 6 | Staged deterministic matching | Exact, probable, grouped (both orientations), partial; config weights / tolerances; indexes, no all-pairs | ✅ |
| 7 | Ambiguity surfaced, never forced | Mutual best by margin; `ambiguous` results list competing candidates; conflicts block matching | ✅ |
| 8 | Duplicates as candidates | Bank exact / near, ledger same-reference / near; `confirmed: false`; nothing excluded | ✅ |
| 9 | Complete, traceable output | Every row in exactly one result; IDs, differences, scores, components, evidence, explanation, limitations, detector | ✅ |
| 10 | Balance checks only where supported | File / reverse order verified; implied opening labelled; breaks; periods; unavailable with reasons | ✅ |
| 11 | Summary reconciles with rows | Test: per-side, per-currency, per-status sums equal | ✅ |
| 12 | Separate evidence family | 10 rules, `fused_in_p9: false`; P9 `result_sha256` unchanged | ✅ |
| 13 | API: token, pagination, validation | 9 endpoints; 401 / 404 / 422 tests; size ≤ 500; no paths | ✅ |
| 14 | UI on real data | A11: counts equal the API; detail shows evidence; P14b labelled | ✅ |
| 15 | Determinism and no mutation | Same run ID and hash; shuffled input identical; canonical digest unchanged | ✅ |
| 16 | Explicit limits | Candidate / grouping limits reported as truncation | ✅ |
| 17 | Full suite passes | 1088 passed, 0 failed, 0 skipped (1043 P0–P13 + 45 P14), 18 min 46 s | ✅ |
| 18 | Frontend checks pass | 13/13 harness, 16/16 browser, 6/6 panel | ✅ |
| 19 | Ruff passes | Clean | ✅ |

## P5 results (synthetic ledger + bank statement + imported masters)

Scopes and semantics as proposed by setup: `…6964 ↔ Company Code = LVE` (785 ledger rows, 784 lines),
`…2401 ↔ Company Code = LVC` (6,486 rows, 6,483 lines), ledger debit = outflow.

| Status | Results | Ledger rows | Bank lines |
|---|---|---|---|
| exact_match | 7,104 | 7,104 | 7,104 |
| probable_match | 144 | 144 | 144 |
| grouped_match | 2 | 6 | 2 |
| partial_match | 8 | 8 | 8 |
| ambiguous | 3 | 6 | 6 |
| unmatched_ledger | 3 | 3 | — |
| unmatched_bank | 3 | — | 3 |
| duplicate_candidate, insufficient_data, not_applicable | 0 | 0 | 0 |
| **total** | | **7,271** | **7,267** |

- **Probable matches.** The 144 probable matches are exactly the payroll bulk uploads and bank charges (72 + 72):
  their narrations name no party.
- **Ambiguous results.** The 3 ambiguous results are pairs of equal duplicate payments to one vendor, cleared on
  adjacent days. The evidence cannot tell which ledger row goes with which line, and every true partner is inside
  its ambiguous group.
- **Precision (evaluation only).** Measured against the generator's own ledger → bank mapping: 7,262 ledger rows
  are paired (exact, probable, grouped or partial) and **0 are paired wrongly**.

| Scenario | Expected | Result |
|---|---|---|
| S-S-01 ledger-only (3) | missing in bank | 3 × `unmatched_ledger` |
| S-S-02 bank-only (3) | unrecorded bank debits | 3 × `unmatched_bank` (2 ACH debits, 1 SMS charge) |
| S-S-03 delayed 8–11 working days (4) | outside normal tolerance | 4 × `partial_match` [date], +10…+13 days |
| S-S-04 batched (3 → 1 debit) | grouped | `grouped_match`, bank settles ledger |
| S-S-05 one deposit for 3 invoices | grouped | `grouped_match` |
| S-S-06 fees (+₹590, −₹236 × 2) | amount difference | 3 × `partial_match` [amount], Δ = +590.00 / −236.00 |
| S-S-07 payee mismatch | different beneficiary | `partial_match` [counterparty]: the narration names the employee, not the vendor |

**Amounts (INR, both sides; never summed across currencies — one currency here):**
- **Matched:** ₹1,492,055,227.00 on each side.
- **Partial:** ledger ₹2,798,945.21, bank ₹2,799,063.21.
- **Ambiguous:** ₹1,013,180.78.
- **Unmatched:** ledger ₹154,740.80, bank ₹99,440.02.
- **Matched-row share:** 7,254 of 7,271 ledger rows and 7,250 of 7,267 bank lines.

**Duplicates.** 8 candidate groups: 3 bank `near`, 2 ledger `near` and 3 ledger `same_reference`. They are the
duplicate-payment scenarios; their member statuses are listed, and nothing is excluded.

**Balances.** Both accounts are `consistent` in file order, with 0 discontinuities.
- `…6964`: implied opening ₹6,000,000.00, movement ₹323,179,510.51, closing ₹329,179,510.51.
- `…2401`: implied opening ₹197,900,000.00, closing ₹2,894,751.16.

**Findings.** 3 unmatched ledger, 3 unmatched bank, 3 amount mismatches, 4 date discrepancies, 1 counterparty
mismatch, 3 repeated settlements and 5 duplicate ledger postings. All are unfused.

**P9.** `result_sha256` is identical before and after reconciliation.

**Without the P12 masters (browser check, A11).**
- Counts: exact 7,104, probable 145, grouped 2, partial 7, ambiguous 3, unmatched 3 / 3.
- S-S-07's beneficiary (an employee) is not a known party without the employee master, so it is a probable
  match.
- This is a documented limitation: payee mismatches are detected only for known parties.

**Timing.** Two runs of P5 with the final code took 2.19 s and 2.18 s on this machine: inputs and P12
resolution 0.74 s, normalisation 0.47–0.49 s, matching 0.62 s, balances and findings 0.32 s.

## PaySim and scale

- **PaySim:** it has no currency and no bank statement of its own. Against a statement in its case, every
  PaySim ledger row is `insufficient_data` (`missing_currency`), with no match of any kind. No setup scope is
  proposed. Reconciliation is not meaningful for PaySim, and the output says so instead of guessing.
- **Generated scale test:** 39,500 ledger rows and 37,000 statement lines, built with known structure:
  35,000 one-to-one with 0–2 day lags, 1,000 grouped settlements of three, 500 fee differences, 1,000
  ledger-only rows and 500 bank-only lines.
  - Results: 35,000 exact, 1,000 grouped, 500 partial and 500 unmatched bank. The 1,000 ledger-only rows are
    unmatched or outside coverage.
  - 0 ambiguous, no truncation, about 7.5 s end to end.
  - Indexing known names by their first two tokens cut this run from about 35 s to 7.5 s.

## Deviations and decisions

- **Assignment.** The roadmap's `linear_sum_assignment` is replaced by mutual-best-by-margin selection.
  An optimal assignment always picks one pairing among equal candidates, which the P14 brief forbids ("do not
  automatically select the first candidate"). Mutual best leaves such cases `ambiguous`.
- **Partial matches.** Partial covers four discrepancy kinds (amount, date, reference, counterparty), and is
  never counted as reconciled. Delayed clearing is a date discrepancy, not a separate status.
- **Coverage.** Unmatched rows whose clearing window falls outside the other side's dates are
  `insufficient_data` (`outside_statement_coverage`), not unmatched: the other side cannot show them.
- **Risk.** "Exceptions feed the risk engine" (roadmap acceptance) is deliberately not done in P14, as the
  P14 brief requires. Findings are a separate family, and P9 / P16 integration needs its own validation.
- **Runs.** Runs are recorded by parameters and result hash, not by storing every result row. Results are
  recomputed deterministically and verified against the hash.
- **Thresholds.** Every tolerance and weight in `reconciliation:` was set before the P5 run and none was changed
  after seeing P5 results. One correctness fix was made after the first P5 run: descriptions are now normalised
  with the P12 name rules. Before it, "QUOR-VEN CARTON L.L.P." did not match the ledger name "Quor-ven Carton
  L.L.P.", and 5 rows of that vendor were probable instead of exact matches. No status of an S-S scenario row
  changed.

## Not implemented in P14 (stated, not claimed)

- **PDF / OCR statements (P14b).** Text PDFs, scanned statements, OCR confidence gating and the roadmap's
  "low-confidence OCR line is never auto-matched" test are not built. PDF uploads are refused with a P14b message.
- **Exceptions feeding the risk engine** (roadmap acceptance). Deliberately not done: the P14 brief requires
  P9 to stay unchanged, and integration is a later, separately validated change.
- **Analyst resolution.** There is no endpoint to accept, reject or re-pair a match, to resolve an ambiguous
  group, or to confirm a duplicate. Results are read-only review items; the workflow is P15.
- **Aging** of unreconciled items, and **FX** matching across currencies.
- **Scope proposals without master data.** Without a group-company master, the analyst enters each scope.
- **Recorded runs after input changes.** Results are not stored row by row. If a dataset's P4 data version or
  the configuration changes, a recorded run can no longer be shown (`inputs_changed`, 409); its register entry
  (parameters, result hash, brief summary) remains.

## Known limitations

See [`reconciliation.md`](reconciliation.md) §11:
- Counterparty evidence depends on narrations naming known parties.
- References of different kinds are not compared.
- Grouping covers contiguous runs only.
- Identical duplicates are left `ambiguous`.
- There is no aging and no FX.
- Scope proposals need a group-company master.
- Without the employee master, S-S-07 (payee is an employee) is a probable match, not a counterparty mismatch.
- Two internal invariants (every row in exactly one result) are `assert` statements, which `python -O`
  would skip. The tests check the same property.
- PDF / OCR is deferred to P14b.
