"""P3: XLSX ingestion — cached values only, unsafe workbook features and zip bombs refused."""

import io
import zipfile
from datetime import date, datetime

import pytest

from ledgerspy.ingest.errors import IngestError
from tests.backend.conftest import make_ingest
from tests.backend.xlsx_builder import Formula, build_xlsx

HEADER = ["Posting Date", "Vendor", "Qty", "Amount", "Dr/Cr", "isFraud"]
ROWS = [
    HEADER,
    [date(2024, 4, 1), "Acme Supplies Ltd", 2, 12500.5, "Dr", 0],
    [date(2024, 4, 2), "Customer A", 1, 50000, "Cr", 1],
    [date(2024, 4, 3), 1001, 3, 8000, "Dr", 0],
]


def openpyxl_workbook() -> bytes:
    """A workbook written by openpyxl itself (real-world styles, shared strings, datetimes)."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ledger"
    for row in ROWS:
        ws.append(row)
    ws.append([datetime(2024, 4, 4, 15, 30), "Late Entry", 1, 10, "Dr", 0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- 8. normal values ----------


def test_xlsx_with_normal_values(ingest):
    ds = ingest.upload(openpyxl_workbook(), "ledger.xlsx")
    assert ds["evidence"]["format"] == "xlsx" and ds["format_details"]["sheet"] == "Ledger"
    assert ds["format_details"]["formula_cells"] == 0
    result = ingest.confirm(ds)
    txns = ingest.transactions(ds["dataset_id"])
    assert result["rows"] == 4 and result["issues"]["total"] == 0
    assert [t["amount_minor"] for t in txns] == [1_250_050, 5_000_000, 800_000, 1_000]
    assert [t["direction"] for t in txns] == ["debit", "credit", "debit", "debit"]
    assert txns[0]["posted_at"] == "2024-04-01T00:00:00" and txns[3]["posted_at"] == "2024-04-04T15:30:00"
    assert txns[2]["payee_raw"] == "1001"  # numeric cell in a text field → its text, not "1001.0"
    assert txns[0]["extra"] == {"Qty": 2}
    assert result["time_basis"] == "datetime"
    assert (
        ds["mapping_suggestion"]["proposed_mapping"]["options"].get("date_format") is None
    )  # real date cells


def test_xlsx_builder_workbook_imports_identically(ingest):
    ds = ingest.upload(build_xlsx({"Ledger": ROWS}), "built.xlsx")
    ingest.confirm(ds)
    assert [t["amount_minor"] for t in ingest.transactions(ds["dataset_id"])] == [
        1_250_050,
        5_000_000,
        800_000,
    ]


# ---------- 9. formulas: cached values only ----------


def test_formulas_use_cached_values_and_are_never_evaluated(ingest):
    rows = [
        HEADER,
        [date(2024, 4, 1), "Acme", 2, Formula("C2*6250", 12500), "Dr", 0],
        # A formula whose cached result is absent: the value is missing (an issue), never computed.
        [date(2024, 4, 2), "Beta", 4, Formula("C3*10", None), "Cr", 0],
        # A cached value that differs from what the formula would give: the cached value wins.
        [date(2024, 4, 3), "Gamma", 1, Formula("C4*100", 999), "Dr", 0],
        [date(2024, 4, 4), "Delta", 1, Formula('WEBSERVICE("http://203.0.113.9/x")', 5), "Dr", 0],
    ]
    ds = ingest.upload(build_xlsx({"Ledger": rows}), "formulas.xlsx")
    assert ds["format_details"]["formula_cells"] == 4
    assert ds["format_details"]["values"].startswith("cached")
    result = ingest.confirm(ds)
    txns = ingest.transactions(ds["dataset_id"])
    assert [t["amount_minor"] for t in txns] == [1_250_000, None, 99_900, 500]
    assert result["issues"]["by_field"] == {"amount": {"missing": 1}}


# ---------- 10. macros and other unsafe workbook features ----------


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"extra_parts": {"xl/vbaProject.bin": b"\xd0\xcf\x11\xe0 fake vba"}}, "xlsx_macro"),
        ({"workbook_content_type": "application/vnd.ms-excel.sheet.macroEnabled.main+xml"}, "xlsx_macro"),
        ({"extra_parts": {"xl/externalLinks/externalLink1.xml": b"<externalLink/>"}}, "xlsx_external_link"),
        ({"extra_parts": {"xl/activeX/activeX1.xml": b"<ax/>"}}, "xlsx_active_content"),
        ({"extra_parts": {"xl/embeddings/oleObject1.bin": b"ole"}}, "xlsx_active_content"),
        (
            {"sheet_prolog": '<!DOCTYPE lol [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'},
            "xlsx_xml_attack",
        ),
    ],
)
def test_unsafe_workbook_features_are_refused(ingest, kwargs, code):
    with pytest.raises(IngestError) as e:
        ingest.upload(build_xlsx({"Ledger": ROWS}, **kwargs), "unsafe.xlsx")
    assert e.value.code == code
    assert not list((ingest.workspace / "evidence").glob("*"))


@pytest.mark.parametrize("name", ["ledger.xlsm", "ledger.xls", "ledger.xlsb"])
def test_macro_and_legacy_extensions_are_refused(ingest, name):
    with pytest.raises(IngestError) as e:
        ingest.upload(build_xlsx({"Ledger": ROWS}), name)
    assert e.value.code == "unsupported_format" and e.value.http_status == 415


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "xlsx_encrypted_or_legacy"),
        (b"Date,Amount\n2024-01-01,5\n", "xlsx_not_zip"),
        (b"PK\x03\x04 truncated archive", "xlsx_invalid"),
    ],
)
def test_broken_or_disguised_workbooks(ingest, data, code):
    with pytest.raises(IngestError) as e:
        ingest.upload(data, "broken.xlsx")
    assert e.value.code == code


def test_zip_without_a_workbook_is_refused(ingest):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "not excel")
    with pytest.raises(IngestError) as e:
        ingest.upload(buf.getvalue(), "fake.xlsx")
    assert e.value.code == "xlsx_invalid"


# ---------- 13. zip bombs ----------


def test_high_compression_ratio_part_is_refused_with_default_limits(ingest):
    padding = b" " * (20 * 1024 * 1024)  # 20 MiB of spaces deflates ~1000:1
    data = build_xlsx({"Ledger": ROWS}, extra_parts={"xl/padding.xml": padding})
    assert len(data) < 200_000
    with pytest.raises(IngestError) as e:
        ingest.upload(data, "bomb.xlsx")
    assert e.value.code == "xlsx_zip_bomb" and e.value.details["part"] == "xl/padding.xml"


def test_total_uncompressed_size_limit(tmp_path):
    small = make_ingest(tmp_path, ingest={"xlsx": {"max_uncompressed_bytes": 4096}})
    with pytest.raises(IngestError) as e:
        small.upload(build_xlsx({"Ledger": ROWS * 20}), "big.xlsx")
    assert e.value.code == "xlsx_zip_bomb"


def test_too_many_zip_entries(tmp_path):
    small = make_ingest(tmp_path, ingest={"xlsx": {"max_zip_entries": 5}})
    with pytest.raises(IngestError) as e:
        small.upload(
            build_xlsx({"Ledger": ROWS}, extra_parts={f"xl/media/x{i}.xml": b"<x/>" for i in range(5)}),
            "many.xlsx",
        )
    assert e.value.code == "xlsx_too_many_entries"


def test_huge_declared_dimension_is_bounded(tmp_path):
    """A sheet declaring A1:XFD1048576 (common after formatting) must not be padded to 16,384 x 1M cells."""
    import time

    limited = make_ingest(tmp_path, ingest={"max_rows": 10})
    data = build_xlsx({"Ledger": ROWS}, dimension="A1:XFD1048576")
    assert b'<dimension ref="A1:XFD1048576"/>' in zipfile.ZipFile(io.BytesIO(data)).read(
        "xl/worksheets/sheet1.xml"
    )
    started = time.perf_counter()
    ds = limited.upload(data, "dim.xlsx")
    assert ds["row_count"] == 3 and len(ds["profile"]["columns"]) == len(HEADER)
    assert time.perf_counter() - started < 5


# ---------- sheets and limits ----------


def test_multiple_sheets_require_an_explicit_choice(ingest):
    data = build_xlsx({"Notes": [["a", "b"], [1, 2]], "Ledger": ROWS})
    with pytest.raises(IngestError) as e:
        ingest.upload(data, "two.xlsx")
    assert e.value.code == "xlsx_sheet_required" and e.value.details["sheets"] == ["Notes", "Ledger"]
    ds = ingest.upload(data, "two.xlsx", sheet="Ledger")
    assert ds["format_details"]["sheet"] == "Ledger" and ds["row_count"] == 3
    with pytest.raises(IngestError) as e:
        ingest.upload(data, "two.xlsx", sheet="Missing")
    assert e.value.code == "xlsx_sheet_not_found"


def test_hidden_sheets_are_not_auto_selected(ingest):
    ds = ingest.upload(
        build_xlsx({"Ledger": ROWS, "Secret": [["x", "y"], [1, 2]]}, hidden=("Secret",)), "h.xlsx"
    )
    assert ds["format_details"]["sheet"] == "Ledger"


def test_xlsx_row_limit(tmp_path):
    limited = make_ingest(tmp_path, ingest={"max_rows": 2})
    with pytest.raises(IngestError) as e:
        limited.upload(build_xlsx({"Ledger": ROWS}), "rows.xlsx")
    assert e.value.code == "too_many_rows"


def test_xlsx_value_beyond_header_is_ragged(ingest):
    rows = [HEADER, [date(2024, 4, 1), "Acme", 1, 10, "Dr", 0, "stray"]]
    with pytest.raises(IngestError) as e:
        ingest.upload(build_xlsx({"Ledger": rows}), "ragged.xlsx")
    assert e.value.code == "ragged_row"
