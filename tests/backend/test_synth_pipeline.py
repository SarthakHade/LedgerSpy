"""P5 → P3 → P4: the synthetic company goes through the real ingestion and integrity pipeline."""

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from ledgerspy.cases import evidence, store
from ledgerspy.config.schema import normalize_header
from ledgerspy.ingest.errors import IngestError
from ledgerspy.ingest.mapping import profile_table
from ledgerspy.ingest.readers import detect_format, iter_records, open_csv
from ledgerspy.quality.engine import run_integrity
from tests.backend.conftest import make_ingest
from tests.backend.test_synth_generation import INPUTS, REFERENCE

GT = json.loads((REFERENCE / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8"))
TRANSACTION_FILES = ("SYNTHETIC_ledger.csv", "SYNTHETIC_bank_statement.csv")
REFERENCE_FILES = [f for f in INPUTS if f not in TRANSACTION_FILES]


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    """Import the synthetic inputs WITHOUT the ground-truth folder present (detectors never see it)."""
    inputs = tmp_path_factory.mktemp("inputs")
    for name in INPUTS:
        shutil.copy(REFERENCE / name, inputs / name)
    assert not (inputs / "ground_truth").exists()
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    h.case_id = store.create_case(
        h.conn,
        name="Larkspur Vale group (SYNTHETIC)",
        actor="tester",
        period_start=date(2023, 4, 1),
        period_end=date(2026, 3, 31),
    )["case_id"]
    out = {}
    for name in TRANSACTION_FILES:
        ds = h.upload(inputs / name)
        result = h.confirm(ds)  # the proposal is confirmed exactly as offered
        report = run_integrity(h.conn, h.cfg, ds["dataset_id"], "tester")
        out[name] = (ds, result, report)
    yield h, out
    h.conn.close()


def _issues(h, report) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for check, txn in h.conn.execute(
        "SELECT check_code, txn_id FROM integrity_issues WHERE run_id = ? AND txn_id IS NOT NULL",
        (report["run_id"],),
    ):
        found.setdefault(check, set()).add(txn)
    return found


# ---------- P3 ----------


@pytest.mark.parametrize("name", TRANSACTION_FILES)
def test_p3_imports_with_an_unambiguous_proposal(pipeline, name):
    _, out = pipeline
    ds, result, _ = out[name]
    assert ds["mapping_suggestion"]["requires_confirmation"] == []
    rows = json.loads((REFERENCE / "SYNTHETIC_manifest.json").read_text())["files"][name]["rows"]
    assert ds["row_count"] == result["rows"] == rows
    assert result["id_source"] == "source_column"  # the generator's Txn IDs become the canonical IDs
    assert result["issues"]["total"] == 0  # every cell converts
    assert result["labels"]["columns"] == []


def test_ground_truth_ids_resolve_to_canonical_transactions(pipeline):
    h, out = pipeline
    ds = out["SYNTHETIC_ledger.csv"][0]
    canonical = {
        r[0]
        for r in h.conn.execute("SELECT txn_id FROM transactions WHERE dataset_id = ?", (ds["dataset_id"],))
    }
    referenced = {t for s in GT["scenarios"] for t in s["affected"]["ledger_txn_ids"]}
    assert referenced and referenced <= canonical
    bank = out["SYNTHETIC_bank_statement.csv"][0]
    bank_ids = {
        r[0]
        for r in h.conn.execute("SELECT txn_id FROM transactions WHERE dataset_id = ?", (bank["dataset_id"],))
    }
    assert {t for s in GT["scenarios"] for t in s["affected"]["bank_txn_ids"]} <= bank_ids


def test_no_ground_truth_in_canonical_transactions(pipeline):
    h, _ = pipeline
    columns = {r[1] for r in h.conn.execute("PRAGMA table_info(transactions)")}
    extra_keys = set()
    for (extra,) in h.conn.execute("SELECT extra FROM transactions"):
        extra_keys |= set(json.loads(extra))
    banned = {"scenarioid", "scenariotype", "iscontrol", "fraud", "isfraud", "label", "target", "groundtruth"}
    assert not {normalize_header(c) for c in columns | extra_keys} & banned
    text = " ".join(str(v) for row in h.conn.execute("SELECT * FROM transactions") for v in row)
    for s in GT["scenarios"]:
        assert s["scenario_id"] not in text and s["scenario_type"] not in text


# ---------- P4 ----------


def test_p4_ledger_issues_are_exactly_the_planned_ones(pipeline):
    h, out = pipeline
    _, _, report = out["SYNTHETIC_ledger.csv"]
    expected = {c: {x["txn_id"] for x in v} for c, v in GT["expected_p4_row_issues"]["ledger"].items()}
    assert _issues(h, report) == expected
    assert {c: len(v) for c, v in expected.items()} == {
        "duplicate_source_id": 4,
        "exact_duplicate": 1,
        "malformed_identifier": 1,
        "missing_optional": 1,
        "near_duplicate": 2,
        "needs_normalization": 3,
    }
    # each expected issue is explained by a scenario or a normal control
    for items in GT["expected_p4_row_issues"]["ledger"].values():
        assert {x["scenario_id"] for x in items} <= {
            "S-C-01",
            "S-C-02",
            "S-D-01",
            "S-U-05",
            "S-U-13",
            "S-V-01",
        }


def test_p4_ledger_dataset_level_results(pipeline):
    _, out = pipeline
    _, _, r = out["SYNTHETIC_ledger.csv"]
    assert r["unmapped_columns"] == [
        "Company Code",
        "GL Account Name",
        "Counterparty ID",
        "Counterparty Type",
        "Payment Mode",
        "Approved By",
        "Posted By",
        "is_synthetic",
    ]
    assert r["summary"]["issues_by_check"]["field_coverage"] == 0  # every expected ledger field is present
    assert set(r["summary"]["checks_not_available"]) == {"balance_continuity"}  # a GL has no running balance
    assert r["readiness"]["status"] == "ready_with_warnings" and 99.0 < r["readiness"]["score"] < 100.0
    assert r["readiness"]["blocking_issues"] == 0


def test_p4_bank_statement_is_clean_and_continuous(pipeline):
    h, out = pipeline
    _, _, r = out["SYNTHETIC_bank_statement.csv"]
    assert _issues(h, r) == {}
    bc = next(c for c in r["checks"] if c["check"] == "balance_continuity")
    assert (
        bc["status"] == "ok" and "per account" in bc["basis"] and "credit raises the balance" in bc["basis"]
    )
    assert r["readiness"]["score"] == 100.0 and r["readiness"]["status"] == "ready"
    assert r["unmapped_columns"] == ["Value Dt", "Balance", "is_synthetic"]
    missing = {f for f, d in r["field_coverage"].items() if d["status"] == "not_in_source"}
    assert missing == {"document_date", "payee_raw", "payer_raw", "txn_type"}


# ---------- reference files through P3's safe reading layer ----------


@pytest.mark.parametrize("name", REFERENCE_FILES)
def test_reference_files_pass_p3_reader_profiler_and_evidence(name, tmp_path):
    h = make_ingest(tmp_path)
    path = REFERENCE / name
    assert detect_format(path, name) == "csv"
    src = open_csv(path, h.cfg.settings.ingest)
    assert src.details["encoding"] == "utf-8" and src.details["delimiter"] == ","
    profile = profile_table(src, h.cfg.settings)
    assert profile.rows == sum(1 for _ in iter_records(src, h.cfg.settings.ingest)) > 0
    assert profile.blank_records == 0
    staged = evidence.stage_file(path, h.workspace, h.cfg.settings.ingest.max_file_bytes)
    from ledgerspy.cases.db import transaction

    with transaction(h.conn):
        stored, new = evidence.store(h.conn, staged, ".csv", h.workspace, "tester")
    assert new and stored.startswith(staged.sha256)
    assert evidence.verify_all(h.conn, h.workspace) == []
    h.conn.close()


def test_reference_files_are_not_transaction_datasets_yet(ingest):
    """The upload API accepts transaction files only (kind=ledger); master/cash-flow ingestion is P12/P18."""
    with pytest.raises(IngestError) as e:
        from ledgerspy.ingest import service

        staged = evidence.stage_file(REFERENCE / "SYNTHETIC_vendor_master.csv", ingest.workspace, 10**8)
        service.ingest_upload(
            ingest.conn,
            ingest.cfg,
            ingest.workspace,
            ingest.case_id,
            staged,
            "SYNTHETIC_vendor_master.csv",
            "vendor_master",
            "tester",
        )
    assert e.value.code == "kind_not_supported"


def test_reference_dataset_is_small_enough_to_commit():
    total = sum(p.stat().st_size for p in Path(REFERENCE).rglob("*") if p.is_file())
    assert total < 4 * 1024 * 1024
