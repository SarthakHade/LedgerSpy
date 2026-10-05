# P6 — Feature Engineering / Single Feature Registry: Acceptance Record

Date: 2026-10-05 · Scope: [`roadmap.md`](roadmap.md) §P6, [`architecture.md`](architecture.md) §5.3/§6 · Registry
documentation: [`feature-registry.md`](feature-registry.md) · Scenario → feature map:
[`synthetic-scenario-matrix.md`](synthetic-scenario-matrix.md).

P6 adds **features only**. It adds:
- no forensic rules or Benford tests (P7);
- no anomaly detection (P8);
- no risk fusion (P9);
- no supervised ML (P10);
- no frontend wiring.

The P1 in-browser heuristic is untouched and documented as legacy.

## Commands and results (final state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .         # All checks passed; 119 files already formatted
.venv/Scripts/python -m pytest                                             # 623 passed (456 P1–P5 + 167 P6)
.venv/Scripts/python -m pytest tests/backend/test_features_*.py            # 167 passed (P6)
.venv/Scripts/python -m pytest --ignore-glob="tests/backend/test_features_*"  # 456 passed (P2–P5, unchanged)
node tests/frontend/harness.js                                             # 24 passed, 0 failed (P1)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 14 passed, 0 failed (P1)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed (P4 panel)
python -m ledgerspy.features.lock                                          # regenerates registry_lock.json (59 features)
```

| P6 test file | Tests | What it proves |
|---|---|---|
| `test_features_units.py` | 59 | One hand-computed unit test per implemented feature (`test_<feature>`) |
| `test_features_registry.py` | 77 | Inventory (59 + 10); complete metadata for every feature; deferred features say why; every feature has its unit test; no duplicate registration; lock = definitions (a change without a version bump fails); definition changes are detectable; no feature implemented outside the registry; legacy JS documented; every feature named in the docs exists |
| `test_features_pit.py` | 19 | A dramatic future leaves the past unchanged; truncation property test (3 seeds × datetime/date-only × 3 cut-offs); input order irrelevant; timestamp ties isolated; undated rows; post-period semantics; peer fallback entity → account → type → global → unavailable; levels skipped without data support; baseline window; missing-data policy for every feature; determinism; config changes visible in the manifest; finite floats |
| `test_features_datasets.py` | 12 | PaySim gets exactly its 28 supported features (time features refused as simulation clock); PaySim fallback to type peers; synthetic gets the corporate matrix (58); scenarios visible in features (ground truth used only to evaluate); feature code reads no labels/extra/ground truth; changed PaySim labels and added label/scenario columns change nothing; same file → same matrix in another workspace; read-only and offline; P4 staleness; quality report flags |

## Acceptance criteria

| # | Criterion | Evidence | Result |
|---|---|---|---|
| 1 | A formal feature registry exists | `ledgerspy/features/spec.py`: `FeatureSpec`, `REGISTRY`, the `@feature` decorator, `deferred(...)`, `definition_sha256`, `lock_entries`. 69 registered: 59 implemented, 10 not available yet | ✅ |
| 2 | Every implemented feature has metadata and a version | Each records name, version, family, description, inputs, kind, units, semantics, scope, peer group, fallback, missing policy and detail, unusual direction, required capabilities, config params, reference and its one implementation. `test_every_feature_has_complete_metadata` (×59) | ✅ |
| 3 | Feature definitions are centralised | One module per level (`transaction.py`, `entity.py`) plus shared machinery (`history.py`). `test_no_feature_is_implemented_outside_the_registry`. The frontend's legacy heuristic is documented, not duplicated | ✅ |
| 4 | Features are deterministic | No randomness. Ties ordered by (source_row, txn_id). Three builds (same workspace, fresh workspace, new import) give identical `transaction_matrix_sha256` / `entity_matrix_sha256` (demo §6). Tests: `test_same_input_same_config_same_values`, `test_input_order_never_matters`, `test_the_same_file_gives_the_same_matrix_in_another_workspace` | ✅ |
| 5 | Point-in-time correctness is tested | History = strictly earlier timestamps; timestamp groups; undated rows excluded. Future-spike test, truncation property test, shuffle test, ties test (`test_features_pit.py`). Demo §4: 60 huge future payments change nothing for any original row | ✅ |
| 6 | Label / ground-truth leakage is impossible and tested | `FeatureRow` has no `extra` and no labels; the service reads only the view and P4 issues. Static scan of `ledgerspy/features`. PaySim with every label flipped or blanked → identical matrices. Synthetic ledger + `is_fraud` + `scenario_id` columns → identical. Features built from an inputs folder without `ground_truth/` | ✅ |
| 7 | Missing-data behaviour is explicit | Three policies (`null_if_input_missing`, `null_if_insufficient_history`, `null_if_not_applicable`) plus a per-feature `missing_detail`. Counts use 0 as a true value; `dq_*` are never null. `test_every_feature_follows_its_missing_data_policy` | ✅ |
| 8 | Peer-group / fallback behaviour is reusable and tested | `peer_levels` / `peer_baseline` in `history.py`, used by `amount_robust_z`, `hour_deviation` and the single-level peer features. The basis is reported per row. Three peer tests | ✅ |
| 9 | PaySim limitations explicitly handled | Adapter profile (`counterparty: payee`, `account_is_category: false`, `corporate: false`). `simulation_step` is never calendar time: 21 transaction + 10 entity features are `not_available` with the reason and get no column. `test_paysim_gets_only_the_features_it_can_support` | ✅ |
| 10 | P5 synthetic data produces the corporate feature matrix | 39/40 transaction features (`txn_type` not mapped) + 19/19 entity features with the case calendar and limits. Scenario evidence: S-A-01, S-H-01, S-H-03, S-L-01, S-K-01, S-K-03, S-F-01, S-J-01, S-P-02, S-U-10, S-C-01, S-V-01, S-B-01, S-U-14, S-Q-01 (`test_injected_scenarios_are_visible_in_the_features`, `test_entity_profiles_reflect_scenarios`) | ✅ |
| 11 | Feature-quality reporting detects constant / low-information features | `report.quality_report` reports rows, nulls, missing share, distinct, variance, mean, min/max, basis and flags (`constant`, `excessive_missing`, `insufficient_history`); nothing is dropped. Synthetic: `dq_is_normalized` constant. PaySim: 7 constant, 4 sparse. `test_quality_report_flags_constant_and_sparse_features` | ✅ |
| 12 | Documentation covers every implemented feature | [`feature-registry.md`](feature-registry.md): definition, formula, inputs, config, units, semantics, scope, peer group, missing behaviour, version, supported datasets (from real runs), limitations and test reference for all 59, plus the 10 deferred. Tested by `test_every_feature_named_in_the_docs_exists` | ✅ |
| 13 | No fake/unavailable feature presented as computed | Unsupported features have no column and a `not_available` reason. Deferred ones have no implementation (`fn=None`, version 0). The quality report gives no statistics for anything not computed (tested) | ✅ |
| 14 | All P1–P5 tests still pass | 456 backend + 24 Node + 14 browser + 6 P4 panel | ✅ |
| 15 | Ruff passes | `ruff check` and `ruff format --check` clean | ✅ |
| 16 | No external network access introduced | Features are pure Python with no new dependency. The whole suite runs under pytest-socket. `test_building_features_writes_nothing_and_needs_no_network` blocks sockets during a build and checks no database write happens | ✅ |

## Feature inventory (59 implemented)

✔ = computed on that dataset (synthetic: with the case's holiday calendar and approval limits; PaySim: sample, 10,127
rows). Full definitions: [`feature-registry.md`](feature-registry.md).

| # | Feature | Level | Family | Semantics | Version | Synthetic | PaySim |
|---|---|---|---|---|---|---|---|
| 1 | `amount_minor` | transaction | amount | row | v1 | ✔ | ✔ |
| 2 | `amount_pctl_prior` | transaction | amount | point_in_time | v1 | ✔ | ✔ |
| 3 | `amount_robust_z` | transaction | amount | point_in_time | v1 | ✔ | ✔ |
| 4 | `is_outflow` | transaction | amount | row | v1 | ✔ | ✔ |
| 5 | `log_amount` | transaction | amount | row | v1 | ✔ | ✔ |
| 6 | `round_level` | transaction | amount | row | v1 | ✔ | ✔ |
| 7 | `threshold_proximity` | transaction | amount | row | v1 | ✔ | ✘ |
| 8 | `txn_type` | transaction | amount | row | v1 | ✘ | ✔ |
| 9 | `amount_to_account_median_ratio` | transaction | behaviour | point_in_time | v1 | ✔ | ✘ |
| 10 | `amount_to_cp_median_ratio` | transaction | behaviour | point_in_time | v1 | ✔ | ✔ |
| 11 | `cp_count_30d_peer_ratio` | transaction | behaviour | point_in_time | v1 | ✔ | ✘ |
| 12 | `cp_log_amount_trend` | transaction | behaviour | point_in_time | v1 | ✔ | ✘ |
| 13 | `hour_deviation` | transaction | behaviour | point_in_time | v1 | ✔ | ✘ |
| 14 | `dq_has_duplicate_issue` | transaction | data_quality | dataset | v1 | ✔ | ✔ |
| 15 | `dq_has_missing_field` | transaction | data_quality | dataset | v1 | ✔ | ✔ |
| 16 | `dq_is_normalized` | transaction | data_quality | row | v1 | ✔ | ✔ |
| 17 | `dq_issue_count` | transaction | data_quality | dataset | v1 | ✔ | ✔ |
| 18 | `cp_days_since_first` | transaction | frequency | point_in_time | v1 | ✔ | ✘ |
| 19 | `cp_days_since_prev` | transaction | frequency | point_in_time | v1 | ✔ | ✘ |
| 20 | `cp_prior_count` | transaction | frequency | point_in_time | v1 | ✔ | ✔ |
| 21 | `is_reciprocal_pair` | transaction | relationship | point_in_time | v1 | ✔ | ✔ |
| 22 | `pair_prior_count` | transaction | relationship | point_in_time | v1 | ✔ | ✔ |
| 23 | `pair_share_of_payer_outflow` | transaction | relationship | point_in_time | v1 | ✔ | ✔ |
| 24 | `payee_distinct_payers_prior` | transaction | relationship | point_in_time | v1 | ✔ | ✔ |
| 25 | `payer_distinct_payees_prior` | transaction | relationship | point_in_time | v1 | ✔ | ✔ |
| 26 | `payer_new_payees_7d` | transaction | relationship | point_in_time | v1 | ✔ | ✘ |
| 27 | `cp_prev_amount_ratio` | transaction | sequence | point_in_time | v1 | ✔ | ✔ |
| 28 | `cp_reference_gap` | transaction | sequence | point_in_time | v1 | ✔ | ✘ |
| 29 | `day_of_week` | transaction | timing | row | v1 | ✔ | ✘ |
| 30 | `days_to_fiscal_year_end` | transaction | timing | row | v1 | ✔ | ✘ |
| 31 | `days_to_month_end` | transaction | timing | row | v1 | ✔ | ✘ |
| 32 | `hour_of_day` | transaction | timing | row | v1 | ✔ | ✘ |
| 33 | `is_after_hours` | transaction | timing | row | v1 | ✔ | ✘ |
| 34 | `is_holiday` | transaction | timing | row | v1 | ✔ | ✘ |
| 35 | `is_non_working_weekday` | transaction | timing | row | v1 | ✔ | ✘ |
| 36 | `posting_lag_days` | transaction | timing | row | v1 | ✔ | ✘ |
| 37 | `cp_amount_sum_30d` | transaction | velocity | point_in_time | v1 | ✔ | ✘ |
| 38 | `cp_count_30d` | transaction | velocity | point_in_time | v1 | ✔ | ✘ |
| 39 | `cp_count_7d` | transaction | velocity | point_in_time | v1 | ✔ | ✘ |
| 40 | `cp_velocity_ratio` | transaction | velocity | point_in_time | v1 | ✔ | ✘ |
| 41 | `ent_round_share` | entity | amount | post_period | v1 | ✔ | ✔ |
| 42 | `ent_log_amount_trend` | entity | behaviour | post_period | v1 | ✔ | ✘ |
| 43 | `ent_median_to_peer_ratio` | entity | behaviour | post_period | v1 | ✔ | ✘ |
| 44 | `ent_recent_amount_ratio` | entity | behaviour | post_period | v1 | ✔ | ✘ |
| 45 | `ent_max_gap_days` | entity | frequency | post_period | v1 | ✔ | ✘ |
| 46 | `ent_first_seen` | entity | profile | post_period | v1 | ✔ | ✘ |
| 47 | `ent_last_seen` | entity | profile | post_period | v1 | ✔ | ✘ |
| 48 | `ent_log_amount_dispersion` | entity | profile | post_period | v1 | ✔ | ✔ |
| 49 | `ent_mean_amount_minor` | entity | profile | post_period | v1 | ✔ | ✔ |
| 50 | `ent_median_amount_minor` | entity | profile | post_period | v1 | ✔ | ✔ |
| 51 | `ent_total_amount_minor` | entity | profile | post_period | v1 | ✔ | ✔ |
| 52 | `ent_txn_count` | entity | profile | post_period | v1 | ✔ | ✔ |
| 53 | `ent_fan_in` | entity | relationship | post_period | v1 | ✔ | ✔ |
| 54 | `ent_fan_out` | entity | relationship | post_period | v1 | ✔ | ✔ |
| 55 | `ent_share_of_flow` | entity | relationship | post_period | v1 | ✔ | ✔ |
| 56 | `ent_reference_sequential_share` | entity | sequence | post_period | v1 | ✔ | ✘ |
| 57 | `ent_after_hours_share` | entity | timing | post_period | v1 | ✔ | ✘ |
| 58 | `ent_non_working_share` | entity | timing | post_period | v1 | ✔ | ✘ |
| 59 | `ent_recent_count_ratio` | entity | velocity | post_period | v1 | ✔ | ✘ |

**Not available yet (10, registered and never computed):**
- `vendor_master_age_days`, `shared_identifier_count`, `employee_link_indicator`, `entity_linkage_count`: masters are not canonical yet (P12/P13).
- `approver_is_poster`, `user_after_hours_share`: approver and poster are not canonical fields.
- `bank_match_status`: P14.
- `benford_subgroup_deviation`: P7 subgroup test, not a row feature.
- `amount_to_balance_ratio`: PaySim balances are `extra` and simulator artefacts; P10.
- `geo_distance`: no location data.

**By point-in-time class:**
- 15 `row`;
- 22 `point_in_time`;
- 3 `dataset` (P4-derived);
- 19 `post_period` (all entity-level).

**Registry SHA-256:** `1b65ef8e0a3ef21309cd38c701ae92c06847ecc5734a8b57c5c998649951eb7f`.

## Feature-quality reports (computed by `ledgerspy.features.report`, real P3 → P4 → P6 runs)

### Synthetic company (`SYNTHETIC_ledger.csv`, imported without `ground_truth/`, case calendar and limits)

```
time 1.46s {"registered": 69, "computed": 58, "not_available": 1, "not_available_yet": 10, "transaction_rows": 7271, "entity_subjects": 159, "constant": ["dq_is_normalized"], "excessive_missing": [], "insufficient_history": ["amount_robust_z", "hour_deviation"]}
  amount_minor                    v1 row           null=0.000 distinct=7040  var=1.861e+15 min=1.77e+04   max=4.343e+08   
  amount_pctl_prior               v1 point_in_time null=0.006 distinct=568   var=0.08448   min=0          max=1           
  amount_robust_z                 v1 point_in_time null=0.006 distinct=7057  var=2.453     min=-15.03     max=15.08      {'account': 414, 'counterparty': 6539, 'global': 278, 'unavailable': 40} insufficient_history
  amount_to_account_median_ratio  v1 point_in_time null=0.058 distinct=6751  var=3.164     min=0.1459     max=47          
  amount_to_balance_ratio         not_available_yet: Balances are not canonical fields (PaySim keeps them in `extra`) and are known simulator artefacts; the decision belongs to the supervised PaySim adapter (P10).
  amount_to_cp_median_ratio       v1 point_in_time null=0.101 distinct=6365  var=0.6106    min=0.1461     max=23.11       
  approver_is_poster              not_available_yet: Approver and poster are not canonical fields: in the synthetic ledger they are unmapped columns ('Approved By', 'Posted By') kept verbatim in `extra`, which features never read. Needs canonical user fields first.
  bank_match_status               not_available_yet: Needs ledger-to-bank reconciliation (P14).
  cp_amount_sum_30d               v1 point_in_time null=0.000 distinct=5544  var=4.592e+15 min=0          max=9.849e+08   
  cp_count_30d                    v1 point_in_time null=0.000 distinct=19    var=9.382     min=0          max=18          
  cp_count_30d_peer_ratio         v1 point_in_time null=0.099 distinct=75    var=8.008     min=0          max=17          
  cp_count_7d                     v1 point_in_time null=0.000 distinct=8     var=0.9595    min=0          max=7           
  cp_days_since_first             v1 point_in_time null=0.022 distinct=7054  var=9.538e+04 min=0.01319    max=1092        
  cp_days_since_prev              v1 point_in_time null=0.022 distinct=6975  var=670.1     min=8.102e-05  max=494.1       
  cp_log_amount_trend             v1 point_in_time null=0.271 distinct=5237  var=0.01223   min=-1.518     max=0.8317      
  cp_prev_amount_ratio            v1 point_in_time null=0.022 distinct=6904  var=1.542     min=0.03879    max=35          
  cp_prior_count                  v1 point_in_time null=0.000 distinct=382   var=3659      min=0          max=381         
  cp_reference_gap                v1 point_in_time null=0.027 distinct=177   var=733.3     min=-806       max=808         
  cp_velocity_ratio               v1 point_in_time null=0.154 distinct=4863  var=0.8485    min=0          max=11.78       
  day_of_week                     v1 row           null=0.000 distinct=7     var=2.671     min=0          max=6           
  days_to_fiscal_year_end         v1 row           null=0.000 distinct=366   var=1.099e+04 min=0          max=365         
  days_to_month_end               v1 row           null=0.000 distinct=31    var=79.84     min=0          max=30          
  dq_has_duplicate_issue          v1 dataset       null=0.000 distinct=2     var=0.000825  min=0          max=1           
  dq_has_missing_field            v1 dataset       null=0.000 distinct=2     var=0.000138  min=0          max=1           
  dq_is_normalized                v1 row           null=0.000 distinct=1     var=0         min=0          max=0           constant
  dq_issue_count                  v1 dataset       null=0.000 distinct=3     var=0.001923  min=0          max=2           
  geo_distance                    not_available_yet: No location data in the canonical transaction (the vendor master city is not ingested; no geocoding offline).
  hour_deviation                  v1 point_in_time null=0.006 distinct=6736  var=1.778     min=0          max=13.01      {'account': 414, 'counterparty': 6539, 'global': 278, 'unavailable': 40} insufficient_history
  hour_of_day                     v1 row           null=0.000 distinct=6215  var=5.511     min=1.25       max=23.7        
  is_after_hours                  v1 row           null=0.000 distinct=2     var=0.002743  min=0          max=1           
  is_holiday                      v1 row           null=0.000 distinct=2     var=0.000138  min=0          max=1           
  is_non_working_weekday          v1 row           null=0.000 distinct=2     var=0.001648  min=0          max=1           
  is_outflow                      v1 row           null=0.000 distinct=2     var=0.1346    min=0          max=1           
  is_reciprocal_pair              v1 point_in_time null=0.000 distinct=2     var=0.009535  min=0          max=1           
  log_amount                      v1 row           null=0.000 distinct=7040  var=4.474     min=5.182      max=15.28       
  pair_prior_count                v1 point_in_time null=0.000 distinct=382   var=3670      min=0          max=381         
  pair_share_of_payer_outflow     v1 point_in_time null=0.004 distinct=5987  var=0.1283    min=0          max=1           
  payee_distinct_payers_prior     v1 point_in_time null=0.000 distinct=20    var=30.1      min=0          max=19          
  payer_distinct_payees_prior     v1 point_in_time null=0.000 distinct=119   var=1985      min=0          max=118         
  payer_new_payees_7d             v1 point_in_time null=0.000 distinct=26    var=5.859     min=0          max=25          
  posting_lag_days                v1 row           null=0.000 distinct=54    var=269.1     min=0          max=95          
  round_level                     v1 row           null=0.000 distinct=6     var=0.4038    min=0          max=5           
  threshold_proximity             v1 row           null=0.263 distinct=5197  var=0.06494   min=0.02523    max=0.9979      
  txn_type                        not_available: no transaction type is mapped
  vendor_master_age_days          not_available_yet: Vendor masters are not ingested as canonical data yet (P3 accepts transaction files only); planned with entity resolution (P12). Until then cp_days_since_first gives the relationship age seen in the ledger.
  benford_subgroup_deviation      not_available_yet: A dataset/subgroup statistical test, not a per-row feature; it belongs to the Benford module (P7).
  employee_link_indicator         not_available_yet: Needs employee and vendor masters as canonical entities (P12) and the attribute graph (P13).
  ent_after_hours_share           v1 post_period   null=0.000 distinct=4     var=0.000448  min=0          max=0.25        
  ent_fan_in                      v1 post_period   null=0.000 distinct=5     var=5.005     min=0          max=19          
  ent_fan_out                     v1 post_period   null=0.000 distinct=4     var=176.2     min=0          max=118         
  ent_first_seen                  v1 post_period   null=0.000 distinct=78    var=None      min=2023-04-01 max=2025-11-24  
  ent_last_seen                   v1 post_period   null=0.000 distinct=75    var=None      min=2024-02-27 max=2026-03-31  
  ent_log_amount_dispersion       v1 post_period   null=0.082 distinct=139   var=0.04812   min=0          max=1.307       
  ent_log_amount_trend            v1 post_period   null=0.145 distinct=132   var=0.02182   min=-0.03523   max=1.724       
  ent_max_gap_days                v1 post_period   null=0.082 distinct=144   var=4563      min=0.01389    max=494.1       
  ent_mean_amount_minor           v1 post_period   null=0.000 distinct=157   var=3.187e+15 min=2.065e+04  max=3.599e+08   
  ent_median_amount_minor         v1 post_period   null=0.000 distinct=157   var=3.519e+15 min=2.065e+04  max=4e+08       
  ent_median_to_peer_ratio        v1 post_period   null=0.082 distinct=146   var=5.346     min=0.1695     max=18.48       
  ent_non_working_share           v1 post_period   null=0.000 distinct=3     var=0.000188  min=0          max=0.1587      
  ent_recent_amount_ratio         v1 post_period   null=0.220 distinct=118   var=0.4277    min=0.4462     max=7.681       
  ent_recent_count_ratio          v1 post_period   null=0.025 distinct=125   var=0.457     min=0          max=3.46        
  ent_reference_sequential_share  v1 post_period   null=0.113 distinct=108   var=0.06361   min=0          max=1           
  ent_round_share                 v1 post_period   null=0.000 distinct=8     var=0.0871    min=0          max=1           
  ent_share_of_flow               v1 post_period   null=0.000 distinct=159   var=0.000582  min=2.174e-05  max=0.1977      
  ent_total_amount_minor          v1 post_period   null=0.000 distinct=157   var=3.256e+18 min=1.487e+06  max=1.352e+10   
  ent_txn_count                   v1 post_period   null=0.000 distinct=76    var=2297      min=1          max=382         
  entity_linkage_count            not_available_yet: Needs resolved entities and the relationship graph (P12, P13).
  shared_identifier_count         not_available_yet: Needs vendor/employee masters as canonical entities with attributes (P12 entity resolution).
  user_after_hours_share          not_available_yet: Needs a canonical posting-user field (see approver_is_poster).
  manifest: {"registry_sha256": "1b65ef8e0a3ef21309cd38c701ae92c06847ecc5734a8b57c5c998649951eb7f", "features_config_sha256": "9b260257d901703d137c60037a71e23a11c0d4a3bc27feeffb46b64ab27174ae", "transaction_matrix_sha256": "120c1e1901da9f30c28feee33978105a64c45935a2dbf1da5ddc16a20dc2f1e5", "entity_matrix_sha256": "d1cdd7400d8e0842c973bb105c89633870fcb1b054c2f4c8088d3ab0397cf39e"}
```

### PaySim sample (`data/raw/paysim_sample_10k.csv`)

```
time 2.34s {"registered": 69, "computed": 28, "not_available": 31, "not_available_yet": 10, "transaction_rows": 10127, "entity_subjects": 6967, "constant": ["dq_has_duplicate_issue", "dq_is_normalized", "ent_fan_out", "is_reciprocal_pair", "pair_prior_count", "pair_share_of_payer_outflow", "payer_distinct_payees_prior"], "excessive_missing": ["amount_to_cp_median_ratio", "cp_prev_amount_ratio", "ent_log_amount_dispersion", "pair_share_of_payer_outflow"], "insufficient_history": ["amount_robust_z"]}
  amount_minor                    v1 row           null=0.000 distinct=10079 var=7.324e+14 min=239        max=1e+09       
  amount_pctl_prior               v1 point_in_time null=0.268 distinct=202   var=0.0885    min=0          max=1           
  amount_robust_z                 v1 point_in_time null=0.268 distinct=7415  var=1.97      min=-10.76     max=8.92       {'counterparty': 924, 'txn_type': 6494, 'unavailable': 2703} insufficient_history
  amount_to_account_median_ratio  not_available: the account field is not a GL/category code for this adapter
  amount_to_balance_ratio         not_available_yet: Balances are not canonical fields (PaySim keeps them in `extra`) and are known simulator artefacts; the decision belongs to the supervised PaySim adapter (P10).
  amount_to_cp_median_ratio       v1 point_in_time null=0.909 distinct=924   var=19.74     min=0.003531   max=80.44       excessive_missing
  approver_is_poster              not_available_yet: Approver and poster are not canonical fields: in the synthetic ledger they are unmapped columns ('Approved By', 'Posted By') kept verbatim in `extra`, which features never read. Needs canonical user fields first.
  bank_match_status               not_available_yet: Needs ledger-to-bank reconciliation (P14).
  cp_amount_sum_30d               not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  cp_count_30d                    not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  cp_count_30d_peer_ratio         not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  cp_count_7d                     not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  cp_days_since_first             not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  cp_days_since_prev              not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  cp_log_amount_trend             not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  cp_prev_amount_ratio            v1 point_in_time null=0.818 distinct=1840  var=8674      min=0.001156   max=2627        excessive_missing
  cp_prior_count                  v1 point_in_time null=0.001 distinct=27    var=12.16     min=0          max=26          
  cp_reference_gap                not_available: no reference (invoice number) is mapped
  cp_velocity_ratio               not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  day_of_week                     not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  days_to_fiscal_year_end         not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  days_to_month_end               not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  dq_has_duplicate_issue          v1 dataset       null=0.000 distinct=1     var=0         min=0          max=0           constant
  dq_has_missing_field            v1 dataset       null=0.000 distinct=2     var=0.001676  min=0          max=1           
  dq_is_normalized                v1 row           null=0.000 distinct=1     var=0         min=0          max=0           constant
  dq_issue_count                  v1 dataset       null=0.000 distinct=5     var=0.2549    min=0          max=4           
  geo_distance                    not_available_yet: No location data in the canonical transaction (the vendor master city is not ingested; no geocoding offline).
  hour_deviation                  not_available: posting timestamps have no real time of day (time basis 'simulation_step')
  hour_of_day                     not_available: posting timestamps have no real time of day (time basis 'simulation_step')
  is_after_hours                  not_available: posting timestamps have no real time of day (time basis 'simulation_step')
  is_holiday                      not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  is_non_working_weekday          not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  is_outflow                      v1 row           null=0.000 distinct=2     var=0.1557    min=0          max=1           
  is_reciprocal_pair              v1 point_in_time null=0.001 distinct=1     var=0         min=0          max=0           constant
  log_amount                      v1 row           null=0.000 distinct=10079 var=4.158     min=1.221      max=16.12       
  pair_prior_count                v1 point_in_time null=0.001 distinct=1     var=0         min=0          max=0           constant
  pair_share_of_payer_outflow     v1 point_in_time null=1.000 distinct=0     var=None      min=None       max=None        constant,excessive_missing
  payee_distinct_payers_prior     v1 point_in_time null=0.001 distinct=46    var=46.26     min=0          max=56          
  payer_distinct_payees_prior     v1 point_in_time null=0.001 distinct=1     var=0         min=0          max=0           constant
  payer_new_payees_7d             not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  posting_lag_days                not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  round_level                     v1 row           null=0.000 distinct=4     var=0.007103  min=0          max=5           
  threshold_proximity             not_available: approval thresholds do not apply to this adapter (not a corporate ledger)
  txn_type                        v1 row           null=0.000 distinct=5     var=None      min=CASH_IN    max=TRANSFER    
  vendor_master_age_days          not_available_yet: Vendor masters are not ingested as canonical data yet (P3 accepts transaction files only); planned with entity resolution (P12). Until then cp_days_since_first gives the relationship age seen in the ledger.
  benford_subgroup_deviation      not_available_yet: A dataset/subgroup statistical test, not a per-row feature; it belongs to the Benford module (P7).
  employee_link_indicator         not_available_yet: Needs employee and vendor masters as canonical entities (P12) and the attribute graph (P13).
  ent_after_hours_share           not_available: posting timestamps have no real time of day (time basis 'simulation_step')
  ent_fan_in                      v1 post_period   null=0.000 distinct=45    var=17.89     min=0          max=62          
  ent_fan_out                     v1 post_period   null=0.000 distinct=1     var=0         min=0          max=0           constant
  ent_first_seen                  not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  ent_last_seen                   not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  ent_log_amount_dispersion       v1 post_period   null=0.891 distinct=762   var=0.6082    min=0.0005732  max=5.835       excessive_missing
  ent_log_amount_trend            not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  ent_max_gap_days                not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  ent_mean_amount_minor           v1 post_period   null=0.000 distinct=6944  var=2.509e+14 min=239        max=1e+09       
  ent_median_amount_minor         v1 post_period   null=0.000 distinct=6943  var=2.334e+14 min=239        max=1e+09       
  ent_median_to_peer_ratio        not_available: the account field is not a GL/category code for this adapter
  ent_non_working_share           not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  ent_recent_amount_ratio         not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  ent_recent_count_ratio          not_available: time basis is 'simulation_step': steps of a simulation are not calendar time
  ent_reference_sequential_share  not_available: no reference (invoice number) is mapped
  ent_round_share                 v1 post_period   null=0.000 distinct=3     var=0.000145  min=0          max=1           
  ent_share_of_flow               v1 post_period   null=0.000 distinct=6946  var=2e-06     min=3.258e-09  max=0.03219     
  ent_total_amount_minor          v1 post_period   null=0.000 distinct=6945  var=6.919e+15 min=239        max=2.362e+09   
  ent_txn_count                   v1 post_period   null=0.000 distinct=28    var=4.41      min=1          max=31          
  entity_linkage_count            not_available_yet: Needs resolved entities and the relationship graph (P12, P13).
  shared_identifier_count         not_available_yet: Needs vendor/employee masters as canonical entities with attributes (P12 entity resolution).
  user_after_hours_share          not_available_yet: Needs a canonical posting-user field (see approver_is_poster).
  manifest: {"registry_sha256": "1b65ef8e0a3ef21309cd38c701ae92c06847ecc5734a8b57c5c998649951eb7f", "features_config_sha256": "23c6e99f4c95b35279318bff6a9191541ba0543dc320ec96e10b098afa5adea6", "transaction_matrix_sha256": "cf8a8479deb99bc1c1a2da007707326afa6a2101e599a7d731be588621de30c3", "entity_matrix_sha256": "84404eee661003d4b6ee673e2f9ac1786b8e39d777c9401a37c5903ae0b77a1c"}
```

How to read these reports:
- **PaySim constant features are honest.** Every PaySim sender appears once, so pair, reciprocal and fan-out features are constant or empty. They are reported, not hidden or dropped.
- **Missing robust-z baselines.** 27% of PaySim rows have no robust-z baseline: the sample's first simulation step holds thousands of rows that share one timestamp, so they have no earlier history.
- **Synthetic `insufficient_history`.** This marks the 40 earliest rows, which have no baseline at any level.

## Point-in-time leakage demonstration (demo §4)

The vendor of S-A-01 (its spike is in the ground truth; used only to pick the vendor). 60 payments of 50× its last amount are appended after the period (in memory) and the matrix is rebuilt:

```
row LVC-JV-000419 (2023-06-20T15:35:47, vendor 'Ulvane Packaging Private Limited', amount 7302968)
  whole-period vendor median (what a leaking feature would use): 6959492 → 9959927
  feature                        without future   with 60 huge future rows
  amount_robust_z              -0.0359836988912531        -0.0359836988912531
  amount_to_cp_median_ratio    0.9845122322439886         0.9845122322439886
  amount_pctl_prior                        0.71                       0.71
  cp_prior_count                             10                         10
  cp_count_30d                                3                          3
  cp_velocity_ratio                        None                       None
  cp_log_amount_trend          -0.17849986966496192       -0.17849986966496192
  cp_prev_amount_ratio         0.7708328979369706         0.7708328979369706
  every feature of every original row unchanged: True
```

## Label / ground-truth isolation (demo §5)

```
PaySim, every isFraud label flipped → transaction matrix identical: True | labels rows: 20252
SYNTHETIC ledger + is_fraud + scenario_id columns → matrices identical: True | is_fraud went to labels: ['is_fraud']
features computed from an inputs dir without ground_truth/: yes (a temporary folder holding only SYNTHETIC_ledger.csv)
```

## Determinism (demo §6)

```
  run 1                                tx 120c1e1901da9f30  ent d1cdd7400d8e0842  cfg 9b260257d901
  run 2 (same workspace)               tx 120c1e1901da9f30  ent d1cdd7400d8e0842  cfg 9b260257d901
  run 3 (fresh workspace, new import)  tx 120c1e1901da9f30  ent d1cdd7400d8e0842  cfg 9b260257d901
  PaySim tx: cf8a8479deb99bc1 | re-run: cf8a8479deb99bc1
```

## Created / modified files

**Created:**
- **Package:** `ledgerspy/features/` — `__init__.py`, `spec.py`, `data.py`, `history.py`, `transaction.py`, `entity.py`, `deferred.py`, `matrix.py`, `report.py`, `service.py`, `lock.py`, `registry_lock.json`.
- **Tests:** `tests/backend/features_support.py`, `test_features_units.py`, `test_features_registry.py`, `test_features_pit.py`, `test_features_datasets.py`.
- **Docs:** `docs/feature-registry.md` and `docs/p6-acceptance.md`.

**Modified:**
- **Config and code:**
  - `config/default.yaml`: the `features` section.
  - `ledgerspy/config/schema.py`: `FeaturesConfig` and sub-models.
  - `pyproject.toml`: package data for `registry_lock.json`.
- **Docs:**
  - `docs/synthetic-scenario-matrix.md`: the "P6 features" column.
  - `docs/architecture.md`: status, config, §16 tree.
  - `docs/roadmap.md`: P6 ✔.
  - `README.md`.

No P1–P5 code was changed.

## Deviations from architecture.md / roadmap.md

1. **Roadmap acceptance "no constant features".** Constant features are reported, not removed, as P6's instructions require. On PaySim, seven are constant by the data's nature (every sender appears once). On the synthetic ledger, only `dq_is_normalized` is constant, because no P4 fix has been applied.
2. **Matrices are built on demand, not persisted.** No `features` table was added. The manifest (registry hash, per-feature version and definition hash, effective config hash, dataset data version, P4 run, matrix hashes) identifies a build exactly; P8–P10 store it with their runs and model cards.
3. **Entities are party keys, not resolved entities** (`payer_entity_id`/`payee_entity_id` are filled only by P12). Features that need masters or entity resolution are deferred, not approximated.
4. **Velocity windows.** Architecture §6 lists 1d/7d/30d. P6 implements 7d and 30d plus the velocity ratio; a 1-day count was left out as redundant with the 7-day window and `cp_days_since_prev`.
5. **Fan-in/fan-out and roundness against the population.** Architecture §6 judges these by population percentile and binomial test. P6 provides the raw counts and shares; judging them against the population is detector logic (P7/P8).
6. **Digit features (architecture §5.3).** These belong to the Benford module (P7). P6 registers `benford_subgroup_deviation` as deferred instead of adding per-row digit flags.
7. **Case calendar and approval limits.** The default config has no holidays and no approval limits. They are case-specific and supplied as config overrides (hashed into the manifest). Until then `is_holiday` and `threshold_proximity` are `not_available`.

## Known limitations

- **Performance.** Pure Python: about 1.5 s for the synthetic ledger (7,271 rows) and about 2.3 s for the PaySim sample on this laptop. Not yet profiled at 100k rows; P8 targets 100k rows in under 60 s.
- **Thresholds.** `threshold_proximity` cannot tell which limit governs a row; it uses the nearest configured limit above the amount.
- **Version hashing.** Shared helpers in `history.py` are not hashed into each feature's definition; changing their behaviour requires bumping the dependent features' versions (documented). Conversely, any edit to a feature function, even formatting-only, changes its hash and requires a version bump.
- **Validation scope.** The synthetic scenario checks show the features carry the injected signal. Detection quality (precision/recall) is measured only when detectors exist (P7/P8).
