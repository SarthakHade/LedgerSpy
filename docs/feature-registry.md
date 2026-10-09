# LedgerSpy — Feature Registry (P6)

Every feature LedgerSpy computes has exactly **one definition, one implementation, one version, one documented
meaning and one unit test**. All of them live in `ledgerspy/features/`. Later phases consume the matrices built from
this registry and never compute a feature of their own:
- P7 rules and Benford;
- P8 anomaly detection;
- P9 risk engine;
- P10 models.

**Status:**
- 69 features are implemented: 43 transaction-level and 26 entity-level. P7 added three (`is_off_hours`, `cp_prior_offhours_months`, `dq_balance_discontinuity`) through the same registry; P12 added seven entity-resolution features (two of them, `shared_identifier_count` and `employee_link_indicator`, were deferred to P12).
- 10 more are registered as `not_available_yet`, each with the reason, and are never computed.
- On the P5 synthetic company, 60 are computed; on the PaySim sample, 29.

## The rules

| Rule | How it is enforced |
|---|---|
| **One definition per feature** | `FeatureSpec` in `ledgerspy/features/spec.py`, registered with `@feature(...)` on its single implementation. A second registration of a name is refused. `test_no_feature_is_implemented_outside_the_registry` fails if another module defines a function with a feature's name |
| **Metadata is complete** | Each feature records name, version, family, description, inputs, output kind, units, point-in-time semantics, scope, peer group, fallback, missing-data policy, direction of "unusual", required dataset capabilities, config parameters and reference. Checked per feature by `test_every_feature_has_complete_metadata` |
| **Versioned** | `definition_sha256` hashes the metadata and the implementation's source. `ledgerspy/features/registry_lock.json` stores (version, hash) per feature. Changing the formula, inputs, normalisation, point-in-time rule, missing-data or fallback behaviour without a version bump fails `test_the_lock_matches_every_definition`. After a deliberate change, bump the version and run `python -m ledgerspy.features.lock` |
| **Only canonical data** | Features read `FeatureRow`, the canonical fields of the P4 normalised view. They never read `extra` (unmapped columns), the `labels` table, P5 ground truth or any file. Tested statically, and by changing labels or adding label/scenario columns, which leaves every value identical |
| **Point in time** | See below. Property-tested by truncating the future at random cut-offs and by shuffling the input order |
| **Deterministic** | No randomness. Ties are ordered by (`source_row`, `txn_id`), never by input order. Same data + config + registry gives the same `matrix_sha256` |
| **No fabricated values** | A feature the dataset cannot support is `not_available` with the reason and gets **no column**. A value that cannot be computed for a row is `null` under the feature's declared policy, never a silent 0 |
| **Unit-tested** | `tests/backend/test_features_units.py` has `test_<feature>` for every implemented feature (hand-computed values). The registry test fails if one is missing |

## Inputs

### Feature row (`FeatureRow`)

`FeatureRow` (`ledgerspy/features/data.py`) holds:
- `txn_id`, `source_row`;
- the posting timestamp;
- `amount_minor` and the flow (`out` for debit/out, `in` for credit/in);
- payer, payee and counterparty keys;
- `account`, `txn_type`, `document_date`, `reference`;
- whether a P4 transformation changed the row.

### Party key

A party key is the name with:
- NFKC normalisation applied;
- zero-width characters removed;
- whitespace collapsed;
- case folded.

So "Javrel  Office Supplies" and "Javrel Office Supplies" are one party. This is **not** entity resolution (P12): different legal-form spellings remain different parties.

### Counterparty

The counterparty rule is per adapter, set in config `features.adapters`:
- **Ledger or bank statement** (`by_direction`): the payee for outflows, the payer for inflows.
- **PaySim** (`payee`): the destination, because every row is booked on the origin account.

A **counterparty stream** is one counterparty and one flow. For example, payments to a vendor and receipts from the same firm as a customer are separate streams.

### Data-quality inputs

These are the row-level issues of the latest P4 integrity run **for the current data version**. If the data changed after that run (a transformation was applied), the `dq_*` features are `not_available` until P4 is run again.

### Configuration

Config `features.*` holds:
- **Thresholds:** history minimums, baseline window, MAD floor.
- **Calendar:** business hours, non-working weekdays, fiscal-year start, holidays.
- **Approval limits.**
- **Adapter profiles.**
- **Report flags.**

**Holidays and approval limits are case-specific and empty by default.** Until a case supplies them as config overrides, `is_holiday` and `threshold_proximity` are `not_available`. The tests and the P6 demo supply the synthetic company's own calendar and limits, read from its input files (`SYNTHETIC_holiday_calendar.csv`, `SYNTHETIC_group_entities.csv`).

### Dataset capabilities

Each feature declares the capabilities it needs (`ledgerspy/features/data.py: CAPABILITIES`).

| Capability | Synthetic ledger | PaySim |
|---|---|---|
| amount, direction, timestamp, counterparty, payer_payee | ✔ | ✔ |
| calendar_time (real dates) | ✔ (`datetime`) | ✘ `simulation_step` |
| time_of_day | ✔ | ✘ |
| account_category (account is a GL code) | ✔ | ✘ (account = sender ID) |
| txn_type | ✘ (not mapped) | ✔ |
| document_date, reference | ✔ | ✘ |
| holidays, approval_thresholds | ✔ with the case overrides | ✘ (not a corporate ledger) |
| p4_current | ✔ after a P4 run | ✔ after a P4 run |
| running_balance (P4 checked a balance) | ✘ (no balance column) | ✔ (origin balance columns) |

PaySim's `step` orders transactions, so count-based point-in-time features work on it. It is **never** treated as a calendar: every feature measured in days or hours, or by weekday, holiday or period end, is `not_available` for PaySim.

## Point-in-time semantics

| Semantics | Meaning | Features |
|---|---|---|
| `row` | Uses only the transaction's own fields and configuration | amount, timing and calendar features, `threshold_proximity`, `dq_is_normalized` |
| `point_in_time` | The row plus **rows with a strictly earlier posting timestamp**. This is what was known when the row was posted | every history, peer, velocity, sequence and relationship feature at transaction level |
| `dataset` | Uses dataset-wide results: P4 issues, whose duplicate checks compare the whole file | `dq_issue_count`, `dq_has_duplicate_issue`, `dq_has_missing_field` |
| `post_period` | A summary over the whole period. Analytical only, **never** "known at" a transaction | every entity-level feature (`ent_*`) |

**How point-in-time features are computed:**
- **Timestamp groups.** Rows are evaluated in timestamp groups. Every row of a group sees the state *before* the group, and only then is the group added.
- **Ties.** Rows sharing a timestamp never see each other. On a date-only ledger, that means no same-day row is "earlier".
- **Undated rows.** Rows without a timestamp get no history-based value and are never part of anyone's history.

Future risk scoring must keep these classes apart. The manifest records each feature's semantics.

Tests:
- `test_a_dramatic_future_does_not_change_the_past`;
- `test_truncating_the_future_never_changes_a_row` (property test: 3 seeds × datetime and date-only clocks × 3 cut-offs);
- `test_input_order_never_matters`;
- `test_rows_sharing_a_timestamp_never_see_each_other`;
- `test_rows_without_a_timestamp_get_no_history_and_are_nobody_s_history`.

All are in `tests/backend/test_features_pit.py`.

## Peer groups and fallback

One reusable hierarchy (`ledgerspy/features/history.py: peer_levels`, `peer_baseline`) serves every peer-relative feature:

```
counterparty  same counterparty + flow        ≥ features.min_entity_history (5) prior rows
→ account     same GL/category account + flow  ≥ features.min_peer_history (20)   only if `account` is a category
→ txn_type    same transaction type + flow     ≥ features.min_peer_history (20)   only if a type is mapped
→ global      same flow                        ≥ features.min_peer_history (20)
→ unavailable the value is null; nothing is fabricated
```

- **Window and scale.** Baselines use at most the `features.baseline_window` (200) most recent prior values of the group. The robust scale is `max(1.4826·MAD, features.mad_scale_floor = 0.1)` on the log scale, so a constant history never divides by zero.
- **Basis.** The level used is returned per row as the feature's **basis**: `counterparty`, `account`, `txn_type`, `global` or `unavailable`.
- **Single-level features.** Features tied to one level never fall back: `amount_to_cp_median_ratio` (entity only), `amount_to_account_median_ratio` (account only), `amount_pctl_prior` (global only). Each always means the same comparison.

Tests:
- `test_peer_fallback_entity_then_account_then_type_then_global_then_unavailable`;
- `test_levels_without_data_support_are_skipped`;
- `test_baseline_uses_only_the_recent_window`.

## Missing data

Every feature declares one policy:
- `null_if_input_missing`: null when a required field of the row is null.
- `null_if_insufficient_history`: null after the fallbacks are exhausted (basis `unavailable` where applicable).
- `null_if_not_applicable`: null when the definition does not apply, as stated per feature (e.g. no earlier row for a "days since" value).

Counts are true values, so `0` means "none earlier", not "unknown". The `dq_*` flags are never null when available. `test_every_feature_follows_its_missing_data_policy` checks every feature:
- on a row with all inputs missing;
- on the first row of a dataset;
- that it produces values when it can.

## Feature matrix, manifest and quality report

`ledgerspy.features.service.build_for_dataset(conn, cfg, dataset_id)` returns a `FeatureRun` with three parts.

**`transactions`:**
- one row per transaction (`txn_id`, `source_row`);
- a column per computed feature, plus `basis` for fallback features;
- `status` for every registered transaction-level feature: `computed`, `not_available` or `not_available_yet`, with the reason.

**`entities`:** one row per counterparty stream (subject ID `"<flow>:<party key>"`), laid out the same way.

**`manifest`:**
- manifest version, `registry_sha256`;
- `features_config_sha256` (the effective `features` config, with case overrides);
- dataset (ID, adapter, time basis, data version, P4 run, rows);
- capabilities;
- per feature: level, status, semantics, version, `definition_sha256` and reason;
- `transaction_matrix_sha256` and `entity_matrix_sha256`.

A later run or model stores this manifest to identify its inputs.

`ledgerspy.features.report.quality_report(run, cfg.features)` reports only computed facts:
- feature counts by status;
- per feature: rows, nulls, missing share, distinct values, variance, mean, min/max, top categories, basis counts.

It also gives each feature these flags (reported, never used to drop a feature):
- `constant` — fewer than `report.min_distinct_values` distinct values;
- `excessive_missing` — more than `report.max_missing_share` nulls;
- `insufficient_history` — some rows have basis `unavailable`.

## Using features (P7+)

```python
from ledgerspy.features import service, report

run = service.build_for_dataset(conn, cfg, dataset_id)
z = run.transactions.columns["amount_robust_z"]  # one value per run.transactions.subjects
level = run.transactions.basis["amount_robust_z"]  # which baseline each value used
run.manifest  # store with the detector run / model card
```

Consumers must:
- check `status` before using a column;
- treat `post_period` and `dataset` features as context, never as "known at the time".

## Supported datasets

| | Synthetic ledger (P5 reference, with its case calendar and limits) | PaySim sample |
|---|---|---|
| Transaction features computed | 41 of 43 (`txn_type` not mapped; no running balance) | 20 of 43 |
| Entity features computed | 19 of 19 (159 counterparty streams) | 9 of 19 (6,967 streams) |
| Not available | 2 | 33 (simulation clock, account is the sender, no references or document dates, no approval policy) |
| Not available yet (deferred) | 10 | 10 |

The synthetic company is the validation environment for corporate features. `test_injected_scenarios_are_visible_in_the_features` and `test_entity_profiles_reflect_scenarios` check that P5 scenarios show up in the features. Ground truth is used there only to choose which rows to look at. Some of the checks:

| Scenario | Feature evidence |
|---|---|
| S-A-01 | robust z > 3 and > 5× the counterparty median |
| S-H-03 | 494 days since the previous payment |
| S-L-01 | the large payment is 35× the previous one |
| S-K-03 | after hours, 0 days to fiscal-year end |
| S-K-01 | holiday / Sunday / night |
| S-F-01 | reference gap 1, threshold proximity 0.92–0.98 |
| S-J-01 | threshold proximity > 0.97 |
| S-P-02 | ≥ 6 new payees in 7 days |
| S-H-01 | velocity ratio > 3 |
| S-U-10 | reciprocal pair |
| S-C-01, S-V-01 | P4 data-quality flags |
| S-B-01 | round share 1.0 |
| S-F-01, S-U-14 | sequential references 1.0 |
| S-Q-01 | positive log-amount trend |

Which features each future detector uses is mapped in [`synthetic-scenario-matrix.md`](synthetic-scenario-matrix.md).

## Legacy feature code (not part of the registry)

`frontend/js/ml-engine.js` (`engineerFeatures`) was the P1 prototype's in-browser heuristic, labelled as such in the UI. P6 did **not** duplicate it; it was **legacy** and was **deleted in P9**, when the UI started rendering backend results (`docs/risk-engine.md`). Its columns mapped to the registry as follows:

| Legacy JS column | Replaced by |
|---|---|
| `amount_norm`, `amount_deviation`, `sudden_spike`, `avg_amount_user` | `amount_robust_z`, `amount_to_cp_median_ratio`, `ent_mean_amount_minor` |
| `round_amount` | `round_level`, `ent_round_share` |
| `threshold_flag` (4,800–5,100) | `threshold_proximity` (configured approval limits) |
| `velocity`, `rapid_txn_flag`, `time_gap` | `cp_count_7d`, `cp_count_30d`, `cp_velocity_ratio`, `cp_days_since_prev` (real time only) |
| `transaction_count`, `is_new_dest`, `new_vendor_risk` | `cp_prior_count` (0 = first time), `cp_days_since_first` |
| `dest_count`, `many_to_one_flag`, `unique_receivers`, `multi_receiver_flag` | `payee_distinct_payers_prior`, `payer_distinct_payees_prior`, `ent_fan_in`, `ent_fan_out` |
| `odd_time`, `time_bucket` (`step % 24`) | `hour_of_day`, `is_after_hours` (only with a real clock; never for PaySim) |
| `type_*` one-hot | `txn_type` (category) |
| `balance_error`, `balance_drain`, `amount_ratio` | P4 `balance_continuity` (data integrity); `amount_to_balance_ratio` deferred to P10 |
| `benford_flag` | P7 Benford module (subgroup tests; no per-row flag) |
| `circular_flag` | P13 temporal cycles (`is_reciprocal_pair` is only one-hop context) |
| `fuzzy_flag`, `name_similarity`, `self_transfer` | P12 entity resolution (removed, not re-implemented) |

## Limitations

- **Names are not resolved entities.** Party keys are normalised names (P12 adds resolution). Look-alike vendors and shared bank accounts are therefore invisible to P6 features.
- **Masters are identity data only.** P12 imports vendor / customer / employee masters for entity resolution (name, address, contacts, bank account, tax ID); approver/poster fields, master dates, bank reconciliation and locations are not available. The 8 deferred features name the missing input and its phase.
- **Thresholds are not tied to transaction kind.** `threshold_proximity` uses the nearest configured limit above the amount; the canonical data does not say which limit governs a row (claim vs vendor payment).
- **One clock per dataset.** Point-in-time history uses posting timestamps. Back-dated postings are visible through `posting_lag_days`, but history is not re-ordered by document date.
- **Bounded baselines.** Medians and MADs use the 200 most recent prior values of a group, and the global baseline is recency-weighted by this window.
- **Pure-Python speed.** About 1 s for ~7–10k rows and about 30 s for a 100k-row synthetic ledger on a laptop (profiled in P8;
  see docs/p8-acceptance.md).
- **Hash sensitivity.** The definition hash includes the implementation's source, so even a formatting-only change to a feature function requires a version bump. Shared helpers in `history.py` are not hashed into each feature: a change there must bump the versions of the features that use it (stated in `history.py`).

## Feature catalogue

Generated from the registry. Support marks come from real runs on both datasets: ✔ computed, ✘ not available with the reason.

### Transaction-level features

#### Amount

**`amount_minor`** · v1 · row · int, minor currency units

- **Definition:** The transaction amount as booked, in minor currency units (paise).
- **Inputs:** amount_minor
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the amount is null
- **Unusual when:** higher · **requires:** amount
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_amount_minor`

**`amount_pctl_prior`** · v1 · point_in_time · float, fraction

- **Definition:** Mid-rank percentile of the amount among the most recent prior amounts of the same flow (global baseline), in [0, 1].
- **Inputs:** amount_minor, direction, posted_at · **config:** features.baseline_window, features.min_peer_history
- **Scope:** transaction vs all prior rows of the same flow · **peer group:** global (same flow)
- **Missing:** `null_if_insufficient_history` — null with fewer than min_peer_history prior rows of the flow; null without amount/timestamp/direction
- **Unusual when:** higher · **requires:** amount, timestamp, direction
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_amount_pctl_prior`

**`amount_robust_z`** · v1 · point_in_time · float, robust standard deviations (log scale)

- **Definition:** Robust z-score of log_amount against prior amounts of the first peer level with enough history: (log x − median) / max(1.4826·MAD, mad_scale_floor).
- **Inputs:** amount_minor, direction, posted_at, payer_raw/payee_raw, account, txn_type · **config:** features.baseline_window, features.min_entity_history, features.min_peer_history, features.mad_scale_floor
- **Scope:** transaction vs its peer baseline · **peer group:** the standard hierarchy (see *Peer groups*) · **fallback:** reported per row as the basis
- **Missing:** `null_if_insufficient_history` — null (basis 'unavailable') when no level has enough prior rows; null without amount/timestamp/direction
- **Unusual when:** both · **requires:** amount, timestamp, direction
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_amount_robust_z`

**`is_outflow`** · v1 · row · bool, flag

- **Definition:** True for money leaving the books (debit / out), false for money coming in (credit / in).
- **Inputs:** direction
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the direction is null
- **Unusual when:** context · **requires:** direction
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_is_outflow`

**`log_amount`** · v1 · row · float, natural log of major currency units

- **Definition:** ln(1 + |amount| in major units): compresses the heavy right tail of amounts.
- **Inputs:** amount_minor
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the amount is null
- **Unusual when:** higher · **requires:** amount
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_log_amount`

**`round_level`** · v1 · row · int, power of ten (0–5)

- **Definition:** Roundness: the largest k in 1..5 such that the amount is a whole multiple of 10^k major units (₹10 … ₹1,00,000); 0 when it is not a multiple of 10 major units (including any paise).
- **Inputs:** amount_minor
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the amount is null or zero (zero is a multiple of everything)
- **Unusual when:** higher · **requires:** amount
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_round_level`

**`threshold_proximity`** · v1 · row · float, ratio

- **Definition:** amount / the smallest configured approval threshold strictly above the amount, in (0, 1). Values just below 1 mean 'just under a limit'. Thresholds are case configuration; the canonical data does not say which limit governs a row, so the nearest one above is used.
- **Inputs:** amount_minor, config: features.approval_thresholds · **config:** features.approval_thresholds
- **Scope:** transaction
- **Missing:** `null_if_not_applicable` — null when the amount is null or ≤ 0, or at/above every configured threshold
- **Unusual when:** higher · **requires:** amount, approval_thresholds
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (approval thresholds do not apply to this adapter (not a corporate ledger))
- **Test:** `tests/backend/test_features_units.py::test_threshold_proximity`

**`txn_type`** · v1 · row · category, category

- **Definition:** The source transaction type as a category (e.g. PaySim TRANSFER, CASH_OUT). An input encoding for models; models one-hot it themselves.
- **Inputs:** txn_type
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the type is null
- **Unusual when:** context · **requires:** txn_type
- **Synthetic ledger:** ✘ (no transaction type is mapped) · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_txn_type`

#### Timing

**`day_of_week`** · v1 · row · int, weekday index

- **Definition:** Weekday of the posting date, Monday = 0 … Sunday = 6.
- **Inputs:** posted_at
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the posting timestamp is null
- **Unusual when:** context · **requires:** calendar_time
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_day_of_week`

**`days_to_fiscal_year_end`** · v1 · row · int, days

- **Definition:** Calendar days from the posting date to the last day of its financial year (0 on that day).
- **Inputs:** posted_at, config: features.calendar.fiscal_year_start_month · **config:** features.calendar.fiscal_year_start_month
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the posting timestamp is null
- **Unusual when:** lower · **requires:** calendar_time
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_days_to_fiscal_year_end`

**`days_to_month_end`** · v1 · row · int, days

- **Definition:** Calendar days from the posting date to the last day of its month (0 on the last day).
- **Inputs:** posted_at
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the posting timestamp is null
- **Unusual when:** lower · **requires:** calendar_time
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_days_to_month_end`

**`hour_of_day`** · v1 · row · float, hours (0–24)

- **Definition:** Posting time as hours since midnight (hour + minute/60 + second/3600).
- **Inputs:** posted_at
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the posting timestamp is null
- **Unusual when:** context · **requires:** time_of_day
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (posting timestamps have no real time of day (time basis 'simulation_step'))
- **Test:** `tests/backend/test_features_units.py::test_hour_of_day`

**`is_after_hours`** · v1 · row · bool, flag

- **Definition:** True when the posting time is before the start or after the end of configured business hours.
- **Inputs:** posted_at, config: features.calendar.business_hours · **config:** features.calendar.business_hours
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the posting timestamp is null
- **Unusual when:** true · **requires:** time_of_day
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (posting timestamps have no real time of day (time basis 'simulation_step'))
- **Test:** `tests/backend/test_features_units.py::test_is_after_hours`

**`is_holiday`** · v1 · row · bool, flag

- **Definition:** True when the posting date is in the case's configured holiday calendar.
- **Inputs:** posted_at, config: features.calendar.holidays · **config:** features.calendar.holidays
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the posting timestamp is null
- **Unusual when:** true · **requires:** calendar_time, holidays
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_is_holiday`

**`is_non_working_weekday`** · v1 · row · bool, flag

- **Definition:** True when the posting date falls on a configured non-working weekday (e.g. Sunday).
- **Inputs:** posted_at, config: features.calendar.non_working_weekdays · **config:** features.calendar.non_working_weekdays
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the posting timestamp is null
- **Unusual when:** true · **requires:** calendar_time
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_is_non_working_weekday`

**`is_off_hours`** · v1 · row · bool, flag

- **Definition:** True when the posting falls outside the configured working time: after hours, on a non-working weekday, or on a configured holiday (holidays count only when a calendar is configured).
- **Inputs:** posted_at, config: features.calendar.business_hours, config: features.calendar.non_working_weekdays, config: features.calendar.holidays · **config:** features.calendar.business_hours, features.calendar.non_working_weekdays, features.calendar.holidays
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the posting timestamp is null
- **Unusual when:** true · **requires:** time_of_day
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (posting timestamps have no real time of day (time basis 'simulation_step'))
- **Test:** `tests/backend/test_features_units.py::test_is_off_hours`

**`posting_lag_days`** · v1 · row · int, days

- **Definition:** Posting date − document (invoice) date in days. Negative = posted before the document date.
- **Inputs:** posted_at, document_date
- **Scope:** transaction
- **Missing:** `null_if_input_missing` — null when the posting timestamp or the document date is null
- **Unusual when:** both · **requires:** calendar_time, document_date
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_posting_lag_days`

#### Frequency

**`cp_days_since_first`** · v1 · point_in_time · float, days

- **Definition:** Days since the counterparty stream's first earlier transaction (relationship age as seen in the data; not the vendor-master onboarding date, which is not canonical data yet).
- **Inputs:** posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_not_applicable` — null for first-time activity (no earlier row: see cp_prior_count = 0); null when the row has no timestamp, counterparty or direction
- **Unusual when:** lower · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_cp_days_since_first`

**`cp_days_since_prev`** · v1 · point_in_time · float, days

- **Definition:** Days since the counterparty stream's most recent earlier transaction (time gap; long gaps = dormancy before this row).
- **Inputs:** posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_not_applicable` — null for first-time activity; null when the row has no timestamp, counterparty or direction
- **Unusual when:** both · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_cp_days_since_prev`

**`cp_prior_count`** · v1 · point_in_time · int, transactions

- **Definition:** Number of earlier transactions of the counterparty stream. 0 = first-time activity with this counterparty (0 is a true count, not a missing value).
- **Inputs:** posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_input_missing` — null when the row has no timestamp, counterparty or direction
- **Unusual when:** lower · **requires:** timestamp, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_cp_prior_count`

#### Velocity

**`cp_amount_sum_30d`** · v1 · point_in_time · int, minor currency units

- **Definition:** Sum of the counterparty stream's earlier amounts in [t − 30d, t) (rows with a null amount are skipped).
- **Inputs:** amount_minor, posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_input_missing` — null when the row has no timestamp, counterparty or direction
- **Unusual when:** higher · **requires:** amount, calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_cp_amount_sum_30d`

**`cp_count_30d`** · v1 · point_in_time · int, transactions

- **Definition:** Earlier transactions of the counterparty stream in the 30 days before this one [t − 30d, t).
- **Inputs:** posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_input_missing` — null when the row has no timestamp, counterparty or direction
- **Unusual when:** higher · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_cp_count_30d`

**`cp_count_7d`** · v1 · point_in_time · int, transactions

- **Definition:** Earlier transactions of the counterparty stream in the 7 days before this one [t − 7d, t).
- **Inputs:** posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_input_missing` — null when the row has no timestamp, counterparty or direction
- **Unusual when:** higher · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_cp_count_7d`

**`cp_velocity_ratio`** · v1 · point_in_time · float, ratio of rates

- **Definition:** Recent rate / historical rate of the counterparty stream: (rows in [t−30d, t) / 30) ÷ (rows before t−30d / days from the first row to t−30d). 1 = usual pace; ≫ 1 = burst.
- **Inputs:** posted_at, direction, payer_raw/payee_raw · **config:** features.velocity_min_history_days, features.min_entity_history
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_insufficient_history` — null when the history before t−30d spans fewer than velocity_min_history_days or has fewer than min_entity_history rows; null when the row has no timestamp, counterparty or direction
- **Unusual when:** higher · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_cp_velocity_ratio`

#### Sequence

**`cp_prev_amount_ratio`** · v1 · point_in_time · float, ratio

- **Definition:** amount / the amount of the counterparty stream's most recent prior row (sequence context: small → small → large).
- **Inputs:** amount_minor, direction, posted_at, payer_raw/payee_raw
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_insufficient_history` — null with no prior row (or a prior amount of 0 / null); null when the row has no timestamp, counterparty or direction
- **Unusual when:** both · **requires:** amount, timestamp, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_cp_prev_amount_ratio`

**`cp_reference_gap`** · v1 · point_in_time · int, reference numbers

- **Definition:** Trailing number of the reference − that of the counterparty stream's most recent earlier row with a numbered reference (a reference ending in digits, optionally one letter). A vendor with many clients rarely sends consecutive numbers (gap 1) to one.
- **Inputs:** reference, posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_not_applicable` — null when this reference has no number or no earlier row of the stream has one; null when the row has no timestamp, counterparty or direction
- **Unusual when:** context · **requires:** reference, timestamp, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (no reference (invoice number) is mapped)
- **Test:** `tests/backend/test_features_units.py::test_cp_reference_gap`

#### Behaviour

**`amount_to_account_median_ratio`** · v1 · point_in_time · float, ratio

- **Definition:** amount / median prior amount of the same GL/category account and flow (peer-relative amount: e.g. a vendor paid far more than its category).
- **Inputs:** amount_minor, direction, posted_at, account · **config:** features.baseline_window, features.min_peer_history
- **Scope:** transaction vs its account peer group · **peer group:** account (same GL/category account and flow)
- **Missing:** `null_if_insufficient_history` — null with fewer than min_peer_history prior rows in the account peer group, or a zero median; null without amount/timestamp/direction/account
- **Unusual when:** both · **requires:** amount, timestamp, direction, account_category
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (the account field is not a GL/category code for this adapter)
- **Test:** `tests/backend/test_features_units.py::test_amount_to_account_median_ratio`

**`amount_to_cp_median_ratio`** · v1 · point_in_time · float, ratio

- **Definition:** amount / median of the counterparty stream's prior amounts (deviation from the entity's own baseline; no fallback, so it always means 'vs this counterparty').
- **Inputs:** amount_minor, direction, posted_at, payer_raw/payee_raw · **config:** features.baseline_window, features.min_entity_history
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters) · **peer group:** counterparty stream only
- **Missing:** `null_if_insufficient_history` — null with fewer than min_entity_history prior rows of the stream, or a zero median; null when the row has no timestamp, counterparty or direction
- **Unusual when:** both · **requires:** amount, timestamp, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_amount_to_cp_median_ratio`

**`cp_count_30d_peer_ratio`** · v1 · point_in_time · float, ratio

- **Definition:** The counterparty stream's rows in [t−30d, t) ÷ the median of the same count for the other counterparties active in that window in the same account peer group (peer-relative frequency).
- **Inputs:** posted_at, direction, payer_raw/payee_raw, account · **config:** features.min_peers
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters) · **peer group:** account (same GL/category account and flow), other counterparties active in the window
- **Missing:** `null_if_insufficient_history` — null with fewer than min_peers other active counterparties in the peer group; null without timestamp/direction/counterparty/account
- **Unusual when:** higher · **requires:** calendar_time, direction, counterparty, account_category
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_cp_count_30d_peer_ratio`

**`cp_log_amount_trend`** · v1 · point_in_time · float, log-amount change per 30 days

- **Definition:** Escalation: least-squares slope of log_amount against time over the counterparty stream's earlier rows in [t − trend_window_days, t), per 30 days (0.11 ≈ +12% a month).
- **Inputs:** amount_minor, posted_at, direction, payer_raw/payee_raw · **config:** features.trend_window_days, features.trend_min_points
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_insufficient_history` — null with fewer than trend_min_points earlier rows (with amounts) in the window, or all at one instant; null when the row has no timestamp, counterparty or direction
- **Unusual when:** higher · **requires:** amount, calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_cp_log_amount_trend`

**`cp_prior_offhours_months`** · v1 · point_in_time · int, months

- **Definition:** Number of distinct calendar months in which the counterparty stream had earlier off-hours postings (see is_off_hours). ≥ 2 means off-hours work with this counterparty is a recurring routine, not a one-off.
- **Inputs:** posted_at, direction, payer_raw/payee_raw, config: features.calendar · **config:** features.calendar.business_hours, features.calendar.non_working_weekdays, features.calendar.holidays
- **Scope:** counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)
- **Missing:** `null_if_input_missing` — null when the row has no timestamp, counterparty or direction
- **Unusual when:** lower · **requires:** time_of_day, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (posting timestamps have no real time of day (time basis 'simulation_step'))
- **Test:** `tests/backend/test_features_units.py::test_cp_prior_offhours_months`

**`hour_deviation`** · v1 · point_in_time · float, hours

- **Definition:** |hour_of_day − median prior posting hour| of the first peer level with enough history (time-pattern deviation).
- **Inputs:** posted_at, direction, payer_raw/payee_raw, account, txn_type · **config:** features.baseline_window, features.min_entity_history, features.min_peer_history
- **Scope:** transaction vs its peer baseline · **peer group:** the standard hierarchy (see *Peer groups*) · **fallback:** reported per row as the basis
- **Missing:** `null_if_insufficient_history` — null (basis 'unavailable') when no level has enough prior rows; null without timestamp/direction
- **Unusual when:** higher · **requires:** time_of_day, direction
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (posting timestamps have no real time of day (time basis 'simulation_step'))
- **Test:** `tests/backend/test_features_units.py::test_hour_deviation`

#### Relationship

**`is_reciprocal_pair`** · v1 · point_in_time · bool, flag

- **Definition:** True when an earlier transaction ran the other way: this row's payee paid this row's payer.
- **Inputs:** payer_raw, payee_raw, posted_at
- **Scope:** payer ↔ payee pair
- **Missing:** `null_if_input_missing` — null when the row has no timestamp, payer or payee
- **Unusual when:** true · **requires:** timestamp, payer_payee
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_is_reciprocal_pair`

**`pair_prior_count`** · v1 · point_in_time · int, transactions

- **Definition:** Number of earlier transactions from the same payer to the same payee (directional pair).
- **Inputs:** payer_raw, payee_raw, posted_at
- **Scope:** payer → payee pair
- **Missing:** `null_if_input_missing` — null when the row has no timestamp, payer or payee
- **Unusual when:** lower · **requires:** timestamp, payer_payee
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_pair_prior_count`

**`pair_share_of_payer_outflow`** · v1 · point_in_time · float, fraction

- **Definition:** Share of the payer's earlier paid amount that went to this payee (flow concentration), in [0, 1].
- **Inputs:** payer_raw, payee_raw, amount_minor, posted_at
- **Scope:** payer → payee pair
- **Missing:** `null_if_insufficient_history` — null when the payer has no earlier amount; null without timestamp/payer/payee
- **Unusual when:** higher · **requires:** amount, timestamp, payer_payee
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_pair_share_of_payer_outflow`

**`payee_distinct_payers_prior`** · v2 · point_in_time · int, parties

- **Definition:** Number of distinct payers seen paying this row's payee before this row (fan-in to date).
- **Inputs:** payer_raw, payee_raw, posted_at
- **Scope:** payee
- **Missing:** `null_if_input_missing` — null when the row has no timestamp or payee
- **Unusual when:** higher · **requires:** timestamp, payer_payee
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_payee_distinct_payers_prior`
- **v2 (P8):** same values as v1; the per-payee payer set is kept incrementally (v1 rebuilt it for
  every row: quadratic at 100k rows).

**`payer_distinct_payees_prior`** · v1 · point_in_time · int, parties

- **Definition:** Number of distinct payees this row's payer paid before this row (fan-out to date).
- **Inputs:** payer_raw, payee_raw, posted_at
- **Scope:** payer
- **Missing:** `null_if_input_missing` — null when the row has no timestamp or payer
- **Unusual when:** higher · **requires:** timestamp, payer_payee
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_payer_distinct_payees_prior`

**`payer_new_payees_7d`** · v1 · point_in_time · int, parties

- **Definition:** Number of distinct payees this row's payer paid for the first time in [t − 7d, t) (a burst of brand-new counterparties: pass-through fan-out).
- **Inputs:** payer_raw, payee_raw, posted_at
- **Scope:** payer
- **Missing:** `null_if_input_missing` — null when the row has no timestamp or payer
- **Unusual when:** higher · **requires:** calendar_time, payer_payee
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_payer_new_payees_7d`

#### Data quality

**`dq_balance_discontinuity`** · v1 · dataset · bool, flag

- **Definition:** True when the current P4 run reported a balance_continuity issue on this transaction: its running balance does not follow from the previous balance and the amount (P4 does the arithmetic and records the kind of mismatch).
- **Inputs:** the latest P4 integrity run of the current data version (integrity_issues), P4 balance_continuity check
- **Scope:** transaction
- **Missing:** `null_if_not_applicable` — never null when available; unavailable without a current P4 run or without a checked running balance (P4 says why)
- **Unusual when:** true · **requires:** p4_current, running_balance
- **Synthetic ledger:** ✘ (P4 balance_continuity: no running-balance column in the source) · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_dq_balance_discontinuity`

**`dq_has_duplicate_issue`** · v1 · dataset · bool, flag

- **Definition:** True when P4 reported an exact_duplicate, near_duplicate or duplicate_source_id issue on this transaction (P4 compares the whole dataset, so this is not point-in-time).
- **Inputs:** the latest P4 integrity run of the current data version (integrity_issues)
- **Scope:** transaction
- **Missing:** `null_if_not_applicable` — never null when available; unavailable without a current P4 run
- **Unusual when:** true · **requires:** p4_current
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_dq_has_duplicate_issue`

**`dq_has_missing_field`** · v1 · dataset · bool, flag

- **Definition:** True when P4 reported a missing_required or missing_optional issue on this transaction.
- **Inputs:** the latest P4 integrity run of the current data version (integrity_issues)
- **Scope:** transaction
- **Missing:** `null_if_not_applicable` — never null when available; unavailable without a current P4 run
- **Unusual when:** true · **requires:** p4_current
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_dq_has_missing_field`

**`dq_is_normalized`** · v1 · row · bool, flag

- **Definition:** True when at least one field of the row comes from an applied, logged P4 transformation (normalised view differs from the evidence).
- **Inputs:** normalized_values (P4 transformations)
- **Scope:** transaction
- **Missing:** `null_if_not_applicable` — never null
- **Unusual when:** context · **requires:** nothing
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_dq_is_normalized`

**`dq_issue_count`** · v1 · dataset · int, issues

- **Definition:** Number of row-level P4 integrity issues on this transaction (any check, any severity).
- **Inputs:** the latest P4 integrity run of the current data version (integrity_issues)
- **Scope:** transaction
- **Missing:** `null_if_not_applicable` — never null when available (0 = no issue); unavailable without a current P4 run
- **Unusual when:** higher · **requires:** p4_current
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_dq_issue_count`

### Entity-level features (post-period)

#### Amount

**`ent_round_share`** · v1 · post_period · float, fraction

- **Definition:** Share of the stream's non-zero amounts that are multiples of 10^round_level_min major units.
- **Inputs:** amount_minor, direction, payer_raw/payee_raw · **config:** features.round_level_min
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_input_missing` — null when the stream has no non-zero amount
- **Unusual when:** higher · **requires:** amount, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_ent_round_share`

#### Timing

**`ent_after_hours_share`** · v1 · post_period · float, fraction

- **Definition:** Share of the stream's rows posted outside configured business hours.
- **Inputs:** posted_at, config: features.calendar.business_hours · **config:** features.calendar.business_hours
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_input_missing` — null when no row of the stream has a timestamp
- **Unusual when:** higher · **requires:** time_of_day, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (posting timestamps have no real time of day (time basis 'simulation_step'))
- **Test:** `tests/backend/test_features_units.py::test_ent_after_hours_share`

**`ent_non_working_share`** · v1 · post_period · float, fraction

- **Definition:** Share of the stream's rows posted on a configured non-working weekday or configured holiday.
- **Inputs:** posted_at, config: features.calendar.non_working_weekdays, config: features.calendar.holidays · **config:** features.calendar.non_working_weekdays, features.calendar.holidays
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_input_missing` — null when no row of the stream has a timestamp
- **Unusual when:** higher · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_ent_non_working_share`

#### Frequency

**`ent_max_gap_days`** · v1 · post_period · float, days

- **Definition:** Longest gap between consecutive transactions of the stream (dormant-period length).
- **Inputs:** posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_insufficient_history` — null with fewer than 2 dated rows
- **Unusual when:** higher · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_ent_max_gap_days`

#### Velocity

**`ent_recent_count_ratio`** · v1 · post_period · float, ratio of rates

- **Definition:** Recent vs historical activity: rate of rows in the last recent_days of the dataset ÷ the stream's rate before that (from its first row).
- **Inputs:** posted_at, direction, payer_raw/payee_raw · **config:** features.recent_days
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_insufficient_history` — null when the stream's history before the recent window spans fewer than recent_days
- **Unusual when:** both · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_ent_recent_count_ratio`

#### Sequence

**`ent_reference_sequential_share`** · v1 · post_period · float, fraction

- **Definition:** Share of consecutive (time-ordered) numbered references of the stream that differ by exactly 1 (sequential invoices).
- **Inputs:** reference, posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_insufficient_history` — null with fewer than 3 numbered references
- **Unusual when:** higher · **requires:** reference, timestamp, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (no reference (invoice number) is mapped)
- **Test:** `tests/backend/test_features_units.py::test_ent_reference_sequential_share`

#### Behaviour

**`ent_log_amount_trend`** · v1 · post_period · float, log-amount change per 30 days

- **Definition:** Escalation over the period: least-squares slope of log_amount against time, per 30 days.
- **Inputs:** amount_minor, posted_at, direction, payer_raw/payee_raw · **config:** features.trend_min_points
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_insufficient_history` — null with fewer than trend_min_points dated rows with amounts, or all at one instant
- **Unusual when:** higher · **requires:** amount, calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_ent_log_amount_trend`

**`ent_median_to_peer_ratio`** · v1 · post_period · float, ratio

- **Definition:** The stream's median amount ÷ the median of the median amounts of the other streams with the same flow and the same most frequent GL/category account (peer-relative amount profile).
- **Inputs:** amount_minor, account, direction, payer_raw/payee_raw · **config:** features.min_peers
- **Scope:** counterparty stream over the whole dataset (all rows of the period) · **peer group:** other streams of the same flow whose most frequent account is the same
- **Missing:** `null_if_insufficient_history` — null with fewer than min_peers peer streams, no amount, or a zero peer median
- **Unusual when:** both · **requires:** amount, direction, counterparty, account_category
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (the account field is not a GL/category code for this adapter)
- **Test:** `tests/backend/test_features_units.py::test_ent_median_to_peer_ratio`

**`ent_recent_amount_ratio`** · v1 · post_period · float, ratio

- **Definition:** Recent vs historical amount: median amount in the last recent_days of the dataset ÷ the stream's median amount before that.
- **Inputs:** amount_minor, posted_at, direction, payer_raw/payee_raw · **config:** features.recent_days
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_insufficient_history` — null when either window has no amount or the earlier median is 0
- **Unusual when:** both · **requires:** amount, calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_ent_recent_amount_ratio`

#### Relationship

**`ent_fan_in`** · v1 · post_period · int, parties

- **Definition:** Distinct payers that paid this counterparty anywhere in the dataset (fan-in of the party).
- **Inputs:** payer_raw, payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — never null when available (0 = nobody paid this party)
- **Unusual when:** higher · **requires:** direction, counterparty, payer_payee
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_ent_fan_in`

**`ent_fan_out`** · v1 · post_period · int, parties

- **Definition:** Distinct payees this counterparty paid anywhere in the dataset (fan-out of the party).
- **Inputs:** payer_raw, payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — never null when available (0 = this party paid nobody)
- **Unusual when:** higher · **requires:** direction, counterparty, payer_payee
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_ent_fan_out`

**`ent_share_of_flow`** · v1 · post_period · float, fraction

- **Definition:** Concentration: the stream's total amount ÷ the total amount of all rows of the same flow.
- **Inputs:** amount_minor, direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_input_missing` — null when the stream or its flow has no amount
- **Unusual when:** higher · **requires:** amount, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_ent_share_of_flow`

#### Profile

**`ent_first_seen`** · v1 · post_period · date, date

- **Definition:** Posting date of the stream's first transaction in the dataset.
- **Inputs:** posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_input_missing` — null when no row of the stream has a timestamp
- **Unusual when:** context · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_ent_first_seen`

**`ent_last_seen`** · v1 · post_period · date, date

- **Definition:** Posting date of the stream's last transaction in the dataset.
- **Inputs:** posted_at, direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_input_missing` — null when no row of the stream has a timestamp
- **Unusual when:** context · **requires:** calendar_time, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✘ (time basis is 'simulation_step': steps of a simulation are not calendar time)
- **Test:** `tests/backend/test_features_units.py::test_ent_last_seen`

**`ent_log_amount_dispersion`** · v1 · post_period · float, log-amount units

- **Definition:** Robust dispersion of the stream's amounts: 1.4826·MAD of log_amount (0 = every amount equal).
- **Inputs:** amount_minor, direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_insufficient_history` — null with fewer than 2 amounts
- **Unusual when:** lower · **requires:** amount, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_ent_log_amount_dispersion`

**`ent_mean_amount_minor`** · v1 · post_period · float, minor currency units

- **Definition:** Mean of the stream's amounts.
- **Inputs:** amount_minor, direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_input_missing` — null when no row of the stream has an amount
- **Unusual when:** higher · **requires:** amount, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_ent_mean_amount_minor`

**`ent_median_amount_minor`** · v1 · post_period · float, minor currency units

- **Definition:** Median of the stream's amounts (historical baseline over the period).
- **Inputs:** amount_minor, direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_input_missing` — null when no row of the stream has an amount
- **Unusual when:** higher · **requires:** amount, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_ent_median_amount_minor`

**`ent_total_amount_minor`** · v1 · post_period · int, minor currency units

- **Definition:** Sum of the stream's amounts (rows with a null amount are skipped).
- **Inputs:** amount_minor, direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_input_missing` — null when no row of the stream has an amount
- **Unusual when:** higher · **requires:** amount, direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_ent_total_amount_minor`

**`ent_txn_count`** · v1 · post_period · int, transactions

- **Definition:** Number of transactions of the stream in the dataset.
- **Inputs:** direction, payer_raw/payee_raw
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — never null (a stream exists only if it has rows)
- **Unusual when:** context · **requires:** direction, counterparty
- **Synthetic ledger:** ✔ · **PaySim:** ✔
- **Test:** `tests/backend/test_features_units.py::test_ent_txn_count`

### Entity resolution (P12)

Entity-level, post-period: computed from the dataset's entity resolution ([`entity-resolution.md`](entity-resolution.md)), which reads every ledger name and the case's imported master records. Never part of a supervised contract.

**`ent_canonical_entity_id`** · v1 · post_period · category, canonical entity ID (CE-…)

- **Definition:** Canonical entity (P12) of the stream's counterparty name: streams whose names are confirmed matches share it; ambiguous or conflicting candidates never do.
- **Inputs:** payer_raw/payee_raw, direction, master records (when imported)
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — never null for a stream
- **Unusual when:** context · **requires:** direction, counterparty · **params:** resolution
- **Synthetic ledger:** ✔ · **PaySim:** ✔ (exact account codes only)
- **Test:** `tests/backend/test_features_units.py::test_ent_canonical_entity_id`

**`ent_resolution_status`** · v1 · post_period · category, status

- **Definition:** Resolution status of the stream's counterparty name: unique (no candidate needing review), resolved (merged with confirmed matches), needs_review (a probable or ambiguous candidate exists) or conflicting_identity (a candidate with contradictory identity evidence exists).
- **Inputs:** payer_raw/payee_raw, direction, master records (when imported)
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — never null for a stream
- **Unusual when:** context · **requires:** direction, counterparty · **params:** resolution
- **Synthetic ledger:** ✔ · **PaySim:** ✔ (exact account codes only)
- **Test:** `tests/backend/test_features_units.py::test_ent_resolution_status`

**`ent_linked_record_count`** · v1 · post_period · int, records

- **Definition:** Number of records (ledger names and master records) in the stream's canonical entity.
- **Inputs:** payer_raw/payee_raw, direction, master records (when imported)
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — never null for a stream (at least 1: its own name)
- **Unusual when:** context · **requires:** direction, counterparty · **params:** resolution
- **Synthetic ledger:** ✔ · **PaySim:** ✔ (exact account codes only)
- **Test:** `tests/backend/test_features_units.py::test_ent_linked_record_count`

**`ent_match_confidence`** · v1 · post_period · float, evidence score

- **Definition:** Evidence score (0–1, not a probability) of the weakest confirmed match that formed the stream's canonical entity.
- **Inputs:** payer_raw/payee_raw, direction, master records (when imported)
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — null when the canonical entity has a single record (nothing was matched)
- **Unusual when:** lower · **requires:** direction, counterparty · **params:** resolution
- **Synthetic ledger:** ✔ · **PaySim:** ✔ (exact account codes only)
- **Test:** `tests/backend/test_features_units.py::test_ent_match_confidence`

**`ent_name_similarity_max`** · v1 · post_period · float, similarity 0–1

- **Definition:** Highest name similarity between the stream's counterparty name and a candidate record of a DIFFERENT canonical entity (a near-identical name that was not merged: a possible look-alike).
- **Inputs:** payer_raw/payee_raw, direction, master records (when imported)
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — null when blocking found no candidate in another canonical entity
- **Unusual when:** higher · **requires:** direction, counterparty · **params:** resolution
- **Synthetic ledger:** ✔ · **PaySim:** ✔ (exact account codes only)
- **Test:** `tests/backend/test_features_units.py::test_ent_name_similarity_max`

**`shared_identifier_count`** · v1 · post_period · int, entities

- **Definition:** Number of other canonical entities that share a tax ID, bank account, phone, e-mail or address with the master records of the stream's canonical entity (links, never merges).
- **Inputs:** payer_raw/payee_raw, direction, master records (when imported)
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — null when the stream's canonical entity has no master record
- **Unusual when:** higher · **requires:** direction, counterparty, master_data · **params:** resolution
- **Synthetic ledger:** ✔ with imported masters · **PaySim:** — (account codes; master data does not apply)
- **Test:** `tests/backend/test_features_units.py::test_shared_identifier_count`

**`employee_link_indicator`** · v1 · post_period · bool, flag

- **Definition:** True when the stream's canonical entity (an organisation) shares a bank account, phone, e-mail, tax ID or address with an employee master record.
- **Inputs:** payer_raw/payee_raw, direction, master records (when imported)
- **Scope:** counterparty stream over the whole dataset (all rows of the period)
- **Missing:** `null_if_not_applicable` — null when the canonical entity has no master record or is itself a person
- **Unusual when:** true · **requires:** direction, counterparty, master_data · **params:** resolution
- **Synthetic ledger:** ✔ with imported masters · **PaySim:** — (account codes; master data does not apply)
- **Test:** `tests/backend/test_features_units.py::test_employee_link_indicator`

### Not available yet (registered, never computed)

| Feature | Level | Meaning | Why not yet |
|---|---|---|---|
| `amount_to_balance_ratio` | transaction | Amount relative to the account's running balance (PaySim). | Balances are not canonical fields (PaySim keeps them in `extra`) and are known simulator artefacts; the decision belongs to the supervised PaySim adapter (P10). |
| `approver_is_poster` | transaction | True when the same user posted and approved the transaction (segregation of duties). | Approver and poster are not canonical fields: in the synthetic ledger they are unmapped columns ('Approved By', 'Posted By') kept verbatim in `extra`, which features never read. Needs canonical user fields first. |
| `bank_match_status` | transaction | Whether a ledger payment has a matching bank line (matched / date-shifted / fee / missing). | Needs ledger-to-bank reconciliation (P14). |
| `benford_subgroup_deviation` | entity | How far a subgroup's leading-digit distribution departs from Benford's law. | A dataset/subgroup statistical test, not a per-row feature; it belongs to the Benford module (P7). |
| `entity_linkage_count` | entity | Number of entities linked to this one through shared attributes or resolved aliases. | Needs the relationship graph (P13); P12 provides the resolved entities and their attribute links (see shared_identifier_count). |
| `geo_distance` | transaction | Distance between the counterparty's location and the company's operating locations. | No location data in the canonical transaction (the vendor master city is not ingested; no geocoding offline). |
| `user_after_hours_share` | entity | Share of a posting user's transactions made after hours (per-user time-pattern profile). | Needs a canonical posting-user field (see approver_is_poster). |
| `vendor_master_age_days` | transaction | Days between the vendor-master onboarding date and the transaction. | P12 imports masters for entity resolution with identity fields only (name, address, contacts, bank account, tax ID); onboarding dates are not a mapped master field yet. Until then cp_days_since_first gives the relationship age seen in the ledger. |
