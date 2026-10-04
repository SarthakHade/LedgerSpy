"""P3: CSV parsing — encodings, delimiters, quoting, malformed input, limits."""

import codecs

import pytest

from ledgerspy.ingest.errors import IngestError
from tests.backend.conftest import FIXTURES, make_ingest


def canonical(ingest, name, mapping=None, **kw):
    ds = ingest.upload(FIXTURES / name, **kw)
    result = ingest.confirm(ds, mapping)
    return ds, result, ingest.transactions(ds["dataset_id"])


# ---------- 1. normal CSV ----------


def test_normal_csv_imports(ingest):
    ds, result, txns = canonical(ingest, "ledger_normal.csv")
    assert ds["format_details"]["encoding"] == "utf-8" and ds["format_details"]["delimiter"] == ","
    assert result["rows"] == 4 and result["issues"]["total"] == 0
    assert result["id_source"] == "source_column"
    first = txns[0]
    assert first["txn_id"] == "JV-0001"
    assert first["posted_at"] == "2024-04-01T00:00:00"
    assert first["amount_minor"] == 1_250_000 and first["currency"] == "INR" and first["direction"] == "debit"
    assert first["payee_raw"] == "Acme Supplies Ltd" and first["reference"] == "INV-1001"
    assert first["description"] == "Office chairs"
    assert first["extra"] == {"Cost Centre": "ADMIN"}
    assert [t["direction"] for t in txns] == ["debit", "debit", "credit", "debit"]
    assert result["time_basis"] == "date"


# ---------- 2. BOM (+ CRLF) ----------


def test_bom_crlf_csv_matches_plain_csv(ingest):
    _, _, plain = canonical(ingest, "ledger_normal.csv")
    ds, _, bom = canonical(ingest, "ledger_bom_crlf.csv")
    assert ds["format_details"]["encoding"] == "utf-8 (BOM)"
    assert "Txn ID" in [c["name"] for c in ds["profile"]["columns"]]  # BOM not glued to the first header
    strip = lambda rows: [{k: v for k, v in r.items() if k != "dataset_id"} for r in rows]  # noqa: E731
    assert strip(bom) == strip(plain)


def test_utf16_with_bom_is_decoded(ingest):
    text = (FIXTURES / "ledger_normal.csv").read_text(encoding="utf-8")
    ds = ingest.upload(codecs.BOM_UTF16_LE + text.encode("utf-16-le"), "utf16.csv")
    assert ds["format_details"]["encoding"] == "utf-16-le (BOM)" and ds["row_count"] == 4


def test_cp1252_fallback_is_detected_and_recorded(ingest):
    ds, _, txns = canonical(ingest, "cp1252.csv", mapping=None)
    assert ds["format_details"]["encoding"] == "cp1252"
    assert txns[0]["payee_raw"] == "Café Noël"


# ---------- 3–5. quoting ----------


def test_quoted_commas(ingest):
    _, _, txns = canonical(ingest, "quoted_commas.csv")
    assert [t["payee_raw"] for t in txns] == ["Acme, Inc.", "Smith, Jones & Co", "Plain Vendor"]
    assert [t["amount_minor"] for t in txns] == [125_000, 30_000, 7_525]


def test_quoted_newlines_keep_record_numbering(ingest):
    _, _, txns = canonical(ingest, "quoted_newlines.csv")
    assert [t["source_row"] for t in txns] == [1, 2, 3]
    assert txns[0]["description"] == "Line one\nline two"
    assert txns[1]["description"] == "First\r\nSecond\nThird"
    assert txns[2]["payee_raw"] == "Gamma"


def test_escaped_quotes(ingest):
    _, _, txns = canonical(ingest, "escaped_quotes.csv")
    assert txns[0]["payee_raw"] == 'The "Best" Traders'
    assert txns[0]["description"] == 'He said "urgent"'
    assert txns[1]["description"] == '"quoted at start'


# ---------- 6. semicolon + decimal comma ----------


def test_semicolon_csv_with_decimal_comma(ingest):
    ds, result, txns = canonical(ingest, "semicolon_decimal_comma.csv")
    assert ds["format_details"]["delimiter"] == ";"
    assert ds["mapping_suggestion"]["proposed_mapping"]["options"]["decimal_separator"] == ","
    assert ds["mapping_suggestion"]["proposed_mapping"]["options"]["date_format"] == "%d.%m.%Y"
    assert [t["amount_minor"] for t in txns] == [123_450, 9_995, 1_000_000]
    assert [t["posted_at"][:10] for t in txns] == ["2024-04-01", "2024-04-15", "2024-04-30"]
    assert txns[1]["payee_raw"] == "Café Central" and result["issues"]["total"] == 0


def test_explicit_delimiter_override(ingest):
    ds = ingest.upload(FIXTURES / "semicolon_decimal_comma.csv", delimiter=";")
    assert ds["format_details"]["delimiter"] == ";"
    with pytest.raises(IngestError) as e:
        ingest.upload(FIXTURES / "ledger_normal.csv", delimiter=":")
    assert e.value.code == "delimiter_unsupported"


# ---------- 7. malformed ----------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("malformed_quote.csv", "malformed_csv"),
        ("unclosed_quote.csv", "malformed_csv"),
        ("ragged.csv", "ragged_row"),
    ],
)
def test_malformed_csv_is_rejected_with_clear_error(ingest, name, code):
    with pytest.raises(IngestError) as e:
        ingest.upload(FIXTURES / name)
    assert e.value.code == code
    assert e.value.message  # human-readable
    assert not list((ingest.workspace / "evidence").glob("*"))  # nothing kept as evidence


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "empty_file"),
        (b"\n\n  \n", "no_header"),
        (b"Date,Amount\n", "no_data_rows"),
        (b"OnlyOneColumn\nvalue\n", "delimiter_not_detected"),
        (b"Date,Date\n2024-01-01,5\n", "duplicate_header"),
        (b"Date,Amount\n2024-01-01,5\x00\n", "binary_content"),
        (b"Date,Amount\n2024-01-01,\x81\x8d\n", "encoding_unsupported"),
        (b"a,b;c\n1,2;3\n", "delimiter_ambiguous"),
    ],
)
def test_structurally_bad_files_are_rejected(ingest, data, code):
    with pytest.raises(IngestError) as e:
        ingest.upload(data, "bad.csv")
    assert e.value.code == code


def test_unsupported_extensions(ingest):
    for name, status in (("ledger.xlsm", 415), ("ledger.xls", 415), ("ledger.pdf", 415), ("ledger", 415)):
        with pytest.raises(IngestError) as e:
            ingest.upload(b"Date,Amount\n2024-01-01,5\n", name)
        assert e.value.http_status == status, name


def test_workbook_disguised_as_csv_is_rejected(ingest):
    with pytest.raises(IngestError) as e:
        ingest.upload(b"PK\x03\x04rest-of-zip", "ledger.csv")
    assert e.value.code == "extension_mismatch"


# ---------- 11–12. limits from config ----------


def test_oversized_file_is_rejected(tmp_path):
    small = make_ingest(tmp_path, ingest={"max_file_bytes": 100})
    with pytest.raises(IngestError) as e:
        small.upload(FIXTURES / "ledger_normal.csv")
    assert e.value.code == "file_too_large" and e.value.http_status == 413
    assert not list((small.workspace / "tmp").glob("*"))  # staging cleaned up


def test_too_many_rows_is_rejected(tmp_path):
    limited = make_ingest(tmp_path, ingest={"max_rows": 3})
    with pytest.raises(IngestError) as e:
        limited.upload(FIXTURES / "ledger_normal.csv")
    assert e.value.code == "too_many_rows" and e.value.details == {"limit": 3}


def test_too_many_columns_is_rejected(tmp_path):
    limited = make_ingest(tmp_path, ingest={"max_columns": 4})
    with pytest.raises(IngestError) as e:
        limited.upload(FIXTURES / "ledger_normal.csv")
    assert e.value.code == "too_many_columns"


def test_oversized_cell_is_rejected(tmp_path):
    limited = make_ingest(tmp_path, ingest={"max_cell_chars": 50})
    with pytest.raises(IngestError) as e:
        limited.upload(b'Date,Vendor,Amount\n2024-01-01,"' + b"x" * 200 + b'",5\n', "big.csv")
    assert e.value.code == "cell_too_large"


def test_rejections_are_audit_logged(ingest):
    with pytest.raises(IngestError):
        ingest.upload(FIXTURES / "ragged.csv")
    row = ingest.conn.execute("SELECT action, details FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    assert row["action"] == "upload.rejected" and '"code":"ragged_row"' in row["details"]


# ---------- blank records, gaps and unconvertible values ----------


def test_gaps_become_none_plus_recorded_issues(ingest):
    ds = ingest.upload(FIXTURES / "blank_rows_and_gaps.csv")
    assert ds["profile"]["blank_records_skipped"] == 2 and ds["row_count"] == 4
    # The bad values mean nothing is *proposed* for these columns; the user confirms explicitly.
    assert ds["mapping_suggestion"]["proposed_mapping"]["fields"] == {"payee_raw": "Vendor"}
    mapping = {
        "adapter": "generic_ledger",
        "fields": {"posted_at": "Date", "payee_raw": "Vendor", "amount": "Amount", "direction": "Dr/Cr"},
        "options": {"decimal_separator": ".", "date_format": "%Y-%m-%d"},
    }
    result = ingest.confirm(ds, mapping)
    txns = {t["source_row"]: t for t in ingest.transactions(ds["dataset_id"])}
    assert sorted(txns) == [1, 4, 5, 6]  # rows 2-3 are blank records: numbered, not converted
    assert txns[4]["amount_minor"] is None and txns[4]["payee_raw"] is None
    assert txns[5]["posted_at"] is None and txns[5]["amount_minor"] is None and txns[5]["direction"] is None
    assert txns[6]["amount_minor"] is None  # 1.005 is never rounded
    assert result["issues"]["by_field"] == {
        "amount": {"missing": 1, "too_many_decimals": 1, "unparseable": 1},
        "direction": {"unknown_value": 1},
        "posted_at": {"unparseable": 1},
    }
    stored = ingest.conn.execute(
        "SELECT source_row, field, code, raw_value FROM ingest_issues WHERE dataset_id = ? ORDER BY 1, 2, 3",
        (ds["dataset_id"],),
    ).fetchall()
    assert ("abc" in [r["raw_value"] for r in stored]) and len(stored) == 5
