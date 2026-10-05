"""P5: deterministic generation, files and schemas, synthetic markers, identifiers, CLI, offline."""

import csv
import io
import json
import socket
from pathlib import Path

import pytest

from ledgerspy.cli import main
from ledgerspy.config import load_config
from ledgerspy.synth.generator import generate_to, params_from
from ledgerspy.synth.output import BANK_COLUMNS, LEDGER_COLUMNS, GeneratorError

REFERENCE = Path(__file__).resolve().parents[2] / "data" / "synthetic" / "reference"
CFG = load_config().settings.synth
INPUTS = [
    "SYNTHETIC_bank_statement.csv",
    "SYNTHETIC_cashflow_monthly.csv",
    "SYNTHETIC_customer_master.csv",
    "SYNTHETIC_employee_master.csv",
    "SYNTHETIC_group_entities.csv",
    "SYNTHETIC_holiday_calendar.csv",
    "SYNTHETIC_ledger.csv",
    "SYNTHETIC_relationships.csv",
    "SYNTHETIC_vendor_master.csv",
]
GROUND_TRUTH = ["ground_truth/SYNTHETIC_ground_truth.csv", "ground_truth/SYNTHETIC_ground_truth.json"]


def read(name: str, root: Path = REFERENCE) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO((root / name).read_text(encoding="utf-8"))))


def all_files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def fresh(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("synth42")
    generate_to(params_from(CFG), out)
    return out


# ---------- determinism ----------


def test_same_seed_is_byte_identical(fresh, tmp_path):
    again = tmp_path / "again"
    manifest = generate_to(params_from(CFG), again)
    assert all_files(again) == all_files(fresh)
    assert (
        manifest["dataset_sha256"]
        == json.loads((fresh / "SYNTHETIC_manifest.json").read_text())["dataset_sha256"]
    )


def test_committed_reference_equals_a_fresh_generation(fresh):
    """data/synthetic/reference is exactly what `ledgerspy synth --out …` produces with the default config."""
    assert all_files(REFERENCE) == all_files(fresh)


def test_different_seed_gives_a_different_dataset(fresh, tmp_path):
    other = generate_to(params_from(CFG, seed=43), tmp_path / "s43")
    assert (
        other["dataset_sha256"]
        != json.loads((fresh / "SYNTHETIC_manifest.json").read_text())["dataset_sha256"]
    )
    assert (tmp_path / "s43" / "SYNTHETIC_ledger.csv").read_bytes() != (
        fresh / "SYNTHETIC_ledger.csv"
    ).read_bytes()
    gt = json.loads(
        (tmp_path / "s43" / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8")
    )
    assert gt["scenario_count"] == 63  # same scenario catalogue, different data


@pytest.mark.parametrize(("years", "scale"), [(2, 1.0), (5, 0.6), (3, 2.0)])
def test_parameters_change_volume_but_keep_every_scenario(tmp_path, years, scale):
    m = generate_to(params_from(CFG, years=years, scale=scale), tmp_path / "out")
    gt = json.loads(
        (tmp_path / "out" / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8")
    )
    assert gt["scenario_count"] == 63
    assert m["files"]["SYNTHETIC_cashflow_monthly.csv"]["rows"] == 2 * 12 * years
    assert m["parameters"]["end_date"] == f"{2023 + years}-03-31"  # April–March financial years


def test_scale_grows_the_normal_population(tmp_path):
    small = generate_to(params_from(CFG, scale=1.0), tmp_path / "a")["files"]["SYNTHETIC_ledger.csv"]["rows"]
    large = generate_to(params_from(CFG, scale=2.0), tmp_path / "b")["files"]["SYNTHETIC_ledger.csv"]["rows"]
    assert large > 1.5 * small


def test_invalid_parameters_are_refused():
    for kw in ({"years": 1}, {"years": 6}, {"scale": 0}):
        with pytest.raises(ValueError):
            params_from(CFG, **kw)


# ---------- files, schemas, markers ----------


def test_expected_files_exist_and_ground_truth_is_separate():
    files = all_files(REFERENCE)
    assert sorted(files) == sorted([*INPUTS, *GROUND_TRUTH, "SYNTHETIC_manifest.json"])
    manifest = json.loads(files["SYNTHETIC_manifest.json"])
    assert manifest["synthetic"] is True and "SYNTHETIC" in manifest["notice"]
    assert manifest["inputs"] == INPUTS and manifest["ground_truth"] == GROUND_TRUTH
    for name, info in manifest["files"].items():
        assert info["bytes"] == len(files[name])


def test_schemas():
    assert list(read("SYNTHETIC_ledger.csv")[0]) == LEDGER_COLUMNS
    assert list(read("SYNTHETIC_bank_statement.csv")[0]) == BANK_COLUMNS
    assert list(read("SYNTHETIC_vendor_master.csv")[0])[:5] == [
        "vendor_id",
        "vendor_name",
        "category",
        "address",
        "city",
    ]
    assert "approval_limit" in read("SYNTHETIC_employee_master.csv")[0]
    assert list(read("SYNTHETIC_cashflow_monthly.csv")[0]) == [
        "month",
        "company_code",
        "opening_balance",
        "inflows",
        "outflows",
        "closing_balance",
        "transactions",
        "is_synthetic",
    ]


@pytest.mark.parametrize("name", INPUTS)
def test_every_input_row_is_marked_synthetic(name):
    assert name.startswith("SYNTHETIC_")
    rows = read(name)
    assert rows and all(r["is_synthetic"] == "true" for r in rows)


def test_identifiers_are_unique():
    for name, key in (
        ("SYNTHETIC_vendor_master.csv", "vendor_id"),
        ("SYNTHETIC_employee_master.csv", "employee_id"),
        ("SYNTHETIC_customer_master.csv", "customer_id"),
        ("SYNTHETIC_ledger.csv", "Txn ID"),
        ("SYNTHETIC_bank_statement.csv", "Txn ID"),
        ("SYNTHETIC_bank_statement.csv", "Ref No"),
        ("SYNTHETIC_relationships.csv", "relationship_id"),
    ):
        values = [r[key] for r in read(name)]
        assert len(values) == len(set(values)), (name, key)


def test_invoice_references_are_unique_except_the_documented_cases():
    gt = json.loads((REFERENCE / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8"))
    allowed = {x["txn_id"] for x in gt["expected_p4_row_issues"]["ledger"].get("duplicate_source_id", [])}
    seen, repeats = {}, set()
    for r in read("SYNTHETIC_ledger.csv"):
        if r["Invoice No"] in seen:
            repeats.add(r["Txn ID"])
        seen.setdefault(r["Invoice No"], r["Txn ID"])
    assert (
        repeats == allowed and len(allowed) == 4
    )  # double posting, re-paid invoice, 2 legitimate installments


def test_fictional_identities_cannot_be_real():
    for v in read("SYNTHETIC_vendor_master.csv"):
        assert v["bank_account"].startswith("SYN") and v["bank_code"].startswith("SYNB0")
        assert v["tax_id"] == "" or v["tax_id"].startswith("SYN-TX-")
        assert v["phone"] == "" or v["phone"].startswith("+91-0")  # never an allocated Indian number
        assert v["email"] == "" or v["email"].endswith(".example")
    for e in read("SYNTHETIC_employee_master.csv"):
        assert (
            e["bank_account"].startswith("SYN")
            and e["phone"].startswith("+91-0")
            and e["email"].endswith(".example")
        )


# ---------- CLI ----------


def test_cli_generates_and_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "cli"
    assert main(["synth", "--out", str(out), "--seed", "7", "--years", "2", "--scale", "0.8"]) == 0
    text = capsys.readouterr().out
    assert "SYNTHETIC dataset written" in text and "dataset sha256" in text
    assert main(["synth", "--out", str(out), "--seed", "7", "--years", "2", "--scale", "0.8"]) == 1
    assert "use --force" in capsys.readouterr().err
    assert main(["synth", "--out", str(out), "--seed", "7", "--years", "2", "--scale", "0.8", "--force"]) == 0
    with pytest.raises(GeneratorError):
        generate_to(params_from(CFG), out)


def test_generation_is_offline(tmp_path, monkeypatch):
    calls = []

    def refuse(*a, **k):
        calls.append(a)
        raise AssertionError("network access")

    for target, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "getaddrinfo"),
        (socket, "create_connection"),
    ):
        monkeypatch.setattr(target, name, refuse)
    generate_to(params_from(CFG, years=2), tmp_path / "offline")
    assert calls == []
