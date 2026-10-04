# P3 — Data Ingestion and Evidence: Acceptance Record

Date: 2026-10-04 · Scope defined in [`roadmap.md`](roadmap.md) §P3.

Flow: **CSV/XLSX → safe ingestion → explicit column mapping → canonical dataset → evidence record.**

P3 adds no analytics: no cleaning, integrity scoring, Benford, anomaly detection, risk engine, entity resolution,
graph or reconciliation. The P1 frontend is unchanged. It still parses files in the browser until it is wired to the
API in P9.

## Commands and results

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .      # All checks passed; 61 files formatted
.venv/Scripts/python -m pytest                                          # 238 passed (P2 + P3)
.venv/Scripts/python -m pytest tests/backend/test_ingest_*.py           # 116 passed (P3)
.venv/Scripts/python -m pytest <the 8 P2 test files>                    # 122 passed (P2, unchanged count)
node tests/frontend/harness.js                                          # 24 passed, 0 failed (P1)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py   # 14 passed, 0 failed (P1)
```

All tests run under the P2 network guard (`pytest-socket --allow-hosts=127.0.0.1`).
`test_ingestion_makes_no_network_calls` goes further: it replaces `socket.connect`, `connect_ex`, `create_connection`
and `getaddrinfo` with functions that fail. It then ingests PaySim and an XLSX containing URL text, and records **zero** calls.

## Acceptance criteria

| Criterion | Evidence | Result |
|---|---|---|
| CSV imports | Normal, BOM+CRLF, UTF-16-BOM, cp1252, quoted commas, quoted newlines, escaped quotes, semicolon + decimal comma (`test_ingest_csv.py`) | ✅ |
| XLSX imports | openpyxl-written workbook (dates, datetimes, numbers) and a hand-built workbook (`test_ingest_xlsx.py`) | ✅ |
| Formulas never evaluated | Cached values used. A formula without a cached value becomes a recorded missing value. A cached value that disagrees with its formula is still the one used. A `WEBSERVICE()` formula is never evaluated | ✅ |
| Malformed files give clear errors | E.g. `ragged_row`: "Data row 2 (line 3) has 5 fields but the header has 4."; `malformed_csv`: "Malformed CSV near line 3: unexpected end of data." The API returns `{"status":"rejected","error":{code,message,details}}` | ✅ |
| Over-limit files rejected | From config: `max_file_bytes` (413; enforced while streaming, even with no Content-Length), `max_rows`, `max_columns`, `max_cell_chars` | ✅ |
| Unsafe XLSX rejected | `vbaProject.bin`, macro-enabled content type, `.xlsm/.xls/.xlsb`, external links, ActiveX, embedded OLE, DTD/ENTITY (XML-bomb), encrypted/legacy OLE files, non-workbook zips | ✅ |
| Zip bombs rejected | 20 MiB padding part → `xlsx_zip_bomb` (ratio 1029:1 > 100:1). Total-uncompressed and entry-count limits are tested too. A sheet declaring `A1:XFD1048576` stays bounded | ✅ |
| PaySim imports to 10,127 canonical rows | `test_paysim_imports_exactly_10127_canonical_rows`; manual HTTP run below | ✅ |
| Correct, deterministic IDs | Every ID equals `txn_id(sha256(file), row)` for rows 1–10,127. All are unique, and a re-import gives identical IDs | ✅ |
| Labels never enter canonical rows | No `transactions` column, field or `extra` key is a label. Serialised rows never mention `isFraud`. Values live only in `labels`. Canonical rows are identical with and without the label columns | ✅ |
| Evidence SHA-256 = original bytes | The DB hash matches `hashlib`, `sha256sum` and `certutil` on the stored copy. The copy is byte-identical (BOM/CRLF/cp1252 kept) | ✅ |
| Evidence immutable | Read-only file (writing raises `PermissionError`). `evidence_files` rows can't be updated or deleted. Canonicalised datasets are frozen. A tampered copy blocks canonicalisation, and `ledgerspy evidence verify` fails | ✅ |
| Source IDs validated | Duplicates or blanks are never proposed. Forcing them returns `source_ids_not_unique` with the duplicate rows (`T1` at rows 1 and 3) | ✅ |
| No placeholder IDs | Every PaySim ID matches `^T-[0-9a-f]{16}$` | ✅ |
| Mapping explicit and inspectable | The upload only proposes. Each field shows status and basis, plus labels, unmapped columns and items needing confirmation. Nothing is canonicalised until `PUT …/mapping` | ✅ |
| Ambiguity requires confirmation | `01/04/2024`-style dates (`%d/%m/%Y` vs `%m/%d/%Y`), `1,234` decimals, two date columns, amount plus debit/credit, no direction source. The proposal can't be confirmed until resolved | ✅ |
| P2 / P1 still pass, ruff passes, no network | See above | ✅ |

## Real PaySim run (`ledgerspy serve`, HTTP, fresh workspace)

1. **Upload without a token** → `401`.
2. **Upload** `POST /api/cases/{case}/files?filename=paysim_sample_10k.csv` → `mapping_required`, 10,127 rows.
   - Evidence `65fb55c44c969035660e040246226f8d12d65963e37be25a8ea78efe9b6615c9`, 1,140,134 bytes, UTF-8, `,`.
3. **Proposed mapping** (adapter `paysim`; nothing needs confirmation):

   | Canonical field | Source | Basis |
   |---|---|---|
   | `posted_at` | `step` | `1970-01-01T00:00:00 + (step − 1) × 1 h`: an artificial clock, not an accounting date |
   | `amount_minor` | `amount` | Decimal point, minor units; no currency in PaySim |
   | `direction` | derived from `type` | `CASH_IN` → in; `CASH_OUT`, `DEBIT`, `PAYMENT`, `TRANSFER` → out (relative to `nameOrig`) |
   | `txn_type` | `type` | |
   | `payer_raw`, `account` | `nameOrig` | |
   | `payee_raw` | `nameDest` | |
   | label columns | `isFlaggedFraud`, `isFraud` | Stored only in `labels` |
   | unmapped → `extra` | `(unnamed column 1)`, `branch`, 4 balance columns, `unusuallogin`, `Acct type`, `Date of transaction`, `Time of day` | Verbatim |

4. **Confirm** `PUT /api/datasets/{id}/mapping` → `canonicalised`: 10,127 rows, `id_source = derived`,
   `time_basis = simulation_step`, `synthetic = true`.
   - mapping sha `15132e56…3aeb2`.
   - Issues: `amount.missing = 2`, `direction.missing = 4`. These are blank `amount`/`type` cells in the source, kept as `NULL`.
5. **Evidence check.** `sha256sum` of the original, `certutil` of `workspace/evidence/65fb…15c9.csv` and the `evidence_files` row
   all give `65fb55c4…6615c9`. The file has the read-only attribute (`-r--r--r--`, `attrib R`).
6. **isFraud separation:**
   - `transactions` columns: `dataset_id … txn_type, extra`, with no label column.
   - 0 of 10,127 rows mention `isFraud`/`isFlaggedFraud` in `extra`.
   - `labels`: `isFlaggedFraud=0` ×10,127, `isFraud=0` ×10,057, `isFraud=1` ×68.
   - The API transactions page contains no `isFraud`.
7. **Integrity.**
   - `ledgerspy evidence verify` → `OK … 1 evidence files match their recorded SHA-256 and are read-only`.
   - `ledgerspy audit verify` → `OK … 6 entries verified` (`schema.migrated` ×2, `case.created`, `evidence.stored`, `dataset.created`, `dataset.canonicalised`).
8. **Rejections over HTTP:** `malformed_csv` 422, `ragged_row` 422, `xlsx_macro` 422, `xlsx_zip_bomb` 422 (22 KB file, ratio 1029:1),
   `.xlsm` 415. Rejected files left no evidence.

## Deviations from `architecture.md` / `roadmap.md` (all recorded in `architecture.md`)

1. **`Transaction.posted_at`, `amount_minor`, `currency` and `direction` are now nullable** (a P2 model change).
   - Why: the roadmap requires all 10,127 PaySim rows, but the file has blank `amount`/`type` cells, and P3 must not invent values.
   - Every resulting `NULL` in a mapped field has an `ingest_issues` row.
   - Currency is `NULL` unless the source has a currency column or the user declares one; PaySim has none.
2. **Canonical rows exist from P3.** The §2 pipeline drew canonical tables after quality/normalise; here they are the *typed mapping*
   of the source, with nothing cleaned. P4 adds checks and logged corrections on top.
3. **The upload is a raw request body (`application/octet-stream`), not multipart.**
   - The size limit is enforced chunk by chunk while hashing, and no multipart-parser dependency is added.
   - The original filename is a query parameter and only metadata.
4. **The frontend still parses files in the browser.** The roadmap bullet "the frontend upload goes to the backend" is deferred to P9,
   where the UI is wired to the API. Per the P3 instructions, no UI rewiring was needed for acceptance.
5. **Only `kind=ledger` is accepted in P3.** Other kinds return `kind_not_supported` until their phases.
6. **Additions:**
   - `GET /api/datasets/{id}/transactions` (paged canonical rows)
   - `ingest_issues` table
   - `ledgerspy evidence verify`
   - frozen-dataset and immutable-evidence triggers
   - config sections `ingest`, `mapping` and `adapters`, with alias and label tables in config
7. **Once canonicalised, a dataset's mapping is final.** To map differently, upload again; the evidence is reused, not duplicated.

## Known limitations (deliberate, for later phases)

- **Header position.** The header must be the first non-blank row; title rows above it are not supported yet.
- **Delimiter detection.** It uses the header plus majority agreement in a 64 KiB sample. Ties ask for an explicit `delimiter`.
- **Source IDs across datasets.** They are unique per dataset only (rows are keyed by `(dataset_id, txn_id)`).
- **PaySim's `Date of transaction` is kept but not used.** It has unknown provenance and is ambiguous (D/M vs M/D), so it stays in `extra`.
