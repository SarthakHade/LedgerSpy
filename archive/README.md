# Archive — legacy hackathon prototype

Read-only reference material from the original hackathon build. **Nothing here is part of the active application,
and nothing here should be imported or quoted as a result.**

| Folder | Contents | Known problems (see `docs/audits/phase-1-current-state-audit.md`) |
|---|---|---|
| `hackathon-prototype/notebooks/` | `core_model.ipynb` (main pipeline), `core_model_variant_with_step.ipynb` (variant), `shap_explainability.ipynb` | Hard-coded `C:/Users/gaura/...` paths; SMOTE before train/test split; features computed on the full dataset; accuracy on 55 partly synthetic rows; constant features; random "industry benchmark"; saved model files (`fraud_model.pkl`, `features.json`) were never included |
| `hackathon-prototype/derived-data/` | Notebook outputs: cleaned (10,106 rows), featured (38 cols), SMOTE-balanced (272 rows, mostly synthetic) | Derived with the leakage above; not inputs to anything |
| `hackathon-prototype/python/` | `main.py`, `test_model.py`, `chatbot.py`, old `requirements.txt`, original README | `main.py` and `test_model.py` do not run (missing `model` module and `test_transactions.csv`) |
| `hackathon-prototype/screenshots/` | UI screenshots, including two of an older 5-tab UI | Show static and heuristic outputs (e.g., 79% "high risk") |

Logic worth carrying forward is listed in `docs/cleanup-log.md` §D, with the roadmap phase that will re-implement it properly.
