# Relationship Graph and Network Analysis (P13)

Code: `ledgerspy/graph/` (model, metrics, cycles, signals, service) · API: `ledgerspy/api/routes/graph.py` ·
UI: `frontend/js/graph-ui.js` (Relationships tab) · config: `graph:` · acceptance: [`p13-acceptance.md`](p13-acceptance.md).

The relationship graph turns the analysed ledger, P12 entity resolution and imported master data into a
deterministic, evidence-backed network an auditor can investigate.

**Every node and edge traces to source records.**
- Payments trace to transaction IDs.
- Shared attributes trace to P12 pair evidence.

**What the graph never does.**
- It never invents a relationship.
- It never reads labels, the P5 ground truth or P11 supervised scores.
- It never changes a P9 score.

Graph metrics and signals are **additional evidence for review, not proof of fraud**.

## 1. Graph schema

### Nodes

One node per **P12 canonical entity** (`CE-…`).

**Identity.**
- Ledger names and master records are grouped only by confirmed matches.
- Probable, ambiguous and conflicting candidates stay separate nodes, linked by a `possible_same_entity` edge.
- Unresolved records keep their own stable canonical ID.

**Which entities become nodes.** Every canonical entity with a ledger name in the time window, or with a master
record (an employee with no payments is still a node, so employee–vendor links are visible).

| Field | Meaning |
|---|---|
| `node_id` | P12 canonical entity ID |
| `label` | The master record's name, else the most frequent ledger spelling |
| `node_type` | From master records: `group_company`, `employee`, `vendor`, `customer`, `vendor_customer`. Without master data, from ledger roles: `payer`, `payee`, `payer_payee`. PaySim codes: `account` |
| `resolution_status`, `members`, `master_records`, `ledger_names`, `has_master_record` | P12 identity facts |
| `roles` | `payer` / `payee` as observed in the window |

### Edges

Edge ID = `G-` + 16 hex digits of SHA-256 of the type, the endpoints and (for payments) the currency.

| Type | Direction | Built from | Content |
|---|---|---|---|
| `paid` | payer → payee | Canonical ledger rows | One edge per (payer, payee, currency) aggregating every row: transaction count, amount (minor units), first / last date, every transaction ID, limitations (missing amounts / dates / currency) |
| `shares_bank_account` | undirected | P12 pair: bank account match | Value, P12 pair IDs |
| `shares_tax_id` | undirected | P12 pair: tax ID / PAN match | Value, P12 pair IDs |
| `shares_contact` | undirected | P12 pair: phone / e-mail match | Field, value, `contact match` or `generic shared contact` |
| `shares_address` | undirected | P12 pair: same address | Address, P12 pair IDs |
| `possible_same_entity` | undirected | P12 `probable_match` / `ambiguous` / `conflicting_identity` | Status, name similarity, conflicts; "needs review in P12: not merged" |
| `potential_conflict_of_interest` | undirected | Employee and organisation sharing a bank account or tax ID | Shared fields |

**Unavailable**, and reported in every summary:
- `employed_by`: no employer field is mapped.
- `owns_or_controls`: no ownership register is ingested; ownership is never inferred.
- Bank counterparties: P14.

`received` is the reverse direction of `paid`, not a duplicate edge. A confirmed same-entity relationship lives
inside one node: its members and merge evidence are in the node detail.

**Currencies are never combined.** There is no conversion source, so a payer–payee pair in two currencies has
two edges, and flows are reported per currency.

### Time window

`from` / `to` (ISO dates) restrict the graph to rows dated in the window. Rows without a date are excluded from a
windowed graph and counted (`excluded_rows`). Metrics, cycles and signals are all recomputed on the windowed
graph.

## 2. Circular flows

### Enumeration

- **What is searched:** simple directed cycles of `min_length` 3 to `max_length` 5 parties over `paid` edges.
  Self-loops are excluded, and so are edges below `min_edge_txns` / `min_edge_amount_minor`.
- **Each cycle is found once**, from its smallest node ID: a DFS visits only larger IDs, in sorted order, so the
  enumeration is deterministic.
- **Bounds:** at most `max_cycles` (500) cycles and `max_search_steps` (2·10⁶) expansions. Reaching either sets
  `truncated: true` with the reason. Two-party back-and-forth is the reciprocal signal, not a cycle.

### Assessment

Cycles are ordinary in trade: a vendor that also buys scrap, group funding. So each cycle is assessed on its
payments.
- **Time-ordered chain.** One payment per edge, each on or after the previous one, completing within
  `window_days` (30).
  - Every rotation is tried, so the chain may start on any edge.
  - From each starting payment (up to `max_chains`; repeated payments give several chains), a bounded search finds
    the chain that retains the most value, then the one with the shortest span.
  - The per-cycle search budget is reported if reached.
- **Value retention:** smallest / largest chain amount, same currency only.

**Status:**

| Status | Meaning |
|---|---|
| `review` | Time-ordered within the window and retention ≥ `retention_min` (0.8) |
| `ordered_low_retention` | Time-ordered, but the value is not retained |
| `not_time_ordered` | Structural cycle only |
| `insufficient_data` | Missing dates or amounts, or mixed currencies; stated |

**Reported per cycle:**
- the path;
- per-edge transactions, amounts per currency and dates;
- the best chain (transactions, dates, amounts);
- span, retention, ordered chains, reasons for review, transaction IDs and limitations.

## 3. Network metrics

Metrics are computed on the money graph (`paid` edges, self-loops excluded). Components are computed both on the
money graph and on all edges. **Null means not applicable or unavailable, never a disguised zero.**

| Metric | Definition | Interpretation / caveat |
|---|---|---|
| in / out degree | Distinct payers / payees | Size of the counterparty network |
| in / out txns | Transaction counts | |
| inflow / outflow | Amounts per currency | Never summed across currencies |
| reciprocity | Share of the node's counterparties it both pays and is paid by | Null without counterparties; two-way trade is normal |
| concentration_out | Herfindahl index of outflow across payees (1 = one payee); `top_payee_share` | Single-currency outflow only |
| counterparty_diversity | Distinct counterparties in either direction | |
| clustering | Local clustering coefficient of the undirected money projection | Null below two neighbours |
| pagerank | Weighted PageRank, d = 0.85. Weights: amounts with one currency, transaction counts otherwise. Dangling mass spread uniformly | Converges or reports "not converged" |
| betweenness | Exact Brandes, directed, unweighted, normalised by (n−1)(n−2) | **Unavailable** above 3,000 nodes or 2·10⁷ nodes × edges. Never sampled silently |
| percentiles | Rank among peers of the same node type with money edges | Peer-relative |
| components | Weakly connected components | Disconnected graphs are handled; an empty graph gives empty metrics |

**Graph level:** density, reciprocity, component counts and sizes, isolated nodes, PageRank and betweenness
status, currencies.

**Interpretation.** Centrality describes position in the observed payment network. The audited group's own
companies are hubs by construction: on P5, LVC has PageRank percentile 1.0, and LVC and LVE are the only nodes
with non-zero betweenness. High centrality is never evidence of fraud on its own, so the high-betweenness signal excludes
group companies.

## 4. Employee–vendor evidence

Built from P12 person ↔ organisation pairs.

| Classification | Evidence |
|---|---|
| `possible_conflict` | A shared bank account or tax ID (also a `potential_conflict_of_interest` edge and a signal) |
| `shared_attribute` | A shared non-generic phone or e-mail |
| `shared_attribute (address only)` | Address only (weak: a shared building is common) |
| `insufficient_evidence` | Only a widely shared (generic) contact |
| `confirmed_relationship` | **Not produced.** No declared relationship register is ingested |

**What each finding contains:**
- the shared values;
- the organisation's received payments (transaction IDs);
- an explanation;
- "ownership or control is never inferred";
- the evidence to obtain: declarations of interest, onboarding approvals, bank confirmation, payment approvals.

Names, addresses and e-mail domains alone never create a relationship.

## 5. Ghost-vendor support

For every payee that receives payments (excluding group companies, employees and account codes), the graph
lists **leads for verification**:
- `no_master_record`, only when a vendor master is imported;
- `shares_strong_identifier_with_employee`;
- `shares_bank_account_with_other_entity` / `shares_tax_id_with_other_entity`;
- `incomplete_master_record` (missing tax ID, phone, e-mail or bank account).

**Never a conclusion.** Every item states "not a finding that the vendor is fictitious" and lists the evidence to
obtain: master record and approval trail, verified tax registration, proof of delivery, bank confirmation of the
beneficiary.

**Without masters.** If no vendor master is imported, the summary says a missing master record "cannot be
assessed".

## 6. Graph signals (an independent evidence family)

Signals use the P7 signal shape:
- a deterministic `signal_id` from detector, version and subject;
- `rule_id` and `rule_version`;
- `family: relationship`;
- affected `entity_ids` and `txn_ids`;
- `metrics`, `evidence`, `explanation`, `limitations`.

**`weight: null` and `fused_in_p9: false`: P9 does not consume them, so no P9 score changes.**

| Detector | Fires when |
|---|---|
| `graph_circular_flow` | A cycle has status `review` |
| `graph_reciprocal_flow` | A→B and B→A within 30 days with amounts within 20% |
| `graph_pass_through` | A receipt is followed within 7 days by payments of 75–125% of it to ≥ 3 counterparties first seen in that window. Counterparties first seen in the data's first 90 days are not "new" |
| `graph_employee_vendor_identifier` | A `possible_conflict` employee–vendor finding |
| `graph_shared_identifier_cluster` | ≥ 3 organisations linked by shared bank accounts or tax IDs |
| `graph_high_betweenness` | A non-group node at or above the 99th betweenness percentile of its peers (context only) |

**Future P9 integration.** A later, controlled integration can map the `relationship` family to a P9 family
with its own cap and weight, behind a configuration switch and with before / after validation. These
signals already carry the transaction IDs and entity IDs needed to attach them to P9 rows and entities.

## 7. P9 metadata (display only)

Each node carries `p9_risk`, the highest P9 entity score among its ledger streams. It is read after the graph
analysis, for display and prioritisation only, and is never an input to a graph detector.

## 8. Visualization data and UI

**`GET …/graph/visualization`.**
- **Node selection:** cycle and signal nodes first, then by exposure. At most `visualization_max_nodes` (250)
  nodes and `visualization_max_edges` (1,500) edges, with a `truncated` block (totals and flags).
- **Node fields:** type, identity, `size_metric` = exposure (in + out flow per currency), `intensity_metric` =
  PageRank percentile, betweenness percentile, P9 risk, cycle membership and signals.
- **Edges:** typed, with amount, count and period.
- **No coordinates are produced.**

**UI (Relationships tab).**
- **Layout:** the browser draws a ring layout by node type and exposure. It is presentational and labelled as
  such.
- **Encoding:** size = exposure; opacity = PageRank percentile; colour = P9 tier (display); red outline = signal;
  amber = cycle; dashed = shared attribute.
- **Controls:** a time window and edge-type filters.
- **Interaction:**
  - selecting a node shows its identity, metrics, relationships, signals and leads;
  - selecting a payment edge shows its transactions;
  - selecting a cycle highlights it and shows its chain;
  - employee–vendor and ghost-vendor tables are shown below the graph.
- **States:** before the analysis, an honest "not available" card; an empty window says so.
- **Rendering:** everything is rendered with `textContent` / `createElementNS` (CSP unchanged).

## 9. API

All endpoints are token-protected and read-only, and take optional `from` / `to`. Page `size` ≤
`graph.limits.max_page_size` (500); larger sizes are rejected with 422.

| Endpoint | Content |
|---|---|
| `GET /api/datasets/{id}/graph` | Summary: counts by type, metrics, cycle summary (with truncation), signals, findings counts, unavailable edge types, timings |
| `GET …/graph/nodes?type&q&sort&page&size` | Nodes with metrics, exposure, P9 display risk |
| `GET …/graph/nodes/{CE-…}` | Node detail and neighbourhood (≤ 200 edges, `truncated`), cycles, signals, findings, leads |
| `GET …/graph/edges?type&node&page&size` | Edges (payment evidence summarised) |
| `GET …/graph/edges/{G-…}` | Edge detail with transactions (≤ 500, `transactions_truncated`) |
| `GET …/graph/cycles?status&page&size` | Circular-flow findings and the search summary |
| `GET …/graph/metrics?sort&page&size` | Node metrics, graph metrics, definitions |
| `GET …/graph/employee-vendor` | Employee–vendor findings |
| `GET …/graph/ghost-vendors` | Ghost-vendor verification leads |
| `GET …/graph/signals` | Graph signals (`fused_in_p9: false`) |
| `GET …/graph/visualization?max_nodes&edge_types` | Bounded visualization payload |

## 10. Performance and limits

| Input | Nodes | Payment edges | Graph time (excluding P9 annotation) | Notes |
|---|---|---|---|---|
| P5 + masters | 163 | 163 | 0.3 s | Exact betweenness; 5 cycles; search complete |
| PaySim | 16,609 | 10,116 | 2.3 s | Betweenness unavailable (limit); 0 cycles |
| Generated 3,000 / 20k rows | 3,000 | 19,976 | 1.3 s | Betweenness unavailable (work limit); cycles truncated at 500 |
| Generated 10,000 / 100k rows | 10,000 | 99,935 | 5.3 s | Betweenness unavailable; cycles truncated by the search budget |

**First request.** P9 annotation runs P9 if it is not cached; for PaySim that is about 14 s. The analysed dataset
in the UI is already cached.

**Caching.** Results are cached in memory by dataset, data version, masters, configuration and window.

## 11. Limitations

- **The graph sees only the imported ledger and masters.** Without masters, a ledger has names only: no shared
  attributes, employee links or master-record leads.
- **No relationship register.** Declared relationships (e.g. P5's relationship register) are not ingested, so no
  relationship is reported as confirmed.
- **Group-company bookkeeping.** In P5 each intercompany transfer is booked in both companies' ledgers, so a
  group payment appears twice on its edge (both rows are kept and traceable).
- **Coverage gaps.**
  - No bank-statement nodes until P14.
  - No currency conversion.
  - Cycles longer than 5 parties are not searched.
- **One threshold was adjusted after observation.** The pass-through threshold (75%) was lowered from 80% after
  observing that the injected S-P-02 fan-out moved 79.9% of its receipt. All other thresholds were set from their
  definitions.
- **Centrality on small or star-shaped ledgers is dominated by the audited company.**
