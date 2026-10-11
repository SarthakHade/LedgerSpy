# LedgerSpy Configuration Reference

Every configurable value is in one YAML file, [`config/default.yaml`](../config/default.yaml). Its comments are
the authoritative description of each key, with the unit and the reason for its value. This page explains how
configuration is applied and lists the sections.

## 1. How configuration is loaded

| Rule | Consequence |
|---|---|
| **One file.** `ledgerspy serve` (and every CLI command) reads `config/default.yaml`, or the file given with `--config <path>` | A file given with `--config` **replaces** the default file; it is not merged with it. Start from a copy of `config/default.yaml` |
| **Strict validation.** Unknown keys, wrong types, duplicate keys and invalid values are refused | The server does not start (exit code 2) and names the problem. `ledgerspy doctor --config <path>` checks a file without starting |
| **Hashed.** The validated configuration has a SHA-256, shown by `doctor` and recorded with every analysis | Results are keyed on it. **Changing the configuration changes the identity of every result.** P9 recomputes. A recorded reconciliation run made under another configuration can no longer be displayed (`inputs_changed`); its register entry and result hash remain |
| **Loopback only.** `server.host` must be `127.0.0.1` | Any other address is refused by the schema |

To use a custom file with the bundle, keep it outside the application folder and pass it to the launcher:

```bat
ledgerspy.cmd --config "D:\engagement\ledgerspy.yaml"
doctor.cmd --config "D:\engagement\ledgerspy.yaml"
```

## 2. Case-specific settings you are most likely to change

The UI cannot edit these. Without them, the dependent checks report "not available" rather than guessing.

| Key | Default | Effect when set |
|---|---|---|
| `features.calendar.holidays` | `[]` | List of `YYYY-MM-DD`. Enables `is_holiday` and holiday-aware timing rules |
| `features.calendar.business_hours` | `["09:30", "18:30"]` | After-hours window for timing features and rules |
| `features.calendar.non_working_weekdays` | `[6]` (Sunday) | Monday = 0 … Sunday = 6 |
| `features.calendar.fiscal_year_start_month` | `4` (April) | Fiscal year for period-end and Benford dimensions |
| `features.approval_thresholds` | `[]` | Approval limits in **minor units**, e.g. `[{name: vendor_payment, amount_minor: 20000000}]`. Enables `threshold_proximity` and the `structuring` rule |
| `server.port` | `8765` | Or `ledgerspy.cmd --port N` for one run |
| `ingest.max_file_bytes`, `ingest.max_rows` | 100 MiB, 1,000,000 | Import limits |

Example: in a copy of `config/default.yaml`, change only these lines.

```yaml
features:
  calendar:
    holidays: ["2025-01-26", "2025-08-15", "2025-10-02"]
  approval_thresholds:
    - {name: expense_claim, amount_minor: 500000}
    - {name: vendor_payment, amount_minor: 20000000}
```

## 3. Sections

| Section | Phase | What it controls |
|---|---|---|
| `config_version`, `server`, `workspace` | P2 | Schema version; loopback address and port; workspace folder and database file name |
| `ingest`, `mapping`, `adapters` | P3 | Size, row, column and XLSX limits; column-alias and label-column tables; PaySim and generic-ledger adapters |
| `quality`, `normalize` | P4 | Integrity checks, severities, the readiness formula, fix proposals |
| `synth` | P5 | The synthetic company generator (seed, years, scale, fictional policies) |
| `features` | P6 / P12 | Point-in-time baselines, calendar, approval thresholds, adapter semantics, entity resolution |
| `rules`, `benford` | P7 | Forensic rule thresholds and severities; Benford tests, minimum sample sizes and subgroups |
| `anomaly` | P8 | Isolation Forest, robust peer statistics, tail percentiles, warm-up, review capacity |
| `risk` | P9 | Signal weights, family and group caps, tiers, review-volume guard, entity risk |
| `supervised` | P10 / P11 | Optional supervised experiments and model artefacts (CLI only) |
| `graph` | P13 | Graph limits, cycle search, metric limits, graph-signal thresholds |
| `reconciliation` | P14 | Matching windows, tolerances, weights, grouping limits, duplicate and balance checks |
| `pdf` | P14b | PDF limits, worker timeout, OCR executable search order, languages, DPI, confidence thresholds |

**Changing detector thresholds or risk weights.** This changes what is flagged. The documented values and their
validation are in the methodology documents:
- [`forensics.md`](forensics.md);
- [`anomaly-model-card.md`](anomaly-model-card.md);
- [`risk-engine.md`](risk-engine.md);
- [`entity-resolution.md`](entity-resolution.md);
- [`relationship-graph.md`](relationship-graph.md);
- [`reconciliation.md`](reconciliation.md);
- [`pdf-extraction.md`](pdf-extraction.md).

Record why a value was changed. The configuration hash in each result shows *that* it changed, not *why*.
