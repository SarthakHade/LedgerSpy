"""P3: explicit mapping — conservative suggestions, confirming ambiguity, validation, unique source IDs."""

import pytest

from ledgerspy.canonical.ids import txn_id
from ledgerspy.config import ConfigError, load_config
from ledgerspy.hashing import sha256_hex
from ledgerspy.ingest.errors import IngestError
from tests.backend.conftest import FIXTURES


def proposed(ds):
    return ds["mapping_suggestion"]["proposed_mapping"]


# ---------- suggestion shape: required / optional / unmapped / labels ----------


def test_generic_suggestion_is_inspectable(ingest):
    ds = ingest.upload(FIXTURES / "ledger_normal.csv")
    s = ds["mapping_suggestion"]
    assert ds["status"] == "mapping_required"  # nothing is canonicalised before confirmation
    assert s["adapter"]["suggested"] == "generic_ledger"
    assert proposed(ds)["fields"] == {
        "txn_id": "Txn ID",
        "posted_at": "Posting Date",
        "payee_raw": "Vendor",
        "description": "Narration",
        "amount": "Amount",
        "direction": "Dr/Cr",
        "currency": "Currency",
        "reference": "Invoice No",
    }
    assert s["fields"]["amount"]["status"] == "proposed" and "exact alias" in s["fields"]["amount"]["basis"]
    assert s["fields"]["account"]["status"] == "unmapped"
    assert s["label_columns"] == ["isFraud"]
    assert s["unmapped_columns"] == ["Cost Centre"]
    assert s["requires_confirmation"] == []
    assert ingest.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    columns = {c["name"]: c for c in ds["profile"]["columns"]}
    assert columns["Txn ID"]["unique"] == {"blanks": 0, "duplicates": 0}
    assert columns["Vendor"]["samples"] == ["Acme Supplies Ltd", "Blue Dart", "Customer A"]


# ---------- 21. ambiguous mapping requires confirmation ----------


def test_ambiguous_date_format_requires_confirmation(ingest):
    ds = ingest.upload(FIXTURES / "ambiguous_dates.csv")
    s = ds["mapping_suggestion"]
    assert proposed(ds)["fields"]["posted_at"] == "Date"
    assert "date_format" not in {k for k, v in proposed(ds)["options"].items() if v}
    item = next(i for i in s["requires_confirmation"] if i["item"] == "options.date_format")
    assert item["reason"] == "ambiguous date format"
    assert set(item["candidates"]) >= {"%d/%m/%Y", "%m/%d/%Y"}

    with pytest.raises(IngestError) as e:  # the proposal as-is cannot be confirmed
        ingest.confirm(ds)
    assert e.value.code == "mapping_invalid"
    assert any("date_format" in err["message"] for err in e.value.details["errors"])

    day_first = {**proposed(ds), "options": {**proposed(ds)["options"], "date_format": "%d/%m/%Y"}}
    ingest.confirm(ds, day_first)
    assert [t["posted_at"][:10] for t in ingest.transactions(ds["dataset_id"])] == [
        "2024-04-01",
        "2024-06-05",
        "2024-08-07",
    ]


def test_ambiguous_decimal_separator_requires_confirmation(ingest):
    ds = ingest.upload(
        b'Date,Vendor,Amount,Dr/Cr\n2024-01-01,A,"1,234",Dr\n2024-01-02,B,"5,678",Cr\n', "amb.csv"
    )
    item = next(i for i in ds["mapping_suggestion"]["requires_confirmation"] if "decimal" in i["item"])
    assert item["reason"] == "ambiguous decimal separator" and item["examples"]
    assert proposed(ds)["options"]["decimal_separator"] is None
    thousands = {**proposed(ds), "options": {**proposed(ds)["options"], "decimal_separator": "."}}
    ingest.confirm(ds, thousands)
    assert [t["amount_minor"] for t in ingest.transactions(ds["dataset_id"])] == [123_400, 567_800]


def test_two_date_columns_are_ambiguous_not_guessed(ingest):
    ds = ingest.upload(b"Date,Posting Date,Vendor,Amount,Dr/Cr\n2024-01-01,2024-01-03,A,5,Dr\n", "two.csv")
    f = ds["mapping_suggestion"]["fields"]["posted_at"]
    assert f["status"] == "ambiguous" and f["candidates"] == ["Date", "Posting Date"]
    assert "posted_at" not in proposed(ds)["fields"]


def test_amount_and_debit_credit_columns_are_ambiguous(ingest):
    ds = ingest.upload(b"Date,Amount,Debit,Credit\n2024-01-01,5,5,\n", "both.csv")
    assert not {"amount", "debit_amount", "credit_amount"} & set(proposed(ds)["fields"])
    assert any(i["item"] == "amount source" for i in ds["mapping_suggestion"]["requires_confirmation"])


def test_no_direction_source_requires_a_declaration(ingest):
    ds = ingest.upload(b"Date,Vendor,Amount\n2024-01-01,A,5\n2024-01-02,B,6\n", "nodir.csv")
    assert any(i["item"] == "direction source" for i in ds["mapping_suggestion"]["requires_confirmation"])
    with pytest.raises(IngestError):
        ingest.confirm(ds)
    declared = {**proposed(ds), "options": {**proposed(ds)["options"], "direction": "debit"}}
    result = ingest.confirm(ds, declared)
    assert {t["direction"] for t in ingest.transactions(ds["dataset_id"])} == {"debit"}
    assert result["issues"]["total"] == 0


def test_values_that_do_not_fit_are_not_proposed(ingest):
    ds = ingest.upload(b"Date,Vendor,Amount,Dr/Cr\nyesterday,A,lots,Dr\ntoday,B,more,Cr\n", "nofit.csv")
    fields = ds["mapping_suggestion"]["fields"]
    assert (
        fields["amount"]["status"] == "low_confidence" and fields["posted_at"]["status"] == "low_confidence"
    )
    assert set(proposed(ds)["fields"]) == {"payee_raw", "direction"}


def test_debit_credit_columns(ingest):
    ds = ingest.upload(FIXTURES / "debit_credit.csv")
    assert proposed(ds)["fields"] == {
        "posted_at": "Value Date",
        "description": "Particulars",
        "debit_amount": "Withdrawal",
        "credit_amount": "Deposit",
        "reference": "Cheque No",
    }
    result = ingest.confirm(ds)
    txns = ingest.transactions(ds["dataset_id"])
    assert [(t["amount_minor"], t["direction"]) for t in txns] == [
        (1_500_000, "debit"),
        (8_200_000, "credit"),
        (0, None),
        (None, None),
    ]
    assert txns[0]["reference"] == "000123"  # leading zeros kept: references are text
    assert result["issues"]["by_field"] == {
        "amount": {"debit_and_credit_both_set": 1},
        "direction": {"zero_amount_no_direction": 1},
    }


def test_mapping_that_does_not_fit_the_data_is_refused(ingest):
    ds = ingest.upload(FIXTURES / "ambiguous_dates.csv")
    wrong = {**proposed(ds), "options": {**proposed(ds)["options"], "date_format": "%Y-%m-%d"}}
    with pytest.raises(IngestError) as e:
        ingest.confirm(ds, wrong)
    assert e.value.code == "mapping_does_not_fit" and e.value.details["unconvertible"] == {"posted_at": 3}
    assert ingest.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0  # rolled back
    assert ingest.conn.execute("SELECT status FROM datasets").fetchone()[0] == "mapping_required"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"fields": {"posted_at": "Nope"}}, "unknown_column"),
        ({"fields": {"amount": "isFraud"}}, "label_column_mapped"),
        ({"label_columns": []}, "label_columns"),
        ({"fields": {"debit_amount": "Amount"}}, "amount_source"),
        (
            {"options": {"decimal_separator": ".", "date_format": "%Y-%m-%d", "direction": "debit"}},
            "direction_source",
        ),
        (
            {"options": {"decimal_separator": ".", "date_format": "%Y-%m-%d", "currency": "USD"}},
            "currency_source",
        ),
        ({"options": {"date_format": "%Y-%m-%d"}}, "options"),
        ({"adapter": "paysim"}, "adapter"),
    ],
)
def test_invalid_mappings_are_rejected(ingest, change, code):
    ds = ingest.upload(FIXTURES / "ledger_normal.csv")
    base = proposed(ds)
    mapping = {**base, **change}
    if "fields" in change:
        mapping["fields"] = {**base["fields"], **change["fields"]}
    with pytest.raises(IngestError) as e:
        ingest.confirm(ds, mapping)
    assert e.value.code == "mapping_invalid"
    assert code in {err["code"] for err in e.value.details["errors"]}


def test_confirmation_is_once_only(ingest):
    ds = ingest.upload(FIXTURES / "ledger_normal.csv")
    ingest.confirm(ds)
    with pytest.raises(IngestError) as e:
        ingest.confirm(ds)
    assert e.value.code == "already_canonicalised" and e.value.http_status == 409


# ---------- 17. source transaction IDs must be unique ----------


def test_duplicate_source_ids_are_not_proposed(ingest):
    ds = ingest.upload(FIXTURES / "duplicate_ids.csv")
    f = ds["mapping_suggestion"]["fields"]["txn_id"]
    assert f["status"] == "low_confidence" and "1 blank, 1 duplicate" in f["basis"]
    assert "txn_id" not in proposed(ds)["fields"]


def test_forcing_duplicate_source_ids_is_refused(ingest):
    ds = ingest.upload(FIXTURES / "duplicate_ids.csv")
    forced = {**proposed(ds), "fields": {**proposed(ds)["fields"], "txn_id": "Txn ID"}}
    with pytest.raises(IngestError) as e:
        ingest.confirm(ds, forced)
    assert e.value.code == "source_ids_not_unique"
    assert e.value.details["duplicates"] == [{"value": "T1", "rows": [1, 3]}]
    assert e.value.details["blank_rows"] == [4]


def test_without_source_ids_derived_ids_are_used(ingest):
    ds = ingest.upload(FIXTURES / "duplicate_ids.csv")
    result = ingest.confirm(ds)
    sha = sha256_hex((FIXTURES / "duplicate_ids.csv").read_bytes())
    txns = ingest.transactions(ds["dataset_id"])
    assert result["id_source"] == "derived"
    assert [t["txn_id"] for t in txns] == [txn_id(sha, r) for r in (1, 2, 3, 4)]
    assert [t["extra"]["Txn ID"] for t in txns] == ["T1", "T2", "T1", ""]  # the column is kept, verbatim


# ---------- config guards on alias tables ----------


def test_alias_tables_must_be_unambiguous():
    with pytest.raises(ConfigError, match="both"):
        load_config(overrides={"mapping": {"aliases": {"amount": ["amount", "vendor"]}}})
    with pytest.raises(ConfigError, match="label column"):
        load_config(overrides={"mapping": {"aliases": {"amount": ["amount", "is_fraud"]}}})
