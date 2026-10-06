# P9 — Risk Engine v1, Explainability v1 and the First End-to-End Slice: Acceptance Record

Date: 2026-10-06 · Scope: [`roadmap.md`](roadmap.md) §P9 (R10, R12, R7 UI), [`architecture.md`](architecture.md) §5.4,
§5.5 · Design: [`risk-engine.md`](risk-engine.md).

P9 fuses P7 rule signals and P8 anomaly signals into one backend review-risk score, with:
- family and group caps;
- tiers that need corroboration;
- a review-volume guard;
- entity risk;
- exact explanations.

It also rewires the existing UI to render only API results, and deletes the in-browser heuristic scorer. It adds no
supervised ML or probability calibration (P10/P11), no entity resolution (P12), and no reconciliation, case
workflow or memo (P14–P16).

## Commands and results (final state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # All checks passed; 162 files already formatted
.venv/Scripts/python -m pytest                                       # 891 passed (758 P1–P8 + 133 P9)
node tests/frontend/harness.js                                       # 13 passed, 0 failed (rewritten for the API UI)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 12 passed, 0 failed (live `ledgerspy serve`)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed (P4 panel)
```

| P9 test file | Tests | What it proves |
|---|---|---|
| `test_risk_fusion.py` | 69 | **Formula:** single-signal score; noisy-OR across sources; contributions reconcile exactly (raw and displayed hundredths, for every group, family and signal, including every subset of 7 signals); bounded below 100 under extreme evidence; largest-remainder rounding. **Caps:** a family cap holds however many signals; diminishing returns inside a family; correlated P8 families together stay at 45 (one source, never high); one family cannot overwhelm others; data quality is computed but never scored; overlapping or unscored-with-cap configuration is rejected. **Monotonicity:** across 40 random cases, adding a signal never lowers the score and removing one never raises it; strengthening (doubling) a signal never lowers it, in every family; capped families stay capped; no evidence → 0; order independence. **Tiers / guard:** high needs ≥ 2 sources and critical ≥ 3; a large benign payment is not critical; a mis-weighted configuration raises the warning without changing scores or tiers, with queue and overflow counted; deterministic ranking and tie-breaks |
| `test_risk_explain.py` | 14 | A planted duplicate of an outlier is review rank 1 at high/critical with ≥ 2 sources. Contributors are the real signals (evidence, features, sentence, cited rows, strength × weight identical). Displayed contributions equal the breakdown and sum to the score. Top contributors are the largest. Limitations name every unavailable detector, and nothing mentions labels, scenarios or "AI detected". Canonical `Score` / `Explanation` validate. Removing the duplicate removes its contributor. Quiet rows say so. Labels / extra columns and row order change nothing. Entity score = noisy-OR of its top transactions, counted once, contributions reconcile. All-disabled detectors → `insufficient_data` rows with no score. Summary numbers recompute from rows. Families and key findings are ordered by contribution |
| `test_risk_datasets.py` | 33 | **Synthetic:** every row scored within budget; scenario coverage equals the recorded baseline (28 injected scenarios, 11/12 P8 targets); 52 of 58 high rows lie in injected scenarios; 12 look-alike controls never reach high (S-U-08 and S-U-14 have one high row each, documented); documented control exceptions recorded; every high row has ≥ 2 sources, ≥ 2 families, reconciling contributions and evidence; anomaly-only rows ≤ 45; entities aggregate and rank. **PaySim:** no high/critical (no rule can corroborate); 7 unavailable detectors with reasons; ~780 data-quality-only rows score 0; flipped labels → identical scores. **Isolation:** static scan; no writes and no network; recomputation is bit-identical; the cache follows config and P4 run; the guard fires on a mis-weighted config. **API:** token required on all 8 endpoints; summary = list totals per tier and deterministic; complete, disjoint, priority-ordered pagination and validation; detail = list row with reconciling contributions; 404s; entity, detector and scatter endpoints |
| `test_frontend_static.py` | 17 | **Guard against client-side scoring.** No legacy engine; no `Math.random`, sigmoid, heuristic weights, `riskScore`, thresholds, `Math.exp`, label columns; no score compared with a constant and no tier assigned in JS; no hard-coded percentages or amounts in the page; innerHTML only for escaped chatbot markup; chatbot escapes every API value; no external URLs; unavailable modules labelled; risk read from the API only. A deliberate `if(score >= 70)` + `Math.random()` injection makes it fail |

Updated earlier tests (strictly necessary compatibility):
- `test_models.py`: the tier literal `review` became `medium`.
- `test_features_registry.py`: the legacy JS engine is now asserted *deleted* rather than present.
- `test_api_security.py`: the served-asset list follows the current `js/` files. The inline-handler minimum
  dropped from 20 to 10, because new controls use listeners instead of `onclick=`, which is stricter.
- `test_ingest_paysim.py`: the backend label list still covers the P1 browser list, which is now a constant in the
  test because the browser no longer handles label columns at all.
- The Node harness and browser check were rewritten for the API UI.

## Acceptance criteria

| # | Criterion | Evidence | Result |
|---|---|---|---|
| 1 | P7 + P8 signals feed one backend risk engine | `ledgerspy/risk/service.analyze`: one P6 build → P7 report + P8 report → fusion. An unknown detector is a config error | ✅ |
| 2 | Deterministic, bounded score | 0 ≤ S < 100 by construction; bounded under 30 signals per family at e = 1; recomputation is bit-identical; same `result_sha256` in another call | ✅ |
| 3 | Family caps exist | `risk.families.*.cap` plus the `risk.groups.anomaly.cap` group cap; tests above | ✅ |
| 4 | Review-volume guard exists | `risk.review`; `calibration.review`: budget, warning, queue, overflow; raw scores and tiers unchanged | ✅ |
| 5 | Entity risk aggregation exists | `risk/entities.py`: top-3 noisy-OR, each transaction once, exact contributions, exposure, P6 entity features | ✅ |
| 6 | Traceable contributions | Signal → family → group → score, exact and displayed-exact (largest remainder); canonical `Score` validates | ✅ |
| 7 | Explanations from real evidence | Contributors carry the signal's own evidence, features, cited rows and sentence; tested for identity | ✅ |
| 8 | Monotonicity tests pass | 40 random add/remove cases, strengthening in 9 families, caps, no-evidence and order tests | ✅ |
| 9 | No labels / ground truth in scoring | Static scan; PaySim label flip → identical per source row; label-like extra columns → identical hash | ✅ |
| 10 | Backend-generated dataset summary | `GET …/risk`: every number computed from rows (tested) | ✅ |
| 11 | Transaction table uses backend data | Server-side paging, tier filter, search and sort; the browser check compares table IDs with the API page | ✅ |
| 12 | Transaction detail shows real explanations | The browser check compares the detail's headline, contributions and limitation with the API | ✅ |
| 13 | No active client-side scoring | `ml-engine.js` and `data-pipeline.js` deleted; static guard; harness A2 | ✅ |
| 14 | No fabricated P9 dashboard values | Hero, donut, amounts, histogram, monthly, Benford, entities, explainability, findings summary all from the API; static check for hard-coded % / ₹ | ✅ |
| 15 | Unavailable functionality labelled | Recon, Monte Carlo, benchmark, graph, PDF; unsupported detectors with reasons; monthly series without calendar time; plain-file mode | ✅ |
| 16 | Consistent, deterministic API | One cached analysis per identity; `result_sha256` on every response; summary = list per tier; detail = list row | ✅ |
| 17 | Security intact | Token on every risk endpoint (401 tested); Host/Origin/CSP middleware unchanged; no CSP violations and only loopback requests in the browser check; text-only rendering | ✅ |
| 18 | Full pytest passes | 891 passed | ✅ |
| 19 | P1 Node / browser checks pass | 13/13 harness, 12/12 browser (rewritten for the API UI) | ✅ |
| 20 | P4 integrity panel passes | 6/6 | ✅ |
| 21 | Ruff passes | Clean | ✅ |
| 22 | P5/P8 synthetic validation documented | Below | ✅ |
| 23 | PaySim validation documented honestly | Below | ✅ |

## Synthetic validation (diagnostic; ground truth read only here)

```
== synthetic ledger: 7271 rows; full analysis (P6+P7+P8+P9) 2.65s; cached repeat 18 ms; result_sha256 dbc3b1cd125af8e91296846230e08e711867d77c7d776010a5c2ed5ff5f657c9 (recomputed identical: True)
counts: {"transactions": 7271, "scored": 7271, "insufficient_data": 0, "with_signals": 739, "tiers": {"low": 7035, "medium": 178, "high": 56, "critical": 2}, "medium_or_higher": 236, "high_or_critical": 58, "detectors_unavailable": 1, "detectors_total": 14}
volume guard: {"scored_rows": 7271, "raw_tier_counts": {"low": 7035, "medium": 178, "high": 56, "critical": 2}, "high_or_critical": 58, "high_or_critical_share": 0.007977, "high_budget": 145, "critical_budget": 36, "max_high_share": 0.02, "max_critical_share": 0.005, "exceeded_high_budget": false, "exceeded_critical_budget": false, "calibration_warning": false, "message": "58 high/critical rows are within the review budget of 145", "review_queue_size": 236, "review_queue_capacity": 250, "review_queue_min_score": 25.45, "over_capacity": 0}
score distribution: [6683, 344, 13, 54, 104, 42, 9, 22, 0, 0]
entities: {"total": 159, "medium_or_higher": 80, "high_or_critical": 36, "with_high_or_critical_transactions": 17}
families (signals / transactions raised / transactions led):
  duplicate               4     4     4  cap 0.6 group duplicate
  transaction_pattern    55    55    53  cap 0.6 group transaction_pattern
  temporal               16    15    15  cap 0.4 group temporal
  entity_evidence         3    59    42  cap 0.3 group entity_evidence
  benford                12   217   217  cap 0.15 group benford
  statistical_anomaly    67    67    49  cap 0.4 group anomaly (group cap 0.45)
  peer_anomaly          123   123   102  cap 0.4 group anomaly (group cap 0.45)
  tail_anomaly          354   354   257  cap 0.2 group anomaly (group cap 0.45)
  data_quality            0     0     0  cap 0.0 group data_quality
unavailable detectors: [{"detector": "balance_continuity", "status": "not_applicable", "reason": "P4 balance_continuity: no running-balance column in the source"}]

| Scenario | Type | Rows | Medium+ | High+ | In review queue | Leading family of its flagged rows |
|---|---|---|---|---|---|---|
| S-A-01 | amount_anomaly | 1 | 1 | 0 | 1 | peer_anomaly 1 |
| S-A-02 | amount_anomaly | 1 | 1 | 0 | 1 | peer_anomaly 1 |
| S-B-01 | round_number | 18 | 18 | 15 | 18 | peer_anomaly 12, entity_evidence 3 |
| S-B-02 | round_number | 35 | 35 | 2 | 35 | entity_evidence 33, temporal 2 |
| S-C-01 | duplicate_transaction | 2 | 1 | 0 | 1 | duplicate 1 |
| S-C-02 | duplicate_transaction | 2 | 1 | 0 | 1 | duplicate 1 |
| S-D-01 | near_duplicate | 2 | 1 | 0 | 1 | duplicate 1 |
| S-D-02 | near_duplicate | 2 | 1 | 0 | 1 | duplicate 1 |
| S-E-01 | lookalike_vendor | 5 | 0 | 0 | 0 | — |
| S-E-02 | lookalike_vendor | 5 | 0 | 0 | 0 | — |
| S-F-01 | ghost_vendor | 6 | 6 | 0 | 6 | entity_evidence 6 |
| S-G-01 | sudden_vendor_appearance | 3 | 1 | 0 | 1 | statistical_anomaly 1 |
| S-G-02 | sudden_vendor_appearance | 6 | 5 | 2 | 5 | transaction_pattern 3, peer_anomaly 2 |
| S-H-01 | velocity_spike | 9 | 5 | 3 | 5 | transaction_pattern 4, peer_anomaly 1 |
| S-H-02 | velocity_spike | 6 | 5 | 2 | 5 | transaction_pattern 3, peer_anomaly 2 |
| S-H-03 | dormant_vendor_reactivation | 17 | 3 | 3 | 3 | transaction_pattern 3 |
| S-I-01 | first_time_high_value | 1 | 1 | 0 | 1 | peer_anomaly 1 |
| S-J-01 | threshold_splitting | 3 | 3 | 3 | 3 | transaction_pattern 3 |
| S-J-02 | threshold_splitting | 3 | 3 | 0 | 3 | transaction_pattern 3 |
| S-K-01 | temporal_anomaly | 6 | 6 | 4 | 6 | temporal 6 |
| S-K-02 | temporal_anomaly | 10 | 10 | 2 | 10 | entity_evidence 8, temporal 2 |
| S-K-03 | period_end_clustering | 5 | 5 | 5 | 5 | temporal 5 |
| S-L-01 | sequence_pattern | 4 | 0 | 0 | 0 | — |
| S-M-01 | employee_vendor_relationship | 7 | 1 | 1 | 1 | transaction_pattern 1 |
| S-N-01 | shared_attributes | 4 | 3 | 2 | 3 | transaction_pattern 2, peer_anomaly 1 |
| S-O-01 | circular_trading | 4 | 1 | 0 | 1 | statistical_anomaly 1 |
| S-O-02 | circular_trading | 4 | 2 | 0 | 2 | statistical_anomaly 2 |
| S-P-01 | concentration_fan_in | 12 | 12 | 12 | 12 | transaction_pattern 12 |
| S-P-02 | concentration_fan_out | 8 | 7 | 0 | 7 | statistical_anomaly 7 |
| S-Q-01 | gradual_risk_escalation | 63 | 35 | 2 | 35 | entity_evidence 33, temporal 2 |
| S-R-01 | silent_small_fraud | 343 | 0 | 0 | 0 | — |
| S-S-01 | bank_mismatch | 3 | 0 | 0 | 0 | — |
| S-S-03 | bank_mismatch | 4 | 0 | 0 | 0 | — |
| S-S-04 | bank_mismatch | 3 | 0 | 0 | 0 | — |
| S-S-05 | bank_mismatch | 3 | 0 | 0 | 0 | — |
| S-S-06 | bank_mismatch | 3 | 0 | 0 | 0 | — |
| S-S-07 | bank_mismatch | 1 | 0 | 0 | 0 | — |
| S-T-01 | digit_distribution_anomaly | 324 | 0 | 0 | 0 | — |
| S-V-01 | data_quality | 5 | 0 | 0 | 0 | — |
| S-W-01 | inconsistent_combination | 4 | 0 | 0 | 0 | — |
| S-X-01 | composite_risk | 6 | 5 | 2 | 5 | transaction_pattern 3, peer_anomaly 2 |

injected scenarios with ≥ 1 medium-or-higher row: 28/41; P8 statistical targets: 11/12

controls (rows / medium+ / high+):
  S-M-02 employee_vendor_relationship: 20 / 0 / 0
  S-N-04 shared_attributes: 154 / 0 / 0
  S-U-01 legit_round_numbers: 36 / 0 / 0
  S-U-02 legit_round_numbers: 72 / 0 / 0
  S-U-04 legit_new_vendor: 58 / 0 / 0
  S-U-05 legit_large_payment_installments: 3 / 1 / 0
  S-U-06 legit_seasonal_velocity: 46 / 0 / 0
  S-U-07 legit_near_threshold: 3 / 0 / 0
  S-U-08 legit_period_end_overtime: 12 / 3 / 1
  S-U-09 legit_multi_party_chain: 6 / 4 / 0
  S-U-10 legit_reciprocal_flows: 37 / 2 / 0
  S-U-12 legit_batch_payments: 72 / 32 / 0
  S-U-13 legit_near_duplicate: 2 / 0 / 0
  S-U-14 legit_sequential_invoices: 36 / 1 / 1
medium+: 236 rows — in injected scenarios 122, in controls 43, outside any scenario 71
high+: 58 rows — in injected scenarios 52, in controls 2, outside any scenario 4
review queue: 236 rows — in injected scenarios 122, in controls 43, outside any scenario 71
ranking vs all injected rows (diagnostic): {"scored": 7271, "positives_scored": 895, "prevalence": 0.12309173428689314, "pr_auc": 0.219351273898995, "at_k": {"50": {"precision": 0.94, "recall": 0.05251396648044693}, "100": {"precision": 0.63, "recall": 0.07039106145251396}, "250": {"precision": 0.492, "recall": 0.13743016759776536}}}

example explanations (review queue top 3):
  [LVC-JV-000812 ['S-B-01']] Review-risk score 79.31 of 100 (critical, 3 independent sources): Transaction-pattern rules 34.89 points; Peer-group deviation 25.72 points; Counterparty-level evidence 17.96 points; Extreme value (tail) 0.74 points.
      +34.89 new_vendor_large: Payment of 100,000.00 to a counterparty with 2 earlier transaction(s) is 5.4 robust deviations above its account peer group. Check the vendor's onboarding and t
      +25.72 robust_peer: amount_minor 100,000.00 is 4.5 robust SDs above the median of its peers (account=5500 (out), n = 594, median 9,968.20). Flagged at |z| ≥ 3.5. Unusual relative t
      +17.96 round_amount: 18 of 18 amounts to this counterparty are multiples of 1,000 (100%) across 9 different values, against 3.0% for all out payments (binomial p = 3.8e-28). Fabrica
  [LVC-JV-000598 ['S-B-01']] Review-risk score 79.00 of 100 (critical, 3 independent sources): Transaction-pattern rules 35.09 points; Peer-group deviation 25.86 points; Counterparty-level evidence 18.05 points.
      +35.09 new_vendor_large: Payment of 75,000.00 to a counterparty with 1 earlier transaction(s) is 4.8 robust deviations above its account peer group. Check the vendor's onboarding and th
      +25.86 robust_peer: amount_minor 75,000.00 is 4.0 robust SDs above the median of its peers (account=5500 (out), n = 594, median 9,968.20). Flagged at |z| ≥ 3.5. Unusual relative to
      +18.05 round_amount: 18 of 18 amounts to this counterparty are multiples of 1,000 (100%) across 9 different values, against 3.0% for all out payments (binomial p = 3.8e-28). Fabrica
  [LVC-JV-002680 ['S-J-01']] Review-risk score 76.00 of 100 (high, 2 independent sources): Transaction-pattern rules 48.80 points; Peer-group deviation 27.20 points.
      +48.80 structuring: 3 payments to the same counterparty within 7 days, each between 95% and 100% of the expense_claim limit 5,000.00, together 14,830.00. Check whether one purchase
      +27.20 robust_peer: amount_minor 4,980.00 is 3.7 robust SDs above the median of its peers (counterparty=arjun acharya (out), n = 28, median 1,429.82). Flagged at |z| ≥ 3.5. Unusual
top entities:
  1. Halvorn Facility Services & Co (out) critical 89.52 sources 3 rows 18/18 exposure 1478000.00
  2. Castrel Estates Services Pvt Ltd (out) critical 82.88 sources 3 rows 6/42 exposure 162171.76
  3. Trelane Paper Co Pvt Ltd (out) critical 80.87 sources 3 rows 9/74 exposure 247992.59
  4. Arjun Acharya (out) high 87.95 sources 2 rows 3/30 exposure 14830.00
  5. Brightwel Supplies (out) high 85.65 sources 2 rows 4/4 exposure 579509.76
```

**How to read it:**
- **Corroboration drives the tiers.** 52 of 58 high/critical rows are injected scenarios. Duplicates sit at medium
  by design: one strong source scores 60, but high needs two sources. They still rank near the top of the review
  queue.
- **Combinations reach critical.** The two critical rows combine a new-vendor amount rule, a peer deviation and a
  counterparty-level round-amount finding.
- **Not seen by design.** Look-alike vendors (P12), bank mismatches (P14), the aggregate "silent small fraud",
  digit-only S-T-01 (a Benford sample, low per row), data quality and inconsistent combinations. S-L-01
  (test-then-exploit) is the one statistical target that stays low.
- **Controls.** No look-alike control reaches high except one row each of two controls that P7 already flagged.
  Each is corroborated by a P8 tail percentile, so it reaches high with two sources:
  - S-U-08: the first routine quarter-end posting, flagged by P7's temporal rule (51.26);
  - S-U-14: one canteen-contractor row, flagged by a P7 transaction-pattern rule (58.11).

  The payroll control reaches medium on 32 rows through P8's bimodal-cadence limitation.
- **Not tuned.** Weights, caps and tiers were set from the design before these numbers and were not tuned. The
  only configuration change during development was a principled one: P8 family caps were raised to the families'
  own single-signal weight, so one signal is never "capped" by itself. The same rule applies to every family.

## PaySim validation (proxy only; labels read only here)

```
== PaySim: 10127 rows; 4.88s
counts: {"transactions": 10127, "scored": 10127, "insufficient_data": 0, "with_signals": 331, "tiers": {"low": 9964, "medium": 163, "high": 0, "critical": 0}, "medium_or_higher": 163, "high_or_critical": 0, "detectors_unavailable": 7, "detectors_total": 14}
volume guard: 0 high/critical rows are within the review budget of 202
score distribution: [9849, 111, 10, 4, 153, 0, 0, 0, 0, 0]
families: [('benford', 3, 60), ('statistical_anomaly', 100, 100), ('peer_anomaly', 153, 153), ('tail_anomaly', 102, 102), ('data_quality', 805, 805)]
unavailable detectors: [('dormancy', "feature cp_days_since_prev is not_available: time basis is 'simulation_step': steps of a simulation are not calendar time"), ('duplicate_near', "time basis is 'simulation_step': steps of a simulation are not calendar time"), ('new_vendor_large', "time basis is 'simulation_step': steps of a simulation are not calendar time"), ('period_end', "time basis is 'simulation_step': steps of a simulation are not calendar time"), ('structuring', 'feature threshold_proximity is not_available: approval thresholds do not apply to this adapter (not a corporate ledger)'), ('temporal', "feature is_off_hours is not_available: posting timestamps have no real time of day (time basis 'simulation_step')"), ('velocity', "feature cp_velocity_ratio is not_available: time basis is 'simulation_step': steps of a simulation are not calendar time")]
entities: {"total": 6967, "medium_or_higher": 160, "high_or_critical": 3, "with_high_or_critical_transactions": 0}
label diagnostic (evaluation only): 68 positives, prevalence 0.7%; PR-AUC of the risk score 0.0146; P@50 2.0% R@50 1.5%; P@100 4.0% R@100 5.9%; P@250 3.2% R@250 11.8%
medium+ rows 163: isFraud rate 3.7%
rows with only data-quality flags: 784; their tiers Counter({'low': 797, 'medium': 8})
```

How to read the PaySim results:
- PaySim supports only amount-based P8 evidence and Benford. It has no calendar time, references, approval policy
  or counterparty history, so 7 detectors are unavailable, each with its reason.
- **No row reaches high.** That is correct: nothing independent corroborates anomaly evidence there.
- **Data-quality signals never become risk.** About 780 balance-discontinuity-only rows stay at 0.
- The risk score's PR-AUC against `isFraud` (0.0146 vs 0.67% prevalence) is a weak proxy diagnostic. It is
  **not** LedgerSpy's fraud-detection performance, and PaySim is not the target domain.

## Frontend rewire

| File | Change |
|---|---|
| `frontend/js/ml-engine.js` | **Deleted** (heuristic features, weights, sigmoid, tiers, Benford counts, dashboard formulas) |
| `frontend/js/data-pipeline.js` | **Deleted** (in-browser CSV parsing, table, add-row/export, preliminary readiness) |
| `frontend/js/api.js` | **New**: token client for the local API (Bearer token, JSON and octet-stream upload) |
| `frontend/js/risk-ui.js` | **New**: import (upload → confirm mapping → integrity) and analysis. Renders hero, overview, data readiness, findings (detectors, dataset findings, Benford), trends (monthly tiers, Benford chart, top counterparties), anomalies (scatter + P8 detectors), relationships (counterparty risk), explainability (method + families), paginated table with explanations, and the findings summary |
| `frontend/js/chatbot-ui.js` | Answers from API data (summary, review queue, explanation by ID, Benford, unavailable detectors); every value escaped |
| `frontend/js/shared.js` | Display helpers only (tier badges, exact amount text, counts); the scoring config is removed |
| `frontend/js/ui-core.js` | The client-side scatter is removed (now API-driven) |
| `frontend/js/integrity-panel.js` | Follows the dataset the risk UI analyses |
| `frontend/index.html`, `css/main.css` | Four tiers, dataset picker, mapping card, Findings tab, server-side table controls, findings summary; honest "Not available" states kept |

**Removed P1 conveniences:**
- *add a row in the browser* and *export the edited CSV* are removed: rows that never reach the evidence store
  cannot be scored by the backend;
- the browser-side *preliminary readiness* is removed (the P4 report replaces it);
- the in-browser memo is removed: it composed text from computed values, and the backend memo is P16. It is
  replaced by a findings summary rendered from the API.

## Deviations from architecture.md / roadmap.md

1. **Layout.** `ledgerspy/risk/` holds `fusion`, `calibration` (tiers + volume guard, no probability calibration),
   `entities`, `explain`, `models` and `service`. The roadmap listed a separate `ledgerspy/explain/` and
   `api/routes/runs.py`. Explanations live in `risk/explain.py` (`explain/` remains for SHAP wrappers in P10/P11),
   and the routes are `api/routes/risk.py` under `/api/datasets/{id}/risk…`, with no persisted "runs".
2. **Tiers.** low / medium / high / critical, as specified for P9. The canonical `Tier` enum's `review` became
   `medium`, and `critical` was added; one P2 test literal was updated. The architecture's "critical signal fires"
   shortcut is not implemented: no such signal exists before reconciliation (P14).
3. **Groups.** Architecture §5.4 has family caps only. P9 adds group caps, so the three overlapping P8 families
   count as one independent source.
4. **Not persisted.** Results are cached in memory by identity, not stored. Findings and analyst decisions are P15.
5. **P8 family caps** were raised to their single-signal weight (0.4) during development; see above.

## Known limitations

See [`risk-engine.md`](risk-engine.md) §Known behaviour and limitations.
- **Not calibrated.** The score is uncalibrated; it is a priority, not a probability.
- **P7/P8 correlation.** Cross-family correlation between P7 rules and P8 anomalies is not modelled.
- **Names, not resolved entities.** Entities are matched by normalised name.
- **Benford.** It contributes only to sample rows, and only slightly.
- **One currency.** Amount totals are shown for single-currency datasets only.
- **No case calendar in the served UI.** In the served UI, holidays and approval thresholds come from case
  configuration that the UI cannot yet edit, so `structuring` and `is_holiday` are unavailable there (P15).
