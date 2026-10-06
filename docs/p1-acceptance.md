# P1 — Truthful Baseline: Acceptance Record

Date: 2026-10-04 · Scope defined in [`roadmap.md`](roadmap.md) ("First implementation phase").

> **Superseded in P9.** The in-browser heuristic scorer described here was deleted when the UI was wired to the
> backend. `tests/frontend/harness.js` and `browser_check.py` were rewritten for the API-driven UI, keeping the P1
> guarantees (no fabricated values, no labels, safe rendering, no network). See [`p9-acceptance.md`](p9-acceptance.md).

P1 changes the **existing browser prototype only**. There is no backend, no new analytics and no new ML. The
heuristic scorer is unchanged in its maths; the change is that it is labelled honestly, no longer reads labels,
and explains itself exactly.

## How to run the tests

```bash
# from the repository root
node tests/frontend/harness.js                 # Node ≥ 18, no dependencies
python tests/frontend/browser_check.py         # optional; needs `pip install playwright` + a local Chromium
python tests/frontend/browser_check.py --screenshots tests/frontend/out/shots
```

Both write machine-readable results to `tests/frontend/out/`, which is git-ignored.

## Results

**Harness: 24 passed, 0 failed. Browser check: 14 passed, 0 failed.**

| ID | Criterion | Evidence | Result |
|---|---|---|---|
| A1 | No fabricated literals, mock functions, external fonts or `Math.random()` in `frontend/` | Harness scans 8 files for 26 forbidden patterns (₹2,45,00,000, 93.7%, 3.4x, zero-knowledge, 68% chance, Stationery L.T.D, MG Road, PMLA, Cluster #4, Risk = 87%, Math.random, fonts.googleapis, +128%, FY 2024, runMockBSR, court-ready, …). It also checks for no external URLs (except the SVG namespace) and no `alert()` | ✅ |
| A2 | Scores identical with and without `isFraud` | All 10,127 rows have identical score and tier when `isFraud` is dropped, set to all-1, or set to all-0. Label columns are never mapped; the engine code has no `isFraud` reference | ✅ |
| A3 | Malicious CSV renders as text, no script execution | Browser: a fixture with `<img onerror>`, `<script>`, `<svg onload>`, `<b>` and `javascript:` payloads. Zero dialogs, zero injected elements, payload visible as literal text after scan, all tabs, row expansion and chatbot queries. Harness: chatbot output escaped in 10 query branches; `innerHTML` only in two allow-listed static/escaped places | ✅ |
| A4 | Zero non-local requests; renders offline | Browser: 8 requests on load, all `file://`. With all network blocked, CSS and scripts load and the page renders | ✅ |
| A5 | Every displayed number is computed or explicitly unavailable; donut = tier counts | Donut segments: 8,493 / 23 / 1,611 with arc lengths = C·n/total (±0.01). Hero, overview, insights and relationship tiles match the summary. Recon, Monte Carlo, benchmark and graph show "Not available". Benford (9 bars), rule summary and memo are built from data. Every score equals its clamped component sum (10,127 rows). Readiness covers all rows and is never 100% when issues exist | ✅ |
| A6 | No `TX-???`; "why is ROW-n flagged?" finds the right row | 10,127 unique `ROW-n` IDs. `ROW-123` → data row 123. The file's own ID column is used when present (`INV-0001`). Rows added in the UI get `NEW-n`, stable across re-scans. Chatbot resolves `ROW-123` and `row 12` exactly, and says "not found" for unknown IDs | ✅ |
| A7 | Existing features still work | Browser flow: upload → scan → filter (high only) → search → expand row (shows points breakdown) → add row (`NEW-10128`, "NOT SCORED") → re-scan (row scored) → export (10,129 lines) → 4 chatbot chips → "why is ROW-123" → copy-memo enabled. Every tab has visible content. No JS errors, no dialogs. Harness: export round-trip + formula neutralisation; quoted-comma, escaped-quote and BOM parsing | ✅ |
| A8 | Heuristic banner shows the flag rate | The banner reads "Heuristic preview (rule-based, not a trained model). Flag rate: 84.1% of rows — above the 5% review budget, so this preview is NOT calibrated for this file (known issue; replaced in roadmap P9)." | ✅ |
| A9 | Single reviewed commit with checklist results | This document plus commit "P1: Truthful baseline …" | ✅ |

### Measured output on `data/raw/paysim_sample_10k.csv` (unchanged maths, now disclosed)

10,127 rows: 8,493 high (83.9%), 23 suspicious (0.2%), 1,611 below thresholds. Average score 85/100. Flag
rate 84.1%, which is above the 5% review budget. The UI now states this openly. Fixing it is P9's job, not P1's.

## What changed (summary)

- **Label leakage removed.** No `isFraud` override. Label columns (`isFraud`, `isFlaggedFraud`, `label`, `target`, …) are excluded from engine mapping, display mapping and scoring.
- **One threshold source.** `frontend/js/shared.js` → `LS_CONFIG` (high ≥ 70, suspicious ≥ 40, review budget 5%), used by the engine, table, memo and chatbot.
- **Exact explanations.** Each score carries its components (sigmoid base + rule points). The table detail row, chatbot and Explainability tab show them. "SHAP-style" wording and the second weight table are gone.
- **Fabricated content removed.** Static donut, quick stats, benchmark, Benford bars, Monte Carlo chart, relationship graph and entity cards (addresses/directors/PMLA), default XAI bars, hard-coded memo findings, fake PDF/Auto-Fix alerts, random bank reconciliation and "Cluster #4" are all gone. Each is replaced by computed values or a "Not available yet" state naming the roadmap phase.
- **XSS fixed.** Data is rendered with `textContent` and DOM nodes (`el()` helper). The chatbot escapes every file-derived value.
- **Offline.** The Google Fonts link was removed. The CSS falls back to local or system fonts. No external requests.
- **IDs.** `ROW-n` (1-based data row) when the file has no ID column; `NEW-n` for rows added in the UI. Added the "Date of transaction" alias.
- **Honest wording.** The About modal no longer presents the PS scenario as a real co-founder quote. Footer, chatbot header and upload prompt (CSV only) were corrected.

## Fixes made along the way (pre-existing bugs found by the new tests)

- **Hidden tabs.** An unclosed `<div>` in the Trends tab nested the Anomalies, Relationships and Explainability tabs inside it, so those three tabs rendered blank in the prototype. It is now closed, and a browser test asserts that every tab has visible content.
- **Re-scanning.** Added rows were shown as "suspicious" without being scored. They are now "NOT SCORED" until a re-scan, which is now possible.
- **Large files.** `Math.max(...amounts)` could overflow the stack on large files. Replaced with a reduce.
- **Column mapping.** Partial-match mapping could bind an empty header (the pandas index column) or `to` → `total`. It now ignores empty and label headers and very short aliases.
- **CSV parsing.** Now handles escaped quotes (`""`), CRLF and a UTF-8 BOM.
- **Export.** Keeps the original row order and neutralises spreadsheet formulas (`=`, `+`, `@`).
- **Scatter plot.** No random jitter or sampling. It is deterministic, and its axes are now score vs log amount (the old "frequency" axis was constant on this data).

## Not in P1 (deliberately)

- **The heuristic's 84% flag rate.** It is disclosed, not tuned; tuning a hand-made formula would be wasted work before P9.
- **Strict CSP.** It requires removing the inline `onclick` handlers (P20). The XSS fix does not depend on CSP.
- **Self-hosted Sora/DM Mono font files.** Planned for P21. The system font fallback looks close and works offline.
