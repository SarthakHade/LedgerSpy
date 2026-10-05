# P7 — Forensic Rule Engine and Benford Analysis: Acceptance Record

Date: 2026-10-06 · Scope: [`roadmap.md`](roadmap.md) §P7 (R5, R16), [`architecture.md`](architecture.md) §5.4, §6,
§7 · Detector documentation: [`forensics.md`](forensics.md).

P7 adds ten deterministic rules, Benford analysis (first digit, first two, last two, with subgroups) and a dataset
forensic report. It adds no anomaly models (P8), no risk fusion, tiers or UI (P9), and no supervised ML (P10).

## Commands and results (final state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # All checks passed; 133 files already formatted
.venv/Scripts/python -m pytest                                       # 693 passed (456 P1–P5 + 174 P6 + 63 P7)
.venv/Scripts/python -m pytest tests/backend/test_forensics_*.py     # 63 passed (P7)
node tests/frontend/harness.js                                       # 24 passed, 0 failed (P1)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 14 passed, 0 failed (P1)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed (P4 panel)
```

| P7 test file | Tests | What it proves |
|---|---|---|
| `test_forensics_rules.py` | 21 | **Every rule exists with a version and documented inputs.** Hand-crafted cases, one or more per rule, positive and negative: an exact duplicate vs recurring payments vs a simulated clock; typo-style near duplicates vs consecutive invoices vs outside the window; round amounts against the population, with variety and population gates; a split under one threshold vs a single claim, parts far apart, or parts at 80%; one-off vs routine off-hours; a new vendor (peer basis, warm-up, inflows excluded); balance from P4 with artefacts ignored, plus not_applicable / not_available; dormancy with gap and ratio gates; a dense and unusual period-end cluster vs routine and normal-sized ones; a velocity spike vs a steady pace vs no baseline. **Also:** statistical helpers, the signal contract and canonical conversion, disabled rules, shuffled input → identical report |
| `test_forensics_benford.py` | 14 | Benford data conforms (MAD ≤ 0.006 / 0.0012); uniform and deliberately non-Benford data do not (digits named); a narrow range is analysed with a warning; last-two digits; n below minimum → `insufficient_data`; zeros/negatives/missing/below-minimum excluded and counted, with the `absolute` option; `invalid_population` / `not_applicable`; a hand-computed MAD, χ² and z; χ² p-value; determinism and no mutation; subgroup min-n gating and counts; unsupported dimensions are `not_applicable` |
| `test_forensics_datasets.py` | 28 | **Synthetic:** every rule runs (balance not applicable) in under 20 s; each target scenario is visible to its detector (10 rules); look-alike controls stay quiet (7 rules); no control is systematically flagged (< 25% of its rows); period-end cluster = S-K-03; Benford finds S-T-01 and reports gating; S-G-01 recorded as a known miss. **PaySim:** 7 detectors not available, each with its reason; flipped labels → identical signals. **Isolation and integrity:** a static scan for labels/extra/ground truth; no table changes and no network; the same file in another workspace gives the same signals; balance discontinuity end to end over the token-protected API |

P6 regression: 174 P6 tests, including the three P6 features P7 added, their unit tests and the order-independent matrix hash.

## Acceptance criteria

| # | Criterion | Evidence | Result |
|---|---|---|---|
| 1 | All listed rules exist and are backend-driven | `ledgerspy/detectors/rules.py`: A duplicate_exact, B duplicate_near, C round_amount, D structuring, E temporal, F new_vendor_large, G balance_continuity, H dormancy, I period_end, J velocity. Served by `POST /api/datasets/{id}/forensics` | ✅ |
| 2 | Benford first / first-two / last-two exists | `ledgerspy/forensics/benford.py`: MAD + Nigrini bands, per-digit z with continuity correction, χ² + p-value, statuses, warnings; subgroups in `service.benford_section` | ✅ |
| 3 | Minimum-n gating works | `insufficient_data` below `min_n` (tested at 299 / 999). Subgroups below `max(subgroups.min_n, test min_n)` are skipped and counted (synthetic counterparty dimension: 157 of 159 skipped). Every rule counts `insufficient` subjects | ✅ |
| 4 | Signals contain defensible evidence | Each signal has the rows, the P6 feature values used, rule-specific evidence (group members, match types, aggregate, p-values, P4 issue details, cluster statistics) and a plain explanation. Examples below | ✅ |
| 5 | No labels used by detection | Static scan; PaySim labels flipped → identical signals per source row; a synthetic ledger with an added `is_fraud` column (P6 test) changes nothing. Labels are read only by the evaluation code in this record | ✅ |
| 6 | No canonical mutation | `test_forensics_never_writes_and_needs_no_network`: table digests and `total_changes` are unchanged | ✅ |
| 7 | Thresholds are config-driven | `rules.*` and `benford` in `config/default.yaml`, validated by `RulesConfig` / `BenfordConfig`. Tests change window and tolerance through config | ✅ |
| 8 | Results are deterministic | Re-run gives an identical `report_sha256`; shuffled input gives an identical report; a second workspace gives identical signals, rules, Benford and inputs. P6's matrix hash is now order-independent | ✅ |
| 9 | P5 injected scenarios are visible to the appropriate detectors | 15 of 15 target scenarios across 10 detectors (table below). **One honest miss outside the target list:** S-G-01 for `new_vendor_large` (z 2.65–2.96 < 3) | ✅ (with the documented miss) |
| 10 | Controls do not systematically trigger | No look-alike control is flagged by the rule it resembles. Across all rules: S-U-08 has 2/12 rows flagged (the first two quarter-ends, before the routine is established), S-U-14 1/36, every other control 0 | ✅ |
| 11 | PaySim works without label leakage and reports unsupported detectors | 3 rules evaluated, 7 not available with reasons (simulated clock, no references, not a corporate ledger); Benford runs; labels are used only after detection (diagnostic below) | ✅ |
| 12 | Full pytest passes | 693 passed | ✅ |
| 13 | P1 browser tests pass | 24/24 harness, 14/14 browser, plus 6/6 P4 panel | ✅ |
| 14 | Ruff passes | clean | ✅ |
| 15 | No regression to P2–P6 | 456 P2–P5 tests unchanged and passing. P6 changes are additive: three features, a `running_balance` capability, P4 check statuses on `FeatureInput`, and an order-independent matrix hash. The P6 tests were updated for the new inventory and all 174 pass | ✅ |

## Synthetic scenario validation (diagnostic development metrics)

synthetic ledger: 1.91s, report_sha256 d829988dfcb1f697942e3ac611a0ae12f63d3939b45d5b58782ee7773964d80e (re-run identical: True). Ground truth is used **only here**, after detection, to label which rows belong to which scenario.

- *Flagged rows* is the union of `txn_ids` of a rule's signals. For Benford, these are the sample rows of nonconforming subgroups.
- *Precision-like* is "in injected scenarios / flagged rows".
- *Target recall* is the share of the rule's target scenarios with at least one flagged row.

| Rule | Status | Evaluated | Insufficient | Signals | Flagged rows | In injected scenarios | In controls | Other | Target scenarios hit |
|---|---|---|---|---|---|---|---|---|---|
| `balance_continuity` | not_applicable | 0 | 0 | 0 | 0 | 0 (—) | 0 | 0 | 0/0 (—) |
| `benford_subgroup` | analyzed | — | — | 12 | 217 | 45 (S-H-03, S-Q-01, S-R-01, S-T-01) | 9 | 163 | 1/1 (S-T-01) |
| `dormancy` | evaluated | 18 | 9 | 3 | 3 | 1 (S-H-03) | 0 | 2 | 1/1 (S-H-03) |
| `duplicate_exact` | evaluated | 7271 | 0 | 1 | 2 | 2 (S-C-01) | 0 | 0 | 1/1 (S-C-01) |
| `duplicate_near` | evaluated | 7271 | 0 | 3 | 6 | 6 (S-C-02, S-D-01, S-D-02) | 0 | 0 | 3/3 (S-C-02, S-D-01, S-D-02) |
| `new_vendor_large` | evaluated | 174 | 316 | 29 | 29 | 21 (S-B-01, S-G-02, S-H-02, S-M-01, S-N-01, S-P-01, S-X-01) | 1 | 7 | 2/2 (S-X-01, S-P-01) |
| `period_end` | evaluated | 131 | 28 | 1 | 5 | 5 (S-K-03) | 0 | 0 | 1/1 (S-K-03) |
| `round_amount` | evaluated | 136 | 23 | 3 | 59 | 59 (S-B-01, S-B-02, S-F-01, S-K-02, S-Q-01) | 0 | 0 | 1/1 (S-B-01) |
| `structuring` | evaluated | 5360 | 1911 | 6 | 6 | 6 (S-J-01, S-J-02) | 0 | 0 | 2/2 (S-J-01, S-J-02) |
| `temporal` | evaluated | 7271 | 0 | 15 | 15 | 13 (S-B-02, S-K-01, S-K-02, S-K-03, S-Q-01) | 2 | 0 | 2/2 (S-K-01, S-K-03) |
| `velocity` | evaluated | 6152 | 1119 | 17 | 17 | 8 (S-H-01, S-H-03, S-K-01, S-K-03) | 0 | 9 | 1/1 (S-H-01) |

```
controls per rule (look-alikes that must stay quiet):
  duplicate_exact: S-U-05 0/3, S-U-13 0/2
  duplicate_near: S-U-05 0/3, S-U-13 0/2
  new_vendor_large: S-U-04 0/58
  period_end: S-U-08 0/12
  round_amount: S-U-01 0/36, S-U-02 0/72
  structuring: S-U-07 0/3
  velocity: S-U-06 0/46, S-U-12 0/72
controls, share of rows flagged by any row-level rule:
  S-M-02 employee_vendor_relationship: 0/20
  S-N-04 shared_attributes: 0/154
  S-U-01 legit_round_numbers: 0/36
  S-U-02 legit_round_numbers: 0/72
  S-U-04 legit_new_vendor: 0/58
  S-U-05 legit_large_payment_installments: 0/3
  S-U-06 legit_seasonal_velocity: 0/46
  S-U-07 legit_near_threshold: 0/3
  S-U-08 legit_period_end_overtime: 2/12
  S-U-09 legit_multi_party_chain: 0/6
  S-U-10 legit_reciprocal_flows: 0/37
  S-U-12 legit_batch_payments: 0/72
  S-U-13 legit_near_duplicate: 0/2
  S-U-14 legit_sequential_invoices: 1/36
```

**How to read the table:**
- **Clean rules.** Duplicates, structuring, round amounts and the period-end rule have no hits outside scenarios. Temporal hits only scenarios plus the first two occurrences of the routine-overtime control.
- **Velocity.** 9 of its 17 hits are ordinary counterparties whose 30-day burst is improbable under their own Poisson pace (p ≤ 10⁻⁴). These are legitimate review leads in generated data, reported as they are.
- **New vendor + large amount.** 7 of 29 hits are ordinary, infrequent vendors paid well above their account peers.
- **Benford account subgroups.** Most GL accounts deviate from Benford, because each mixes a few vendors with narrow log-normal amount ranges. Its "other" rows are the example rows of those subgroups, sampling leads and not findings. Counterparty subgroups find S-T-01 (and S-R-01).
- **Not tuned to the benchmark.** Thresholds were fixed from definitions and statistics before these numbers, not tuned to maximise them. During development the diagnostics exposed definitional flaws; each was fixed on principle and is recorded in [`forensics.md`](forensics.md):
  - "just below a threshold" = within 5%;
  - a 90-day warm-up before calling a vendor new;
  - Poisson significance with a multiple-testing-aware p for velocity;
  - near-duplicate tolerance 0;
  - Benford `invalid_population` only for degenerate data, with a narrow range as a warning.

```
Benford (synthetic):
  dataset first: analyzed n=7271 MAD=0.007497 band=acceptable chi2=51.7255 p=0.0 excluded={} warnings=[]
  dataset first_two: analyzed n=7271 MAD=0.001605 band=acceptable chi2=225.7147 p=0.0 excluded={} warnings=[]
  dataset last_two: analyzed n=7271 MAD=0.001211 band=None chi2=851.9404 p=0.0 excluded={} warnings=[]
  counterparty: groups 159, analysed 2, skipped (n < 300) 157 (6565 rows)
     out:solvane housekeeping & co: n=324 MAD=0.1878 nonconformity over=[7, 8, 9]
     out:zoya kamath: n=382 MAD=0.103452 nonconformity over=[1, 8, 9]
  account: groups 23, analysed 10, skipped (n < 300) 13 (1140 rows)
     5500: n=641 MAD=0.092462 nonconformity over=[7, 8, 9]
     5110: n=513 MAD=0.056267 nonconformity over=[3, 4, 5]
     6100: n=973 MAD=0.054312 nonconformity over=[1]
     6110: n=306 MAD=0.05011 nonconformity over=[1, 2]
     5100: n=796 MAD=0.045159 nonconformity over=[1, 2]
     4000: n=814 MAD=0.042716 nonconformity over=[3, 4, 5]
     4010: n=319 MAD=0.042634 nonconformity over=[1, 7, 8]
     5200: n=1017 MAD=0.040707 nonconformity over=[2, 3]
     5600: n=405 MAD=0.030636 nonconformity over=[6, 7, 9]
     5510: n=347 MAD=0.027097 nonconformity over=[3, 4]
  txn_type: not_applicable (no transaction type is mapped)
  fiscal_year: groups 3, analysed 3, skipped (n < 300) 0 (0 rows)
     FY2023-24: n=2311 MAD=0.010731 acceptable over=[2]
     FY2024-25: n=2467 MAD=0.008165 acceptable over=[3]
     FY2025-26: n=2493 MAD=0.007556 acceptable over=[]
```

```
example signals:
  [duplicate_near] transaction LVC-JV-001498: Payment of 368,266.71 (ref GMP6/00195A) to the same payee 1.1 days after LVC-JV-001496 (368,266.71, ref GMP6/00195): amount equal, reference suffix letter. Check whether the same invoice was paid twice.
  [structuring] transaction LVC-JV-002678: 3 payments to the same counterparty within 7 days, each between 95% and 100% of the expense_claim limit 5,000.00, together 14,830.00. Check whether one purchase was split to stay under the approval limit.
  [period_end] group out:trelane paper co pvt ltd@2025-03-31: 5 postings to this counterparty in the last 3 day(s) before the fiscal year end 2025-03-31 (about 0.19 expected), at 3.7× its median amount. Check cut-off: were these real period expenses?
  [round_amount] entity out:halvorn facility services & co: 18 of 18 amounts to this counterparty are multiples of 1,000 (100%) across 9 different values, against 3.0% for all out payments (binomial p = 3.8e-28). Fabricated amounts are often round; review the supporting invoices.
  [velocity] transaction LVC-JV-001867: 6 earlier transactions with this counterparty in the last 30 days (20,944.32), 6.5× its usual pace (about 0.9 expected; Poisson p = 5.3e-05). Check what drove the burst.
  [benford_subgroup] group benford:first:account=4000: first-digit distribution of account '4000' (n = 814) departs from Benford's law: MAD 0.0427 (nonconformity); digits 3, 4, 5 occur more often than expected. A deviation is a reason to sample these rows, not evidence of fraud.
```

## PaySim validation (compatibility / proxy)

```
== PaySim: 1.41s {"rules": 10, "evaluated": 3, "not_available": 7, "not_applicable": 0, "disabled": 0, "signals": 808, "signals_by_rule": {"balance_continuity": 805, "benford_subgroup": 3}, "affected_transactions": 859, "affected_entities": 0, "affected_groups": 3, "benford_subgroup_signals": 3}
  balance_continuity: evaluated evaluated=10127 insufficient=0 signals=805 
  dormancy: not_available evaluated=0 insufficient=0 signals=0 feature cp_days_since_prev is not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  duplicate_exact: evaluated evaluated=10120 insufficient=7 signals=0 
  duplicate_near: not_available evaluated=0 insufficient=0 signals=0 time basis is 'simulation_step': steps of a simulation are not calendar time
  new_vendor_large: not_available evaluated=0 insufficient=0 signals=0 time basis is 'simulation_step': steps of a simulation are not calendar time
  period_end: not_available evaluated=0 insufficient=0 signals=0 time basis is 'simulation_step': steps of a simulation are not calendar time
  round_amount: evaluated evaluated=186 insufficient=6780 signals=0 
  structuring: not_available evaluated=0 insufficient=0 signals=0 feature threshold_proximity is not_available: approval thresholds do not apply to this adapter (not a corporate ledger)
  temporal: not_available evaluated=0 insufficient=0 signals=0 feature is_off_hours is not_available: posting timestamps have no real time of day (time basis 'simulation_step')
  velocity: not_available evaluated=0 insufficient=0 signals=0 feature cp_velocity_ratio is not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  Benford first: analyzed n=10118 MAD=0.00393 band=close excluded={'below_minimum': 7, 'missing': 2}
  Benford first_two: analyzed n=10118 MAD=0.000912 band=close excluded={'below_minimum': 7, 'missing': 2}
  Benford last_two: analyzed n=10067 MAD=0.000797 band=None excluded={'below_minimum': 58, 'missing': 2}
  counterparty: groups 6967, analysed 0, skipped 6967: 
  account: not_applicable (the account field is not a GL/category code for this adapter)
  txn_type: groups 5, analysed 5, skipped 0: DEBIT n=346 MAD=0.026321 nonconformity; CASH_IN n=1953 MAD=0.018908 nonconformity; CASH_OUT n=1336 MAD=0.017726 nonconformity; PAYMENT n=5533 MAD=0.008209 acceptable; TRANSFER n=946 MAD=0.006169 acceptable
  fiscal_year: not_applicable (time basis is 'simulation_step': steps of a simulation are not calendar time)
  label diagnostic (evaluation only): base isFraud rate 0.0067 over 10125 labelled rows
    balance_continuity: 805 flagged rows, isFraud rate 0.0012
    benford_subgroup: 60 flagged rows, isFraud rate 0.0167
```

How to read the PaySim results:
- **Unavailable detectors.** All are reported with the P6 reason: `simulation_step` is never calendar time, PaySim has no references and no approval policy, and its account field is the sender. `new_vendor_large` is unavailable because its warm-up gate needs real time.
- **Balance discontinuities.** These are simulator output. The 805 `other_mismatch` rows have a *lower* fraud-label rate (0.12%) than the base rate (0.67%): PaySim's fraud rows tend to be the documented zeroed-balance artefacts, which the rule ignores by design. This is reported as observed; labels never feed detection.
- **Benford.** First/first-two digits conform overall (MAD 0.0039 close). DEBIT, CASH_IN and CASH_OUT subgroups deviate.

## Deviations from architecture.md / roadmap.md

1. **File layout.** Rules are in `ledgerspy/detectors/rules.py` and Benford/duplicates in `ledgerspy/forensics/`, as the roadmap's file list says. Signals and the report are in `forensics/models.py` and `forensics/service.py`; there is no separate `signals.py`, because the signal type lives in `models.py`.
2. **P6 extended, not bypassed.** `is_off_hours`, `cp_prior_offhours_months` and `dq_balance_discontinuity` were added to the P6 registry with versions, metadata and unit tests, plus a `running_balance` capability (from P4's check status). P6's matrix hash became order-independent: the values are the same, only the hash definition changed, and it is documented.
3. **Canonical model.** `SubjectType.GROUP` was added for Benford subgroups and period-end clusters.
4. **Roadmap acceptance: "the injected round-amount vendor is detected in the subgroup analysis".** Not by Benford: that vendor has 18 rows, below any defensible Benford minimum (300). It is detected by the `round_amount` rule (binomial p = 3.8e-28). Benford detects the digit-subgroup scenario S-T-01 instead.
5. **Strength and weight are placeholders** (binary strength, configured weight), because P9 owns calibration. Severity is configured per rule.
6. **Dataset-level Benford results are reported but do not create signals.** Only subgroup deviations do, at low severity.
7. **No persistence.** Reports are computed on request; P9 decides what to store. The report's `inputs` and `report_sha256` make each one reproducible.

## Known limitations

See [`forensics.md`](forensics.md) §Limitations. In brief:
- **Names, not resolved entities.** Rules see normalised names, not resolved entities (P12).
- **Retrospective rules.** `period_end` and `round_amount` are retrospective (`post_period`).
- **Routine-recurring fraud.** Off-hours work that recurs is treated as routine after two months.
- **Gates exclude brand-new bursts.** New counterparties have no velocity baseline, and LVE's new vendors have no account peer group.
- **Benford subgroups need n ≥ 300.**
- **One threshold per amount.** Structuring cannot tell which approval limit governs a row.
- **Known miss.** S-G-01 is missed by `new_vendor_large`.
