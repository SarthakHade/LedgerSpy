# Phase 2 Repository Cleanup Log (2026-10-04)

The original folder state is preserved in git commit **`Baseline: original hackathon project state before Phase 2
cleanup`** (the first commit). Anything removed below can be restored with
`git checkout <baseline-commit> -- <path>`.

Duplicates were verified by **MD5 hash** before deletion. Zip contents were verified by extracting and hashing.
Screenshots were verified by pixel comparison.

## A. Preserved (active)

| Original path | New path | Reason |
|---|---|---|
| `Datasets.csv` | `data/raw/paysim_sample_10k.csv` | The only raw dataset (md5 `aff98f7c…`). Renamed to describe what it is |
| `Frontend_Hack/ledgerspy.html` | `frontend/index.html` | The active application UI |
| `Frontend_Hack/css/*` | `frontend/css/*` | Active styles |
| `Frontend_Hack/js/*` | `frontend/js/*` | Active scripts (to be fixed in P1, then replaced in P9) |

## B. Moved to archive (`archive/hackathon-prototype/`)

| Original path | Archive path | Why it is kept |
|---|---|---|
| `core_model.ipynb` | `notebooks/core_model.ipynb` | Feature-engineering, rule and Monte Carlo logic to port; documents past methodology |
| `core_model (2).ipynb` | `notebooks/core_model_variant_with_step.ipynb` | A *different* variant (adds `step`, `class_weight='balanced'`, retrains on SMOTE data); unique history |
| `model_2.ipynb` | `notebooks/shap_explainability.ipynb` | Real SHAP usage to port |
| `cleaned_fraud_data.csv`, `featured_fraud_data.csv`, `smote_balanced_data.csv` | `derived-data/` | Notebook outputs that reproduce the audit findings (leakage, degenerate features). Not inputs to anything |
| `fraud-detection-project/main.py` | `python/main.py` | Broken demo (missing `model` module); kept as history |
| `fraud-detection-project/requirements.txt` | `python/requirements.txt` | Old, incomplete dependency list |
| `fraud-detection-project/README.md` | `python/README_original.md` | Old README (superseded) |
| `Frontend_Hack/chatbot.py` | `python/chatbot.py` | Python original of the JS chatbot |
| `Frontend_Hack/test_model.py` | `python/test_model.py` | Broken script (missing CSV/module); not a test |
| `fraud-detection-project/image-1.png` | `screenshots/hero-after-scan.png` | Shows the 79% high-risk behaviour |
| `fraud-detection-project/image.png` | `screenshots/trends-tab.png` | Shows the old static trends alert |
| `Frontend_Hack/dnv kvc.png` | `screenshots/old-ui-relationships.png` | Older UI version |
| `Frontend_Hack/sdfcsdv.png` | `screenshots/old-ui-explainability.png` | Older UI version |

## C. Deleted (exact duplicates only)

| Deleted | Identical to (kept) | Evidence |
|---|---|---|
| `archive.zip` | `Datasets.csv` | Contains only `Datasets.csv`, md5 `aff98f7c430c0a2b8c8c74b82cf8d5f0` |
| `Datasets.csv.zip` | `Datasets.csv` | Same as above |
| `fraud-detection-project/core_model.ipynb` | `core_model.ipynb` | md5 `f45b0d25…` |
| `Frontend_Hack/core_model (3).ipynb` | `core_model.ipynb` | md5 `f45b0d25…` |
| `fraud-detection-project/model_2.ipynb` | `model_2.ipynb` | md5 `62a0031d…` |
| `Frontend_Hack/model_2 (2).ipynb` | `model_2.ipynb` | md5 `62a0031d…` |
| `fraud-detection-project/cleaned_fraud_data.csv` | `cleaned_fraud_data.csv` | md5 `b65dfd18…` |
| `fraud-detection-project/featured_fraud_data.csv` | `featured_fraud_data.csv` | md5 `8d549192…` |
| `fraud-detection-project/smote_balanced_data.csv` | `smote_balanced_data.csv` | md5 `e4daa7e1…` |
| `fraud-detection-project/WhatsApp Image 2026-04-19 at 3.19.03 AM.jpeg` | `image-1.png` | Same 1600×742 pixels, mean absolute difference 0.0 |

Empty folders `fraud-detection-project/` and `Frontend_Hack/` were removed after the moves.

## D. To be merged into the main implementation later (from the archive)

| Source | Destination phase |
|---|---|
| Notebook feature code (balance error, amount ratio, duplicates, threshold, round amount) | P6/P7 feature registry and rules, redefined per `architecture.md` §6 |
| `predict_transaction` output contract (`decision`, `risk_score`, `reasons`, `action`) | P9 explanation/score schema |
| SHAP notebook | P11 explainability |
| `monte_carlo_simulation` skeleton | P18 (with real inputs) |
| `chatbot.py` / `chatbot-ui.js` intents | P17 |

## E. Added in Phase 2

`README.md`, `.gitignore`, `archive/README.md`, `data/README.md`, and `docs/` (problem statement, requirements,
architecture, data strategy, roadmap, this log, Phase 1 audit).
