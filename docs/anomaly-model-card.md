# Model Card: LedgerSpy P8 Statistical Anomaly Detection

Version: detectors `isolation_forest` v1, `robust_peer` v1, `tail_percentile` v1 · Code: `ledgerspy/anomaly/` ·
Configuration: `config/default.yaml` → `anomaly` · Acceptance record: [`p8-acceptance.md`](p8-acceptance.md).

> **Unusual is not fraudulent.** Every P8 output means "unusual relative to the stated population". It is a reason
> to look at a transaction, never evidence that it is wrong. P8 produces evidence for P9 risk fusion; it does not
> produce a risk score, a tier or a finding.

## Purpose

P8 provides label-free evidence: which transactions stand out statistically from the rest of the case's own data. It
has three independent detectors:

| Detector | Question it answers | Population |
|---|---|---|
| `isolation_forest` | Is this transaction's *combination* of amount, history, cadence and timing unusual? | All reference transactions |
| `robust_peer` | Is one behavioural value far from its peers' median? This covers amount, time gap, 30-day activity and posting hour | The transaction's own counterparty history; a GL account or transaction type if that history is too short |
| `tail_percentile` | Is one value among the most extreme of its flow? | Every outgoing (or incoming) transaction of the reference |

## Intended use

- Ranking transactions for audit sampling within one case, alongside P7 rule signals, under a fixed review capacity.
- Corporate ledgers with real posting dates (the target domain). Other ledgers run with fewer features; every
  unsupported feature is reported with the reason.

## Not intended for

- **Deciding** that a transaction is fraudulent, or that it is clean because nothing was flagged.
- Comparing scores across datasets. Scores are relative to each dataset's own reference population.
- Use as a supervised classifier. P8 never sees labels; supervised models are P10.
- Replacing P7 rules (duplicates, structuring, Benford and so on), which encode domain definitions that P8 does not.

## Inputs (features)

All inputs are **P6 registry features**, read from the feature matrix. P8 defines no feature.

**Eligibility** is checked for every configured feature on every run (`ledgerspy/anomaly/eligibility.py`). A feature is
used only if it meets all of these:

1. It is registered and implemented.
2. It is transaction-level and numeric (float, int or bool).
3. It has **row or point-in-time semantics**. Dataset (P4) and post-period features would leak the future in
   chronological mode, so they are excluded.
4. Its name is not label-like or identifier-like. This is defence in depth: the matrix cannot hold either.
5. It is computed for this dataset; otherwise it is `not_available`, with P6's reason.
6. In the reference rows it is at most 50% null (`excessive_missing`) and has at least 2 distinct values (`constant`).

Each run records the exact list of features used and excluded, with reasons.

**Isolation Forest inputs.** Each feature is listed with the reason it is eligible:

| Feature | Transform | Why it is eligible |
|---|---|---|
| `log_amount` | none | Amount level, already log-compressed |
| `amount_robust_z` | none | Point-in-time deviation from the transaction's peer baseline |
| `amount_pctl_prior` | none | Where the amount sits among earlier amounts of its flow |
| `amount_to_cp_median_ratio` | signed log1p | Deviation from the counterparty's own baseline |
| `amount_to_account_median_ratio` | signed log1p | Deviation from the GL category's baseline |
| `cp_prev_amount_ratio` | signed log1p | Sequence jump, such as small test payments followed by a large one |
| `cp_prior_count` | signed log1p | First-time and young relationships |
| `cp_days_since_prev` | signed log1p | Time gap: long means dormancy, short means a burst |
| `cp_days_since_first` | signed log1p | Relationship age |
| `cp_count_7d`, `cp_count_30d` | signed log1p | Recent activity |
| `cp_velocity_ratio` | signed log1p | Recent pace against the counterparty's own pace |
| `cp_count_30d_peer_ratio` | signed log1p | Recent activity against peers in the same account |
| `cp_log_amount_trend` | none | Escalation |
| `hour_deviation` | none | Posting time against the usual time |
| `payer_new_payees_7d` | signed log1p | A burst of brand-new counterparties (fan-out) |
| `pair_share_of_payer_outflow` | none | Concentration of the payer's outflow |

**Deliberately not used:**

| Feature(s) | Why |
|---|---|
| `amount_minor` | Duplicates `log_amount` on a raw scale |
| `round_level` | A rare discrete code: Isolation Forest isolates rare categories regardless of context. Roundness is judged in context by P7 `round_amount` |
| `threshold_proximity` | Policy-relative; owned by P7 `structuring` |
| `is_*` flags | Owned by P7's timing rules |
| `dq_*` | Data quality is P4's domain, and these have dataset semantics |
| `txn_type`, `day_of_week` | Categorical; `txn_type` is used as a peer group instead |
| Entity-level and post-period features | Not point-in-time |
| Identifiers, labels, P7 signal outputs | Never used. P8 stays an independent detector family |

**Robust peer features** are listed in order of peer preference:

| Feature | Peers | Direction | Resolution floor | Why |
|---|---|---|---|---|
| `log_amount` | counterparty, then account, then txn_type | both | 0.1 log units (≈10%) | Amount against what this vendor (or category) is usually paid |
| `cp_days_since_prev` | counterparty | both | 0.1 log units | Gap against the vendor's usual cadence |
| `cp_count_30d` | counterparty | higher | 0.1 log units | 30-day activity against the vendor's usual activity |
| `hour_of_day` | counterparty | both | 0.5 h | Posting time against the vendor's usual time |

**Tail features** use flow populations: `amount_minor`, `amount_to_cp_median_ratio`,
`amount_to_account_median_ratio`, `cp_velocity_ratio`, `cp_days_since_prev` and `hour_deviation`, all looking at
the high tail.

**Missing values (Isolation Forest).** A null is replaced by the feature's median over the reference rows, and a
`missing:<feature>` indicator column is added whenever some (not all) reference rows are null. Evidence always
shows the original null and lists the imputed features. A row with fewer than half of the model's features present
is not scored (`insufficient_data`), and no score is invented for it.

## Reference population and leakage controls

- **`dataset` mode** (default). The model is fitted on the case's own transactions, and every eligible row is both
  reference and scored. This is transductive by design: the audit question is what is unusual in this period
  (architecture §5.3).
- **`chronological` mode.** The earliest `chronological_fraction` (default 50%) of rows by posting time is the
  reference: the forest, imputation medians, peer medians and MADs, tail cutoffs and feature eligibility all come
  from it alone. Only rows at or after the split timestamp are scored. A test rewrites every row after the split and
  checks that the forest hash and the tail cutoffs are unchanged.
- **Warm-up.** With real calendar dates, rows in the first `warmup_days` (90) are neither reference nor scored, and
  are reported as `insufficient_data: warm_up`. Point-in-time history is not representative there, because every
  long-standing vendor looks new. This follows the same reasoning as P7's `new_vendor_large`. The warm-up is not
  applied to simulation steps (PaySim), and the report says so.
- **Labels and ground truth.**
  - They are never read in the detection path. `FeatureRow` has no labels or `extra`, and a static test scans
    `ledgerspy/anomaly` for label, ground-truth and `extra` access and for P7 signal use.
  - Flipping every `isFraud` and `isFlaggedFraud` value in PaySim gives identical scores, signals and forest hash.
  - Adding label-like `extra` columns to a ledger gives an identical report.
  - Labels and P5 ground truth are read only by the evaluation code, after detection.

## Algorithms and thresholds

**Isolation Forest** (Liu, Ting & Zhou 2008), pure Python with no new dependency:

- **Settings:** `random_state` 42, `n_estimators` 100 (path lengths converge well before 100 trees), ψ =
  `max_samples` 256, height limit ⌈log₂ ψ⌉ = 8.
- **Splits:** a uniformly chosen feature that is not constant in the node, split uniformly in [min, max).
- **Score:** s(x) = 2^(−E[h(x)]/c(ψ)) in (0, 1]. **Higher means more unusual** throughout the API (scikit-learn's
  `score_samples` is −s). A score of s ≈ 0.5 everywhere means nothing is distinct.
- **Threshold policy** (instead of scikit-learn's `contamination`, which would label a fixed share of every dataset
  as anomalous). A row is flagged when all three hold:
  - its mid-rank percentile among the reference scores is at least 1 − `tail_fraction` (0.01);
  - its score is above `min_score` (0.5);
  - it is among the `max_flagged` (100) highest scores, the review capacity.

  So a dataset with nothing distinct flags little, and 100k rows never flag more than can be reviewed.
- **Determinism:**
  - sub-samples come from the reference rows sorted by their model-input vectors, with a single seeded generator;
  - the fitted forest has a `model_sha256`.

  The same feature values and configuration therefore give the same forest and the same scores, whatever the row
  order, IDs or label columns. Tests cover this for the fixed seed, shuffled rows, a re-import into another
  workspace and flipped labels.
- **Explanation:** a robust z of each model input against the reference rows, labelled *feature deviation (not
  SHAP)* (architecture §5.5).

**Robust peer z:**

- **Formula:** z = (x − median)/scale, where scale = max(1.4826·MAD (or 1.2533·mean absolute deviation when
  MAD = 0, after Iglewicz & Hoaglin), `min_scale`).
- **Threshold:** |z| ≥ 3.5, the Iglewicz–Hoaglin cut-off for modified z-scores.
- **Peer groups:** a group needs `min_group_size` (20) reference values. The first sufficient peer in the
  configured order is used, and the basis is counted per feature. A row with no sufficient group is
  `insufficient_data`; there is never a global fallback.

**Tail percentile:**

- **Percentile:** mid-rank, (values below + ½·values equal)/n, within each flow of the reference.
- **Tails:** top 1% and top 0.5%. Every signal reports the population size, percentile, observed value, cutoff,
  feature and population.
- **Minimum population:** 200, the smallest population in which the 0.5% tail holds a row.
- **Ties:** tied values share a percentile, so a block of ties never fills a tail.

**Strength** (a provisional input to P9) follows architecture §5.3: s = clip((p − p0)/(1 − p0), 0, 1), with
p0 = 1 − tail. Robust-peer strength is 1.0 when the detector fires. P9 calibrates both strength and weight.

## Evaluation methodology

`ledgerspy/anomaly/evaluation.py` provides the evaluation functions. They are pure functions of scores and of
labels passed in by the caller:

- precision@k and recall@k. Recall counts unscored positives as missed;
- PR-AUC as average precision, with tied scores treated as a single threshold;
- top-k scenario coverage;
- score distributions.

How they are applied:

- **k** comes from `anomaly.evaluation.review_capacity` (50, 100, 250).
- **Accuracy is never reported.** At 1.7% (synthetic) or 0.7% (PaySim) prevalence, it would be meaningless.
- **Real ledgers without labels get no precision or recall.** The report carries only label-free score
  distributions and top-k lists.
- **Synthetic validation targets** are fixed from the scenario definitions before any P8 run (12 scenarios that are
  genuine statistical outliers in amount, velocity, dormancy, timing or vendor behaviour). This is recorded in
  `tests/backend/test_anomaly_datasets.py::P8_TARGETS`.
- **Baselines.** Detection is not tuned to maximise these numbers. They are recorded as a baseline so that later
  changes are visible.

## Results

### Synthetic multi-year ledger

7,271 rows; 535 are in the warm-up. Full tables are in [`p8-acceptance.md`](p8-acceptance.md).

| Detector | Flagged rows | Target scenarios hit | PR-AUC (targets) | Prevalence | P@50 | Target scenarios in top 50 / 100 / 250 |
|---|---|---|---|---|---|---|
| `isolation_forest` | 67 | 6/12 | 0.037 | 1.7% | 8% | 5 / 6 / 9 |
| `robust_peer` | 123 | 7/12 | 0.026 | 1.7% | 6% | 5 / 6 / 7 |
| `tail_percentile` | 354 | 9/12 | 0.131 | 1.7% | 34% | 9 / 9 / 9 |
| any P8 detector | 462 | **12/12** | — | — | — | — |

Against *all* injected scenarios, including non-statistical ones, the Isolation Forest's top 50 is 46% injected rows,
against a 12.6% base rate. Many of its top rows are pass-through fan-out (S-P-02), circular-trading (S-O-0x) and
look-alike vendors (S-E-0x). These are new parties behaving unlike established ones. The look-alike controls of the
P8 targets stay below 25% for every detector: S-U-01 and S-U-02 (rents and retainers), S-U-04 (ordinary new vendor),
S-U-06 (seasonal velocity) and S-U-08 (period-end overtime).

### PaySim 10k sample (proxy only)

**PaySim is a proxy development dataset, not LedgerSpy's target domain.** It is a mobile-money simulation with
simulated time steps, no counterparty history to speak of, and accounts that are account holders rather than GL
categories.

**What runs:**

| Detector | Features used | Rows scored |
|---|---|---|
| Isolation Forest | 4 (`log_amount`, `amount_robust_z`, `amount_pctl_prior`, `cp_prior_count`) | 10,118 |
| Robust peer | `log_amount`, against transaction-type peers (counterparty groups are too small) | 10,121 |
| Tail | `amount_minor` | 10,121 |

13 Isolation Forest features are excluded, each with its reason (no calendar time; excessive missingness). The
Isolation Forest scores have median 0.46, 99th percentile 0.65 and maximum 0.70.

**Label diagnostics** (68 positives, prevalence 0.67%, labels read after detection):

| Score | PR-AUC | P@100 | R@250 |
|---|---|---|---|
| `isolation_forest` | 0.013 | 1% | 10.3% |
| `robust_peer` | 0.028 | 3% | 7.4% |
| `tail_percentile` | 0.060 | 4% | 14.7% |

All three are above the prevalence and far from a useful fraud classifier. These numbers say that the pipeline runs
on a second schema without leakage. They say nothing about fraud detection in corporate ledgers.

## Performance

| Data | P6 features | P8 | Total |
|---|---|---|---|
| Synthetic, 7,271 rows | ≈ 1.3–1.9 s | ≈ 0.9 s | ≈ 2–3 s |
| PaySim, 10,127 rows | ≈ 1.1–1.7 s | ≈ 1–2 s | ≈ 2–4 s |
| Synthetic, 99,797 rows (scale 14, 4 years) | ≈ 30 s | ≈ 18–21 s | ≈ 50 s |

The roadmap target is < 60 s at 100k rows. The test guard allows 120 s for CI headroom. In pure Python, Isolation
Forest scoring is O(n · trees · depth). Nothing in P8 is O(n²).

## Limitations and known failure modes

- **Only the case's own data.** Scores are within-dataset. A uniformly fraudulent stream looks normal.
- **New counterparties dominate the forest.** First rows have few prior observations, so missing-indicator columns and
  `cp_prior_count = 0` make them easy to isolate. This is real "first-time activity", but legitimate new parties
  appear too: the instalment control S-U-05 has 3 of 3 rows flagged and the multi-party chain S-U-09 has 6 of 6.
- **Bimodal cadences break robust z.** Both group companies pay one "payroll clearing a/c" minutes apart, so the
  stream's gaps alternate between 30 days and 0 days, and 32 of 72 payroll rows (S-U-12) are flagged on
  `cp_days_since_prev`.
- **The tail always contains the largest payments.** Payroll (33 of 72) and the canteen contractor (27 of 36, high
  against its account median) sit in the top 1% legitimately. Tail signals have low severity for this reason.
- **Missed by individual detectors**, but seen by at least one:
  - **Isolation Forest** misses S-A-01, S-H-01, S-H-03, S-I-01, S-K-01 and S-K-03: single-feature extremes inside
    long-established streams are diluted among 17 inputs.
  - **Robust peer** misses S-G-01, S-L-01 and S-Q-01: the counterparty groups are too young or small, and the
    account groups are dispersed. It also misses S-H-03 and S-K-03.
- **Gradual escalation (S-Q-01)** is only caught where its new-vendor rows stand out. A slow drift moves its own
  baseline.
- **Names, not resolved entities.** Counterparty peer groups use P6's normalised names, not resolved entities (P12).
- **The warm-up hides the first 90 days.** Anomalies there are not scored by P8; P7 rules still run.
- **Pure-Python Isolation Forest.** It is faithful to the paper but has no SHAP explanations. Explanations are
  feature deviations, and they are labelled as such.
