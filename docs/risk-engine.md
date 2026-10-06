# Risk Engine v1 and Explainability v1 (P9)

Code: `ledgerspy/risk/` · Configuration: `config/default.yaml` → `risk` · API: `ledgerspy/api/routes/risk.py` ·
UI: `frontend/js/risk-ui.js` · Acceptance record: [`p9-acceptance.md`](p9-acceptance.md).

> **The risk score is a review priority, not a probability of fraud.** It ranks transactions for an auditor's
> attention on a 0–100 scale. Nothing in it has been calibrated against confirmed outcomes. Supervised, calibrated
> probabilities are P10/P11. Every API response and every explanation says so.

## Inputs

One analysis of a dataset's current state does the following:

1. Builds the P6 feature matrix **once**.
2. Runs the P7 forensic report (rules and Benford) and the P8 anomaly report on it.
3. Fuses their signals per transaction.

Signals keep their own contract (`ForensicSignal` → canonical `Signal`); P9 does not re-detect anything. Labels,
`extra` columns and ground truth are never read. A static test scans `ledgerspy/risk`, and flipping PaySim's label
columns gives identical scores.

**Which rows a signal raises:**
- A *transaction* signal raises its subject. Other rows it cites, such as the original of a duplicate, are context
  only.
- An *entity* or *group* signal raises every row it cites: the round-amount rows of a counterparty, a period-end
  cluster, or the sample rows of a Benford subgroup.

## Score

For one transaction, with signal evidence e = strength × weight. Both come from the detector's own configuration
(`rules.*`, `anomaly.*`).

```
family f:  F_f = min(cap_f, 1 − Π_k (1 − e_(k) · d^k))     signals strongest first (ties: signal ID), d = 0.5
group g:   G_g = min(cap_g, 1 − Π_{f in g} (1 − F_f))      an independent evidence source
score:     S   = 100 · (1 − Π_g (1 − G_g))                  bounded in [0, 100)
```

- **Within a family**, correlated signals add with diminishing returns: the 2nd counts half, the 3rd a quarter.
  They never exceed the family cap.
- **Across independent sources**, evidence combines like independent reasons to look (noisy-OR). The score only
  rises when evidence is added, and never reaches 100.
- **Not a sum of detector scores.** Raw P8 scores (an Isolation Forest score, a z, a percentile) enter only
  through their signals' provisional strength, which P8 derives from how deep the row is in its tail.

### Families, groups and caps (config `risk.families`, `risk.groups`)

| Family | Detectors | Group (independent source) | Family cap | Why |
|---|---|---|---|---|
| Duplicate payments | duplicate_exact, duplicate_near | duplicate | 0.6 | Strong, specific evidence, but one row alone stays below *high* |
| Transaction-pattern rules | structuring, new_vendor_large, dormancy, velocity | transaction_pattern | 0.6 | Policy and behaviour rules on the row |
| Timing rules | temporal, period_end | temporal | 0.4 | Timing alone is weaker evidence |
| Counterparty-level evidence | round_amount | entity_evidence | 0.3 | A property of the counterparty, applied to the rows it cites |
| Benford subgroup sample | benford_subgroup | benford | 0.15 | A sampling lead about a population, never proof about a row |
| Statistical anomaly | isolation_forest | **anomaly** (group cap **0.45**) | 0.4 | P8 families overlap in features … |
| Peer-group deviation | robust_peer | **anomaly** | 0.4 | … so together they count as one source … |
| Extreme value (tail) | tail_percentile | **anomaly** | 0.2 | … and anomaly evidence alone can never reach *high* |
| Data-quality issue | balance_continuity | data_quality | 0 (**not scored**) | P4 integrity: shown beside the risk, never fraud risk |

Each cap is at least the family's strongest single weight, so a cap only bites when signals accumulate. A detector
missing from every family is a configuration error, as is one listed in two families.

### Contributions (exact)

In log-survival space, L = −ln(1 − G), and S = 100 · (1 − e^(−ΣL)). The score is split from the top down:

- **Groups:** group g contributes S · L_g / ΣL.
- **Families:** a group's contribution is split across its families by −ln(1 − F_f).
- **Signals:** a family's contribution is split across its signals by −ln(1 − e_(k) d^k).

A cap scales everything it caps alike. Displayed values are rounded to hundredths with largest remainders, so the
displayed contributions add up **exactly** to the displayed score.

Every transaction detail returns the breakdown:
- per family: uncapped and capped evidence, whether the cap applied, the family's standalone points ("base"), and
  its contribution;
- per signal: evidence, rank in family, effective evidence and contribution;
- per group: evidence, cap and contribution.

The same breakdown converts to the canonical `Score` / `Explanation` models, whose validators check that the shares
sum to 1.

## Tiers (config `risk.tiers`)

| Tier | Rule |
|---|---|
| critical | score ≥ 75 **and** ≥ 3 independent sources |
| high | score ≥ 50 **and** ≥ 2 independent sources |
| medium | score ≥ 25 |
| low | otherwise |

- **Corroboration is required.** One detector, however strong (an exact duplicate scores 60), or a single large
  but otherwise ordinary payment (tail + peer deviation ≤ 45), cannot reach *high*.
- **Explained.** Each row carries the reason for its tier, such as "high needs corroboration from 2 independent
  sources, this has 1".
- **Displayed scores.** Tiers compare the displayed score, so what is shown and the tier always agree.

## Review-volume guard (config `risk.review`)

- **Ranking.** Rows with a positive score are ranked by tier, then score, then independent sources, then |amount|,
  then transaction ID.
- **Queue.** The review queue holds the first `top_k` (250) rows of tier medium or above. High or critical rows
  beyond it are marked `over_capacity`.
- **Guard.** The guard compares the raw high + critical count with `max_high_share` (2%) of scored rows, and the
  critical count with `max_critical_share` (0.5%). Exceeding either sets `calibration_warning` with a message.
- **No hidden changes.** **Raw scores and tiers are never altered.** The raw tier counts, budgets, queue size, the
  queue's lowest score and the overflow are all reported.

## Entity (counterparty) risk

- **Identity.** An entity is P6's counterparty stream (`out:<party>` / `in:<party>`); no entity resolution happens
  before P12.
- **Score.** 100 · (1 − Π_j (1 − r_(j) · 0.5^j)) over the entity's 3 highest transaction scores. Contributions
  per transaction are exact.
- **No double counting.** Each transaction counts once. Counterparty-level signals (round amounts, a Benford
  subgroup) are already inside the scores of the rows they cite; they are listed with the entity, not added again.
- **Independent sources.** These are the groups with evidence in those top transactions.
- **Exposure.** Monetary exposure (|amount| of medium-or-higher rows), the count of suspicious rows and selected P6
  entity features are reported. Exposure orders the review list but never raises the score: a large counterparty is
  not riskier for being large.

## Data quality and coverage

- **Readiness is separate.** P4 readiness is reported next to the risk (`summary.readiness`), and data-quality
  signals appear in a separate "data-quality flags" list.
- **Data quality never becomes risk.** On PaySim, about 780 rows whose only signal is a balance discontinuity score
  0 and stay low.
- **Unavailable detectors contribute nothing.** A detector that cannot run on a dataset (no calendar time, no
  approval thresholds, no running balance, …) is listed with its reason, and every explanation names it among the
  limitations.
- **Rows nothing can assess.** A row no detector could assess is `insufficient_data`, with no score.

## Explanations

`GET /api/datasets/{id}/risk/transactions/{txn_id}` returns:

| Field | Content |
|---|---|
| `transaction` | The row as listed: fields, score, tier, tier reason, review rank and priority, key findings |
| `explanation.headline` | e.g. "Review-risk score 79.31 of 100 (critical, 3 independent sources): Transaction-pattern rules 34.89 points; …" |
| `explanation.contributors` | Largest first. Each has signal ID, detector and family (with plain labels), contribution in points and share, whether a family cap applied, severity, the signal's own evidence and P6 feature values, the transaction(s) it cites, and the detector's own sentence |
| `explanation.data_quality_flags` | Unscored P4 issues |
| `explanation.limitations` | Not a probability; tier reason; detectors unavailable on this dataset; detectors with insufficient data for this row; caps applied; the Benford caveat |
| `breakdown` | The full fusion breakdown (groups, families, signals) |
| `entity` | The counterparty's risk |

Every value comes from a signal or the transaction record. Explanation sentences are the detectors' own, written
from the data, and the frontend renders them verbatim.

## API (token-protected, read-only, one cached analysis)

| Endpoint | Returns |
|---|---|
| `POST /api/datasets/{id}/risk` | Runs the analysis (or reuses the identical cached one); returns the summary |
| `GET /api/datasets/{id}/risk` | The summary. Counts, tiers, amounts, entities, volume guard, score distribution, monthly series (calendar data only), readiness, top findings, dataset-level findings, unavailable detectors, method, input hashes, `result_sha256` |
| `GET …/risk/transactions?page&size≤500&tier&q&sort` | Paginated rows. `sort` is `priority`, `score`, `amount` or `source_row`; `q` searches ID, payer, payee and reference |
| `GET …/risk/transactions/{txn_id}` | Detail and explanation (404 if unknown) |
| `GET …/risk/entities?page&size&tier&q`, `GET …/risk/entities/{E-…}` | Entity list and detail |
| `GET …/risk/detectors` | Every P7 rule, Benford and P8 detector: status, reason, family, signals, rows raised; the Benford tests; the P8 population |
| `GET …/risk/scatter` | Every row with a positive score, plus an even stride of zero-score rows (≤ 2,500 points) |

**Caching.** The cache identity is the dataset, its normalised data version, the latest P4 run, the effective
configuration hash, the feature-registry hash and the risk and detector versions. Any change gives a new result;
the same identity gives the same object.

**Consistency.** Every response carries `result_sha256`, so the summary, list and detail can be checked against
each other. Recomputing from scratch gives the identical hash.

## Frontend (rendering only)

`frontend/js/risk-ui.js` imports a ledger:
1. upload to the evidence store;
2. confirm the proposed mapping;
3. run the P4 integrity check.

It then calls the risk endpoints and renders the results:
- the hero tiers and volume guard;
- the overview donut, amounts and score distribution;
- the findings (detectors, dataset-level findings, Benford);
- the monthly tiers, Benford chart and top counterparties;
- the score-vs-amount scatter;
- counterparty risk;
- the method and family summary;
- the paginated transaction table with explanations;
- the findings summary.

The assistant answers from the same API data.

**What the browser never does:** compute a score, tier, threshold, statistic or explanation. The legacy heuristic
scorer (`ml-engine.js`) and in-browser CSV pipeline are deleted. A static test (`tests/backend/test_frontend_static.py`)
fails if scoring formulas, score comparisons, random numbers, label columns or hard-coded results return.

**Honest states:**
- Opened as a plain file, the page says the local service is required and shows no numbers.
- Bank reconciliation, Monte Carlo, benchmarking, the entity graph and the PDF memo stay labelled "not available".

## Known behaviour and limitations

- **Not calibrated.** Caps, decay, tiers and detector weights are expert-set and documented. They were not fitted to
  any benchmark, and the synthetic results are diagnostics. Calibration against confirmed analyst outcomes (P15)
  and supervised models (P10/P11) come later.
- **Correlated rule families.** Two rules in different families can still be about the same underlying fact. For
  example, `new_vendor_large` and the P8 peer deviation both see a large amount, as two independent sources. The
  anomaly group cap limits P8's share, but cross-family correlation between P7 and P8 is not modelled.
- **Inherited detector limitations:**
  - the two-company payroll stream has a bimodal cadence, so 32 control rows reach medium through P8 alone;
  - the routine quarter-end overtime control (S-U-08) and the canteen contractor (S-U-14) each have one row at
    high: a P7 rule hit (temporal before the routine is established, and a transaction-pattern rule) corroborated
    by a P8 tail percentile.
- **Entity risk uses names, not resolved entities.** Entities with several medium transactions from different
  sources can reach *high* even when no single transaction does. That is intended: repeated, independently
  supported unusualness is a reason to look at the counterparty.
- **Benford raises sample rows only, slightly.** A digit-pattern scenario such as S-T-01 therefore stays low per
  row and appears as a dataset-level finding.
- **Out of scope for row-level P7/P8 evidence:** look-alike vendors (P12), bank mismatches (P14), aggregate small
  fraud, and inconsistent combinations. These scenarios are not flagged.
- **One currency.** Amount totals are shown only for single-currency datasets; mixed currencies are reported as
  such.
- **Not stored.** Results are computed on request and held in memory. Findings and case workflow (persistence,
  analyst decisions) are P15.
