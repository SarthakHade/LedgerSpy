# Model Card: LedgerSpy P10 Supervised Models (experimental)

Code: `ledgerspy/supervised/` · Configuration: `config/default.yaml` → `supervised` · CLI:
`ledgerspy evaluate --dataset-id D-… --labels paysim|synthetic [--ground-truth FILE]` ·
Acceptance record: [`p10-acceptance.md`](p10-acceptance.md).

> **A model ranking score, not a probability of fraud.** P10 trains and evaluates supervised models as an
> *optional, experimental* evidence layer. No model is persisted (P11), and none feeds the P9 review-risk score,
> any API endpoint or the UI. An output is called a *calibrated score* only when its calibration status is
> `calibrated`. Even then it is an estimated positive rate in data like the evaluation split, never the
> probability that a given transaction is fraudulent.

## Intended use

- Measuring whether supervised models trained on labelled data rank suspicious transactions better than
  LedgerSpy's label-free forensic pipeline (P7 rules, P8 anomalies, P9 fusion) at a fixed review capacity.
- Producing reproducible, leakage-checked experiment records that P11 can build on.

## Not intended for

- Scoring a client ledger. There are no labels from the target domain, and these models are not deployed.
- Replacing or overriding P7/P8/P9. LedgerSpy works entirely without a supervised model.
- Claims of fraud probability, accuracy, or performance on real corporate ledgers.

## Training data and labels

Labels enter **only** through `ledgerspy/supervised/labels.py`. A test checks that it is the only module in the
package that reads the `labels` table, and that P6–P9 do not import the supervised package.

| Dataset | Label | Positives | Notes |
|---|---|---|---|
| PaySim 10k sample | `isFraud`, from the `labels` table P3 filled. `isFlaggedFraud` is never used | 68 of 10,125 (0.67%) | A mobile-money **simulation**, used as a development proxy. Not LedgerSpy's corporate-ledger domain |
| P5 synthetic ledger | Rows of injected scenarios with severity ≥ medium, except `digit_distribution_anomaly` (the ground truth says S-T-01 is "not labelled as fraud"). Controls and all other rows are 0 | 549 of 7,271 (7.6%), in 34 scenarios | Injected patterns; S-R-01 alone is 343 positives. The ground-truth file is passed explicitly, and scenario IDs are split metadata only |

## Features (contract, version 1)

Every feature comes from the **P6 registry** (`supervised.features`). Each is checked against the registry: it
must be implemented, at transaction level, and have row or point-in-time semantics. Post-period and dataset (P4)
features are excluded because they contain future or whole-period information. Each experiment records the
contract (status and reason per feature) and its SHA-256, together with the feature-registry hash.

| Feature | Transform | Synthetic | PaySim |
|---|---|---|---|
| log_amount, amount_robust_z, amount_pctl_prior | none | ✔ | ✔ |
| amount_to_cp_median_ratio, cp_prev_amount_ratio | signed log1p | ✔ | dropped (> 50% null in training) |
| amount_to_account_median_ratio | signed log1p | ✔ | not available (account is not a category) |
| cp_prior_count | signed log1p | ✔ | ✔ |
| cp_days_since_prev, cp_days_since_first, cp_count_7d, cp_count_30d, cp_velocity_ratio, cp_count_30d_peer_ratio | signed log1p | ✔ | not available (simulated time) |
| cp_log_amount_trend, hour_of_day, hour_deviation | none | ✔ | not available |
| is_after_hours, is_non_working_weekday, is_holiday | none | ✔ (dropped where constant in training) | not available |
| days_to_month_end, days_to_fiscal_year_end, posting_lag_days | none / signed log1p | ✔ | not available |
| round_level, threshold_proximity | none | ✔ | round_level ✔ |
| is_outflow | none | ✔ | ✔ (derived from the PaySim type; removed in the ablation) |
| pair_prior_count, pair_share_of_payer_outflow, payer_new_payees_7d, payer_distinct_payees_prior, payee_distinct_payers_prior, is_reciprocal_pair | signed log1p for counts | ✔ | payee_distinct_payers_prior ✔, the others constant or null |
| txn_type (one-hot, categories learnt on training) | — | not mapped | ✔ (removed in the ablation) |

**Never features:**
- transaction, entity and invoice identifiers;
- labels and anything derived from them;
- P7 signals, P8 anomaly scores and P9 risk scores (used only as *baselines*);
- P5 scenario IDs;
- post-period / dataset-level P6 features.

**Preprocessing, fitted on the training split only:**
- median imputation, with a missing indicator when training rows have nulls;
- a feature is dropped (and recorded) when it is constant or more than 50% null in training;
- standardisation for logistic regression;
- one-hot over training categories, so an unseen category is all zeros.

## Models

Both models are pure Python, because no scikit-learn or numpy is installed and adding them requires a network
install. They are standard algorithms, unit-tested.

| Model | Algorithm | Configuration |
|---|---|---|
| `logistic_regression` | L2 logistic regression with an unpenalised intercept, fitted by Newton/IRLS to the optimum. A test checks the gradient is zero there | l2 1.0, max 50 iterations, tol 1e-8 |
| `gradient_boosting` | Histogram gradient-boosted trees, the HistGradientBoostingClassifier algorithm: logistic loss, ≤ 32 quantile bins, depth-wise trees with Newton gain, a minimum leaf size and L2 on leaf values. No randomness | 100 trees, learning rate 0.1, depth 3, min leaf 20, L2 1.0 |

The seed is 42 (splits); bootstrap seed 7.

**Imbalance:** `none` vs `class weights (balanced)`, with weights from training counts only. No oversampling and
no SMOTE anywhere; validation and test rows are never weighted or resampled.

## Splits (split first; nothing is fitted before)

| Strategy | Definition | Leakage it prevents | Leakage it leaves (measured per run) |
|---|---|---|---|
| `random_stratified` | Rows shuffled within each class, 60 / 20 / 20 | — | Counterparties and scenarios on both sides |
| `chronological` | Posting-time order; equal timestamps stay together | Future rows in training | Counterparties and long-running scenarios span the cut |
| `group` | Connected components of rows sharing a counterparty stream **or** an injected scenario (union–find), assigned whole; positive and negative components cut separately | Same counterparty or same scenario on both sides | None measured |

**Bootstrap clusters:**
- `group` resamples whole components;
- `chronological` resamples days (real or simulated);
- `random_stratified` resamples rows.

Rows of one cluster are not independent, so intervals on grouped splits are wide. With 34 test components (the
synthetic grouped split), they are very wide.

## Threshold and calibration

- **Operating threshold.** The score of the top **2%** of *validation* rows, which is the review capacity. The
  test split is evaluated **once** at that threshold: confusion matrix, precision, recall and F1. Tests confirm
  that changing test or validation labels never changes the model or the threshold.
- **Calibration.** Platt scaling, fitted on validation. Isotonic is not offered, because it needs about 1,000
  positives. Calibration is attempted only with ≥ 30 validation positives. The resulting status (calibration ECE
  measured on the test split, limit 0.05) is one of:
  - `calibrated`;
  - `not_validated`;
  - `not_attempted`: the output remains a ranking score.

## Metrics

PR-AUC (average precision with tied scores as one threshold) is the primary measure. Also reported:
- ROC-AUC;
- precision@k and recall@k (k = 25, 50);
- F1 and the confusion matrix at the validation threshold;
- Brier score and ECE;
- 95% percentile bootstrap intervals (500 resamples) for PR-AUC and precision/recall@k.

Accuracy is never reported.

**Baselines,** scored on the same test rows:
- prevalence (a constant score);
- amount-only;
- the P8 Isolation Forest score;
- the P9 review-risk score;
- the per-type positive rate from the training split (PaySim).

## Results (default configuration, test split; full tables in [`p10-acceptance.md`](p10-acceptance.md))

**Synthetic ledger:**

| Split | Model | PR-AUC [95% CI] | ROC-AUC | Precision@50 | Recall@50 | F1 | Calibration |
|---|---|---|---|---|---|---|---|
| group | baseline: prevalence | 0.073 | 0.500 | 0.02 | 0.01 | 0.14 | — |
| group | baseline: amount only | 0.096 | 0.629 | 0.14 | 0.06 | 0.00 | — |
| group | baseline: P8 Isolation Forest | 0.117 | 0.591 | 0.16 | 0.07 | 0.10 | — |
| group | baseline: P9 review risk | 0.446 [0.252, 0.649] | 0.777 | 0.48 | 0.22 | 0.26 | — |
| group | logistic regression + class weights | 0.405 [0.015, 0.631] | 0.629 | 0.70 | 0.32 | 0.43 | calibrated |
| group | gradient boosting + class weights | 0.475 [0.035, 0.714] | 0.760 | 0.74 | 0.34 | 0.48 | calibrated |
| random | gradient boosting | 0.878 [0.807, 0.938] | 0.977 | 0.96 | 0.44 | 0.29 | calibrated |
| chronological | gradient boosting | 0.864 [0.773, 0.939] | 0.970 | 0.96 | 0.63 | 0.58 | calibrated |

**PaySim proxy:**

| Split | Model | PR-AUC [95% CI] | ROC-AUC | Precision@50 | Recall@50 | F1 | Calibration |
|---|---|---|---|---|---|---|---|
| group | baseline: prevalence | 0.010 | 0.500 | 0.00 | 0.00 | 0.02 | — |
| group | baseline: amount only | 0.073 | 0.698 | 0.06 | 0.15 | 0.11 | — |
| group | baseline: type rate | 0.054 | 0.911 | 0.06 | 0.15 | 0.11 | — |
| group | baseline: P8 / P9 | 0.016 / 0.011 | 0.603 / 0.508 | 0.04 / 0.02 | — | — | — |
| group | logistic regression | 0.315 [0.152, 0.547] | 0.968 | 0.18 | 0.45 | 0.28 | not attempted |
| group | gradient boosting + class weights | 0.326 [0.186, 0.565] | 0.973 | 0.30 | 0.75 | 0.36 | not attempted |
| group | gradient boosting, no simulator fields | 0.395 [0.242, 0.670] | 0.966 | 0.28 | 0.70 | 0.49 | not attempted |
| random | logistic regression | 0.436 | 0.961 | 0.18 | 0.64 | 0.27 | not attempted |
| random | logistic regression, no simulator fields | 0.157 | 0.902 | 0.06 | 0.21 | 0.10 | not attempted |

The PaySim chronological split is not evaluated: the sample's fraud rows lie almost entirely in its first
simulated hours (66 train, 2 validation, 0 test positives), so the split has no test positives.

## Findings

1. **Does supervised ML materially improve ranking?**
   - **Synthetic: no evidence, once leakage is removed.** On the grouped split, gradient boosting (0.475) and
     logistic regression (0.405) are statistically indistinguishable from the label-free P9 score (0.446). Their
     intervals span almost the whole range with 34 test components.
   - **Synthetic, row-level splits look excellent but are inflated.** The random and chronological splits score
     0.78–0.88 because scenarios such as S-R-01 (343 rows) appear on both sides, so the model recognises patterns
     it has already seen.
   - **PaySim: yes.** Gradient boosting reaches PR-AUC about 0.33–0.40 against ≤ 0.07 for every label-free
     baseline, on a counterparty-isolated split.
2. **At the same review capacity, are precision and recall better?**
   - **PaySim:** at k = 50 on the grouped split, gradient boosting finds 13–15 of 20 positives (precision 0.26–0.30,
     95% CI 0.16–0.42), against 1–3 for the baselines.
   - **Synthetic, grouped split:** 35–38 hits against 24 for P9, but the intervals overlap completely.
3. **Robust across splits?**
   - **Synthetic: no.** The gain appears only where scenarios leak across the split.
   - **PaySim:** the grouped and random splits agree on the ordering. The tiny positive counts (14–20 per test
     split) keep every interval wide.
4. **Simulator-specific leakage?**
   - **Yes, for logistic regression on PaySim.** The transaction type dominates its coefficients (TRANSFER /
     CASH_OUT up, PAYMENT / DEBIT down). Removing type and type-derived direction cuts PR-AUC from 0.44 to 0.16
     (random split).
   - **Type alone** (type-rate baseline) reaches ROC-AUC 0.91.
   - **Gradient boosting is barely affected** (0.33 → 0.40 on the grouped split). It relies on amount and on
     `payee_distinct_payers_prior`, which in PaySim reflects the simulator's account structure (merchants have
     many payers, mule accounts few). That is another proxy, not a transferable fraud signal.
5. **Is calibration trustworthy?**
   - **PaySim: no.** It was not attempted (9–13 validation positives), so outputs are uncalibrated ranking scores.
   - **Synthetic:** Platt scaling lowers the test ECE (e.g. 0.13 → 0.01) on the random and chronological splits.
     Those splits are leaky, though, and on the grouped split two of the four runs fail validation (both unweighted models).
   - **Overall:** calibration is not trustworthy for anything beyond these experiments.
6. **What should P11 persist?** See the recommendation below. Not a production model chosen by its best metric.

## Recommendation for P11

- **Persist the experiment pipeline, not a "fraud model".** Store:
  - the feature contract and its hash;
  - the fitted preprocessor;
  - the model parameters with `model_sha256`;
  - the validation-derived threshold policy;
  - the calibration status;
  - the experiment record, as a hash-verified artefact with this card.
- **Load it only as an optional, clearly labelled evidence source** ("model ranking score, experimental").
  Integrate it into P9 only after it beats the label-free score on a leakage-safe split of labelled data from the
  target domain.
- **Candidate:** class-weighted gradient boosting with the simulator fields removed. It was the most stable across
  splits and the least dependent on simulator structure.
- **Do not persist a PaySim-trained model for client ledgers.** Its domain does not transfer.

## P11: persisted artefacts

P11 persists single P10 runs as hash-verified artefacts ([`model-lifecycle.md`](model-lifecycle.md)), following the
recommendation above.
- **What an artefact holds:** contract, preprocessor, model with its hash, validation threshold, calibration status,
  evaluation record and this card's essentials.
- **What persistence reproduces:** a persisted model reproduces its P10 run exactly (same `model_sha256`, same test
  PR-AUC).
- **Status:** PaySim and synthetic artefacts are `experiment` by policy. They cannot become candidates, approved or
  active.
- **Output:** an uncalibrated ranking score, never fused into P9.

## Known limitations

- **Few positives.** There are 68 PaySim positives (14–20 per test split), so the intervals are wide. The full
  public PaySim set would give credible numbers.
- **Synthetic labels are injected patterns.** Grouping by scenario removes memorisation but leaves very few
  independent test clusters (34).
- **Positive mapping is ours.** Severity ≥ medium, excluding the digit scenario, is a documented choice; another
  mapping would change the numbers.
- **Undetectable positives.** Some positives cannot be seen in ledger rows at all (bank mismatches, look-alike
  vendors), which caps recall.
- **Pure-Python implementations.** They are tested but not cross-checked against scikit-learn in this
  environment.
- **Few hyperparameter settings.** Two imbalance settings and fixed hyperparameters: a trustworthy baseline, not a
  tuned model.
