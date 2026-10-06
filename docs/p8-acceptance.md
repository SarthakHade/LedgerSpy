# P8 — Statistical Anomaly Detection: Acceptance Record

Date: 2026-10-06 · Scope: [`roadmap.md`](roadmap.md) §P8 (R7), [`architecture.md`](architecture.md) §5.3, §5.5 ·
Model card: [`anomaly-model-card.md`](anomaly-model-card.md).

P8 adds three label-free detectors over the P6 feature matrix and a dataset anomaly report:
- an Isolation Forest;
- robust peer-group z-scores;
- tail percentiles.

It adds no risk fusion, tiers or UI (P9) and no supervised ML (P10). P7 rules are unchanged; P8 is a separate
signal family (`model_anomaly`).

## Commands and results (final state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # All checks passed; 145 files already formatted
.venv/Scripts/python -m pytest                                       # 758 passed (693 P1–P7 + 65 P8)
.venv/Scripts/python -m pytest tests/backend/test_anomaly_*.py       # 65 passed (P8)
node tests/frontend/harness.js                                       # 24 passed, 0 failed (P1)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 14 passed, 0 failed (P1)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed (P4 panel)
```

| P8 test file | Tests | What it proves |
|---|---|---|
| `test_anomaly_iforest.py` | 24 | **Algorithm:** c(n) matches the paper; an obvious outlier scores highest (> 0.7) while the typical cluster point is < 0.5; a fixed seed gives an identical forest hash and scores, and another seed differs; constant features are never split on; all-constant data isolates nothing. **Detector:** the planted outlier is ranked first, with a "feature deviation (not SHAP)" explanation; the tail share, `min_score` and review-capacity cap all hold; imputation is explicit (reference median + missing indicator, evidence keeps the null); rows with too few features are `insufficient_data` with no score. **Eligibility:** constant, excessive-missing, not-available, non-numeric, entity-level and dataset-semantics features are excluded with reasons; an unknown name is a configuration error; label and ID names are forbidden. **Other:** too few rows or features gives `insufficient_data` with no model; label-like extra columns give an identical report; shuffled rows give an identical report and forest; a disabled detector says so |
| `test_anomaly_robust.py` | 24 | **Robust z:** hand-computed median/MAD/z; zero MAD falls back to mean absolute deviation and all-equal values have an undefined scale; an obvious outlier is flagged against its own counterparty; a small counterparty is judged by its account (basis counted); an insufficient peer group is reported, never replaced by a global baseline; a constant group uses the resolution floor; missing values; direction; an unsupported peer is `not_available`; order independence. **Tail:** known percentiles and the 1% / 0.5% boundary; ties never inflate a tail; exactly the top 1% of 1..200 is flagged, with n / percentile / cutoff / population; populations below the minimum are insufficient; flows are separate populations. **Populations:** warm-up rows are insufficient and outside the reference; in chronological mode, rewriting the future changes neither the forest nor the cutoffs; no timestamps means `not_available`. **Contract:** signal fields, deterministic IDs, canonical `Signal` conversion. **Evaluation:** precision@k, recall@k, PR-AUC (hand example, ties), coverage, distribution; the report carries no label metrics |
| `test_anomaly_datasets.py` | 17 | **Synthetic:** every detector runs; the warm-up is applied; the per-detector target hits match the recorded baseline and their union is 12/12; Isolation Forest and tail ranking match the recorded PR-AUC, P@50 and coverage; P8 look-alike controls stay below 25% for every detector; known failure modes are asserted (so they cannot silently change); every signal explains itself and never claims fraud. **PaySim:** the features used and excluded (with reasons); label diagnostics after detection (PR-AUC above prevalence for each detector); flipping `isFraud` and `isFlaggedFraud` gives identical scores, signals and forest. **Isolation and integrity:** a static scan for labels, extra, ground truth and P7 signal use; no table changes and no network; a second workspace gives identical scores and signals. **API:** schema over the token-protected endpoint, plus honest `insufficient_data` on a tiny dataset. **Scale:** 100k synthetic rows complete within the guard |

P6 regression: all 174 P6 tests pass. The one P6 change (`payee_distinct_payers_prior` v2) is value-identical.

## Acceptance criteria

| # | Criterion | Evidence | Result |
|---|---|---|---|
| 1 | Isolation Forest is implemented and deterministic | `ledgerspy/anomaly/isolation_forest.py` (pure Python, Liu et al. 2008). Fixed seed gives an identical `model_sha256` and scores. The forest depends only on feature values and seed: shuffles, re-imports and label flips give the same result | ✅ |
| 2 | Robust peer-group anomaly detection is implemented | `robust_stats.robust_peer`: median / 1.4826·MAD (mean-AD fallback, resolution floor); counterparty → account / txn_type in order; `min_group_size` 20; \|z\| ≥ 3.5 | ✅ |
| 3 | Tail-percentile detection is implemented | `robust_stats.tail_percentile`: mid-rank percentile per flow; top 1% / 0.5%; min population 200; n, percentile, value, cutoff, feature and population reported | ✅ |
| 4 | The P6 feature registry is reused | Every input is a P6 registry feature read from the matrix, and eligibility checks registry metadata. An unknown name is a configuration error. P8 defines no feature and imports no private P6 helper | ✅ |
| 5 | No labels or ground truth enter detection | Static scan; PaySim with both label columns flipped gives identical results; label-like extra columns give an identical report. Labels and ground truth are read only by evaluation code after detection | ✅ |
| 6 | Missing, constant and unsupported features are handled honestly | Exclusions with reasons (`not_available`, `excessive_missing`, `constant`, `not_point_in_time`, …). Explicit imputation with indicators, and evidence keeps the null. `insufficient_data` rows and detectors carry no invented scores | ✅ |
| 7 | Scores are deterministic | Re-run gives the same `report_sha256`; a second workspace gives identical scores, signals, detectors and inputs | ✅ |
| 8 | Shuffled input does not change subject-level results | Unit tests: shuffled records give an identical report. Dataset test: a differently encoded file (new txn_ids) gives identical scores per source row | ✅ |
| 9 | P5 scenarios provide meaningful validation | Targets were fixed before any run. Recorded baseline: union 12/12; IF top-50 covers 5. Controls and false-positive diagnostics below | ✅ |
| 10 | PaySim works as a proxy without label leakage | Features used and excluded are reported; labels are used only after detection; the label flip is a no-op | ✅ |
| 11 | precision@k / recall@k / PR-AUC exist where labels are legitimate | `ledgerspy/anomaly/evaluation.py`; k from `anomaly.evaluation.review_capacity`; no label metrics in the report | ✅ |
| 12 | No accuracy-only headline | Accuracy is not computed anywhere. PR-AUC is always shown next to prevalence | ✅ |
| 13 | Model card exists | [`anomaly-model-card.md`](anomaly-model-card.md) | ✅ |
| 14 | Full pytest passes | 758 passed | ✅ |
| 15 | P1 Node and browser tests pass | 24/24, 14/14 | ✅ |
| 16 | P4 integrity panel test passes | 6/6 | ✅ |
| 17 | Ruff passes | Clean | ✅ |
| 18 | No regression to P1–P7 | All 693 earlier tests pass. P6 change: one feature reimplemented with identical values (v2, lock regenerated). P7 untouched | ✅ |
| — | Roadmap: < 60 s at 100k synthetic rows | ≈ 50 s for P6 + P8 on 99,797 rows on the development laptop; the test guard is 120 s | ✅ |

## Synthetic scenario validation (diagnostic development metrics)

synthetic ledger: 7271 rows; P6 1.85s, P8 0.93s; report_sha256 965f5b99cf67b511c6c7288285486c8b52b27b5e1f62621d9fa896d67e719184 (re-run identical: True)

Ground truth is used **only here**, after detection.

- **Target scenarios** are the 12 statistical-outlier scenarios in `P8_TARGETS`. *Other injected* means
  scenarios owned by other detectors.
- A row in both an injected scenario and a control counts as injected.

```
population: {"mode": "dataset", "status": "ok", "reason": "", "rows": 7271, "reference_rows": 6736, "scoring_rows": 6736, "excluded_rows": 535, "warm_up": {"days": 90, "applied": true, "until": "2023-06-30T16:47:05", "rows": 535}}
  isolation_forest: evaluated scored=6734 insufficient={'too_few_present_features': 2, 'warm_up': 535} features=17 used / 0 excluded; signals=67 psi=256 height=8 inputs=26 model_sha256=4578ea52de168689… flagged=67 cutoff=0.597634
  robust_peer: evaluated scored=6675 insufficient={'no_sufficient_population': 61, 'warm_up': 535} features=4 used / 0 excluded; signals=123
  tail_percentile: evaluated scored=6736 insufficient={'warm_up': 535} features=6 used / 0 excluded; signals=354

| Detector | Flagged rows | In P8 target scenarios | In other injected scenarios | In controls | Outside any scenario | P8 target scenarios hit |
|---|---|---|---|---|---|---|
| `isolation_forest` | 67 | 5 | 21 | 22 | 19 | 6/12 (S-A-02, S-G-01, S-H-02, S-L-01, S-Q-01, S-X-01) |
| `robust_peer` | 123 | 7 | 32 | 34 | 50 | 7/12 (S-A-01, S-A-02, S-H-01, S-H-02, S-I-01, S-K-01, S-X-01) |
| `tail_percentile` | 354 | 21 | 21 | 78 | 234 | 9/12 (S-A-01, S-A-02, S-H-01, S-H-02, S-H-03, S-I-01, S-K-01, S-K-03, S-X-01) |
| `any P8 detector` | 462 | 28 | 50 | 104 | 280 | 12/12 (S-A-01, S-A-02, S-G-01, S-H-01, S-H-02, S-H-03, S-I-01, S-K-01, S-K-03, S-L-01, S-Q-01, S-X-01) |

missed target scenarios per detector:
  isolation_forest: ['S-A-01', 'S-H-01', 'S-H-03', 'S-I-01', 'S-K-01', 'S-K-03']
  robust_peer: ['S-G-01', 'S-H-03', 'S-K-03', 'S-L-01', 'S-Q-01']
  tail_percentile: ['S-G-01', 'S-L-01', 'S-Q-01']
  any P8 detector: []

controls touched (rows flagged / control rows):
  isolation_forest: {'S-U-01': '1/36', 'S-U-04': '1/58', 'S-U-05': '3/3', 'S-U-08': '1/12', 'S-U-09': '6/6', 'S-U-10': '5/37', 'S-U-12': '4/72', 'S-U-13': '1/2'}
  robust_peer: {'S-U-08': '1/12', 'S-U-10': '1/37', 'S-U-12': '32/72'}
  tail_percentile: {'S-M-02': '3/20', 'S-N-04': '1/154', 'S-U-02': '2/72', 'S-U-05': '2/3', 'S-U-08': '1/12', 'S-U-09': '6/6', 'S-U-10': '3/37', 'S-U-12': '33/72', 'S-U-14': '27/36'}

ranking diagnostics (score columns; k = review capacity):
| Detector score | Positives | Scored | Prevalence | PR-AUC | P@50 | R@50 | P@100 | R@100 | P@250 | R@250 | Target scenarios in top 50 / 100 / 250 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `isolation_forest.score` | P8 targets (115) | 6734 | 1.7% | 0.037 | 8.0% | 3.5% | 6.0% | 5.2% | 6.0% | 13.0% | 5/6/9 |
| `isolation_forest.score` | all injected (895) | 6734 | 12.6% | 0.184 | 46.0% | 2.6% | 30.0% | 3.4% | 21.6% | 6.0% | — |
| `robust_peer.max_abs_z` | P8 targets (115) | 6675 | 1.7% | 0.026 | 6.0% | 2.6% | 5.0% | 4.3% | 3.6% | 7.8% | 5/6/7 |
| `robust_peer.max_abs_z` | all injected (895) | 6675 | 12.6% | 0.125 | 8.0% | 0.4% | 33.0% | 3.7% | 19.6% | 5.5% | — |
| `tail_percentile.max_tail_percentile` | P8 targets (115) | 6736 | 1.7% | 0.131 | 34.0% | 14.8% | 20.0% | 17.4% | 8.4% | 18.3% | 9/9/9 |
| `tail_percentile.max_tail_percentile` | all injected (895) | 6736 | 12.7% | 0.107 | 44.0% | 2.5% | 27.0% | 3.0% | 13.6% | 3.8% | — |
IF top-50 covers: ['S-A-02', 'S-G-01', 'S-H-02', 'S-L-01', 'S-X-01']
IF top-10 rows:
  LVE-JV-000631 score 0.697057 ['S-P-02']: payer_new_payees_7d z+14.2, cp_prior_count z-3.7, cp_count_30d z-1.8
  LVE-JV-000635 score 0.696586 ['S-P-02']: payer_new_payees_7d z+19.9, cp_prior_count z-3.7, cp_count_30d z-1.8
  LVC-JV-000928 score 0.690711 ['S-U-09']: pair_share_of_payer_outflow z+56.2, payer_new_payees_7d z+7.1, cp_prior_count z-3.7
  LVE-JV-000632 score 0.688487 ['S-P-02']: payer_new_payees_7d z+16.4, cp_prior_count z-3.7, cp_count_30d z-1.8
  LVE-JV-000633 score 0.686547 ['S-P-02']: payer_new_payees_7d z+18.3, cp_prior_count z-3.7, cp_count_30d z-1.8
  LVE-JV-000630 score 0.685368 ['S-P-02']: payer_new_payees_7d z+11.2, cp_prior_count z-3.7, cp_count_30d z-1.8
  LVC-JV-004602 score 0.678048 ['S-O-02']: payer_new_payees_7d z+7.1, cp_prior_count z-3.7, cp_count_30d z-1.8
  LVE-JV-000101 score 0.673922 ['S-U-09']: cp_prior_count z-3.7, amount_robust_z z+2.2, log_amount z+1.9
  LVE-JV-000627 score 0.673174 ['S-P-02']: payer_new_payees_7d z+7.1, cp_prior_count z-3.7, cp_count_30d z-1.8
  LVC-JV-004969 score 0.6723 ['S-P-01']: amount_to_account_median_ratio z+8.2, amount_robust_z z+4.3, cp_prior_count z-3.7
score distribution (IF): {"n": 6734, "min": 0.354918, "p50": 0.429341, "p90": 0.503827, "p99": 0.597497, "p999": 0.678048, "max": 0.697057, "histogram": {"edges": [0.354918, 0.389132, 0.423346, 0.45756, 0.491774, 0.525988, 0.560201, 0.594415, 0.628629, 0.662843, 0.697057], "counts": [979, 2017, 1815, 1054, 446, 248, 101, 43, 17, 14]}}

example signals:
  [isolation_forest] LVC-JV-000956 (['S-Q-01']): Isolation Forest score 0.609 (higher = more unusual) is in the top 0.84% of 6,734 reference transactions (flagged: top 1.0%, score > 0.5). Most deviating inputs (feature deviation, not SHAP): cp_prior_count = 0 (robust z -3.7), cp_count_30d = 0 (robust z -1.8), cp_count_30d_peer_ratio = 0 (robust z -1.2). Null inputs, scored at the reference median: amount_to_cp_median_ratio, cp_prev_amount_ratio, cp_days_since_prev, cp_days_since_first, cp_velocity_ratio, cp_log_amount_trend. Unusual relative to this dataset — a reason to review, not a finding of fraud.
  [robust_peer] LVC-JV-003356 (['S-I-01']): amount_minor 46,500.86 is 5.6 robust SDs above the median of its peers (counterparty=kiran varma (out), n = 37, median 1,691.76). Flagged at |z| ≥ 3.5. Unusual relative to these peers — a reason to review, not a finding of fraud.
  [tail_percentile] LVC-JV-003356 (['S-I-01']): amount_to_cp_median_ratio 23.11 is in the top 0.5% of 5,377 rows of flow=out (percentile 99.99; cutoff 5.31); amount_to_account_median_ratio 32.02 is in the top 0.5% of 5,556 rows of flow=out (percentile 99.97; cutoff 13.73). Unusual relative to this population — a reason to review, not a finding of fraud.
```

**How to read the results:**
- **Complementary detectors.** No single detector sees everything:
  - the **Isolation Forest** finds *combinations* (new vendor + burst + jump: S-X-01, S-G-01, S-L-01, S-Q-01);
  - **robust peer z** finds single-value deviations inside established streams (S-A-01, S-I-01, S-H-01, S-K-01);
  - the **tail** finds the extremes of each flow.

  Their union covers all 12 targets.
- **Not tuned to the benchmark.** Targets and thresholds come from definitions and published conventions:
  - ψ = 256 and 100 trees (Liu et al.);
  - \|z\| ≥ 3.5 (Iglewicz & Hoaglin);
  - top 1% / 0.5% tails;
  - a 0.5 score floor.

  The first diagnostic runs exposed definitional flaws, each fixed on principle and recorded in the model card:
  - **Cold start:** the first weeks of data dominated the forest, because every vendor looks new there. Fixed with
    a 90-day warm-up, as in P7.
  - **`round_level`:** a rare discrete code that Isolation Forest isolates regardless of context; roundness is
    P7's. Removed from the model inputs.
  - **Whole-vendor peer flags:** account-level peer z flagged every row of a vendor that differs from its account.
    Fixed by peer preference, so a row is judged against its own counterparty first.
  - **Day-level cadence z:** a very regular cadence turned a one-day difference into a large z. Fixed with a
    resolution floor (0.1 log units, as P6's `mad_scale_floor`).
  - **ID-dependent forest:** re-imported files (new IDs) gave a different forest. Fixed by sampling from rows
    ordered by their feature values.
- **Controls and false positives.** The P8 look-alikes (S-U-01, S-U-02, S-U-04, S-U-06, S-U-08) stay below 25% for
  every detector. Controls flagged at a high rate are real statistical unusualness, documented as known failure
  modes in the model card:
  - S-U-12 payroll: a bimodal cadence, and among the largest payments;
  - S-U-14 canteen: high against its account median;
  - S-U-05 and S-U-09: brand-new parties.
- **Rows outside any scenario.** These are ordinary rows that are genuinely extreme within this generated company.
  The tail flags the most, by construction, which is why its severity is low.

## PaySim validation (proxy only)

PaySim is a proxy development dataset, not LedgerSpy's corporate-ledger domain.

```
== PaySim: 10127 rows; P6 1.71s, P8 2.04s; warm-up: {'days': 90, 'applied': False, 'reason': "time basis is 'simulation_step': steps of a simulation are not calendar time"}
  isolation_forest: evaluated scored=10118 insufficient={'too_few_present_features': 9} used=['log_amount', 'amount_robust_z', 'amount_pctl_prior', 'cp_prior_count'] signals=100
     excluded: [('amount_to_cp_median_ratio', 'excessive_missing'), ('amount_to_account_median_ratio', 'not_available'), ('cp_prev_amount_ratio', 'excessive_missing'), ('cp_days_since_prev', 'not_available'), ('cp_days_since_first', 'not_available'), ('cp_count_7d', 'not_available'), ('cp_count_30d', 'not_available'), ('cp_velocity_ratio', 'not_available'), ('cp_count_30d_peer_ratio', 'not_available'), ('cp_log_amount_trend', 'not_available'), ('hour_deviation', 'not_available'), ('payer_new_payees_7d', 'not_available'), ('pair_share_of_payer_outflow', 'excessive_missing')]
  robust_peer: evaluated scored=10121 insufficient={'no_sufficient_population': 6} used=['log_amount'] signals=153
     excluded: [('cp_days_since_prev', 'not_available'), ('cp_count_30d', 'not_available'), ('hour_of_day', 'not_available')]
  tail_percentile: evaluated scored=10121 insufficient={'no_sufficient_population': 6} used=['amount_minor'] signals=102
     excluded: [('amount_to_cp_median_ratio', 'excessive_missing'), ('amount_to_account_median_ratio', 'not_available'), ('cp_velocity_ratio', 'not_available'), ('cp_days_since_prev', 'not_available'), ('hour_deviation', 'not_available')]
  IF score distribution: {"n": 10118, "min": 0.389572, "p50": 0.461646, "p90": 0.549277, "p99": 0.646949, "p999": 0.687777, "max": 0.695748, "histogram": {"edges": [0.389572, 0.42019, 0.450807, 0.481425, 0.512042, 0.54266, 0.573278, 0.603895, 0.634513, 0.66513, 0.695748], "counts": [2421, 1529, 2132, 1994, 883, 541, 313, 142, 112, 51]}}
  label diagnostic (evaluation only): 68 isFraud=1 of 10125 labelled rows
| Detector score | Scored | Prevalence | PR-AUC | P@50 | P@100 | P@250 | R@250 |
|---|---|---|---|---|---|---|---|
| `isolation_forest.score` | 10118 | 0.7% | 0.0131 | 0.0% | 1.0% | 2.8% | 10.3% |
| `robust_peer.max_abs_z` | 10121 | 0.7% | 0.0276 | 4.0% | 3.0% | 2.0% | 7.4% |
| `tail_percentile.max_tail_percentile` | 10121 | 0.7% | 0.0603 | 8.0% | 4.0% | 4.0% | 14.7% |
  isolation_forest: 100 flagged rows, isFraud rate 0.0100
  robust_peer: 153 flagged rows, isFraud rate 0.0327
  tail_percentile: 102 flagged rows, isFraud rate 0.0392
```

How to read the PaySim results:

- **What runs.** With simulated steps, no calendar features exist, so the warm-up and every velocity, cadence and
  timing feature are `not_available`. Counterparties rarely repeat, so counterparty baselines are
  `excessive_missing` and robust z uses transaction-type peers.
- **Ranking.** Each detector ranks fraud above the 0.67% base rate (PR-AUC 0.013–0.060), far from a usable
  classifier. That is expected for label-free amount statistics on a simulator. These are diagnostics, never
  optimised.

## Deviations from architecture.md and roadmap.md

1. **Package layout.** The roadmap lists `ledgerspy/detectors/anomaly.py`. P8 is the package `ledgerspy/anomaly/`
   (`eligibility`, `isolation_forest`, `robust_stats`, `evaluation`, `models`, `service`), following the P8
   specification. `architecture.md` §11 is updated.
2. **No scikit-learn.** Isolation Forest is implemented in pure Python, faithful to the paper, so that no dependency
   (or network install) is added. There are no SHAP explanations; the Isolation Forest is explained by *feature
   deviations*, labelled as such (architecture §5.5 allows this).
3. **`n_estimators` 100, not ≈ 200**, and no `contamination="auto"`:
   - 100 trees is the paper's convergence point, and it keeps 100k rows under the time target;
   - the threshold is the review-capacity policy described in the model card.
4. **P6 change.** `payee_distinct_payers_prior` became v2 with identical values. v1 was quadratic and took 29 of the
   55 s at 100k rows. The registry lock was regenerated and the docs updated.
5. **Peer keys and median are defined in `ledgerspy/anomaly`.** These are detector groupings, like P7's Benford
   dimensions, because the P6 guard forbids importing its private helpers.
6. **Report, not persistence.** `POST /api/datasets/{id}/anomaly` computes the report on request; nothing is stored.
   `inputs`, `model_sha256` and `report_sha256` make each report reproducible. P9 decides what to persist.
7. **Strength** is provisional: the architecture's tail formula for the Isolation Forest and tail, and 1.0 for
   robust peer. P9 calibrates strength and weight.

## Known limitations

See [`anomaly-model-card.md`](anomaly-model-card.md) §Limitations. In brief:
- scores are within-dataset;
- new counterparties dominate the forest;
- bimodal cadences defeat robust z;
- tails always contain the largest legitimate payments;
- slow drifts move their own baselines;
- peer groups use names, not resolved entities (P12);
- the 90-day warm-up is not scored by P8.
