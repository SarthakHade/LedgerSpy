# P5 — Synthetic Scenario Data: Acceptance Record

Date: 2026-10-05 · Scope defined in [`roadmap.md`](roadmap.md) §P5 · Data design in [`data-strategy.md`](data-strategy.md) §4 ·
Coverage map in [`synthetic-scenario-matrix.md`](synthetic-scenario-matrix.md).

P5 adds a **deterministic, offline generator of fictional test data** and a committed reference dataset with a
separate ground truth. It does not add feature engineering, fraud detectors, a risk engine, entity resolution,
graph analysis, reconciliation or ML. Those phases (P6+) will use this data for evaluation.

## Commands and results (final repository state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .        # All checks passed; 101 files already formatted
.venv/Scripts/python -m pytest                                            # 456 passed (317 P1–P4 + 139 P5)
.venv/Scripts/python -m pytest tests/backend/test_synth_*.py              # 139 passed (P5)
.venv/Scripts/python -m pytest --ignore-glob="tests/backend/test_synth_*" # 317 passed (P2–P4, unchanged)
node tests/frontend/harness.js                                            # 24 passed, 0 failed (P1)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 14 passed, 0 failed (P1)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed (P4 dashboard)
.venv/Scripts/ledgerspy synth --out <dir> --seed 42                       # regenerates the reference byte for byte
```

| P5 test file | Tests | What it proves |
|---|---|---|
| `test_synth_generation.py` | 24 | Determinism (same seed gives byte-identical files; the committed reference equals a fresh run; seed 43 differs), parameters, file schemas, `is_synthetic` markers, unique IDs, fictional identities, CLI, no network |
| `test_synth_dataset.py` | 27 | Ledger/bank/cash-flow coherence, normal routine, every letter A–X present, 63/45/18 counts, every ground-truth reference resolves, GT CSV = JSON, shared transactions, scenario semantics, leakage scan of every input file |
| `test_synth_scenarios.py` | 69 | **One data-level check per ground-truth scenario.** The registry must equal the set of GT scenario IDs. Also: controls never share rows with injected scenarios; shared rows are declared; affected entities include every counterparty; master attributes are shared only where a scenario says so |
| `test_synth_variants.py` | 3 | The same 63 checks pass on other variants: seed 43; seed 7 at scale 0.3 over 2 years; seed 2024 from 2021-01-01 over 4 years |
| `test_synth_pipeline.py` | 16 | P3 import of ledger and bank **without the ground-truth folder**; P4 row issues exactly equal to the planned ones; dataset-level P4 results; reference files through the P3 reader, profiler and evidence store; size limit |

## Acceptance criteria

| # | Criterion | Evidence | Result |
|---|---|---|---|
| 1 | Same seed gives the same output | CLI run twice with seed 42: all 12 files byte-identical, and identical to the committed reference (table below). `test_same_seed_is_byte_identical`, `test_committed_reference_equals_a_fresh_generation` | ✅ |
| 2 | A different seed gives a different output | Seed 43: 11 of 12 files differ, `dataset_sha256` differs. The holiday calendar is the same by design: it depends on the period, not the seed | ✅ |
| 3 | 2–3 year synthetic company | 2023-04-01 → 2026-03-31 (3 fiscal years); two group companies (parent and subsidiary). `--years` accepts 2–5 | ✅ |
| 4 | Vendor master | `SYNTHETIC_vendor_master.csv`, 99 vendors, 10 categories. Names are unique up to their legal form, so there are no unplanned look-alikes | ✅ |
| 5 | Employee master | `SYNTHETIC_employee_master.csv`, 37 employees with department, role, manager, approval limit, home address, bank account | ✅ |
| 6 | Ledger | `SYNTHETIC_ledger.csv`, 7,271 rows (AP, claims, receipts, payroll, statutory, bank charges, intercompany) | ✅ |
| 7 | Bank statement | `SYNTHETIC_bank_statement.csv`, 7,267 rows over 2 accounts. Running balance continuous per account (P4 `balance_continuity` ok). The row count reconciles with the ledger by construction (test) | ✅ |
| 8 | Cash-flow history | `SYNTHETIC_cashflow_monthly.csv`, 72 rows (36 months × 2 companies), derived from and coherent with the bank (test) | ✅ |
| 9 | Separate ground truth | `ground_truth/SYNTHETIC_ground_truth.{json,csv}` in its own folder, outside the inputs list in the manifest. Fields per scenario: `scenario_id`, type, control flag, severity, description, expected detection targets and phases, affected ledger/bank/vendor/employee/customer/group IDs, stages, injection parameters, start/end dates, explanation evidence, `also_part_of`. Plus `expected_p4_row_issues` | ✅ |
| 10 | All required scenarios present | Letters A–X: 45 injected scenarios, including the two added in this pass, S-H-03 (dormant-vendor reactivation) and S-K-03 (year-end late-night clustering). Every item in `data-strategy.md` §4 is covered (see the matrix) | ✅ |
| 11 | Normal control cases present | 18 controls (S-U-01…16, S-M-02, S-N-04). Each has a data-level check that it resembles a suspicious pattern while being legitimate. None shares a row with an injected scenario (test) | ✅ |
| 12 | Every ground-truth reference resolves | All 1,653 ledger, bank and entity references resolve against the generated files and against the P3 canonical transactions (demo below and tests) | ✅ |
| 13 | **The generated dataset contains every scenario** (not just Python definitions) | `test_scenario_is_observable_in_the_generated_data` × 63 reads the ledger, bank statement and masters and checks the scenario's defining property. Examples: S-H-03 has no payment for 494 days and then 6 payments in ≤ 21 days at ~3× the earlier level; S-K-03 has 5 postings at 21:30–22:00 on the fiscal year's last working day, by one AP user, at > 3× the supplier median | ✅ |
| 14 | No ground-truth leakage into canonical transactions | P3 run with no `ground_truth/` folder present. Canonical rows and `extra` contain no label-like column or word, and no scenario ID or type (demo below; `test_no_ground_truth_in_canonical_transactions`; regex scan of every input file). Entity IDs follow onboarding/joining order, so IDs give nothing away | ✅ |
| 15 | P3 ingestion works | Ledger and bank upload through the real API. The auto-proposed mapping needs no confirmation; all rows convert with 0 issues; the generator's Txn IDs become canonical IDs. The 7 master/reference files pass the P3 reader, profiler and evidence store. They are not transaction datasets, so upload refuses `kind=vendor_master` until P12/P18 | ✅ |
| 16 | P4 validation works with expected results | Ledger row issues **exactly** equal the planned set (12 issues in 6 checks, each explained by a scenario or a control). Readiness 99.9 `ready_with_warnings`. Bank: 0 row issues, readiness 100.0 `ready` | ✅ |
| 17 | Offline / no network access | The generator imports no network module. `test_generation_is_offline` patches `connect`, `connect_ex`, `getaddrinfo` and `create_connection` to fail during generation. The whole suite runs under pytest-socket (`--allow-hosts=127.0.0.1`). The demo used only loopback HTTP | ✅ |
| 18 | Feature/scenario coverage matrix | [`synthetic-scenario-matrix.md`](synthetic-scenario-matrix.md): 30 behaviours → scenarios → controls → source fields → distinct affected records → detector phase, plus the full 63-row scenario table generated from the ground truth | ✅ |

## Determinism (final generator, via the CLI)

`dataset_sha256` = SHA-256 of canonical JSON `{file: sha256}` over all files except the manifest.

| | seed 42 run A | seed 42 run B | committed reference | seed 43 |
|---|---|---|---|---|
| `dataset_sha256` | `f62613fffb0b1e82…5f44f` | `f62613fffb0b1e82…5f44f` | `f62613fffb0b1e82…5f44f` | `8ba7882eb05a3b79…6cdd4` |
| `SYNTHETIC_ledger.csv` | `68fce3beb05d` | `68fce3beb05d` | `68fce3beb05d` | `bcc13c059d16` |
| `SYNTHETIC_bank_statement.csv` | `f801b97cb84c` | `f801b97cb84c` | `f801b97cb84c` | `71811715cffc` |
| `SYNTHETIC_vendor_master.csv` | `ac6695814e9c` | `ac6695814e9c` | `ac6695814e9c` | `84a9357336a4` |
| `ground_truth/SYNTHETIC_ground_truth.json` | `d456d021ae87` | `d456d021ae87` | `d456d021ae87` | `1dabcb6489b6` |

Full reference hash: `f62613fffb0b1e82f50d1e6ad08a1ac8942fb3832904d3209e93a57777f5d44f`; seed 43:
`8ba7882eb05a3b7955479adfa4a0c235b9ace0856ca21ba0052d319738b6cdd4`. `diff -r` run A vs run B: no differences.

## Dataset structure (`data/synthetic/reference/`, 2.9 MB)

```
SYNTHETIC_group_entities.csv       2   companies (approval limits from config, business hours)
SYNTHETIC_employee_master.csv      37  employees
SYNTHETIC_vendor_master.csv        99  vendors
SYNTHETIC_customer_master.csv      26  customers
SYNTHETIC_relationships.csv        3   declared relationships
SYNTHETIC_holiday_calendar.csv     18  holidays
SYNTHETIC_ledger.csv               7,271 postings          ┐ transaction files, importable through P3
SYNTHETIC_bank_statement.csv       7,267 statement lines   ┘
SYNTHETIC_cashflow_monthly.csv     72  company-months
SYNTHETIC_manifest.json            parameters, per-file sha256/bytes/rows, dataset_sha256
ground_truth/                      evaluation only, never an input
  SYNTHETIC_ground_truth.json      63 scenarios + expected P4 row issues
  SYNTHETIC_ground_truth.csv       1,653 (scenario, record kind, record id) rows
```

## End-to-end demonstration (generator → files → P3 → P4, over HTTP)

1. `ledgerspy synth --out final/s42a --seed 42` generated the files.
2. Only the 9 input CSVs were copied to `inputs_only/`; no `ground_truth/` folder exists there.
3. A fresh `ledgerspy serve --workspace final/ws` ran on 127.0.0.1, with the session token.
4. Through the API, the demo:
   - created a case with period 2023-04-01 → 2026-03-31;
   - uploaded the ledger and the bank statement;
   - confirmed each proposed mapping unchanged;
   - ran P4 on both.
5. The ground truth was loaded only afterwards, to score the results.

| | Ledger | Bank statement |
|---|---|---|
| Rows uploaded / canonical transactions | 7,271 / 7,271 | 7,267 / 7,267 |
| Mapping needing confirmation · conversion issues · label columns | none · 0 · none | none · 0 · none |
| ID source | `source_column` (generator Txn IDs) | `source_column` |
| P4 readiness | 99.9 `ready_with_warnings`, 0 blocking | 100.0 `ready`, 0 blocking |
| P4 row issues | duplicate_source_id 4, exact_duplicate 1, malformed_identifier 1, missing_optional 1, near_duplicate 2, needs_normalization 3 | 0 |
| P4 dataset-level | unmapped_columns 8 (INFO); balance_continuity `not_available` (a GL has no running balance) | unmapped_columns 3; field_coverage 2 (no payee/payer/document date/type in a bank statement); near_duplicate `not_available` (no payee column); balance_continuity **ok** |
| Matches `expected_p4_row_issues` | **exactly** (S-C-01, S-C-02, S-D-01, S-U-05, S-U-13, S-V-01) | **exactly** (none expected) |

Other counts from the same run:
- entities: 2 companies, 37 employees, 99 vendors, 26 customers, 3 relationships;
- cash flow: 72 rows;
- every one of the 1,653 ground-truth references resolved (0 missing), and no scenario lacks affected records.

**Ground-truth isolation** (same run):
- **Canonical record keys:** the canonical fields plus `extra` (the unmapped source columns: Company Code, GL Account Name, Counterparty ID/Type, Payment Mode, Approved By, Posted By, Value Dt, Balance, is_synthetic).
- **Label-like words** (`fraud|ghost|scenario|suspicious|inject|anomal|ground truth|is_control|shell|fake`) in canonical records: none.
- **Scenario IDs or types** in canonical records: none.

## Scenario inventory (from the ground truth; entities = vendors + employees + customers + group companies)

| Scenario | Type | Kind | Severity | Ledger rows | Bank lines | Entities | Total records |
|---|---|---|---|---|---|---|---|
| S-A-01 | amount_anomaly | injected | high | 1 | 0 | 1 | 2 |
| S-A-02 | amount_anomaly | injected | medium | 1 | 0 | 1 | 2 |
| S-B-01 | round_number | injected | medium | 18 | 0 | 1 | 19 |
| S-B-02 | round_number | injected | medium | 35 | 0 | 1 | 36 |
| S-C-01 | duplicate_transaction | injected | high | 2 | 0 | 1 | 3 |
| S-C-02 | duplicate_transaction | injected | high | 2 | 0 | 1 | 3 |
| S-D-01 | near_duplicate | injected | high | 2 | 0 | 1 | 3 |
| S-D-02 | near_duplicate | injected | medium | 2 | 0 | 1 | 3 |
| S-E-01 | lookalike_vendor | injected | high | 5 | 0 | 2 | 7 |
| S-E-02 | lookalike_vendor | injected | high | 5 | 0 | 2 | 7 |
| S-F-01 | ghost_vendor | injected | critical | 6 | 0 | 2 | 8 |
| S-G-01 | sudden_vendor_appearance | injected | high | 3 | 0 | 1 | 4 |
| S-G-02 | sudden_vendor_appearance | injected | medium | 6 | 0 | 1 | 7 |
| S-H-01 | velocity_spike | injected | medium | 9 | 0 | 1 | 10 |
| S-H-02 | velocity_spike | injected | medium | 6 | 0 | 1 | 7 |
| S-H-03 | dormant_vendor_reactivation | injected | high | 17 | 0 | 1 | 18 |
| S-I-01 | first_time_high_value | injected | medium | 1 | 0 | 1 | 2 |
| S-J-01 | threshold_splitting | injected | high | 3 | 0 | 1 | 4 |
| S-J-02 | threshold_splitting | injected | high | 3 | 0 | 2 | 5 |
| S-K-01 | temporal_anomaly | injected | medium | 6 | 0 | 2 | 8 |
| S-K-02 | temporal_anomaly | injected | medium | 10 | 0 | 2 | 12 |
| S-K-03 | period_end_clustering | injected | medium | 5 | 0 | 2 | 7 |
| S-L-01 | sequence_pattern | injected | high | 4 | 0 | 1 | 5 |
| S-M-01 | employee_vendor_relationship | injected | critical | 7 | 0 | 2 | 9 |
| S-M-02 | employee_vendor_relationship | control | none | 20 | 0 | 2 | 22 |
| S-N-01 | shared_attributes | injected | high | 4 | 0 | 2 | 6 |
| S-N-02 | shared_attributes | injected | critical | 0 | 0 | 2 | 2 |
| S-N-03 | shared_attributes | injected | critical | 0 | 0 | 2 | 2 |
| S-N-04 | shared_attributes | control | none | 154 | 0 | 2 | 156 |
| S-N-05 | shared_attributes | injected | medium | 0 | 0 | 2 | 2 |
| S-O-01 | circular_trading | injected | critical | 4 | 0 | 4 | 8 |
| S-O-02 | circular_trading | injected | critical | 4 | 0 | 6 | 10 |
| S-P-01 | concentration_fan_in | injected | high | 12 | 0 | 3 | 15 |
| S-P-02 | concentration_fan_out | injected | high | 8 | 0 | 9 | 17 |
| S-Q-01 | gradual_risk_escalation | injected | high | 63 | 0 | 2 | 65 |
| S-R-01 | silent_small_fraud | injected | medium | 343 | 0 | 1 | 344 |
| S-S-01 | bank_mismatch | injected | high | 3 | 0 | 3 | 6 |
| S-S-02 | bank_mismatch | injected | high | 0 | 3 | 0 | 3 |
| S-S-03 | bank_mismatch | injected | low | 4 | 4 | 4 | 12 |
| S-S-04 | bank_mismatch | injected | low | 3 | 1 | 1 | 5 |
| S-S-05 | bank_mismatch | injected | low | 3 | 1 | 1 | 5 |
| S-S-06 | bank_mismatch | injected | low | 3 | 3 | 3 | 9 |
| S-S-07 | bank_mismatch | injected | critical | 1 | 1 | 2 | 4 |
| S-T-01 | digit_distribution_anomaly | injected | medium | 324 | 0 | 1 | 325 |
| S-U-01 | legit_round_numbers | control | none | 36 | 0 | 1 | 37 |
| S-U-02 | legit_round_numbers | control | none | 72 | 0 | 2 | 74 |
| S-U-03 | legit_similar_names | control | none | 0 | 0 | 2 | 2 |
| S-U-04 | legit_new_vendor | control | none | 58 | 0 | 1 | 59 |
| S-U-05 | legit_large_payment_installments | control | none | 3 | 0 | 1 | 4 |
| S-U-06 | legit_seasonal_velocity | control | none | 46 | 0 | 1 | 47 |
| S-U-07 | legit_near_threshold | control | none | 3 | 0 | 3 | 6 |
| S-U-08 | legit_period_end_overtime | control | none | 12 | 0 | 2 | 14 |
| S-U-09 | legit_multi_party_chain | control | none | 6 | 0 | 2 | 8 |
| S-U-10 | legit_reciprocal_flows | control | none | 37 | 0 | 2 | 39 |
| S-U-11 | legit_fan_in | control | none | 0 | 0 | 16 | 16 |
| S-U-12 | legit_batch_payments | control | none | 72 | 0 | 0 | 72 |
| S-U-13 | legit_near_duplicate | control | none | 2 | 0 | 1 | 3 |
| S-U-14 | legit_sequential_invoices | control | none | 36 | 0 | 1 | 37 |
| S-U-15 | legit_employee_vendor_relationship | control | none | 0 | 0 | 2 | 2 |
| S-U-16 | legit_shared_attributes | control | none | 0 | 0 | 2 | 2 |
| S-V-01 | data_quality | injected | low | 5 | 0 | 2 | 7 |
| S-W-01 | inconsistent_combination | injected | low | 4 | 0 | 3 | 7 |
| S-X-01 | composite_risk | injected | critical | 6 | 0 | 2 | 8 |

63 scenarios (45 injected, 18 controls), 1653 scenario-record links (= the 1,653 rows of the ground-truth CSV).

## Problems found and fixed in this final verification pass

Checking each scenario against the actual data, not only the Python definitions, and running the checks on 16 more seeds plus scale/period variants, found these generator defects. All are fixed and covered by tests.

| Problem | Effect | Fix |
|---|---|---|
| Fixed scenario names could collide with generated names ("Kesrin Freight Carriers Pvt Ltd" existed twice) | An unplanned exact-name duplicate vendor. S-N-04 picked up the other vendor's 70 rows | Firm names are unique up to their legal form (`names.core`). Prescribed names take a free stem. `new_vendor` refuses a taken name |
| E-mail domain came from the first word of the name | 28 groups of unrelated vendors shared an e-mail domain, a false shared-attribute signal | The domain is the full name core, so it is unique per firm |
| GT `affected` entity lists named only the scenario's main entity | e.g. S-W-01's RTGS rows paid V0055/V0056 but only V0024 was listed | Every counterparty of an affected row is added. S-S-07 lists the employee who received the money |
| Late injectors picked rows belonging to controls or other scenarios | S-S-03/S-S-07 rows were also S-U-10 control rows | Row-picking injectors run last and skip every row/entity already used. Their vendors are reserved first |
| S-U-06 seasonal control relied on Poisson luck | For seed 42 the Oct–Dec rise was not visible in two of three years | The control vendor has its own cadence: 5–6 payments in festive months, 2–3 otherwise |
| Descriptions or parameters not always true | S-G-01 "~4x" was 2.6–3.3×; S-C-02 "9 days" could be 10; S-H-03 "over a year" is false in a 2-year dataset | Amounts are relative to the category median. Parameters and descriptions come from the actual values |
| Edge cases on other seeds/scales | Scale ≤ 0.2 crashed (too few sales executives); D-02 was not a transposition when the digits were equal; the "only supplier in Morvik" relocation broke a shared address; a vendor could run out | Pool floors; transposition of the lowest differing digit pair; the remote city is reserved; vendors for late injectors are reserved up front |

After the fixes, the 63 scenario checks plus the 5 cross-scenario checks pass for:
- 16 seeds (1, 2, 3, 7, 8, 13, 21, 34, 43, 55, 89, 144, 233, 377, 610, 2024);
- scales 0.05, 0.2, 0.3, 0.5, 2, 3 and 5;
- periods of 2, 4 and 5 years, and a 2021 start.

Three of these variants are in the committed test suite.

## Deviations and known limitations

- **Not built in P5.** The rendered PDF bank statement is deferred to P14b. There is no UI "synthetic data" banner: the UI does not list synthetic datasets separately yet, but every file, column and the P3 case name say SYNTHETIC.
- **Ring lengths.** Circular-trading rings have k = 3 and 4; the plan said 3–6.
- **Masters and cash flow are not P3 datasets yet.** The upload API accepts transaction files only (`kind=ledger`). The 7 master/reference files were verified through the P3 safe reader, profiler and evidence store, not imported as canonical data; P4 therefore ran on the ledger and the bank statement only.
- **Bit-identity across Python versions/platforms is not promised.** `random`'s float transforms depend on the platform. The manifest records the Python version (3.12.4). The determinism tests compare on the same interpreter.
- **Fictional but plausible names.** Person names combine common Indian first names and surnames, so a combination can coincide with a real person's name. No other attribute is real: phones `+91-0…`, accounts `SYN…`, tax IDs `SYN-TX-…`, `.example` e-mails, invented cities.
- **Known, declared overlaps:**
  - S-X-01 composes S-A-02, S-G-02, S-H-02 and S-N-05.
  - S-Q-01 contains S-B-02 and S-K-02.
  - S-F-01/S-N-02 and S-M-01/S-N-03 describe the same entities from two angles.
  - S-D-01 and S-D-02 use the same vendor.
  - E0008 is the AP user in S-K-01/02/03 and S-Q-01.
  - C0001 is a member of the fan-in control S-U-11 and the payer in S-S-05, which uses new rows, not control rows.
- **Entity-only scenarios.** S-N-02, S-N-03, S-N-05, S-U-03, S-U-11, S-U-15 and S-U-16 are master-data scenarios by design, with 0 ledger rows of their own.
- **What the checks prove.** They show that the data has each property. They do not show that a future detector will find it; that is what P6–P14 will be evaluated on. Realism against real fraud cases is not validated.
- **Reference size.** The committed reference is 2.9 MB, under the 4 MB test limit. Larger sets are generated with `--scale`, `--years` and `--seed`.

## Files

Created:
- **Package:** `ledgerspy/synth/` — `__init__.py`, `rng.py`, `names.py`, `calendar.py`, `model.py`, `context.py`, `entities.py`, `ledger.py`, `scenarios.py`, `bank.py`, `output.py`, `generator.py`.
- **Reference data:** `data/synthetic/reference/` — 9 `SYNTHETIC_*.csv` inputs, `SYNTHETIC_manifest.json`, `ground_truth/SYNTHETIC_ground_truth.{json,csv}`.
- **Tests:** `tests/backend/test_synth_generation.py`, `test_synth_dataset.py`, `test_synth_scenarios.py`, `test_synth_variants.py`, `test_synth_pipeline.py`.
- **Docs:** `docs/synthetic-scenario-matrix.md` and `docs/p5-acceptance.md`.

Modified:
- **Config and code:**
  - `config/default.yaml`: the `synth` section.
  - `ledgerspy/config/schema.py`: `SynthConfig`, `ApprovalLimits`.
  - `ledgerspy/cli.py`: `ledgerspy synth`.
  - `pyproject.toml`: S311 ignored for the seeded generator.
  - `.gitattributes`: `data/synthetic/** -text`, so checkouts keep the hashed bytes.
- **Docs:**
  - `docs/data-strategy.md`: §4 as built.
  - `docs/architecture.md`: status, §16 tree, config.
  - `docs/roadmap.md`: P5 ✔.
  - `README.md`.
  - `data/README.md`.
