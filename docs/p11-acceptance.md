# P11 — Supervised Model Persistence and Safe Inference: Acceptance Record

Date: 2026-10-09 · Scope: [`roadmap.md`](roadmap.md) §P11, [`architecture.md`](architecture.md) §5.2 · Design and
lifecycle: [`model-lifecycle.md`](model-lifecycle.md) · Model card: [`supervised-model-card.md`](supervised-model-card.md).

**What P11 adds.**
- **Artefact:** a self-describing, deterministic JSON artefact for both P10 model types, with a canonical SHA-256.
- **Loader:** a strict safe loader with semantic checks, and exact feature-contract compatibility.
- **Registry:** a local, append-only, audit-logged registry with a lifecycle and an approval gate.
- **Inference:** a deterministic batch inference service.
- **Interfaces:** token-protected read and inference endpoints, and the `ledgerspy model …` CLI.

**What it leaves alone.**
- P9 scoring is unchanged. Supervised output is never fed into it and is never called a fraud probability.
- The UI is unchanged: the frontend does not depend on P11.

## Commands and results (final state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # All checks passed
.venv/Scripts/python -m pytest                                       # 964 passed (925 P1–P10 + 39 P11)
node tests/frontend/harness.js                                       # 13 passed, 0 failed
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 12 passed, 0 failed
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed
.venv/Scripts/ledgerspy model train --dataset-id D-… --labels paysim --model gradient_boosting
.venv/Scripts/ledgerspy model list | inspect M-… | verify M-… | infer --dataset-id D-… --model-id M-… --experimental
```

| P11 test file | Tests | What it proves |
|---|---|---|
| `test_model_artifact.py` | 25 | **Artefact:** byte-identical rebuilds; hash independent of key order, whitespace and location; self-describing fields. **Round trip** (LR and GBDT): identical design matrix, scores (`==`), threshold decisions, model / preprocessor hashes and metadata. **Preprocessing:** state persisted exactly; shifted, unseen-category and extreme populations change nothing. **Batch:** repeated, shuffled and 1 / 7 / 333 / all batch sizes give identical per-row scores; no input mutation. **Tampering:** six field edits and a one-byte text edit detected; a re-sealed edit refused by the pinned hash. **Malformed:** empty, truncated, not an object, NaN, duplicate keys, bad UTF-8, wrong format, oversize. **Versions:** 0, 2, "1", 1.5, true, null refused. **Schema:** missing, extra and mistyped fields, out-of-range threshold, post-period feature, probability claim. **Semantics:** tree cycles, out-of-range input or bin, tree count, unsorted bins, impossible hyperparameters, coefficient count, parameter hash, threshold / calibration bound to another model, inconsistent calibration, relabelled domain. **Contract:** exact match; reordered / missing features refused; registry, definition, semantics changes and removed features make it incompatible; implementation-version mismatch; a missing dataset feature gives `not_applicable` (no substitution); extra features ignored; declared-but-dropped features allowed. **Safety:** no pickle / eval imports or calls; a pickle payload refused unexecuted; IDs only. **Registry:** registration is `experiment` and never active; unregistered files are unusable; PaySim / synthetic stay experimental; full gate → approve → activate; append-only tables; audit log; tampering after approval disables the model; retire is final; derived `incompatible` |
| `test_model_datasets.py` | 14 | **Real data:** persisted PaySim LR / GBDT and synthetic GBDT reproduce the in-memory P10 models' scores and threshold decisions exactly, and the artefact's test PR-AUC; real-data artefacts are deterministic. **PaySim:** artefacts stay experimental, calibration not attempted. **Metadata:** inference metadata, repeatability, no probability field, no DB writes, records on disk. **Point in time:** truncating the future leaves earlier scores identical. **Cross-domain:** the PaySim model is not applicable to the synthetic ledger; its no-simulator form runs only as a warned out-of-domain experiment. **P9 isolation:** garbage / tampered / incompatible artefacts, no models directory, and inference runs leave both datasets' P9 `result_sha256` and the tables unchanged. **API:** token required on all 5 endpoints; list / inspect / active / experiment flow; paths and unknown fields refused; production inference only with an approved active model. **CLI:** lifecycle |

**Earlier code updated (necessary only).**
- A configuration key `supervised.inference.batch_size`.
- Database migration 4 (registry tables).
- `Preprocessor.state/from_state` and a `Frame`.
- Model `parameters/from_parameters`.
- `_run_model(..., fitted=True)` returns the fitted objects (P10 output unchanged).
- The P10 isolation test now allows exactly one route module (`api/routes/models.py`) to import `inference, registry`,
  and still forbids any "fraud probability" wording.

## Acceptance criteria

| # | Criterion | Evidence | Result |
|---|---|---|---|
| 1 | Both P10 model types can be persisted | LR and GBDT artefacts from PaySim and the synthetic ledger; round-trip tests | ✅ |
| 2 | Artefacts are deterministic | Byte-identical rebuilds (toy and real data); no timestamps inside | ✅ |
| 3 | Cryptographic integrity hashes | SHA-256 of canonical JSON; model ID derived from it; registry pins it | ✅ |
| 4 | Tampering is detected | Field and byte edits → `artifact_integrity_failed`; re-sealed edits refused by the pinned hash | ✅ |
| 5 | Feature-contract compatibility enforced | Exact policy; registry / definition / order / missing checks; `not_applicable`; config warning | ✅ |
| 6 | Preprocessing state persisted | Exact `preprocessor` block (hash-checked); population-shift test | ✅ |
| 7 | Model round-trip scores match | Exact equality on toy and real data, including threshold decisions and P10 metrics | ✅ |
| 8 | Batch inference deterministic | Repeated / shuffled / partitioned runs identical; same inference ID and `scores_sha256` | ✅ |
| 9 | No future-derived preprocessing at inference | No fitting at inference; row / PIT features only; truncation test | ✅ |
| 10 | Safe loading executes no code | JSON + closed schema; static import / call scan; pickle payload refused | ✅ |
| 11 | Model lifecycle / status exists | experiment / candidate / approved / retired + derived incompatible / integrity_failed | ✅ |
| 12 | Experiments cannot silently become production | Registration is experiment; only approved models activate; unregistered files unusable; experiments need `experimental=true` | ✅ |
| 13 | PaySim models explicitly experimental / ineligible | Domain policy in code; promotion refused (`training_domain_production_eligible`) | ✅ |
| 14 | P9 works with no supervised model | P9 hashes unchanged with no models dir, garbage, tampered and incompatible artefacts | ✅ |
| 15 | API model operations authenticated | 401 without / with a wrong token on all 5 endpoints | ✅ |
| 16 | No arbitrary filesystem paths | ID regex on path and body; closed body schema (`path` / `file` → 422); fixed file location | ✅ |
| 17 | Full P0–P10 tests pass | 964 passed | ✅ |
| 18 | P1 Node / browser tests pass | 13/13 harness, 12/12 browser | ✅ |
| 19 | P4 integrity panel passes | 6/6 | ✅ |
| 20 | Ruff passes | Clean | ✅ |
| 21 | Documentation / lifecycle complete | [`model-lifecycle.md`](model-lifecycle.md) (schema, lifecycle, registry, integrity, compatibility, inference, gate, calibration, security, PaySim, P9 distinction, example lifecycle) | ✅ |

## Demo (default configuration: 100 trees, 500 bootstrap resamples)

Train → register → load ×20 → experimental inference → promotion attempt, on PaySim (10,127 rows) and the synthetic
ledger (7,271 rows). Group split, class weights.

| Data | Model | Model ID | Bytes | Train s | Load ms | Inference s (features / score) | Test PR-AUC | Calibration | Status | Promote |
|---|---|---|---|---|---|---|---|---|---|---|
| PaySim | logistic_regression | `M-d577a9fe4d7dfbb4` | 19,439 | 10.2 | 1.3 | 1.42 (1.24 / 0.038) | 0.157 | not_attempted | experiment | refused: domain not eligible |
| PaySim | gradient_boosting | `M-507974176598d1fa` | 95,408 | 12.9 | 10.0 | 1.62 (1.22 / 0.342) | 0.326 | not_attempted | experiment | refused: domain not eligible |
| PaySim | gradient_boosting [no_simulator_fields] | `M-b9c9027e3125181d` | 101,165 | 11.9 | 10.7 | 1.64 (1.25 / 0.336) | 0.338 | not_attempted | experiment | refused: domain not eligible |
| Synthetic | logistic_regression | `M-ffafac585fd8fe1c` | 27,652 | 8.1 | 2.1 | 1.54 (1.41 / 0.086) | 0.405 | calibrated | experiment | refused: domain not eligible |
| Synthetic | gradient_boosting | `M-7810f6ebd3c8432c` | 136,310 | 11.4 | 13.3 | 1.81 (1.43 / 0.324) | 0.475 | calibrated | experiment | refused: domain not eligible |

**Agreement with P10.**
- Every artefact's `parameters_sha256` equals the P10 run's `model_sha256` (e.g. GBDT PaySim `10e9ae31ffb9…`,
  synthetic `a2e9688b61e1…`).
- Every test PR-AUC equals the P10 class-weighted grouped-split result.

**Other checks.**
- The LR artefacts were rebuilt a second time: byte-identical.
- After all training, registration and inference, P9 `result_sha256` was unchanged for both datasets, and no model
  was active.

**Performance.**
- Loading an artefact takes 1–13 ms (parse, schema, hash, semantic checks, rebuild).
- Inference is dominated by building the P6 features (1.2–1.4 s, the same as P9). Transforming and scoring take
  0.04–0.34 s for 7–10k rows.
- No retraining or refitting happens at inference.

## Deviations and decisions

1. **No fusion into P9, no SHAP.**
   - The roadmap's P11 plugs the supervised signal into fusion with SHAP attributions. This phase's instructions
     forbid changing P9 or feeding supervised predictions into it, and P10 found no reliable advantage over P9.
   - Fusion and attributions wait for a model that is production-eligible and validated in the target domain.
   - The `model_supervised` family stays unused.
2. **Registry in the workspace database, not a JSON index.**
   - Three append-only tables (migration 4) plus audit-log entries give tamper-evident status history.
   - The artefact files remain independently verifiable (`ledgerspy model verify --file`).
3. **Status vocabulary.**
   - Recorded statuses: experiment / candidate / approved / retired.
   - `incompatible` and `integrity_failed` are derived on every read rather than stored, so they can never be stale.
   - "Evaluated" is not a separate status: every artefact carries its evaluation record from training.
4. **Candidate needs an eligible domain.**
   - PaySim and synthetic artefacts cannot even become candidates ("remain experimental").
   - The approval path is tested with a stand-in artefact: synthetic labels declared as `audited_outcomes`. No real
     audited-outcome adapter exists.
5. **Experimental inference is explicit.**
   - Non-production models run only when requested by ID with `experimental: true`.
   - Output is labelled "experimental supervised model", with domain / out-of-domain / in-sample warnings.
6. **Lifecycle changes are CLI-only.** The API is read-only for models (list, inspect, active status) apart from
   running inference and reading inference metadata.
7. **No frontend change.** The spec makes UI exposure optional, so the UI keeps showing P9 only. Nothing in the
   frontend depends on P11.
8. **Integrity, not signatures.**
   - The SHA-256 detects modification. Authenticity comes from the registry's pinned hash and the audit log.
   - Signing (e.g. a workspace key) would belong to P20 security hardening.
9. **Features configuration differences are warnings.** Calendars and approval thresholds legitimately differ
   between engagements; feature *definitions* must match exactly.

## Known limitations

- **No production-eligible model exists.** PaySim and synthetic models are experimental by policy, and no labelled
  audit outcomes are available.
- **Feature cost.** Inference recomputes the dataset's P6 features (the same cost as P9).
- **Records on disk.** Inference records are local JSON files. A record with the same ID is overwritten by a rerun with
  identical inputs, which has identical scores.
- **One compatibility mode.** Only exact contract matching exists: any feature-registry change requires retraining.
- P10's limitations carry over: tiny PaySim positive counts, few independent synthetic clusters, pure-Python models,
  fixed hyperparameters.
