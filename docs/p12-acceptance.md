# P12 — Entity Resolution: Acceptance Record

Date: 2026-10-09 · Scope: [`roadmap.md`](roadmap.md) §P12 (R6), [`architecture.md`](architecture.md) §8 ·
Methodology: [`entity-resolution.md`](entity-resolution.md).

**What P12 adds.** A deterministic, evidence-based entity-resolution layer:
- explicit, evidence-backed master-data import (vendor, customer, employee, group company);
- documented normalisation;
- identifier-first, rule-based pair decisions over several name metrics;
- blocking;
- cannot-link-safe clustering into stable canonical IDs;
- explanations, a review queue and P13-ready links;
- seven P6 registry features;
- resolved counterparty grouping in P9 (transaction scores unchanged);
- token-protected API, CLI, a P5 validation suite;
- an identity column in the UI's counterparty table.

## Commands and results (final state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # All checks passed
.venv/Scripts/python -m pytest                                       # 1013 passed (964 P1–P11 + 44 new P12 tests + 5 new registry parametrisations)
node tests/frontend/harness.js                                       # 13 passed, 0 failed
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 12 passed, 0 failed
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed
.venv/Scripts/ledgerspy entities import-master --case-id C-… --file vendors.csv --entity-type vendor --map name=Vendor Name --map tax_id=GSTIN …
.venv/Scripts/ledgerspy entities resolve --dataset-id D-…
.venv/Scripts/ledgerspy entities validate        # exit 1 if any false merge
```

| P12 test file | Tests | What it proves |
|---|---|---|
| `test_resolution_units.py` | 25 | **Normalisation:** case, punctuation, whitespace, zero-width, legal suffixes ("Stationery L.T.D" ≡ "Stationery Ltd"), business words kept, phone / e-mail / bank / tax (GSTIN ↔ PAN) / address, missing values, raw kept. **Exact identifiers:** a strong identifier confirms; a conflict is never merged even with identical names; a missing identifier falls back to names; the same number at another bank differs. **Fuzzy:** a near match goes to review; Trading ≠ Traders; ambiguous same name in two cities; common personal names; person ≠ organisation. **Conflicts:** switchboard phone (shared contact); same e-mail under different names; generic e-mail domain and bank code are not evidence. **Blocking:** deterministic; identifier blocks always expanded; oversized name blocks skipped; exhaustive mode; candidate recall 1.0 on the validation set. **Clustering:** no transitive over-merge; blocked merge recorded; stable canonical IDs. **Determinism:** shuffled and repeated input give an identical hash and clusters. **PaySim:** account codes exact only. **Safety:** no labels, ground truth or supervised output. **Explanations:** no "AI" claims. **Validation:** false merges are measured and 0 |
| `test_resolution_datasets.py` | 12 | **Import:** master import is evidence-backed, explicit (suggestion never applied silently), immutable, idempotent and audit-logged. **P5 resolution:** look-alikes, shared bank and sister concerns are `conflicting_identity`; employee–vendor links are `shared_attribute`, never merged; ledger names link with 0 wrong links. **Integrity:** canonical rows and master records unchanged. **P6 features:** computed with masters. **P9:** transaction risk is unchanged by resolution; on name variants, splits go down, there are 0 false merges, and typos stay apart; the entity formula is unchanged. **P13:** IDs are stable. **PaySim:** exact codes only. **Scale:** 3,300 records. **API:** token required on 6 endpoints, pagination, filters, details, 404 / 422. **CLI** |
| `test_features_units.py` (+7) | 7 | One unit test per new registry feature |

Timings vary by a few seconds between runs; counts and metrics are deterministic and were reproduced exactly
on the final code.

**Earlier code and tests updated (necessary only).**
- **Config:** `features.resolution` and `risk.entities.grouping`.
- **Database:** migration 5 (master tables).
- **Feature input:** `FeatureInput.masters` plus the `master_data` capability. `features.service.input_for` passes the
  case's masters to every feature build (P7, P8, P9, P10 callers switched to it).
- **P9 entities:** `risk/entities.py` (stream groups, `resolution` block, `member_streams`).
- **UI:** an identity column and updated wording in the Relationships tab.
- **Registry tests:** inventory counts (62 → 69 implemented, 10 → 8 deferred), P6 dataset feature sets (+5 computed
  on both datasets; the 2 master-data features are unavailable without masters), and the regenerated lock. Every
  earlier lock entry is byte-identical.

## Acceptance criteria

| # | Criterion | Evidence | Result |
|---|---|---|---|
| 1 | Deterministic normalisation | `resolution/normalize.py`, rules recorded per value; tests | ✅ |
| 2 | Exact identifier matching | Tax ID (+ GSTIN ↔ PAN), bank account (+ bank code), e-mail, phone; identifier-first rules; conflicts block | ✅ |
| 3 | Fuzzy candidate matching | Edit, Jaro-Winkler, soft-token, token-set, trigram (+ exact token Jaccard), word-level variants vs distinguishing words | ✅ |
| 4 | Candidate blocking | Name, identifier, domain and postal keys; capped name blocks; reduction 95.6% (P5) to 99.9% (22k records) | ✅ |
| 5 | Similarity matrix / evidence | Per-pair components, identifiers, address, conflicts, supporting behaviour, status, rule, evidence score, explanation; on demand, deterministic hash | ✅ |
| 6 | Shared-payment evidence | Shared bank account / contacts / address (identity evidence); ledger behaviour (reference patterns, counterparties, accounts, amount profile) as supporting only | ✅ |
| 7 | Confidence / status policy | Seven ordered rules, configurable thresholds, six statuses; evidence score orders review | ✅ |
| 8 | Ambiguous matches not merged | Only `confirmed_match` merges; spelling-only variants: 8 / 8 left for review | ✅ |
| 9 | Conflicts surfaced | `conflicting_identity` + conflict list on every pair; record, entity and P9 statuses; review queue first | ✅ |
| 10 | Deterministic clusters | Shuffled / repeated input → identical hash; anchor-based IDs stable with unrelated additions | ✅ |
| 11 | False merges measured | Validation: false-merge pairs and clusters (0), P9 grouping false-merge entities (0) | ✅ |
| 12 | P5 validation | `ledgerspy entities validate` (below) | ✅ |
| 13 | Adversarial false-match tests | 9 cases in validation + unit tests; none merged | ✅ |
| 14 | P6 registry integration | 7 entity features (versioned, documented, locked, unit-tested); 2 formerly deferred | ✅ |
| 15 | P9 consumes canonical IDs | `risk.entities.grouping: resolved`; `resolution` block on every entity | ✅ |
| 16 | P9 score formula stable | Transaction scores identical in every comparison; merged entity = same noisy-OR (test) | ✅ |
| 17 | P13 can consume identities | Stable `CE-` IDs; typed `shared_attribute` / `conflicting_identity` links between canonical entities | ✅ |
| 18 | API token-protected | 401 without / with a wrong token on all 6 endpoints | ✅ |
| 19 | Full P0–P11 tests pass | 1013 passed, 0 failed | ✅ |
| 20 | P1 Node / browser tests pass | 13/13 harness, 12/12 browser | ✅ |
| 21 | P4 integrity panel passes | 6/6 | ✅ |
| 22 | Ruff passes | Clean | ✅ |
| 23 | Methodology and limitations documented | [`entity-resolution.md`](entity-resolution.md) | ✅ |

## P5 validation (`ledgerspy entities validate`)

**Records:**
- 164 P5 master records;
- 55 deterministic variants;
- 20 adversarial records;
- the P5 ledger's 153 names.

**Candidate pairs:** 3,133 of 76,636 possible pairs (reduction 95.9%). Blocking found 61 of 61 true master pairs.

**Master records:**

| Metric | Value |
|---|---|
| Candidate recall | **1.0** |
| Auto-match precision (same-cluster pairs) | **1.0** |
| False merges (pairs / clusters) | **0 / 0** |
| False splits | 8, all left for review (0 missed) |
| Ambiguous rate (review pairs / candidates) | 0.8% |
| Blocked merges | 0 |
| Runtime | 0.6 s |

**Variant categories** (expected outcome by design):

| Category | Variants | Expected | Merged | Review | Missed |
|---|---|---|---|---|---|
| exact duplicate | 10 | merged | 10 | 0 | 0 |
| case / punctuation | 10 | merged | 10 | 0 | 0 |
| legal suffix (name + address only) | 6 | merged | 6 | 0 | 0 |
| spelling + tax ID | 8 | merged | 8 | 0 | 0 |
| **spelling, name + address only** | 8 | **review, never merged** | 0 | 8 | 0 |
| address formatting (+ phone) | 8 | merged | 8 | 0 | 0 |
| name variant + shared e-mail / phone / bank | 5 | merged | 5 | 0 | 0 |

**Adversarial cases** (none merged):

| Case | Outcome |
|---|---|
| ABC Trading Ltd / ABC Traders Ltd | no match ("traders" is a distinguishing word) |
| One legal name in two cities, different tax IDs | conflicting identity |
| One legal name in two cities, no identifiers | ambiguous (different city) |
| Two employees "Priya Sharma" | conflicting identity |
| Shared gmail.com domain | not even a candidate |
| Three firms at one business-centre address | no match (different tax IDs) |
| Shared IFSC code | no match |
| Shared switchboard phone (3 firms) | shared attribute (shared contact) |
| Meridian / Meridien Steel, different tax IDs | conflicting identity |

**P5 relations** (scored with the ground truth after resolution; all surfaced, none merged):
- **Look-alike vendors** S-E-01 / S-E-02: conflicting identity.
- **Three vendors on one bank account** S-P-01: conflicting identity.
- **Sister concerns** S-N-04: conflicting identity.
- **Ghost vendor and employee–vendor links** S-F-01, S-M-01 / 02, S-N-01 / 02 / 03 / 05: shared attribute.

**Ledger names → master records** (scored against the ledger's unmapped `Counterparty ID`):
- 149 of 153 linked, **0 wrong** (precision 1.0, recall 0.974).
- Three of the unlinked names have no master record: the tax gateway, payroll clearing and the bank.
- One employee whose duplicate validation record carries no shared identifier stays in review, by design.

## Resolution of the P5 dataset (ledger + imported masters)

- **Records:** 317 (153 ledger names, 164 master records).
- **Candidates:** 2,199 of 50,086 pairs (95.6% reduction).
- **Pairs:** 158 confirmed, 11 conflicting identity, 5 shared attribute.
- **Entities:** 163 canonical entities (150 with several records): 141 resolved, 9 conflicting identity, 13 unique.
- **Merges:** 0 blocked merges.
- **Runtime:** 0.13 s.

**Review queue** (first rows):
- Quorven Cartons LLP / Quor-ven Carton L.L.P.: conflicting identity. Names are 94% similar; tax ID and bank
  account differ.
- Ulvane Alloys Private Limited / Ulvanee Alloy Pvt Ltd: conflicting identity (86%; same conflicts).
- Brightwell Supply Co / Brightwel Supplies / BW Supply House: conflicting identity. They share one bank account
  under three names and three tax IDs.
- Varnel Freight Carriers / Varnel Roadways: conflicting identity. They share a bank account, phone and address,
  with different tax IDs.
- Employee ↔ vendor shared bank account / phone / address (5 pairs): shared attribute.

## P9 before / after

**P5 ledger (as generated) with its masters.**
- 159 entities under name grouping and under resolved grouping; 0 merged.
- Transaction scores are identical across name grouping, resolved grouping, and before / after the master
  import, and every entity score is unchanged.
- The P5 ledger contains no differently-spelled names of one counterparty. P12 adds identity facts to every
  entity: 147 resolved (linked to master data), 9 conflicting identity, 3 unique.

**P5 ledger with deterministic counterparty-name variants** (11 vendors; every 2nd payment re-spelled: legal
suffix, upper case, punctuation or a one-letter typo). Truth is the ledger's `Counterparty ID`:

| Grouping | Entities | False-merge entities | Split counterparties | High / critical entities |
|---|---|---|---|---|
| name (before P12) | 167 | 0 | 8 (V0001, V0019, V0028, V0037, V0055, V0064, V0073, V0091) | 38 |
| resolved (P12) | 161 | **0** | 2 (V0028, V0064: the name-only typos, kept apart by design) | 37 |

- **Transaction scores:** identical.
- **Upper-case variants:** never split, because P6 party keys already case-fold.
- **Merged entities' scores** use the unchanged top-3 noisy-OR over all their rows:

| Entity | Before (two streams) | After (one entity) |
|---|---|---|
| BW Supply House | 82.47 / 82.47 (high, high) | 85.65 (high) |
| Delvane Metals | 41.40 / 12.76 (medium, low) | 44.48 (medium) |
| Elvaran Wraps | 46.58 / 24.32 | 46.58 (medium) |
| Ilvaran Audit Partners | 21.04 / 20.32 (low, low) | 26.84 (medium) |
| Teravin Electricals | 21.55 / 20.45 | 24.23 (low) |
| Hessarin Estates | 0 / 0 | 0 |

The entity count falls (167 → 161) and the high / critical count falls (38 → 37), because two high streams of one
vendor become one entity. No risk is added for being merged.

## PaySim and performance

| Input | Records | Candidate pairs | Reduction | Time | Result |
|---|---|---|---|---|---|
| PaySim (10,125 rows) | 16,616 account codes | 0 | — | 0.22 s (1.4 s incl. features) | Exact code identity only; legal-entity fields marked unavailable |
| P5 dataset + masters | 317 | 2,199 of 50,086 | 95.6% | 0.13 s | above |
| Validation set | 392 | 3,133 of 76,636 | 95.9% | 0.6 s | above |
| Population 2,200 | 2,200 | 11,159 of 2.4 M | 99.54% | 2.5–3 s | recall 1.0, 0 false merges |
| Population 11,000 | 11,000 | 146,016 of 60.5 M | 99.76% | 30–34 s | recall 1.0, 0 false merges |
| Population 22,000 | 22,000 | 144,501 of 242 M | 99.94% | 16–20 s | recall 1.0, 0 false merges |

- The population records are organisation names with tax IDs, bank accounts and phones, plus ~10% suffix / case
  variants.
- At 22k, more name blocks exceed the cap (330 skipped), so there are fewer candidates. Recall holds because the
  variants share identifiers; name-only populations at that size would lose some candidates (documented
  limitation).

## API endpoints

- `GET /api/datasets/{id}/resolution`
- `GET /api/datasets/{id}/resolution/entities` (`status`, `q`, `page`, `size ≤ 500`)
- `GET /api/datasets/{id}/resolution/entities/{CE-…}`
- `GET /api/datasets/{id}/resolution/pairs` (`status`, `q`, `page`, `size`)
- `GET /api/datasets/{id}/resolution/pairs/{P-…}`
- `GET /api/cases/{case_id}/masters`

All are token-protected and read-only. IDs are validated by pattern, and no endpoint accepts a path. P9's
`/risk/entities` items now carry `resolution`.

## Deviations and decisions

1. **Master data import.**
   - P3 accepts ledgers only, so P12 adds an evidence-backed master import (migration 5, CLI) instead of
     extending the ledger pipeline.
   - Its mapping is explicit; unmapped fields are absent.
   - There is no browser upload yet (P15).
2. **Pure-Python metrics.** No rapidfuzz, jellyfish or TF-IDF (offline; no new dependency). Trigram Jaccard and a
   consonant skeleton stand in for TF-IDF cosine and Double Metaphone.
3. **Rule-based decisions.** The decisions are explicit rules plus a documented evidence score, not
   Fellegi–Sunter weights. The guarantees (conflicts never hidden, person ≠ organisation, confirmed-only merges)
   are then testable rules.
4. **No analyst confirm / reject or alias table.** Per the phase instructions, P12 exposes the review queue,
   evidence and suggested actions; decisions and their persistence are P15.
5. **No new P7 / P9 signals.** `entity.near_duplicate_vendor` and `entity.employee_vendor_link` were not added:
   they would change risk scores. The same facts are P6 features (`ent_name_similarity_max`,
   `employee_link_indicator`) and resolution statuses.
6. **P9 entities stay per flow.** The resolved grouping merges streams of the same flow only: a group company that
   pays and is paid stays two entities, as in P9. The canonical ID links them.
7. **Thresholds weren't tuned.** They come from the rule definitions; the P5 ground truth and the ledger's
   `Counterparty ID` are read only by the evaluation, after resolution.

## Known limitations

See [`entity-resolution.md`](entity-resolution.md) §10:
- ledger-only data is name-only;
- no canonical counterparty-ID field;
- no analyst decisions yet;
- no transliteration, phonetic algorithm or honorific stripping;
- word-based address comparison;
- capped name blocks can lose candidates for very common names;
- different bank accounts block merges of genuine multi-account companies (surfaced for review);
- the evidence score is not calibrated.
