# LedgerSpy — Forensic Rules and Benford Analysis (P7)

P7 adds the first detection layer: ten deterministic, explainable rules and a Benford module. They turn the P6
feature registry into **signals**. Each signal is a reason to review a transaction, counterparty or group, with the
evidence and the feature values behind it.

A signal is never a fraud finding. Risk fusion, tiers and the UI belong to P9.

**Where the code lives:**
- `ledgerspy/detectors/rules.py`: the rules (the roadmap's `detectors/` package).
- `ledgerspy/forensics/`:
  - `models.py`: signal and outcome types;
  - `duplicates.py`: duplicate grouping;
  - `benford.py`: Benford tests;
  - `service.py`: the dataset report.
- The API is `POST /api/datasets/{id}/forensics`: token-protected, read-only, nothing stored.

## Principles

| Principle | How |
|---|---|
| **One source of features** | Rules read P6 registry features (`run.transactions/entities.columns`), and the duplicate rules read the canonical fields of P6 `FeatureRow`s. P7 needed three new inputs, and added them to the P6 registry (`is_off_hours`, `cp_prior_offhours_months`, `dq_balance_discontinuity`) rather than computing them inside a detector |
| **No labels, no ground truth** | Rules never read `extra`, the `labels` table, files or P5 ground truth. Tests: a static scan, flipped PaySim labels give the same signals, and the synthetic ledger is imported without `ground_truth/` |
| **No mutation** | The report is computed in memory. A test hashes `transactions`, `labels`, `integrity_issues` and `normalized_values` before and after a run, and checks `total_changes` |
| **Config-driven** | Every threshold lives in `config/default.yaml` → `rules.<rule_id>` and `benford`, validated by the P2 schema (`RulesConfig`, `BenfordConfig`). The only constants in code are mathematical ones (1.4826, the Benford law) |
| **Honest availability** | Each rule declares the features and dataset capabilities it needs. If they are missing, the rule status is `not_available`, or `not_applicable` when the concept doesn't apply (no running balance), with the P6/P4 reason. A subject whose needed value is null is counted as **insufficient** and never judged on a substitute |
| **Deterministic** | No randomness. Iteration follows sorted keys, and ties are ordered by (timestamp, source_row, txn_id). `signal_id` = hash(rule, version, subject). `report_sha256` covers everything. Shuffled input gives an identical report (tested) |
| **Efficient** | No all-pairs scans. Rules group by key, sort, use sliding windows and bisect. The synthetic ledger (7,271 rows, 3 years) takes about 2 s for features + rules + Benford |

## Signal

Every signal (`ForensicSignal.to_dict()`) has these fields:
- `signal_id`, `rule_id`, `rule_version`, `code` (`<family>.<rule_id>`), `family`;
- `subject_type` (`transaction`, `entity` or `group`), `subject_id`;
- `txn_ids` — every row the evidence points at;
- `triggered`, `severity`, `strength`, `weight`;
- `evidence`, `features` (the P6 values used), `explanation`.

Two of these are **placeholders for P9:**
- `strength` is binary (1.0 when a rule fires);
- `severity` and `weight` come from config.

`to_canonical()` converts a signal into the architecture's `ledgerspy.canonical.Signal` (§5.4), which P9 fuses. P7 added `SubjectType.GROUP` for Benford subgroups and period-end clusters.

## The rules

| Rule (family) | Fires when | Inputs (P6) | Config (`rules.*`) | Not available / insufficient | Evidence |
|---|---|---|---|---|---|
| `duplicate_exact` (duplication) | ≥ 2 rows share payer, payee, amount, direction, posting date and normalised reference (if references are mapped). Every row after the first of the group is signalled | `amount_minor` + canonical fields; timestamp, counterparty | severity, weight | No timestamp/counterparty → not_available. Rows lacking a key field → insufficient. A simulated clock compares the exact step, not a "date" | group ID, original, all members, matched attributes |
| `duplicate_near` (duplication) | A later row to the same payee and direction within `window_days` (14). The amount matches (equal, or two adjacent digits transposed; plus `amount_relative_tolerance`, 0 by default). The reference matches (equal after normalisation, an added letter, or two adjacent characters transposed). A changed digit is *not* a match, because consecutive invoices are different bills. Without references, the amount must be equal | `amount_minor` + canonical fields; calendar time | window_days, amount_relative_tolerance | No real calendar (PaySim) → not_available | each earlier match with its amount/reference match type and days apart |
| `round_amount` (amount, entity) | A counterparty stream with ≥ `min_rows` (6) amounts and round share ≥ `min_round_share` (0.5), at multiples of 10^`features.round_level_min` (₹1,000). It also needs ≥ `min_distinct_round_amounts` (4) different round values, because one contractual amount repeated is not fabrication, and a one-sided binomial p ≤ `max_p_value` (0.001) against its flow's round rate | `round_level`, `ent_round_share` | min_rows, min_round_share, min_distinct_round_amounts, max_p_value | Streams under `min_rows` → insufficient | round rows, share, population rate, distinct values, p-value, examples |
| `structuring` (structuring) | ≥ `min_parts` (2) rows of one counterparty within `window_days` (7). Each part is "just below" the same configured approval threshold (`threshold_proximity` ≥ `min_proximity`, 0.95), and together they exceed it | `threshold_proximity`, `amount_minor` | window_days, min_parts, min_proximity | No case approval thresholds, or not a corporate ledger → not_available. **No regulatory threshold is assumed** | threshold name and amount, parts, aggregate, span |
| `temporal` (timing) | An off-hours posting (`is_off_hours`: after business hours, a non-working weekday or a configured holiday) to a counterparty for which off-hours work is not routine: `cp_prior_offhours_months` < `routine_after_months` (2). The first two months of recurring overtime are flagged, later ones are not | `is_off_hours`, `cp_prior_offhours_months`, plus the detail flags | routine_after_months | No real time of day → not_available | conditions, prior off-hours months, hour features |
| `new_vendor_large` (entity) | A payment (`is_outflow`) to a counterparty with ≤ `max_prior_count` (3) earlier rows whose `amount_robust_z` ≥ `min_robust_z` (3.0). The baseline must be a **peer** level (`peer_bases`: account, txn_type), because a new counterparty has no own baseline and the global one mixes unlike payments | `cp_prior_count`, `amount_robust_z` (+ basis), `is_outflow`; calendar time | max_prior_count, min_robust_z, min_days_observed, peer_bases | Rows in the first `min_days_observed` (90) days → insufficient, since every long-standing vendor looks new there. A global or unavailable baseline → insufficient. A simulated clock → not_available | prior count, z, peer basis |
| `balance_continuity` (data quality) | P4 found a running balance that does not follow from the previous balance ± amount (`dq_balance_discontinuity`). P4 does the arithmetic. Kinds in `ignore_kinds` (PaySim's documented simulator artefacts: unchanged and zeroed balances) are not signalled | `dq_balance_discontinuity` + P4 issue details | ignore_kinds | No running balance → **not_applicable**, with P4's reason. No current P4 run → not_available | the P4 issue (before/after/expected/kind, message) |
| `dormancy` (entity) | The first row after ≥ `min_dormant_days` (180) of silence (`cp_days_since_prev`) at ≥ `min_amount_ratio` (2.0) × the counterparty's earlier median (`amount_to_cp_median_ratio`) | `cp_days_since_prev`, `amount_to_cp_median_ratio` | min_dormant_days, min_amount_ratio | Too little earlier history for a median → insufficient | dormant days, ratio |
| `period_end` (timing, group) | ≥ `min_cluster` (3) rows of one counterparty in the last `window_days` (3) of a period (`period`: fiscal_year or month). The count must be ≥ `min_concentration` (5) × the stream's normal rate for such a window, and the median amount ≥ `min_amount_ratio` (1.5) × the stream's median. **Post-period** analysis: it uses the stream's whole history | `days_to_fiscal_year_end` / `days_to_month_end`, `amount_minor`, `ent_median_amount_minor`, `ent_txn_count` | period, window_days, min_stream_rows, min_cluster, min_concentration, min_amount_ratio | Streams under `min_stream_rows` (10) → insufficient. No calendar → not_available | period end, rows in window, expected count, concentration, medians |
| `velocity` (timing) | `cp_velocity_ratio` ≥ `min_velocity_ratio` (4), with ≥ `min_count_30d` (4) earlier rows in 30 days. The burst must also be unusual at the counterparty's own past rate: a Poisson upper tail ≤ `max_p_value` (0.0001), where the expected count = `cp_count_30d` / `cp_velocity_ratio`. The p-value is strict because ~10⁴ rows are tested; n × p < 1 keeps chance alarms rare | `cp_velocity_ratio`, `cp_count_30d`, `cp_amount_sum_30d` | min_velocity_ratio, min_count_30d, max_p_value | Less than 90 days / 5 rows of history (P6 gates) → insufficient. Simulated clock → not_available | expected count, Poisson p, 30-day sum |

## Benford analysis

Implemented in `ledgerspy/forensics/benford.py` (architecture §7).

- **Tests:**
  - first digit (1–9), expected `log10(1 + 1/d)`;
  - first two digits (10–99), expected `log10(1 + 1/dd)`;
  - last two digits (00–99), expected uniform 1/100.
- **Extraction, with every exclusion counted:**
  - missing → `missing`;
  - zero → `zero`;
  - negative → `negative` (or `negative_amounts: absolute` uses |x|);
  - first-digit tests need |amount| ≥ `min_value_major` (₹10), otherwise `below_minimum`;
  - the last-two test needs whole units ≥ `last_two_min_major` (₹100), so the digits are trailing ones.
  - Leading digits are read from the minor-unit integer, which equals the major-unit digits. Source values are never changed.
- **Statuses:**
  - `analyzed`;
  - `insufficient_data` (n < `min_n`: 300 for first digit, 1,000 for first two and last two);
  - `invalid_population` (fewer than `min_distinct_values` = 10 distinct amounts: degenerate);
  - `not_applicable` (no amounts, or the subgroup dimension doesn't exist for the dataset).
  - Warnings flag a small sample (< `warn_n`) and a narrow range (max/min < 100: "not expected to follow Benford; a deviation may only reflect the range, which may itself be the finding").
- **Measures:**
  - **MAD**, the primary measure, with Nigrini bands: first digit ≤ 0.006 close, ≤ 0.012 acceptable, ≤ 0.015 marginal, else nonconformity; first two ≤ 0.0012 / 0.0018 / 0.0022. No band exists for last two.
  - **Per-digit z** with continuity correction; digits with z > 1.96 are listed as over- or under-represented.
  - **χ²** with 8/89/99 degrees of freedom, and its p-value from the regularized incomplete gamma function. χ² rejects almost any large sample, which is why MAD is primary.
- **Subgroups** (`benford.subgroups`):
  - Dimensions: counterparty stream, account (only where it is a GL/category code), transaction type, fiscal year.
  - A group is tested only when its n ≥ max(`subgroups.min_n` = 300, the test's `min_n`). The report gives groups total / analysed / skipped and the rows in skipped groups, so there is no explosion into thousands of tiny tests.
  - A group in a band listed in `signal_bands` (nonconformity) becomes a low-severity `digit.benford_subgroup` signal. It names the over-represented digits and up to `max_example_rows` rows to sample.

**Why Benford is not proof of fraud.** Many legitimate populations don't follow the law:
- fixed prices, retainers and caps;
- narrow ranges;
- assigned numbers;
- per-vendor amounts clustered around a typical invoice;
- small samples.

Large samples also "fail" χ² for trivial differences. A deviation says *where to sample*, never *what happened*. Every Benford output and signal says so. The synthetic company shows the point: its overall ledger is in the acceptable band, but most GL-account subgroups deviate, because each account mixes a few vendors with narrow log-normal amount ranges. That is ordinary for such data.

## Results on the datasets (diagnostic, not a benchmark)

The numbers are in [`p7-acceptance.md`](p7-acceptance.md). In brief:
- **Synthetic ledger:** every target scenario is seen by its detector, and no look-alike control is flagged by the rule it resembles.
- **S-U-08** (routine quarter-end overtime): its first two quarter-ends are flagged by `temporal`, before the routine is established.
- **S-G-01** is a **known miss** of `new_vendor_large`. Its robust z of 2.65–2.96 is just under 3σ, because the raw-material peer group is widely dispersed. The `amount_to_account_median_ratio` of ≈ 4 remains in the P6 matrix; P8's Isolation Forest flags S-G-01 (see `p8-acceptance.md`).
- **No tuning to the benchmark.** Thresholds were set from definitions and statistics (e.g. "just below" = within 5%, 3σ, a multiple-testing-aware Poisson p), not tuned to maximise P5 recall. The remaining "other" signals are reported, not hidden.
- **PaySim:** 7 rules are not available (simulated clock, no references, not a corporate ledger), with reasons. The balance rule, exact duplicates, round amounts and Benford run.

## How P7 feeds P8 and P9

- **P8 (anomaly detection)** reads the same P6 matrix. It must not re-implement P7 rules; it adds model-based signals of family `model_anomaly`.
- **P9 (risk fusion)** consumes `report["signals"]`, or `ForensicSignal.to_canonical()`:
  - noisy-OR within family caps;
  - calibration of `weight` / `strength` (the P7 values are placeholders);
  - tiers and the volume guard.
- **Semantics must be respected.** Each rule's `semantics` tells P9 which signals describe what was known at posting time (`point_in_time`) and which are retrospective (`post_period`, `dataset`).
- **Reproducibility.** A report is identified by `inputs`: the feature registry, features config, matrix hashes, rules and Benford config hashes, and rule and Benford versions. Together with `report_sha256`, that is what a P9 run records.

## Limitations

- **Party keys are normalised names, not resolved entities (P12).** Look-alike vendors, shared bank accounts and employee links are therefore invisible to P7.
- **`structuring` thresholds.** It uses the nearest configured limit above each amount; the canonical data doesn't say which limit governs a row.
- **`period_end` and `round_amount` are retrospective** (whole-period statistics). They are documented as `post_period` and must not be treated as "known at posting time".
- **`temporal` treats recurring off-hours work as routine after 2 months.** A fraud that recurs monthly would, after two months, look routine to this rule; P8's behavioural features complement it.
- **Velocity and new-vendor rules miss what their gates exclude.** Bursts by brand-new counterparties (no baseline: S-H-02, S-P-02) are not velocity signals, and new counterparties of the LVE entity have no account peer group (S-P-02).
- **Benford subgroups need n ≥ 300.** On the synthetic ledger only 2 of 159 counterparty streams qualify. The round-amount vendor (n = 18) is found by `round_amount`, not by Benford.
- **PaySim's balance discontinuities are simulator output** (`other_mismatch`), not accounting evidence.
