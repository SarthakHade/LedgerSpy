"""P3: PaySim adapter (10,127 rows), deterministic IDs, and label columns never entering canonical data."""

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ledgerspy.canonical.ids import txn_id
from ledgerspy.canonical.models import Transaction
from ledgerspy.config import load_config
from ledgerspy.config.schema import normalize_header
from tests.backend.conftest import FIXTURES, PAYSIM

PAYSIM_SHA = hashlib.sha256(PAYSIM.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def paysim(tmp_path_factory):
    from tests.backend.conftest import make_ingest

    harness = make_ingest(tmp_path_factory.mktemp("paysim"))
    ds = harness.upload(PAYSIM)
    result = harness.confirm(ds)
    txns = harness.transactions(ds["dataset_id"])
    yield harness, ds, result, txns
    harness.conn.close()


# ---------- 14. PaySim ingestion ----------


def test_paysim_imports_exactly_10127_canonical_rows(paysim):
    harness, ds, result, txns = paysim
    assert ds["row_count"] == 10_127 and result["rows"] == 10_127 and len(txns) == 10_127
    assert [t["source_row"] for t in txns] == list(range(1, 10_128))
    count = harness.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE dataset_id = ?", (ds["dataset_id"],)
    )
    assert count.fetchone()[0] == 10_127


def test_paysim_suggestion_uses_the_paysim_adapter(paysim):
    _, ds, result, _ = paysim
    s = ds["mapping_suggestion"]
    assert s["adapter"]["suggested"] == "paysim" and s["requires_confirmation"] == []
    assert s["proposed_mapping"]["fields"] == {
        "posted_at": "step",
        "amount": "amount",
        "txn_type": "type",
        "payer_raw": "nameOrig",
        "account": "nameOrig",
        "payee_raw": "nameDest",
    }
    assert s["fields"]["direction"]["status"] == "derived"
    assert "NOT an accounting date" in s["fields"]["posted_at"]["basis"]
    assert result["adapter"] == "paysim" and result["synthetic"] is True
    assert result["time_basis"] == "simulation_step"
    assert any("SYNTHETIC" in n for n in result["notes"])


def test_paysim_ids_are_correct_and_deterministic(paysim):
    _, ds, result, txns = paysim
    assert ds["evidence"]["sha256"] == PAYSIM_SHA
    assert result["id_source"] == "derived"
    expected = [txn_id(PAYSIM_SHA, r) for r in range(1, 10_128)]
    assert [t["txn_id"] for t in txns] == expected
    assert len(set(expected)) == 10_127
    assert all(re.fullmatch(r"T-[0-9a-f]{16}", t["txn_id"]) for t in txns)  # never a placeholder


def test_reimport_gives_identical_ids(paysim, tmp_path):
    from tests.backend.conftest import make_ingest

    _, _, _, txns = paysim
    other = make_ingest(tmp_path)
    ds = other.upload(PAYSIM)
    other.confirm(ds)
    assert [t["txn_id"] for t in other.transactions(ds["dataset_id"])] == [t["txn_id"] for t in txns]
    other.conn.close()


def test_paysim_step_maps_to_posted_at(paysim):
    _, _, _, txns = paysim
    p = load_config().settings.adapters.paysim
    epoch = datetime.fromisoformat(p.step_epoch)
    assert "step" not in txns[0]["extra"]  # mapped columns are not duplicated into extra
    first = txns[0]
    assert first["posted_at"] == epoch.isoformat()  # step 1
    # Every posted_at is epoch + (step-1) hours with step from the source file.
    import csv

    with PAYSIM.open(encoding="utf-8", newline="") as f:
        steps = [int(r["step"]) for r in csv.DictReader(f)]
    assert [t["posted_at"] for t in txns] == [(epoch + timedelta(hours=s - 1)).isoformat() for s in steps]


def test_paysim_direction_amount_and_gaps(paysim):
    _, _, result, txns = paysim
    by_type = {}
    for t in txns:
        by_type.setdefault(t["txn_type"], set()).add(t["direction"])
    assert by_type["CASH_IN"] == {"in"}
    for kind in ("PAYMENT", "TRANSFER", "CASH_OUT", "DEBIT"):
        assert by_type[kind] == {"out"}
    assert txns[0]["amount_minor"] == 983_964 and txns[0]["currency"] is None
    assert txns[0]["payer_raw"] == "C1231006815" == txns[0]["account"]
    # The source has blanks: they stay None and are recorded, never invented.
    assert result["issues"]["by_field"] == {"amount": {"missing": 2}, "direction": {"missing": 4}}
    assert sum(t["amount_minor"] is None for t in txns) == 2
    assert sum(t["txn_type"] is None for t in txns) == 4


def test_paysim_unmapped_columns_are_preserved_verbatim(paysim):
    _, _, _, txns = paysim
    assert set(txns[0]["extra"]) == {
        "(unnamed column 1)",
        "branch",
        "oldbalanceOrg",
        "newbalanceOrig",
        "oldbalanceDest",
        "newbalanceDest",
        "unusuallogin",
        "Acct type",
        "Date of transaction",
        "Time of day",
    }
    assert txns[0]["extra"]["Date of transaction"] == "3/1/2018"  # kept, never interpreted
    assert txns[0]["extra"]["newbalanceOrig"] == "160296.36"


# ---------- 16. label columns never enter canonical data ----------


def test_labels_are_separated_and_absent_from_transactions(paysim):
    harness, ds, result, txns = paysim
    assert result["labels"]["columns"] == ["isFlaggedFraud", "isFraud"]
    labels = {normalize_header(n) for n in load_config().settings.mapping.label_columns}
    # 1. no transaction field or extra key is a label column
    for t in txns:
        assert not {normalize_header(k) for k in t} & labels
        assert not {normalize_header(k) for k in t["extra"]} & labels
    # 2. the table has no such column, and the serialised rows never mention them
    cols = {r[1] for r in harness.conn.execute("PRAGMA table_info(transactions)")}
    assert not {normalize_header(c) for c in cols} & labels
    assert "isFraud" not in json.dumps(txns) and "isFlaggedFraud" not in json.dumps(txns)
    # 3. the values live only in `labels`, linked by txn_id
    rows = harness.conn.execute(
        "SELECT column_name, value, COUNT(*) FROM labels WHERE dataset_id = ? GROUP BY 1, 2 ORDER BY 1, 2",
        (ds["dataset_id"],),
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("isFlaggedFraud", "0", 10_127),
        ("isFraud", "0", 10_057),
        ("isFraud", "1", 68),
    ]
    # 4. the canonical model itself refuses a label field
    with pytest.raises(ValueError):
        Transaction(**{**txns[0], "extra": {}}, isFraud=1)


def test_scores_would_be_identical_without_label_columns(paysim, tmp_path):
    """Canonical rows are the same whether or not the file has label columns (P1 A2, now in the backend)."""
    import csv
    import io

    from tests.backend.conftest import make_ingest

    _, _, _, txns = paysim
    with PAYSIM.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    keep = [i for i, h in enumerate(rows[0]) if h not in ("isFraud", "isFlaggedFraud")]
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows([[r[i] for i in keep] for r in rows])
    stripped = make_ingest(tmp_path)
    ds = stripped.upload(buf.getvalue().encode("utf-8"), "paysim_no_labels.csv")
    stripped.confirm(ds)
    drop = ("txn_id", "dataset_id")  # IDs differ because the evidence bytes differ
    clean = [{k: v for k, v in t.items() if k not in drop} for t in stripped.transactions(ds["dataset_id"])]
    assert clean == [{k: v for k, v in t.items() if k not in drop} for t in txns]
    stripped.conn.close()


@pytest.mark.parametrize("header", ["is_fraud", "IS-FRAUD", "Is Fraud", "label", "Target", "ground_truth"])
def test_label_name_variants_are_detected(ingest, header):
    ds = ingest.upload(f"Date,Vendor,Amount,Dr/Cr,{header}\n2024-01-01,A,5,Dr,1\n".encode(), "l.csv")
    assert ds["mapping_suggestion"]["label_columns"] == [header]
    ingest.confirm(ds)
    (t,) = ingest.transactions(ds["dataset_id"])
    assert header not in t["extra"] and "1" not in t["extra"].values()


def test_user_declared_label_column_is_also_separated(ingest):
    ds = ingest.upload(FIXTURES / "ledger_normal.csv")
    mapping = ds["mapping_suggestion"]["proposed_mapping"]
    mapping = {**mapping, "label_columns": [*mapping["label_columns"], "Cost Centre"]}
    ingest.confirm(ds, mapping)
    assert all("Cost Centre" not in t["extra"] for t in ingest.transactions(ds["dataset_id"]))
    stored = ingest.conn.execute("SELECT DISTINCT column_name FROM labels").fetchall()
    assert {r[0] for r in stored} == {"isFraud", "Cost Centre"}


# The label columns the P1 browser prototype ignored (deleted in P9: labels never reach the browser).
P1_FRONTEND_LABELS = {
    "isfraud",
    "fraud",
    "isflaggedfraud",
    "flaggedfraud",
    "flagged",
    "label",
    "target",
    "groundtruth",
}


def test_backend_label_list_covers_the_p1_frontend_list():
    shared = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "shared.js").read_text(
        encoding="utf-8"
    )
    assert "labelColumns" not in shared  # the browser no longer handles label columns at all
    backend = {normalize_header(n) for n in load_config().settings.mapping.label_columns}
    assert backend >= P1_FRONTEND_LABELS
