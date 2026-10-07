# P10 — Supervised Training and Leakage-Safe Evaluation: Acceptance Record

Date: 2026-10-07 · Scope: [`roadmap.md`](roadmap.md) §P10 (R17), [`architecture.md`](architecture.md) §5.2 · Model
card: [`supervised-model-card.md`](supervised-model-card.md).

P10 adds an **optional, experimental** supervised layer:
- a label adapter;
- a versioned training-data contract;
- leakage-safe splits;
- logistic regression and histogram gradient boosting;
- validation-only threshold and calibration;
- baselines;
- bootstrap intervals;
- interpretation;
- reproducible experiment records (`ledgerspy evaluate`).

It persists no model and adds no inference (P11). It does not change P9, any API endpoint or the UI. There is no
production fraud-probability endpoint.

## Commands and results (final state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # All checks passed
.venv/Scripts/python -m pytest                                       # 925 passed (891 P1–P9 + 34 P10)
node tests/frontend/harness.js                                       # 13 passed, 0 failed
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 12 passed, 0 failed
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed
.venv/Scripts/ledgerspy evaluate --dataset-id D-… --labels paysim                   # writes <workspace>/experiments/S-….json
.venv/Scripts/ledgerspy evaluate --dataset-id D-… --labels synthetic --ground-truth data/synthetic/reference/ground_truth/SYNTHETIC_ground_truth.json
```

| P10 test file | Tests | What it proves |
|---|---|---|
| `test_supervised_units.py` | 21 | **Metrics:** hand-computed AP / ROC-AUC (ties count half), threshold confusion / precision / recall / F1, @k, prevalence baseline = base rate, Brier / ECE; the bootstrap is deterministic and cluster-aware. **Models:** LR recovers the signal direction, is deterministic, and its gradient is zero at the optimum; GBDT learns XOR (LR cannot), is deterministic, and respects min leaf size. **Splits:** deterministic, disjoint and complete for all three strategies; the seed matters; the chronological split orders time and keeps ties together; the group split isolates entities and scenarios (the random split shares them, reported). **Preprocessor:** fitted on training rows only (test values and unseen categories change nothing); imputation + indicator; constant / missing features dropped; ablation removal. **Imbalance:** class weights from training counts, one weight per row, no resampling. **Threshold:** review share of validation. **Calibration:** Platt recovers a known map; not attempted with few positives |
| `test_supervised_datasets.py` | 13 | **Isolation:** only `supervised/labels.py` reads labels and no detector imports the supervised package; no API route or UI exposes a fraud probability. **Contract:** registry features without IDs, labels or detector outputs; post-period features excluded and unknown names rejected; deterministic. **Leakage:** with the split fixed, flipping test labels (and validation labels) changes neither the model, the preprocessing nor the threshold, only test metrics; running experiments leaves P9's `result_sha256` and every table unchanged; scenario overlap is measured and a row-level split inflates PR-AUC. **Records:** identity, threshold, calibration, metrics with intervals, baselines, no accuracy; interpretation for both models; exact reproduction. **PaySim:** proxy note, chronological warning, ablation, type baseline, calibration not attempted. **CLI:** writes a local experiment file; synthetic labels need an explicit ground-truth file |

Updated earlier code (necessary only):
- the configuration schema gains a `supervised` section;
- the CLI gains `evaluate`.

No P1–P9 behaviour changed.

## Acceptance criteria

| # | Criterion | Evidence | Result |
|---|---|---|---|
| 1 | Leakage-safe pipeline exists | `ledgerspy/supervised/`: split first; preprocessing / model on train; threshold and calibration on validation; test once | ✅ |
| 2 | Logistic regression baseline | `models.LogisticRegression` (IRLS, L2) | ✅ |
| 3 | Tree model | `models.GradientBoosting` (histogram GBDT, the HistGradientBoosting algorithm) | ✅ |
| 4 | Correct train / validation / test separation | Disjointness tests; fit only on train; test labels cannot change the model or threshold | ✅ |
| 5 | Leakage-safe imbalance handling | Class weights from training counts; no oversampling / SMOTE anywhere | ✅ |
| 6 | Threshold from validation only | Top 2% of validation scores; tested with flipped test labels | ✅ |
| 7 | PR-AUC primary | Comparison tables lead with PR-AUC [CI]; no accuracy anywhere | ✅ |
| 8 | Precision@K / recall@K | k = 25, 50, with bootstrap intervals | ✅ |
| 9 | Baselines | Prevalence, amount-only, P8 Isolation Forest, P9 review risk, type rate | ✅ |
| 10 | Explicit calibration status | `calibrated` / `not_validated` / `not_attempted` plus `output_label` on every run | ✅ |
| 11 | Versioned, reproducible feature list | Contract with version, registry hash and per-feature status / reason; tests | ✅ |
| 12 | Reproducible experiment metadata | Identity hashes; `result_sha256` reproduced exactly on rerun (test) | ✅ |
| 13 | Labels cannot enter P6–P9 | Static isolation test; P9 unchanged after experiments; P9's PaySim label-flip test | ✅ |
| 14 | PaySim leakage investigated | Type ablation, type-only baseline, coefficient / permutation review, proxy-feature discussion | ✅ |
| 15 | Synthetic scenario leakage investigated | Scenario / counterparty overlap per split; grouped vs row-level results | ✅ |
| 16 | Model interpretation | LR coefficients (standardised, direction, odds ratio / SD); GBDT gain + validation permutation importance; "not causal" caveat | ✅ |
| 17 | Model card | [`supervised-model-card.md`](supervised-model-card.md) | ✅ |
| 18 | Full suite passes | 925 passed | ✅ |
| 19 | Ruff passes | Clean | ✅ |
| 20 | No frontend regression | 13/13 harness, 12/12 browser, 6/6 panel; frontend unchanged | ✅ |
| 21 | No production fraud-probability claim | No API / UI change; every report labels outputs as ranking scores unless calibrated | ✅ |

## Results (default configuration)

```
== SYNTHETIC ledger: {'labelled': 7271, 'positives': 549, 'unlabelled': 0} · 98s · result_sha256 fbe51a559fe9858e7e22ce7a014cf634d013072d75346acce263d9fbd9d33c9c · P9 result unchanged by the experiment: True
labels: {"rule": "positive = rows of injected scenarios with severity ≥ medium, excluding types ['digit_distribution_anomaly']; controls and all other rows negative", "positive_scenarios": ["S-A-01", "S-A-02", "S-B-01", "S-B-02", "S-C-01", "S-C-02", "S-D-01", "S-D-02", "S-E-01", "S-E-02", "S-F-01", "S-G-01", "S-G-02", "S-H-01", "S-H-02", "S-H-03", "S-I-01", "S-J-01", "S-J-02", "S-K-01", "S-K-02", "S-K-03", "S-L-01", "S-M-01", "S-N-01", "S-O-01", "S-O-02", "S-P-01", "S-P-02", "S-Q-01", "S-R-01", "S-S-01", "S-S-07", "S-X-01"], "positives": 549, "rows_labelled": 7271, "ground_truth_sha256": "d456d021ae87c10348602aa7dc5de418da53e876405de52fb60a9b49c40319e3"}
contract: [('txn_type', 'excluded', 'not available for this dataset: no transaction type is mappe')]
| Split | Model | PR-AUC [95% CI] | ROC-AUC | Precision@50 | Recall@50 | F1 @ val. threshold | Calibration |
|---|---|---|---|---|---|---|---|
| random_stratified | baseline: prevalence | 0.076 [0.060, 0.090] | 0.500 | 0.00 | 0.00 | 0.14 | n/a (ranking) |
| random_stratified | baseline: amount_only | 0.050 [0.041, 0.062] | 0.286 | 0.00 | 0.00 | 0.00 | n/a (ranking) |
| random_stratified | baseline: p8_isolation_forest | 0.176 [0.127, 0.252] | 0.689 | 0.26 | 0.12 | 0.17 | n/a (ranking) |
| random_stratified | baseline: p9_review_risk | 0.310 [0.226, 0.392] | 0.649 | 0.56 | 0.25 | 0.27 | n/a (ranking) |
| chronological | baseline: prevalence | 0.052 [0.039, 0.068] | 0.500 | 0.08 | 0.05 | 0.10 | n/a (ranking) |
| chronological | baseline: amount_only | 0.035 [0.026, 0.047] | 0.270 | 0.02 | 0.01 | 0.00 | n/a (ranking) |
| chronological | baseline: p8_isolation_forest | 0.337 [0.206, 0.477] | 0.836 | 0.34 | 0.22 | 0.29 | n/a (ranking) |
| chronological | baseline: p9_review_risk | 0.205 [0.118, 0.310] | 0.621 | 0.44 | 0.29 | 0.17 | n/a (ranking) |
| group | baseline: prevalence | 0.073 [0.011, 0.176] | 0.500 | 0.02 | 0.01 | 0.14 | n/a (ranking) |
| group | baseline: amount_only | 0.096 [0.018, 0.263] | 0.629 | 0.14 | 0.06 | 0.00 | n/a (ranking) |
| group | baseline: p8_isolation_forest | 0.117 [0.051, 0.244] | 0.591 | 0.16 | 0.07 | 0.10 | n/a (ranking) |
| group | baseline: p9_review_risk | 0.446 [0.252, 0.649] | 0.777 | 0.48 | 0.22 | 0.26 | n/a (ranking) |
| random_stratified | logistic_regression | 0.777 [0.696, 0.853] | 0.926 | 0.88 | 0.40 | 0.39 | calibrated |
| random_stratified | logistic_regression + class weights | 0.712 [0.619, 0.811] | 0.937 | 0.82 | 0.37 | 0.43 | calibrated |
| random_stratified | gradient_boosting | 0.878 [0.807, 0.938] | 0.977 | 0.96 | 0.44 | 0.29 | calibrated |
| random_stratified | gradient_boosting + class weights | 0.873 [0.804, 0.940] | 0.977 | 0.96 | 0.44 | 0.25 | calibrated |
| chronological | logistic_regression | 0.805 [0.689, 0.915] | 0.943 | 0.94 | 0.62 | 0.23 | calibrated |
| chronological | logistic_regression + class weights | 0.699 [0.557, 0.839] | 0.948 | 0.88 | 0.58 | 0.24 | calibrated |
| chronological | gradient_boosting | 0.864 [0.773, 0.939] | 0.970 | 0.96 | 0.63 | 0.58 | calibrated |
| chronological | gradient_boosting + class weights | 0.843 [0.756, 0.932] | 0.971 | 0.96 | 0.63 | 0.57 | calibrated |
| group | logistic_regression | 0.412 [0.018, 0.613] | 0.602 | 0.76 | 0.35 | 0.46 | not_validated |
| group | logistic_regression + class weights | 0.405 [0.015, 0.631] | 0.629 | 0.70 | 0.32 | 0.43 | calibrated |
| group | gradient_boosting | 0.485 [0.045, 0.699] | 0.757 | 0.76 | 0.35 | 0.49 | not_validated |
| group | gradient_boosting + class weights | 0.475 [0.035, 0.714] | 0.760 | 0.74 | 0.34 | 0.48 | calibrated |
splits:
   {"strategy": "random_stratified", "rows": {"train": 4362, "validation": 1455, "test": 1454}, "positives": {"train": 329, "validation": 110, "test": 110}, "excluded_rows": 0, "counterparties_in_train_and_test": 141, "scenarios_in_train_and_test": ["S-B-01", "S-B-02", "S-C-01", "S-C-02", "S-F-01", "S-G-02", "S-H-01", "S-H-02", "S-H-03", "S-J-02", "S-K-01", "S-K-02", "S-L-01", "S-M-01", "S-M-02", "S-N-01", "S-N-04", "S-P-01", "S-P-02", "S-Q-01", "S-R-01", "S-S-01", "S-S-03", "S-S-04", "S-T-01", "S-U-01", "S-U-02", "S-U-04", "S-U-05", "S-U-06", "S-U-07", "S-U-08", "S-U-09", "S-U-10", "S-U-12", "S-U-14", "S-X-01"]}
   {"strategy": "chronological", "rows": {"train": 4363, "validation": 1454, "test": 1454}, "positives": {"train": 321, "validation": 152, "test": 76}, "excluded_rows": 0, "counterparties_in_train_and_test": 129, "scenarios_in_train_and_test": ["S-B-02", "S-K-02", "S-M-02", "S-N-04", "S-Q-01", "S-R-01", "S-S-01", "S-S-03", "S-S-06", "S-T-01", "S-U-01", "S-U-02", "S-U-04", "S-U-06", "S-U-08", "S-U-10", "S-U-12", "S-U-14", "S-W-01"], "time_ranges": {"train": ["2023-04-01T16:47:05", "2025-02-03T16:13:52"], "validation": ["2025-02-04T11:59:59", "2025-09-05T15:07:50"], "test": ["2025-09-05T15:38:32", "2026-03-31T20:26:00"]}}
   {"strategy": "group", "rows": {"train": 4451, "validation": 1343, "test": 1477}, "positives": {"train": 388, "validation": 53, "test": 108}, "excluded_rows": 0, "counterparties_in_train_and_test": 0, "scenarios_in_train_and_test": [], "components": 129, "grouping": "counterparty stream ∪ injected scenario (connected components)"}
warnings: []
  [random_stratified · logistic_regression + class weights] used 43 design columns; dropped ['is_holiday']
     top: log_amount -2.779792; amount_pctl_prior 2.621961; missing:amount_to_cp_median_ratio 2.56752; cp_count_30d_peer_ratio 2.048292; pair_prior_count 1.872724
     permutation (validation): cp_count_30d_peer_ratio 0.614688; log_amount 0.598282; pair_prior_count 0.506303; amount_pctl_prior 0.237179; round_level 0.112087
     calibration: calibrated — uncalibrated test ECE 0.131638 Brier 0.075462; Platt test ECE 0.011633 Brier 0.032208
     threshold 0.9788 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 32, 'fp': 6, 'fn': 78, 'tn': 1338}
  [random_stratified · gradient_boosting + class weights] used 43 design columns; dropped ['is_holiday']
     top: cp_count_30d_peer_ratio 0.278123; pair_share_of_payer_outflow 0.246443; log_amount 0.095063; round_level 0.086974; amount_to_account_median_ratio 0.032332
     permutation (validation): pair_share_of_payer_outflow 0.379161; cp_count_30d_peer_ratio 0.310257; round_level 0.084275; amount_to_account_median_ratio 0.041884; threshold_proximity 0.029868
     calibration: calibrated — uncalibrated test ECE 0.055365 Brier 0.028709; Platt test ECE 0.006227 Brier 0.018257
     threshold 0.9910 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 16, 'fp': 1, 'fn': 94, 'tn': 1343}
  [chronological · logistic_regression + class weights] used 44 design columns; dropped []
     top: log_amount -3.795736; amount_pctl_prior 3.667104; missing:cp_count_30d_peer_ratio -2.582739; cp_count_30d_peer_ratio 2.453281; missing:amount_to_cp_median_ratio 2.264876
     permutation (validation): log_amount 0.584629; cp_count_30d_peer_ratio 0.565869; amount_pctl_prior 0.327609; pair_prior_count 0.208344; cp_prior_count 0.162662
     calibration: calibrated — uncalibrated test ECE 0.08404 Brier 0.041988; Platt test ECE 0.020369 Brier 0.022456
     threshold 0.9771 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 11, 'fp': 3, 'fn': 65, 'tn': 1375}
  [chronological · gradient_boosting + class weights] used 44 design columns; dropped []
     top: cp_count_30d_peer_ratio 0.340995; pair_share_of_payer_outflow 0.251433; log_amount 0.087791; round_level 0.061083; posting_lag_days 0.044085
     permutation (validation): cp_count_30d_peer_ratio 0.343851; pair_share_of_payer_outflow 0.279305; round_level 0.092103; log_amount 0.04375; cp_days_since_first 0.032345
     calibration: calibrated — uncalibrated test ECE 0.03437 Brier 0.017635; Platt test ECE 0.019112 Brier 0.014858
     threshold 0.9869 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 31, 'fp': 2, 'fn': 45, 'tn': 1376}
  [group · logistic_regression + class weights] used 42 design columns; dropped ['is_non_working_weekday', 'is_holiday']
     top: log_amount -4.781115; amount_pctl_prior 4.121514; missing:amount_to_account_median_ratio -3.527986; missing:cp_count_30d_peer_ratio -2.612422; payee_distinct_payers_prior -2.240297
     permutation (validation): amount_pctl_prior 0.401195; amount_to_account_median_ratio 0.302493; log_amount 0.268958; cp_days_since_first 0.217017; payee_distinct_payers_prior 0.140981
     calibration: calibrated — uncalibrated test ECE 0.068949 Brier 0.067728; Platt test ECE 0.037462 Brier 0.054822
     threshold 0.6542 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 36, 'fp': 23, 'fn': 72, 'tn': 1346}
  [group · gradient_boosting + class weights] used 42 design columns; dropped ['is_non_working_weekday', 'is_holiday']
     top: log_amount 0.55401; cp_count_30d_peer_ratio 0.179599; amount_to_account_median_ratio 0.102495; pair_share_of_payer_outflow 0.053672; round_level 0.023373
     permutation (validation): amount_to_account_median_ratio 0.213605; pair_share_of_payer_outflow 0.191273; round_level 0.133129; cp_days_since_first 0.059712; missing:cp_velocity_ratio 0.054323
     calibration: calibrated — uncalibrated test ECE 0.041001 Brier 0.049362; Platt test ECE 0.030699 Brier 0.049419
     threshold 0.3265 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 34, 'fp': 1, 'fn': 74, 'tn': 1368}

== PaySim: {'labelled': 10125, 'positives': 68, 'unlabelled': 2} · 68s · result_sha256 478c5c5b2ccbecfd63d5725fc65e538320fbc12e09f34b4549c0fdd577009fd1
labels: {"column": "isFraud", "rows_labelled": 10125, "rows_with_invalid_label": 0, "positives": 68, "note": "PaySim is a mobile-money simulation: a development proxy, not LedgerSpy's target domain."}
included features: ['log_amount', 'amount_robust_z', 'amount_pctl_prior', 'amount_to_cp_median_ratio', 'cp_prev_amount_ratio', 'cp_prior_count', 'round_level', 'is_outflow', 'pair_prior_count', 'pair_share_of_payer_outflow', 'payer_distinct_payees_prior', 'payee_distinct_payers_prior', 'is_reciprocal_pair', 'txn_type']
| Split | Model | PR-AUC [95% CI] | ROC-AUC | Precision@50 | Recall@50 | F1 @ val. threshold | Calibration |
|---|---|---|---|---|---|---|---|
| random_stratified | baseline: prevalence | 0.007 [0.003, 0.011] | 0.500 | 0.00 | 0.00 | 0.01 | n/a (ranking) |
| random_stratified | baseline: amount_only | 0.094 [0.007, 0.262] | 0.633 | 0.06 | 0.21 | 0.10 | n/a (ranking) |
| random_stratified | baseline: p8_isolation_forest | 0.023 [0.008, 0.069] | 0.683 | 0.06 | 0.21 | 0.11 | n/a (ranking) |
| random_stratified | baseline: p9_review_risk | 0.019 [0.006, 0.059] | 0.590 | 0.06 | 0.21 | 0.11 | n/a (ranking) |
| random_stratified | baseline: type_rate | 0.036 [0.017, 0.063] | 0.906 | 0.04 | 0.14 | 0.08 | n/a (ranking) |
| group | baseline: prevalence | 0.010 [0.006, 0.014] | 0.500 | 0.00 | 0.00 | 0.02 | n/a (ranking) |
| group | baseline: amount_only | 0.073 [0.011, 0.211] | 0.698 | 0.06 | 0.15 | 0.11 | n/a (ranking) |
| group | baseline: p8_isolation_forest | 0.016 [0.008, 0.042] | 0.603 | 0.04 | 0.10 | 0.07 | n/a (ranking) |
| group | baseline: p9_review_risk | 0.011 [0.007, 0.026] | 0.508 | 0.02 | 0.05 | 0.04 | n/a (ranking) |
| group | baseline: type_rate | 0.054 [0.028, 0.088] | 0.911 | 0.06 | 0.15 | 0.11 | n/a (ranking) |
| random_stratified | logistic_regression | 0.436 [0.161, 0.666] | 0.961 | 0.18 | 0.64 | 0.27 | not_attempted |
| random_stratified | logistic_regression [no_simulator_fields] | 0.157 [0.028, 0.338] | 0.902 | 0.06 | 0.21 | 0.10 | not_attempted |
| random_stratified | logistic_regression + class weights | 0.263 [0.095, 0.496] | 0.962 | 0.18 | 0.64 | 0.29 | not_attempted |
| random_stratified | logistic_regression + class weights [no_simulator_fields] | 0.138 [0.026, 0.307] | 0.898 | 0.08 | 0.29 | 0.13 | not_attempted |
| random_stratified | gradient_boosting | 0.418 [0.146, 0.688] | 0.956 | 0.16 | 0.57 | 0.25 | not_attempted |
| random_stratified | gradient_boosting [no_simulator_fields] | 0.269 [0.082, 0.585] | 0.906 | 0.14 | 0.50 | 0.26 | not_attempted |
| random_stratified | gradient_boosting + class weights | 0.327 [0.113, 0.578] | 0.960 | 0.16 | 0.57 | 0.28 | not_attempted |
| random_stratified | gradient_boosting + class weights [no_simulator_fields] | 0.207 [0.069, 0.474] | 0.908 | 0.16 | 0.57 | 0.32 | not_attempted |
| group | logistic_regression | 0.315 [0.152, 0.547] | 0.968 | 0.18 | 0.45 | 0.28 | not_attempted |
| group | logistic_regression [no_simulator_fields] | 0.223 [0.097, 0.406] | 0.963 | 0.16 | 0.40 | 0.12 | not_attempted |
| group | logistic_regression + class weights | 0.157 [0.077, 0.334] | 0.945 | 0.18 | 0.45 | 0.26 | not_attempted |
| group | logistic_regression + class weights [no_simulator_fields] | 0.207 [0.067, 0.385] | 0.941 | 0.08 | 0.20 | 0.13 | not_attempted |
| group | gradient_boosting | 0.336 [0.186, 0.607] | 0.967 | 0.26 | 0.65 | 0.42 | not_attempted |
| group | gradient_boosting [no_simulator_fields] | 0.395 [0.242, 0.670] | 0.966 | 0.28 | 0.70 | 0.49 | not_attempted |
| group | gradient_boosting + class weights | 0.326 [0.186, 0.565] | 0.973 | 0.30 | 0.75 | 0.36 | not_attempted |
| group | gradient_boosting + class weights [no_simulator_fields] | 0.338 [0.195, 0.597] | 0.965 | 0.26 | 0.65 | 0.43 | not_attempted |
splits:
   {"strategy": "random_stratified", "rows": {"train": 6075, "validation": 2025, "test": 2025}, "positives": {"train": 41, "validation": 13, "test": 14}, "excluded_rows": 0, "counterparties_in_train_and_test": 386, "scenarios_in_train_and_test": []}
   {"strategy": "chronological", "rows": {"train": 7162, "validation": 2963, "test": 0}, "positives": {"train": 66, "validation": 2, "test": 0}, "excluded_rows": 0, "counterparties_in_train_and_test": 0, "scenarios_in_train_and_test": [], "time_ranges": {"train": ["1970-01-01T00:00:00", "1970-01-01T05:00:00"], "validation": ["1970-01-01T06:00:00", "1970-01-01T06:00:00"]}}
   {"strategy": "group", "rows": {"train": 6074, "validation": 2024, "test": 2027}, "positives": {"train": 39, "validation": 9, "test": 20}, "excluded_rows": 0, "counterparties_in_train_and_test": 0, "scenarios_in_train_and_test": [], "components": 6975, "grouping": "counterparty stream ∪ injected scenario (connected components)"}
warnings: ["chronological: a split has no positives ({'train': 66, 'validation': 2, 'test': 0}); no model is evaluated on it", 'random_stratified/logistic_regression: only 14 test positives — metrics are highly uncertain']
  [random_stratified · logistic_regression + class weights] used 20 design columns; dropped ['amount_to_cp_median_ratio', 'cp_prev_amount_ratio', 'pair_prior_count', 'pair_share_of_payer_outflow', 'payer_distinct_payees_prior', 'is_reciprocal_pair']
     top: txn_type=PAYMENT -5.674742; txn_type=CASH_OUT 4.846585; txn_type=TRANSFER 4.796948; txn_type=DEBIT -3.255807; is_outflow 1.697197
     permutation (validation): amount_pctl_prior 0.140833; log_amount 0.140786; txn_type=TRANSFER 0.112397; payee_distinct_payers_prior 0.111661; missing:amount_pctl_prior 0.105425
     calibration: not_attempted — uncalibrated test ECE 0.154674 Brier 0.114119; 13 validation positives < 30: too few to fit a calibration map; the output is an uncalibrated ranking score
     threshold 0.8808 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 9, 'fp': 40, 'fn': 5, 'tn': 1971}
  [random_stratified · gradient_boosting + class weights] used 20 design columns; dropped ['amount_to_cp_median_ratio', 'cp_prev_amount_ratio', 'pair_prior_count', 'pair_share_of_payer_outflow', 'payer_distinct_payees_prior', 'is_reciprocal_pair']
     top: txn_type 0.632804; log_amount 0.103186; is_outflow 0.093441; amount_pctl_prior 0.05561; amount_robust_z 0.051919
     permutation (validation): txn_type=PAYMENT 0.089941; log_amount 0.052927; is_outflow 0.037029; amount_robust_z 0.034895; txn_type=CASH_OUT 0.02741
     calibration: not_attempted — uncalibrated test ECE 0.097594 Brier 0.063927; 13 validation positives < 30: too few to fit a calibration map; the output is an uncalibrated ranking score
     threshold 0.8830 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 8, 'fp': 36, 'fn': 6, 'tn': 1975}
  [group · logistic_regression + class weights] used 20 design columns; dropped ['amount_to_cp_median_ratio', 'cp_prev_amount_ratio', 'pair_prior_count', 'pair_share_of_payer_outflow', 'payer_distinct_payees_prior', 'is_reciprocal_pair']
     top: txn_type=PAYMENT -5.91766; txn_type=CASH_OUT 5.187198; txn_type=TRANSFER 5.119203; txn_type=DEBIT -3.670858; payee_distinct_payers_prior -2.124324
     permutation (validation): payee_distinct_payers_prior 0.102992; txn_type=PAYMENT 0.092565; txn_type=CASH_OUT 0.085246; amount_pctl_prior 0.075226; txn_type=TRANSFER 0.072889
     calibration: not_attempted — uncalibrated test ECE 0.140597 Brier 0.104899; 9 validation positives < 30: too few to fit a calibration map; the output is an uncalibrated ranking score
     threshold 0.8936 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 7, 'fp': 27, 'fn': 13, 'tn': 1980}
  [group · gradient_boosting + class weights] used 20 design columns; dropped ['amount_to_cp_median_ratio', 'cp_prev_amount_ratio', 'pair_prior_count', 'pair_share_of_payer_outflow', 'payer_distinct_payees_prior', 'is_reciprocal_pair']
     top: txn_type 0.605779; is_outflow 0.114438; log_amount 0.093941; payee_distinct_payers_prior 0.072846; amount_robust_z 0.059996
     permutation (validation): log_amount 0.21602; txn_type=PAYMENT 0.14799; amount_robust_z 0.138468; amount_pctl_prior 0.092364; payee_distinct_payers_prior 0.075918
     calibration: not_attempted — uncalibrated test ECE 0.083308 Brier 0.052012; 9 validation positives < 30: too few to fit a calibration map; the output is an uncalibrated ranking score
     threshold 0.8641 (score of the top 2.0% of validation rows (review capacity)): test confusion {'tp': 9, 'fp': 21, 'fn': 11, 'tn': 1986}
```

The interpretation of these tables (questions 1–6 and the P11 recommendation) is in the model card §Findings.

## Deviations and decisions

1. **No scikit-learn.**
   - scikit-learn and numpy are not installed, and installing them needs the network. Like P8's Isolation Forest,
     both models, Platt scaling and the metrics are implemented in pure Python and unit-tested.
   - **No dependency was added.** When an offline wheelhouse exists (P21), swapping in scikit-learn behind the same
     contract is straightforward.
   - Runtime with the default configuration: about 100 s (synthetic) and 70 s (PaySim) for all splits, models,
     settings, bootstraps and permutation importance.
2. **File layout.** The roadmap names `ledgerspy/detectors/supervised.py` and `ledgerspy train`. P10 is the
   package `ledgerspy/supervised/` (labels, dataset, splits, models, metrics, calibration, experiment, service),
   and the CLI `ledgerspy evaluate` writes experiment JSON to `<workspace>/experiments/`. `train` with a persisted
   artefact and `models/<name>/model_card.json` are P11.
3. **Cross-validation.** No repeated k-fold. The architecture suggests repeated stratified k-fold on train; P10
   uses one fixed train / validation / test split per strategy, with three strategies and cluster bootstrap
   intervals. That keeps the pure-Python runtime practical and makes the split structure explicit.
4. **Stratification uses labels.** Splits are stratified by label, a standard and documented use. A test proves
   that, once the split is fixed, test labels cannot reach the fit.
5. **Calibration judged on the test split.** Calibration is fitted on validation and its quality (ECE) is judged on
   the test split. The status is reported, not used to select anything.
6. **No API endpoint and no UI.** Results stay local JSON. Nothing in the frontend depends on P10.

## Known limitations

See the model card §Known limitations:
- tiny PaySim positive counts;
- few independent synthetic test clusters;
- our label mapping;
- undetectable positives;
- implementations not cross-checked against scikit-learn;
- fixed hyperparameters.
