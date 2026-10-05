# Synthetic scenario / feature coverage matrix

Generated data: `data/synthetic/reference/` (`ledgerspy synth`, seed 42, 2023-04-01 → 2026-03-31). Scenario IDs refer to `ground_truth/SYNTHETIC_ground_truth.json`, which lists every affected transaction, bank line and entity.

This is a **test-data coverage map**. It says which data exists to evaluate each future detector; it does not claim that any detector exists yet (P5 implements none). "Partial" marks behaviours the data can support only in part.

| Feature / forensic behaviour | Injected scenarios | Normal controls (must not be flagged) | Required source fields | Affected records (distinct, injected scenarios only) | Future detector phase |
|---|---|---|---|---|---|
| Amount anomaly | S-A-01, S-A-02 | S-U-05 (legit ₹48 lakh installments) | Ledger: `Amount`, `Dr/Cr`, `Payee`, `Counterparty ID` | 2 ledger, 2 entities | P6 features, P8 robust z / Isolation Forest, P9 |
| Benford / digit behaviour | S-T-01 (unlabelled subgroup, n ≥ 300) | the rest of the ledger (log-normal amounts) | Ledger: `Amount`, `Dr/Cr`, `Payee`, `Counterparty ID` | 324 ledger, 1 entities | P7 Benford (first digit, subgroups, MAD) |
| Fuzzy entity matching | S-E-01, S-E-02, S-P-01 | S-U-03 (similar names, unrelated firms) | Vendor master: name, address, phone, bank account, tax ID, onboarding date | 22 ledger, 7 entities | P12 entity resolution |
| Duplicates (exact) | S-C-01 (double posting), S-C-02 (re-paid invoice) | S-U-05 (installments on one invoice) | Ledger: all fields, `Invoice No` | 4 ledger, 2 entities | P4 (exact_duplicate, duplicate_source_id), P7 |
| Duplicates (near) | S-D-01 (next day, ref +A), S-D-02 (transposed digits) | S-U-13 (two meters, same amount, same day) | Ledger: `Payee`, `Counterparty ID`, `Amount`, `Dr/Cr`, `Invoice No`, `Posting Date` (date + time) | 4 ledger, 1 entities | P4 near_duplicate (S-D-01, S-U-13), P7 fuzzy |
| Circular trading | S-O-01 (k=3), S-O-02 (k=4) | S-U-09 (chains), S-U-10 (reciprocal vendor/customer) | Ledger: `Company Code`, `Payer`, `Payee`, `Posting Date` (date + time); vendor + customer masters | 8 ledger, 8 entities | P12 (vendor = customer), P13 temporal cycles |
| Bank mismatch | S-S-* (S-S-01 … S-S-07: ledger-only, bank-only, delayed, batched, one-to-many, fee, payee mismatch) | every other row: matched with 0–2 working-day shifts; S-U-12 payroll batch | Bank statement: `Txn Date`, `Narration`, `Ref No`, `Withdrawal`, `Deposit`, `Balance`; ledger | 17 ledger, 13 bank, 13 entities | P14 reconciliation |
| Frequency | S-H-01, S-H-03 (dormant vendor reactivated), S-R-01 | S-U-06 (seasonal Oct–Dec rise) | Ledger: `Payee`, `Counterparty ID`, `Posting Date` (date + time) | 369 ledger, 3 entities | P6 rolling counts, P8 |
| Velocity | S-H-01, S-H-02, S-H-03, S-Q-01 | S-U-06 | Ledger: `Payee`, `Counterparty ID`, `Posting Date` (date + time), `Amount`, `Dr/Cr` | 95 ledger, 5 entities | P6, P8 |
| Time anomalies | S-K-01, S-K-02, S-K-03 (year-end late-night cluster) | S-U-08 (quarter-end overtime) | Ledger: `Posting Date` (date + time), `Posted By`; holiday calendar; business hours (group entities) | 21 ledger, 4 entities | P7 after-hours / weekend / holiday / period-end rules |
| Behavioural deviation | S-A-01, S-H-01, S-H-03, S-I-01, S-Q-01 | per-vendor/employee normal profiles | Ledger: `Amount`, `Dr/Cr`, `Payee`, `Counterparty ID`, `Posting Date` (date + time) | 91 ledger, 6 entities | P8 |
| First-time activity | S-I-01, S-G-01, S-L-01 | S-U-04 (new vendor, ordinary amounts) | Ledger: `Amount`, `Dr/Cr`, `Payee`, `Counterparty ID`; master onboarding date | 8 ledger, 3 entities | P6 (history), P7, P8 |
| Sequence patterns | S-L-01 (small→small→small→large), S-H-03 (active→dormant→burst), S-Q-01 stages | — | Ledger: `Payee`, `Counterparty ID`, `Amount`, `Dr/Cr`, `Posting Date` (date + time) | 84 ledger, 4 entities | P7 suspicious sequences |
| Threshold buffering / splitting | S-J-01 (claims), S-J-02 (vendor), S-F-01 (just under the CFO limit) | S-U-07 (isolated claims at 97% of the limit) | Ledger: `Amount`, `Dr/Cr`, `Payee`, `Counterparty ID`, `Approved By`, `Posted By`; limits in group entities + config | 12 ledger, 5 entities | P7 (thresholds from config) |
| New-vendor velocity | S-G-01, S-X-01 (S-G-02, S-H-02) | S-U-04 | Ledger: `Payee`, `Counterparty ID`, `Posting Date` (date + time); vendor onboarding date | 9 ledger, 3 entities | P6 vendor age, P7, P9 |
| Employee/vendor linkage | S-F-01, S-M-01, S-N-01, S-N-02, S-N-03, S-N-05 | S-M-02/S-U-15 (declared conflict), S-N-04/S-U-16 (declared sister concerns) | Vendor + employee masters (address, phone, bank account), relationships register, `Approved By`/`Posted By` | 17 ledger, 8 entities | P12 attribute links, P13 attribute edges, P15 segregation |
| Peer comparison | S-R-01 (department), S-B-01 (category), S-T-01 | normal peers in each category/department | Ledger: `Amount`, `Dr/Cr`, `Counterparty Type`; employee master department | 685 ledger, 3 entities | P6 peer groups, P8 |
| Round-number bias | S-B-01, S-B-02, S-F-01 | S-U-01 (rent), S-U-02 (retainers) | Ledger: `Amount`, `Dr/Cr`, `Payee`, `Counterparty ID` | 59 ledger, 4 entities | P7 roundness vs population |
| Sequential invoices | S-F-01 (001…006) | S-U-14 (single-client canteen contractor) | Ledger: `Invoice No`, `Payee`, `Counterparty ID` | 6 ledger, 2 entities | P7 |
| Data quality | S-V-01 (+ the duplicates above) | — | Ledger: `Payee`, `Invoice No`, `Description` | 5 ledger, 2 entities | P4 (asserted exactly in tests) |
| Inconsistent combinations | S-W-01 (GL vs vendor category; RTGS under ₹2 lakh) | — | Ledger: `GL Account`, `Payment Mode`; vendor category | 4 ledger, 3 entities | P7 |
| Geographic / contextual | S-X-01 (only supplier in Morvik, local service from a remote city) | all other vendors in operating cities | Vendor master `city`; employee `location` | 6 ledger, 2 entities | P7/P9 — **partial**: city only, no transaction geolocation |
| Personalised behaviour | S-H-01 vs S-U-06, S-I-01 | vendor-specific cadence, level and spread; employee claim habits | Ledger: all | 10 ledger, 2 entities | P6, P8 |
| Risk escalation | S-Q-01 (stages 1→3) | — | Ledger: `Amount`, `Dr/Cr`, `Posting Date` (date + time), `Posted By` | 63 ledger, 2 entities | P9 trend, P15 |
| Relationship graph | S-O-*, S-P-01, S-P-02, S-M-01, S-N-* | S-U-09, S-U-10, S-U-11, S-N-04 | Ledger: `Payee`, `Company Code`, `Payer`; masters; relationships register | 39 ledger, 27 entities | P13 |
| Money-flow tracking | S-O-01, S-O-02, S-P-02 (receipt → fan-out) | S-U-09 | Ledger: `Company Code`, `Payer`, `Payee`, `Posting Date` (date + time), `Amount`, `Dr/Cr`; bank statement | 16 ledger, 16 entities | P13 |
| Concentration / fan-in / fan-out | S-P-01 (shared account fan-in), S-P-02 (fan-out burst) | S-U-11 (customer fan-in), S-U-12 (payment runs/payroll) | Vendor master bank account, ledger payer/payee | 20 ledger, 12 entities | P13 |
| Composite risk | S-X-01 (+ S-G-02, S-H-02, S-A-02, S-N-05), S-F-01 | controls that each resemble one component | all of the above | 12 ledger, 4 entities | P9 fusion |
| Explanations | every scenario: `explanation_evidence` in the ground truth | controls carry the reason they are legitimate | — | all 63 scenarios | P9 explanation objects (evaluated against the evidence lists) |
| Action suggestions | severity + scenario type per scenario | controls: severity `none` | — | all 63 scenarios | P15/P16 — **partial**: ground truth gives severity and evidence, not prescribed actions |

## All scenarios (from the ground truth)

| Scenario | Type | Control | Severity | Ledger rows | Bank lines | Entities | Period |
|---|---|---|---|---|---|---|---|
| S-A-01 | amount_anomaly |  | high | 1 | 0 | 1 | 2024-11-29 → 2024-11-29 |
| S-A-02 | amount_anomaly |  | medium | 1 | 0 | 1 | 2025-12-05 → 2025-12-05 |
| S-B-01 | round_number |  | medium | 18 | 0 | 1 | 2023-06-17 → 2025-01-20 |
| S-B-02 | round_number |  | medium | 35 | 0 | 1 | 2025-01-06 → 2025-10-15 |
| S-C-01 | duplicate_transaction |  | high | 2 | 0 | 1 | 2024-05-21 → 2024-05-21 |
| S-C-02 | duplicate_transaction |  | high | 2 | 0 | 1 | 2024-10-16 → 2024-10-25 |
| S-D-01 | near_duplicate |  | high | 2 | 0 | 1 | 2023-12-27 → 2023-12-28 |
| S-D-02 | near_duplicate |  | medium | 2 | 0 | 1 | 2025-10-14 → 2025-10-15 |
| S-E-01 | lookalike_vendor |  | high | 5 | 0 | 2 | 2024-10-18 → 2025-01-15 |
| S-E-02 | lookalike_vendor |  | high | 5 | 0 | 2 | 2025-02-07 → 2025-04-30 |
| S-F-01 | ghost_vendor |  | critical | 6 | 0 | 2 | 2024-02-14 → 2024-08-12 |
| S-G-01 | sudden_vendor_appearance |  | high | 3 | 0 | 1 | 2025-02-03 → 2025-02-21 |
| S-G-02 | sudden_vendor_appearance |  | medium | 6 | 0 | 1 | 2025-11-24 → 2025-12-09 |
| S-H-01 | velocity_spike |  | medium | 9 | 0 | 1 | 2025-04-26 → 2025-05-09 |
| S-H-02 | velocity_spike |  | medium | 6 | 0 | 1 | 2025-11-24 → 2025-12-09 |
| S-H-03 | dormant_vendor_reactivation |  | high | 17 | 0 | 1 | 2023-04-11 → 2025-06-26 |
| S-I-01 | first_time_high_value |  | medium | 1 | 0 | 1 | 2024-11-05 → 2024-11-05 |
| S-J-01 | threshold_splitting |  | high | 3 | 0 | 1 | 2024-07-13 → 2024-07-13 |
| S-J-02 | threshold_splitting |  | high | 3 | 0 | 2 | 2025-08-12 → 2025-08-14 |
| S-K-01 | temporal_anomaly |  | medium | 6 | 0 | 2 | 2024-12-01 → 2024-12-25 |
| S-K-02 | temporal_anomaly |  | medium | 10 | 0 | 2 | 2025-01-12 → 2025-10-12 |
| S-K-03 | period_end_clustering |  | medium | 5 | 0 | 2 | 2025-03-31 → 2025-03-31 |
| S-L-01 | sequence_pattern |  | high | 4 | 0 | 1 | 2025-05-16 → 2025-06-27 |
| S-M-01 | employee_vendor_relationship |  | critical | 7 | 0 | 2 | 2023-12-18 → 2024-09-21 |
| S-M-02 | employee_vendor_relationship | yes | none | 20 | 0 | 2 | 2023-05-19 → 2026-02-27 |
| S-N-01 | shared_attributes |  | high | 4 | 0 | 2 | 2024-09-02 → 2024-12-04 |
| S-N-02 | shared_attributes |  | critical | 0 | 0 | 2 | — |
| S-N-03 | shared_attributes |  | critical | 0 | 0 | 2 | — |
| S-N-04 | shared_attributes | yes | none | 154 | 0 | 2 | 2023-04-11 → 2026-03-27 |
| S-N-05 | shared_attributes |  | medium | 0 | 0 | 2 | — |
| S-O-01 | circular_trading |  | critical | 4 | 0 | 4 | 2024-06-21 → 2024-07-04 |
| S-O-02 | circular_trading |  | critical | 4 | 0 | 6 | 2025-05-23 → 2025-06-10 |
| S-P-01 | concentration_fan_in |  | high | 12 | 0 | 3 | 2025-07-11 → 2025-09-30 |
| S-P-02 | concentration_fan_out |  | high | 8 | 0 | 9 | 2025-09-29 → 2025-10-03 |
| S-Q-01 | gradual_risk_escalation |  | high | 63 | 0 | 2 | 2023-09-26 → 2025-10-15 |
| S-R-01 | silent_small_fraud |  | medium | 343 | 0 | 1 | 2023-06-03 → 2026-01-30 |
| S-S-01 | bank_mismatch |  | high | 3 | 0 | 3 | 2024-02-27 → 2025-10-24 |
| S-S-02 | bank_mismatch |  | high | 0 | 3 | 0 | — |
| S-S-03 | bank_mismatch |  | low | 4 | 4 | 4 | 2023-08-08 → 2026-01-31 |
| S-S-04 | bank_mismatch |  | low | 3 | 1 | 1 | 2024-11-15 → 2024-11-20 |
| S-S-05 | bank_mismatch |  | low | 3 | 1 | 1 | 2024-05-27 → 2024-05-27 |
| S-S-06 | bank_mismatch |  | low | 3 | 3 | 3 | 2023-11-24 → 2025-10-09 |
| S-S-07 | bank_mismatch |  | critical | 1 | 1 | 2 | 2025-06-17 → 2025-06-17 |
| S-T-01 | digit_distribution_anomaly |  | medium | 324 | 0 | 1 | 2023-04-07 → 2026-03-31 |
| S-U-01 | legit_round_numbers | yes | none | 36 | 0 | 1 | 2023-04-05 → 2026-03-05 |
| S-U-02 | legit_round_numbers | yes | none | 72 | 0 | 2 | 2023-04-10 → 2026-03-10 |
| S-U-03 | legit_similar_names | yes | none | 0 | 0 | 2 | — |
| S-U-04 | legit_new_vendor | yes | none | 58 | 0 | 1 | 2024-06-11 → 2026-03-18 |
| S-U-05 | legit_large_payment_installments | yes | none | 3 | 0 | 1 | 2024-08-03 → 2024-11-01 |
| S-U-06 | legit_seasonal_velocity | yes | none | 46 | 0 | 1 | 2023-10-06 → 2025-12-26 |
| S-U-07 | legit_near_threshold | yes | none | 3 | 0 | 3 | 2023-11-07 → 2025-04-21 |
| S-U-08 | legit_period_end_overtime | yes | none | 12 | 0 | 2 | 2023-06-30 → 2026-03-31 |
| S-U-09 | legit_multi_party_chain | yes | none | 6 | 0 | 2 | 2023-09-20 → 2024-09-20 |
| S-U-10 | legit_reciprocal_flows | yes | none | 37 | 0 | 2 | 2023-06-14 → 2026-03-24 |
| S-U-11 | legit_fan_in | yes | none | 0 | 0 | 16 | — |
| S-U-12 | legit_batch_payments | yes | none | 72 | 0 | 0 | 2023-04-29 → 2026-03-31 |
| S-U-13 | legit_near_duplicate | yes | none | 2 | 0 | 1 | 2024-04-23 → 2024-04-23 |
| S-U-14 | legit_sequential_invoices | yes | none | 36 | 0 | 1 | 2023-04-12 → 2026-03-12 |
| S-U-15 | legit_employee_vendor_relationship | yes | none | 0 | 0 | 2 | — |
| S-U-16 | legit_shared_attributes | yes | none | 0 | 0 | 2 | — |
| S-V-01 | data_quality |  | low | 5 | 0 | 2 | 2023-10-31 → 2025-08-29 |
| S-W-01 | inconsistent_combination |  | low | 4 | 0 | 3 | 2023-11-24 → 2025-11-21 |
| S-X-01 | composite_risk |  | critical | 6 | 0 | 2 | 2025-11-24 → 2025-12-09 |

Totals: 63 scenarios: 45 injected and 18 normal controls. Each scenario's `also_part_of` field records shared transactions, for interaction testing.
