# P2 — Architecture Foundation: Acceptance Record

Date: 2026-10-04 · Scope defined in [`roadmap.md`](roadmap.md) §P2.

P2 creates the `ledgerspy` package and its infrastructure. It adds **no analytics**: no ingestion, cleaning,
Benford, anomaly detection, risk engine, entity resolution, graph, reconciliation or ML. The P1 frontend and its
heuristic scorer are unchanged (that scorer is replaced in P9).

## Setup and commands

```bash
py -3.12 -m venv .venv                       # Python ≥ 3.11 required
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .
.venv/Scripts/python -m pytest               # P2 suite; outbound network blocked
node tests/frontend/harness.js               # P1 regression
.venv/Scripts/ledgerspy serve                # prints and opens http://127.0.0.1:8765/?token=…
.venv/Scripts/ledgerspy audit verify         # exit 0 = chain intact, 1 = tampered, 2 = no database
```

## Results

**`pytest`: 122 passed. `ruff check` / `ruff format --check`: clean. P1 harness: 24 passed. P1 browser check: 14 passed.**

| Roadmap criterion | Evidence | Result |
|---|---|---|
| Config validation | `test_config.py`: 14 invalid configs rejected (non-loopback host, string port, bad range, unknown keys, missing section, duplicate key, bad YAML, …); overrides validated | ✅ |
| Config hash determinism | Same hash across loads; independent of YAML formatting, comments and key order; changes when any value changes; overrides included | ✅ |
| ID determinism (same file + row = same `txn_id`) | `test_ids.py`: documented construction reproduced independently; order-independent; 400,000 IDs over two files without collision; ambiguous inputs rejected | ✅ |
| Audit-log chain verification | `test_db_audit.py`: every link recomputed; `ledgerspy audit verify` → `OK … 2 entries verified` on a live workspace | ✅ |
| Tamper detection | Edits to `details`/`actor`/`ts`/`action`/`prev_hash`, a deleted middle entry, a re-hashed forged entry, an appended forged entry, a dropped trigger and an emptied log all fail. Manual run: edit on a copy → `FAIL … entry 2: hash mismatch`, exit 1 | ✅ |
| API binds only to loopback | `test_server_binding.py`: real uvicorn sockets all `127.0.0.1`; `build_server` refuses other hosts even if validation is bypassed. Manual: `netstat` shows only `127.0.0.1:8765 LISTENING`; request to the LAN IP fails | ✅ |
| Token required | `test_api_security.py`: no / wrong / Basic / cookie-only / query-string token → 401. Manual `curl` → 401 `session token required` | ✅ |
| Security headers | Present on the page, static assets, API, 400/401/404. Script CSP has no `unsafe-inline`; in a real browser the UI's own handlers run while injected `onclick`/`onerror` are blocked | ✅ |
| Outbound network blocked in tests | `pytest-socket --allow-hosts=127.0.0.1`; `test_network_guard.py` shows TCP, UDP and HTTP to non-loopback addresses raise `SocketConnectBlockedError` | ✅ |
| `ledgerspy serve` opens the UI with no external requests | `test_cli.py` starts the real CLI. Manual browser check: 7 resources, all `http://127.0.0.1:8765/…`; upload → scan → tab switch works, zero CSP violations | ✅ |

## Decisions that differ from or add to `architecture.md`

All of these are now written into `architecture.md`.

1. **`txn_id` uses 16 hex characters, not 12.** With 12, a 10⁶-row file has a ~0.2% chance of a collision somewhere; with 16 it is ~3·10⁻⁸.
2. **Canonical models live in `ledgerspy/canonical/`.** §16 did not name a home for them.
3. **`config/default.yaml` is at the repository root.** This follows the roadmap and §2; §16 had implied it sat inside the package. The schema has no defaults, so the YAML is the only place values are set.
4. **Interim CSP.** It uses `'unsafe-hashes'` with one hash per existing inline handler, instead of a strict `script-src 'self'`. Strict needs the handlers removed (P20), and P2 must not change the UI. `style-src` keeps `'unsafe-inline'` for now.
5. **How the token is delivered.** The entry URL token is exchanged for an HttpOnly, SameSite=Strict cookie that unlocks the page. The API accepts only the `Authorization: Bearer` header. Fetch Metadata (`Sec-Fetch-Site`) checks were added.
6. **`/api/health` is public.** It returns only status, versions and the config hash.
7. **Audit-log details.** The trigger-integrity check was added. Tail truncation can't be detected from the DB alone, so the head hash is printed so it can be anchored elsewhere (P16).

## Notes for later phases

- **`Transaction.posted_at` is required, as specified.** PaySim only has `step`, so the P3 PaySim adapter must decide how to map it.
- **Source-ID uniqueness.** `txn_id(source_id=…)` uses a source ID verbatim. P3 must check that the IDs are unique within a dataset first, and key transactions by `(dataset_id, txn_id)`.
- **`fastapi` depends on `opentelemetry-api`.** It is the no-op API only; no SDK or exporter is installed, and nothing is sent.
- **`httpx2` is the TestClient transport.** Starlette 1.x deprecates `httpx` for this.
- **Playwright stays an optional, unpinned tool** for `tests/frontend/browser_check.py`. On a Windows console, set `PYTHONIOENCODING=utf-8` so it can print `₹`.
