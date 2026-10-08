# Supervised Model Artefacts, Registry and Inference (P11)

Scope: [`roadmap.md`](roadmap.md) §P11 · code: `ledgerspy/supervised/{artifact,registry,train,inference}.py`,
`ledgerspy/api/routes/models.py`, `ledgerspy model …` · model card: [`supervised-model-card.md`](supervised-model-card.md)
· acceptance: [`p11-acceptance.md`](p11-acceptance.md).

P11 turns a P10 experiment into a **local, versioned, hash-verified artefact** that can be:
- inspected;
- verified;
- registered;
- moved through a lifecycle;
- used for inference.

It does **not** change P9:
- The supervised score is never fused into the review-risk score.
- Nothing in P6–P9 imports the supervised package.
- LedgerSpy works exactly as before when no model exists, or when a model is broken, incompatible or retired.

## 1. P9 review risk vs supervised ranking score

| | P9 review risk | P11 supervised ranking score |
|---|---|---|
| Source | Label-free forensic rules (P7) + statistical anomalies (P8), fused with caps | A model fitted to labels (PaySim `isFraud`, or injected synthetic scenarios) |
| Status | The production baseline; always computed | Optional and experimental; only an approved, active model counts as production |
| Meaning | Review priority with an exact, per-family explanation | Similarity to the training positives: an **uncalibrated ranking score** |
| Probability? | No | No. `probability_claim_permitted` is always `false` in P11 |
| Where | `/api/datasets/{id}/risk…`, the UI | `/api/models…`, `/api/datasets/{id}/supervised/inference`, the CLI; not in the UI |
| Interaction | Never reads supervised output | Never writes to P9 or the database |

## 2. Artefact format

**One UTF-8 JSON file per model:** `<workspace>/models/M-<16 hex>.json`.
- **No pickle, joblib or executable content.** Loading parses numbers and strings, validates them against a closed
  schema and rebuilds the model.
- **Floats** are written with Python's shortest round-trip representation, so a re-loaded model is bit-identical.
  The documented tolerance is **exact**, and the round-trip tests compare with `==`.
- **The file itself is deterministic:** sorted keys and fixed indentation. Training twice with the same inputs gives
  byte-identical files. Nothing in the artefact depends on the clock, path or machine; the Python version is
  recorded.
- **Files are written read-only.** A file is never overwritten with different content.

### Schema (artefact version 1)

| Field | Content |
|---|---|
| `artifact_format`, `artifact_version` | `ledgerspy-supervised-model`, `1` (the only supported version) |
| `model_id` | `M-` + the first 16 hex digits of the artefact hash |
| `model` | `type` (logistic_regression / gradient_boosting); `implementation` and `implementation_version`; `hyperparameters`; `parameters` (LR: intercept and coefficients; GBDT: init log-odds, per-input bin edges and trees as node lists); `parameters_sha256`; `input_columns` (the design columns, in order); `input_standardised`; `output`; `fit` diagnostics |
| `feature_names` | The P6 features the contract declares, in order |
| `feature_contract` | `contract_version`; `contract_sha256`; `feature_registry_sha256`; `features_config_sha256`; per feature: kind, version, definition hash, level, semantics and transform; excluded features with reasons; the `compatibility` policy (§5) |
| `preprocessor` | The complete training-split state: per numeric feature the transform, median (imputation), mean and SD (scaling) and missing indicator; categorical categories (one-hot order); dropped features with reasons; design columns; `preprocessor_sha256` |
| `threshold_policy` | The rule (top `review_share` of **validation** rows), the selected value, validation rows / k, and the model hash it was selected for |
| `calibration` | `status` (not_attempted / calibrated / not_validated); Platt parameters if fitted; output label; validation positives and minimum; test ECE before / after; `max_ece`; `probability_claim_permitted: false`; the model hash it belongs to |
| `training` | Dataset ID, file, evidence SHA-256, data version, transaction-matrix hash; label source, labels hash, label definition; **training domain** and **production eligibility** (from the policy in §8); split description (rows and positives per part, overlaps); imbalance, ablation, seed, config and P10 contract hashes |
| `evaluation` | P10 experiment ID; test metrics (PR-AUC with CI, ROC-AUC, precision/recall@k, confusion at the validation threshold); validation PR-AUC; label-free baselines on the same test rows; warnings |
| `model_card` | Name, intended use, not intended for, training domain, score semantics, limitations, documentation |
| `creation` | LedgerSpy version, Python version, writer, timestamp policy (registration time lives in the registry) |
| `initial_status` | Always `experiment` |
| `integrity` | `sha256`, the canonicalisation, what the hash covers, `artifact_sha256` |

## 3. Integrity

**The hash.**
- `artifact_sha256` = SHA-256 of the canonical JSON of every top-level field except `integrity` and `model_id`.
- Canonical JSON means sorted keys, `(",", ":")` separators, UTF-8, and NaN/Infinity refused.
- It is independent of key order, whitespace, file name, location and creation path. Tests re-serialise with reversed
  keys, compact and indented JSON, and save to two directories.

**What it does and doesn't prove.**
- **Integrity:** any edit fails the check (tested: coefficients, threshold, calibration label, domain flag, model card,
  model ID, a one-byte text edit).
- **Not authorship:** anyone can re-seal an edited document, so the registry **pins** the hash at registration, and
  every use re-checks the file against the pinned hash. A re-sealed, edited artefact therefore fails as "not the one
  registered under this model ID", and gets a different model ID anyway.

**Safe load** (`artifact.load_bytes`), in order; any failure raises a typed error and nothing is executed:
1. Size limit (32 MiB). Strict JSON parse: no NaN/Infinity tokens, duplicate keys refused, the top level must be an
   object. → `artifact_malformed`.
2. Format and version. → `artifact_version_unsupported`.
3. Closed schema with pydantic, strict mode, `extra="forbid"`. This catches missing or unknown fields, wrong types, a
   string for a number, a bool for an int, a threshold outside [0, 1], post-period semantics, a probability claim,
   and an initial status other than experiment. → `artifact_invalid`.
4. The hash is recomputed and compared, plus the pinned hash when loaded through the registry. →
   `artifact_integrity_failed`.
5. **Semantic checks.** → `artifact_invalid`.
   - Hyperparameters are re-validated with the config schema (impossible values are refused).
   - LR has one coefficient per input.
   - GBDT bin edges are strictly increasing and at most max_bins − 1, and there is one tree per estimator.
   - Each tree is acyclic: children point forward, every node except the root has exactly one parent, and depth is at
     most max_depth.
   - Split inputs and bins are in range.
   - `parameters_sha256` is recomputed from the rebuilt model.
   - The feature list, contract (hash recomputed), preprocessor (hash recomputed), design columns and model inputs
     agree, in order.
   - The threshold and calibration carry this model's hash; the calibration status is consistent with its
     parameters, positive counts and ECE.
   - The training domain matches the label-source policy.
6. Implementation version. A different `models.IMPLEMENTATION_VERSION` gives `artifact_incompatible`.

## 4. Preprocessing persistence

**Everything learnt from training is in `preprocessor`:**
- scaling;
- imputation;
- missing indicators;
- category mappings;
- feature and column order.

**Nothing is recomputed at inference** (`Preprocessor.from_state` + `transform`; there is no `fit` on this path).
- A row's design values depend only on that row.
- Tests score shifted populations (amounts ×10, unseen categories, 500 extreme extra rows). The persisted state is
  unchanged, and the original rows' scores are identical.

## 5. Feature-contract compatibility (P11: exact contract only)

`artifact.compatibility` checks against the running LedgerSpy.

| Situation | Policy |
|---|---|
| Feature registry hash differs | **Reject** (`artifact_incompatible`, 409) |
| A declared feature is missing from the registry, unimplemented, or has a different version / definition hash / kind / semantics / level | **Reject** |
| Feature order differs from the artefact's | **Reject** (also caught by the loader's own consistency checks) |
| A feature the model reads is not computed for the dataset | **`model_not_applicable`** with the missing features and reasons; nothing is imputed or substituted |
| The registry or dataset has features the artefact does not declare | **Ignored** (never read) |
| A declared feature that the preprocessor dropped (constant, mostly null, ablation) | Allowed; never read |
| The P6 features *configuration* (calendar, approval thresholds, windows) differs from training | **Warning** in every inference result |

No migration or "compatible minor version" mode exists; the policy is written into every artefact as
`compatibility.mode = "exact"`.

## 6. Inference process

`inference.infer`, also used by the API and `ledgerspy model infer`:
1. **Resolve the model.**
   - Without a model ID: the active production model. It must still be `approved`, verify against its pinned hash
     and match the registry. No active model gives `inference_status: no_active_model`, and nothing else changes.
   - With a model ID: a registered model only.
     - Production if it is the active approved model.
     - Otherwise only with `experimental: true`.
     - A retired model never runs.
2. **Features.** The dataset's P6 features are built exactly as P9 builds them. The exact contract is checked and
   applicability enforced.
3. **Score.** Transform with the persisted state, score in batches (`supervised.inference.batch_size`), and apply the
   persisted validation threshold when asked.
4. **Return** the ranking score and its metadata.
   - **Model:** model ID, artefact hash, model type, status, training domain, production eligibility.
   - **Feature contract:** version, hash, registry hash, the features read.
   - **Calibration:** status and label.
   - **Score and threshold:** `score_type: "uncalibrated ranking score"`; threshold value, rule and how many rows
     are at or above it.
   - **Run:** `inference_status` and `inference_mode` (production / experimental); dataset identity (evidence hash,
     data version, P4 run, matrix and features-config hashes); inference config hash; `scores_sha256`; warnings;
     timings.

**Calibrated models.** When the status is `calibrated`, rows also carry `calibrated_score` (an estimated rate in data
like the training domain's test split). It is never a fraud probability.

**Storage.** A JSON record (metadata + scores, no labels) is written to `<workspace>/inference/<I-…>.json`.
`inference_id` is derived from the artefact hash, dataset identity and inference config, so the same inputs give the
same ID.

### Inference-time feature policy (no future leakage)

- **Allowed semantics.** Only P6 `row` and `point_in_time` features can be in an artefact: enforced by the schema,
  the loader and the running-registry check. A transaction's inputs therefore use only rows before it, never later
  rows, even though the whole ledger is present.
- **Excluded semantics.** `dataset` and `post_period` features (whole-period summaries) are excluded at training,
  rejected in an artefact, and rejected if the registry ever changes a feature to them.
- **Truncation test.** Scores of transactions up to a cut-off are identical whether or not the later half of the
  ledger exists.

### Batch determinism

Same artefact + same input gives the same scores, ID and `scores_sha256`. Tests also show:
- shuffled rows give identical per-transaction scores;
- batch sizes 1 / 7 / 333 / all give identical scores;
- repeated runs are identical;
- the input frame, the feature run and the database are not modified (table digest compared).

P6 features are themselves independent of input row order (P6 tests).

## 7. Model registry

**Storage.**
- **Files:** artefacts stay independently verifiable files in `<workspace>/models/`.
- **Index:** database migration 4 adds three append-only tables (UPDATE/DELETE triggers abort):
  - `model_artifacts`: model ID ↔ pinned artefact hash, type, domain, label source, who and when;
  - `model_status_events`: lifecycle (the current status is the latest event; details hold the gate results and
    approval metadata);
  - `model_activation_events`: the active production model is the latest event's model ID (NULL = none).
- **Audit:** every registration, status change, activation and deactivation is also written to the hash-chained
  audit log in the same transaction (`model.registered`, `model.status_changed`, `model.activated`,
  `model.deactivated`).

**Operations.** List, inspect (full metadata without the bulky parameters, plus history), verify, register (training,
or importing a file: verified, then a copy is stored), activate / deactivate, and the active-model status.

**Effective status.** It is recomputed on every read:
- `integrity_failed`: the file is missing, unreadable or modified;
- `incompatible`: the contract no longer matches the running registry.

Either overrides the recorded status.

**Unregistered files.** A file in `models/` that is not registered is listed as unregistered and never used: no
artefact becomes active "by being present".

## 8. Lifecycle, approval gate and training domains

```
                 promote (gate)            approve (gate + approval)       activate (gate)
  experiment ─────────────────▶ candidate ───────────────────────▶ approved ──────────────▶ active
      ▲                            │                                  │
      └──────── demote ────────────┘                                  │
  (any status) ── retire ──▶ retired (final; deactivated if it was active)
```

**Training-domain policy** (in code, not configuration):

| Label source | Training domain | Production-eligible |
|---|---|---|
| `paysim` | `paysim_simulation` | **No**: a mobile-money simulator; its fraud is defined by the simulator |
| `synthetic` | `synthetic_injected_scenarios` | **No**: development and testing only |
| `audited_outcomes` | `audited_outcomes` | Yes, but no adapter produces such labels yet |

**Gate checks.** Each is recorded with pass/fail and detail.

| Check | Promote → candidate | Approve | Activate |
|---|---|---|---|
| Integrity against the pinned hash | ✓ | ✓ | ✓ |
| Exact feature-contract compatibility | ✓ | ✓ | ✓ |
| Model card present | ✓ | ✓ | ✓ |
| Evaluation record present (experiment ID, test PR-AUC) | ✓ | ✓ | ✓ |
| Calibration status known | ✓ | ✓ | ✓ |
| Training domain declared | ✓ | ✓ | ✓ |
| Training domain production-eligible | ✓ | ✓ | ✓ |
| Approval metadata (actor and a reason of ≥ 10 characters) | — | ✓ | ✓ |
| Current status | experiment | candidate | approved |

**Consequence.** Every PaySim- or synthetic-trained artefact stays `experiment`. It can be inspected, and run
explicitly as a labelled experiment, but never becomes a candidate, approved or active. The approval path is exercised
in tests with a **test stand-in**: the synthetic labels declared as audited outcomes.

### Example lifecycle

```bash
# experiment (evaluated): training runs the P10 procedure, writes the evaluation record into the artefact
ledgerspy model train --dataset-id D-… --labels synthetic --ground-truth …/SYNTHETIC_ground_truth.json \
    --model gradient_boosting --imbalance balanced --split group
ledgerspy model inspect M-…        # model card, contract, threshold, calibration, evaluation, history
ledgerspy model verify M-…         # integrity + compatibility (exit 1 if either fails)
ledgerspy model infer --dataset-id D-… --model-id M-… --experimental   # labelled experiment only

# candidate → approved → active: only for a production-eligible training domain
ledgerspy model promote  M-… --reason "evaluated on audited outcomes, beats P9 on a grouped split"
ledgerspy model approve  M-… --reason "reviewed model card, evaluation and limitations"
ledgerspy model activate M-… --reason "production supervised evidence for engagement X"
ledgerspy model infer --dataset-id D-…                              # production: the active model

# retired: final; deactivates it if active
ledgerspy model retire M-… --reason "superseded by M-…"
```

For a PaySim or synthetic model, `promote` fails with `[FAIL] training_domain_production_eligible`.

## 9. Calibration status

The status is copied from the P10 assessment (Platt on validation, judged on test) and validated on load.

| Status | Meaning |
|---|---|
| `not_attempted` | Fewer than `min_validation_positives` validation positives; output is an uncalibrated ranking score |
| `not_validated` | Platt was fitted, but the test ECE exceeds `max_ece`; output is an uncalibrated ranking score |
| `calibrated` | Test ECE ≤ `max_ece`. `calibrated_score` is added, as an estimated rate in data like the training domain's test split |

In every case the main output is `score` with `score_type: "uncalibrated ranking score"`, and no field is a fraud
probability. Only a future artefact version with validated target-domain calibration could permit that claim.

## 10. API and CLI

All API routes are token-protected (session bearer token, Host/Origin checks).

| Method and path | Purpose |
|---|---|
| `GET /api/models` | Registered models with effective status, active model, unregistered files |
| `GET /api/models/active` | Active production model and whether it is usable |
| `GET /api/models/{model_id}` | Full metadata (no raw parameters), verification and history |
| `POST /api/datasets/{dataset_id}/supervised/inference` | Body `{model_id?, experimental?, apply_threshold?, page?, size?}`: metadata + a page of rows |
| `GET /api/supervised/inference/{inference_id}` | Metadata of an inference run |

Model IDs must match `^M-[0-9a-f]{16}$`, and the request body is a closed schema, so a `path`, `file` or any other
field is refused (422). Files are located only as `<workspace>/models/<model_id>.json`. **Lifecycle changes are
CLI-only**: there is no API to promote, approve, activate or retire.

CLI: `ledgerspy model train | register --file | list | inspect | verify [id | --file] | promote | approve | demote |
retire | activate | deactivate | infer`.

## 11. Security considerations

- **No executable deserialisation.**
  - The artefact modules import no pickle, joblib, marshal, shelve, dill, importlib or yaml, and call no
    eval/exec/compile/`__import__` (static test).
  - A pickle payload is refused as malformed without being executed (test).
  - Code-like text inside an artefact is inert data.
- **No arbitrary paths.**
  - The API takes model IDs only; registry lookups re-validate the ID.
  - `register --file` (CLI) reads the file, verifies it and stores a copy under its model ID.
- **Least privilege.** The API is read-only for models, apart from running inference, which writes only the local
  inference record.
- **Integrity and pinning.**
  - Approval and activation re-run the gate.
  - A model modified after approval stops being usable at once; P9 is unaffected.
- **Offline.** Nothing touches the network (the test suite blocks sockets).

## 12. PaySim model limitations

- PaySim is a **mobile-money simulator**. Its fraud exists only in TRANSFER / CASH_OUT, and its account structure is
  simulator-specific.
- P10 found simulator leakage:
  - transaction type alone reaches ROC-AUC 0.91;
  - `payee_distinct_payers_prior` reflects the simulator's account structure.
- A PaySim artefact therefore:
  - is `production_eligible: false` and stays `experiment`;
  - is **not applicable** to a ledger without `txn_type` (refused with the missing feature);
  - in its `no_simulator_fields` form can only run on a client ledger as an **out-of-domain experiment**, with
    explicit warnings.
- Calibration is `not_attempted` (9–13 validation positives).
