# Entity Resolution (P12)

Code: `ledgerspy/resolution/` (normalize, similarity, records, matching, engine, ledger, masters, service,
validation) · P6 features: `ledgerspy/features/resolution.py` · P9 grouping: `ledgerspy/risk/entities.py` ·
API: `ledgerspy/api/routes/resolution.py` · CLI: `ledgerspy entities …` · acceptance:
[`p12-acceptance.md`](p12-acceptance.md).

Entity resolution decides whether different representations of a party refer to one real-world entity: ledger
names, and vendor / customer / employee / group-company master records. It is **deterministic and
evidence-based**:
- Every decision is a documented rule over explicit evidence (names, identifiers, addresses).
- Every result carries the evidence, the conflicts and a plain-language explanation.
- It never uses fraud labels, P5 ground truth or P11 supervised scores.
- **It prefers a false negative to a dangerous false merge.** Only `confirmed_match` merges records.

## 1. Inputs and entity types

| Source | Entity type | Class | Fields |
|---|---|---|---|
| Canonical ledger (every dataset) | `ledger_party`, with its roles (`payer` / `payee`) | organisation if the name has a legal form, else unknown | the name only (P6 party key) |
| Imported vendor master | `vendor` | organisation | mapped fields: record ID, name, address, city, phone, e-mail, bank account, bank code, tax ID |
| Imported customer master | `customer` | organisation | as mapped |
| Imported employee master | `employee` | person | as mapped |
| Imported group-company list | `group_company` | organisation | as mapped |
| PaySim party codes | `ledger_party` | account code | the code only |

**Not supported:**
- Bank counterparties need bank statements (P14).
- A ledger's own counterparty-ID column is not a canonical field. It stays an unmapped column that matching never
  reads.

**Master data.** `ledgerspy entities import-master` imports a master file:
- The file becomes evidence (hashed, read-only, audit-logged), exactly like a ledger.
- The raw text of the mapped columns goes to the append-only `master_records` table (database migration 5).
- The mapping must be explicit. A suggestion (one unambiguous header per field) is shown, and used only when the
  caller accepts it explicitly.
- Unmapped fields are absent. Nothing is guessed from other columns.

## 2. Normalisation (raw and normalised kept apart)

Normalisation exists only inside a resolution run; stored data is never modified. Each normalised value
records the rules that changed it (shown in the API's record detail).

| Field | Rules |
|---|---|
| all text | Unicode NFKC; zero-width characters removed; case-folded; whitespace collapsed |
| name | `&` → `and`; dots and apostrophes inside words removed ("Pvt." → `pvt`, "L.L.P." → `llp`, "L.T.D" → `ltd`); hyphens between letters removed ("Quor-ven" → `quorven`); other punctuation → space. A trailing legal form is canonicalised and kept **separately** from the core name: `private limited` / `pvt ltd` / `p ltd` / `pte ltd` → `pvt ltd`; `limited` → `ltd`; `l l p` → `llp`; `and co` / `and company` → `and co`; `co` / `company` → `co`; `inc`, `corp`, `llc`, `plc`, `gmbh`. Business words ("Trading", "Traders", "Supplies") are never removed |
| person name | as names, plus the sorted words (word order is not identity) |
| phone | digits only; leading `00` and the configured country code (default 91) removed when ≥ 8 digits remain; one trunk `0` removed; < 6 digits → unusable |
| e-mail | trimmed, case-folded; exactly one `@` and a dotted domain, else unusable. The local part is never rewritten (no dot / `+tag` folding) |
| address | punctuation → space; abbreviations expanded (`rd` → road, `st` → street, `indl` → industrial, `est` → estate, …); a 6-digit postal (PIN) code extracted |
| bank account | letters and digits only, upper-cased; < 6 → unusable. The bank (IFSC) code is a separate field and never identifies anyone on its own |
| tax ID | letters and digits only, upper-cased; < 5 → unusable. A GSTIN-shaped value also yields its embedded PAN, so a GSTIN and its PAN compare equal |
| PaySim code | trimmed, upper-cased; compared exactly |

## 3. Similarity components

All pure Python, each in [0, 1] and reported separately for every pair:
- **edit:** 1 − Levenshtein / longer length.
- **jaro_winkler**.
- **token_jaccard:** exact words; reported but not averaged.
- **soft_token:** a Dice coefficient over words, where spelling variants with edit ≥ `typo_token` also count as
  shared.
- **token_set:** the token-set ratio.
- **trigram:** Jaccard over character 3-grams.

`name_similarity` is the mean of edit, Jaro-Winkler, soft-token, token-set and trigram, computed on the **core**
names (legal form excluded).

Two more measures feed the decisions:
- **Word differences.** Differing words are split into *spelling variants* ("alloy" ~ "alloys") and
  *distinguishing words* ("trading" vs "traders", edit 0.57). A distinguishing word caps a name-only decision.
- **Address similarity.** Word Jaccard, plus postal-code and city agreement.

## 4. Candidate generation (blocking)

Each record gets block keys; records sharing a key become candidate pairs.

| Kind | Keys | Expanded when the block is large? |
|---|---|---|
| name | first 4 letters of the core name; each core word of ≥ 3 letters; the sorted words; a consonant skeleton of the first word | no: blocks over `max_block_size` (50) are skipped and counted in the report |
| identifier | tax ID, PAN, bank account, e-mail, phone | **always**: a value shared by many records is evidence that must stay visible |
| other | non-generic e-mail domain; postal code | no (size-capped like names) |

**Exhaustive fallback.** It compares all pairs at or below `exhaustive_max_records` records. The default is 0:
always block, and the validation measures blocking against all pairs.

**Recall limitations.**
- A true pair is missed only when the two records share no identifier and every name / address key they share
  sits in an oversized block. An example would be two very common one-word names with no identifier.
- Spelling changes in the first four letters and the first word at once can escape the name keys.
- The validation reports candidate recall against all pairs (§9).

## 5. Pair decisions

Statuses:
- `confirmed_match`: merged.
- `probable_match`, `ambiguous`: review; never merged.
- `conflicting_identity`: contradictory identity evidence; review; never merged.
- `shared_attribute`: different entities sharing a contact, address or account. A link for P13, never a merge.
- `no_match`.

Rules, in order (the first that applies decides):
1. **Person vs organisation:** never a match. Shared identifiers or address give `shared_attribute`
   (employee–vendor links).
2. **Account codes (PaySim):** an equal code only; never fuzzy.
3. **Same tax ID** (or GSTIN ↔ PAN):
   - `confirmed_match` when names are ≥ `tax_name_min` (0.60) similar and no bank account conflicts;
   - otherwise `conflicting_identity`.
4. **Hard conflict:** both records have a tax ID, or both a bank account, and they differ.
   - `conflicting_identity` when any identifier matches, or the names are the same or ≥ `review_name` (0.80)
     similar;
   - otherwise `no_match`.
   - **A high name similarity never hides a conflict.**
5. **Same core name** (case, punctuation, spacing or legal-form spelling only), with no soft conflict (a
   different legal-form family, or a different city):
   - organisations → `confirmed_match`;
   - persons → `confirmed_match` only with a matching identifier, else `ambiguous`, because personal names are
     common;
   - a ledger name → a master record: `confirmed_match` only when that master identity is the only one with the
     name.
6. **A shared bank account, e-mail or phone** (not a shared contact):
   - names ≥ `contact_name_min` (0.85) → `confirmed_match` if a bank account or two identifiers match and nothing
     soft conflicts, else `probable_match`;
   - less similar names → `conflicting_identity` (one identifier under different names).
7. **Names only:**
   - ≥ `probable_name` (0.92) with no distinguishing word and no soft conflict → `probable_match`;
   - ≥ `review_name` → `ambiguous`;
   - a shared contact or address → `shared_attribute`;
   - else `no_match`.

**Shared contacts.** A phone or e-mail used by ≥ `shared_contact_generic_min` (3) differently named records is a
*shared contact* (a switchboard, a shared mailbox), not an identity. Tax IDs and bank accounts are never
treated this way.

**Rules that use master data.**
- A ledger name linked exactly to a master record is not also a review candidate for other master records.
- Two ledger names linked to master records take the master records' decision. The most cautious one wins, so a
  look-alike vendor's ledger name stays apart from the original's.

**Supporting behaviour** (ledger names only): shared reference patterns, counterparties, accounts, or a typical
amount within 10%. It is listed as "supporting (not decisive)" and adds at most `behaviour_bonus_max` (0.05) to
the evidence score. **It never changes a status.**

**The evidence score** (0–1) orders the review queue. It is not a probability.

**Explanations** state the decision, the evidence, the conflicts and the supporting behaviour. For example:

> Conflicting identity (needs review) because normalised names are 86% similar. Conflicting evidence:
> different tax ID; different bank account.

## 6. Clusters and canonical IDs

**Merge order.** Confirmed matches are merged in descending evidence order.

**Merge rule.** Two groups merge only when no cross pair (evaluated even if blocking skipped it):
- has a hard identifier conflict;
- is `conflicting_identity`;
- joins a person and an organisation;
- has names below `review_name` without a shared identifier.

So A ≈ B and B ≈ C never put A and C together on their own. A refused merge is recorded as a **blocked merge**
with the cross pair that prevented it.

**Canonical ID.**
- `CE-` + 16 hex digits of SHA-256 of the cluster's **anchor**: its first master record by key, else its first
  ledger name.
- The ID does not depend on input order or on unrelated records, and stays the same while the anchor stays in the
  cluster.

**Statuses.**
- Entity: `unique`, `resolved` (several records), `needs_review` (a probable or ambiguous candidate exists) or
  `conflicting_identity`.
- Record: the same vocabulary, from its own pairs with other entities.

## 7. Integration

- **P6 registry.** Seven entity-level, post-period features, all versioned, documented, locked and unit-tested
  (see [`feature-registry.md`](feature-registry.md)). Post-period, so never in a supervised contract.
  - `ent_canonical_entity_id`
  - `ent_resolution_status`
  - `ent_linked_record_count`
  - `ent_match_confidence`
  - `ent_name_similarity_max` (look-alike indicator)
  - `shared_identifier_count` (needs master data)
  - `employee_link_indicator` (needs master data)
  - `shared_identifier_count` and `employee_link_indicator` were deferred to P12 in P6.
  - No existing feature changed: the lock entries of all 62 earlier features are unchanged.
- **P9.** With `risk.entities.grouping: resolved` (default), streams of the same flow whose names resolve to one
  canonical entity through confirmed matches are aggregated as one counterparty.
  - Their entity ID derives from `<flow>:<CE-…>`; every other entity keeps its P9 ID.
  - Transaction scores and the entity formula are unchanged. A merged entity's score is the same top-3 noisy-OR
    over all its transactions, so it can rise because it now sees all of its rows. That is identity consistency,
    not a risk bonus.
  - Every P9 entity carries a `resolution` block: canonical ID, status, linked records, merged names, master data.
  - `grouping: name` restores the P9 behaviour and still shows the resolution.
- **P13.** Canonical IDs are stable and deterministic. `shared_attribute` and `conflicting_identity` pairs give
  typed links between canonical entities (employee → vendor, vendor → vendor), ready for the relationship graph.

## 8. API and CLI

| Endpoint (token-protected, read-only, paginated) | Content |
|---|---|
| `GET /api/datasets/{id}/resolution` | Summary: counts, candidate reduction, statuses, field coverage, unavailable fields, policy |
| `GET /api/datasets/{id}/resolution/entities?status&q&page&size` | Canonical entities |
| `GET /api/datasets/{id}/resolution/entities/{CE-…}` | Members with raw / normalised fields and rules, merge evidence, review pairs, links, blocked merges |
| `GET /api/datasets/{id}/resolution/pairs?status&q&page&size` | Candidate pairs. The default is the review queue and links; other statuses by filter |
| `GET /api/datasets/{id}/resolution/pairs/{P-…}` | Full evidence of one pair, with suggested action |
| `GET /api/cases/{case_id}/masters` | Imported master files and their mappings |

**Pairs are generated on demand.** The candidate pairs are computed per run (cached in memory by dataset, data
version, master records and configuration), never persisted.

**CLI.** `ledgerspy entities import-master | masters | resolve | validate`.

**UI.** The Relationships tab's counterparty table shows each counterparty's identity from the API:
- a resolved / unique / possible-match / conflicting-identity badge;
- linked records;
- merged ledger names.

**Similarity matrix (pre-P23 gap closure, `frontend/js/similarity-ui.js`).** The Relationships tab's *Entity
Similarity Matrix* card shows the candidate pairs of `GET …/resolution/pairs`. It loads when the tab is opened.
- **Matrix.** Rows and columns are the records of the current page of pairs, at most 16 per axis; every pair is
  also in the table. A cell is the pair's API name similarity, coloured by its P12 status. A blank cell means the
  two records were not compared (blocking) or the pair is outside the view; it never means a similarity of zero.
- **Filters.** By status and by name, with paging.
- **Pair detail** (`…/pairs/{id}`):
  - status, rule and suggested action;
  - the evidence score, labelled as identity evidence, not a fraud probability;
  - the name-similarity components, identifiers and address;
  - evidence and conflicts;
  - the shared payment patterns (supporting behaviour) and the records.
- **Case ID.** The card names the case, which the CLI master import needs (installation guide §4).

The card is read-only. Analyst confirm / reject is recorded through an investigation (P15). "Per-pair risk" is
the P12 review status and evidence score; no fused pair risk score exists. No score or percentage is computed in
the browser.

## 9. Validation (P5)

`ledgerspy entities validate` runs on four inputs:
- **P5 masters:** 164 records.
- **Deterministic variants:** 55. Exact duplicate; case / punctuation; legal suffix with name and address only;
  one-letter spelling change with the tax ID; one-letter spelling change with name and address only; address
  formatting; name variant with shared contacts.
- **Adversarial cases:** 20 records in 9 cases.
- **The P5 ledger's 153 names.**

It reports:
- candidate recall;
- auto-match precision;
- false merges (pairs and clusters);
- false splits (left for review vs missed);
- the ambiguous rate;
- per-category outcomes;
- the P5 relations;
- ledger-link precision and recall.

**Ground truth is read only to score the result, after resolution ran.** It comes from the P5 ground-truth file
and the ledger's unmapped `Counterparty ID` column. The thresholds were set from the rule definitions above, not
tuned on these results. See [`p12-acceptance.md`](p12-acceptance.md) for the numbers.

## 10. Limitations

- **Ledger-only data is name-only.** Without master data, two ledger names merge only when they differ in
  formatting or legal-form spelling. Spelling variants stay apart for review.
- **Counterparty IDs aren't used.** A mapped counterparty ID column would be a strong identifier, but canonical
  transactions have no such field yet.
- **No analyst decisions.** Confirm / reject decisions and their persistence across reruns belong to the
  investigation workflow (P15). P12 exposes the review queue and suggested actions only.
- **Names.**
  - No transliteration.
  - No phonetic algorithm: a consonant skeleton key stands in for Double Metaphone.
  - No honorific stripping for people.
  - Legal-form tables are English / Indian forms only.
- **Address comparison is word-based.** There is no geocoding, so two spellings of one street with different
  words compare as different.
- **Blocking can miss pairs.** Oversized name blocks are skipped (§4). The scale test measures recall, but real
  data with very common names could lose candidates.
- **Hard conflicts never merge.** Different bank accounts are a hard conflict, so a company that genuinely uses two
  accounts is never merged on its own. It is surfaced as `conflicting_identity` for review instead.
- **The evidence score isn't calibrated.** It orders the review queue and is not a match probability.

## 11. Deviations from the architecture (§8)

- **No external libraries.** No rapidfuzz, jellyfish or TF-IDF: pure-Python metrics (no network install;
  offline). The character-trigram Jaccard replaces the TF-IDF cosine.
- **Rule-based decisions, not Fellegi–Sunter weights.**
  - The decisions are explicit rules, and the evidence score is a documented weighted sum.
  - Rules make the "conflicts never hidden" and "person ≠ organisation" guarantees testable.
- **No alias table, no `entity.*` signals.**
  - There is no alias table and no analyst confirm / reject yet (P15).
  - There are no `entity.near_duplicate_vendor` / `entity.employee_vendor_link` signals in P7 / P9. Adding
    signals would change risk scores, and P12 must not inflate risk because of identity work. The P6 features
    carry these facts instead (`ent_name_similarity_max`, `employee_link_indicator`).
- **Masters are imported as evidence.** The imported master files are evidence with an explicit mapping, not a
  canonical master table with creation dates. `vendor_master_age_days` stays deferred.
