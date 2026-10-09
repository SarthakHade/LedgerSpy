"""P14 on real imports: the P5 ledger + bank statement (+ masters), PaySim, a large generated reconciliation,
determinism and persistence, P9 isolation, and the API.

The P5 ground truth and the generator's ledger → bank mapping are read only to SCORE reconciliation results,
never as input.
"""

import secrets
import time
from collections import Counter
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases import store
from ledgerspy.cases.db import db_path_for
from ledgerspy.config import load_config
from ledgerspy.reconciliation import duplicates, engine, service
from ledgerspy.reconciliation.records import (
    NameIndex,
    Scope,
    account_key,
    alnum,
    bank_rows,
    fallback_entity,
    ledger_rows,
)
from ledgerspy.resolution import masters, validation
from ledgerspy.risk import service as risk_service
from ledgerspy.synth.generator import generate, params_from
from tests.backend.conftest import BASE_URL, PORT, make_ingest
from tests.backend.test_features_datasets import PAYSIM, REFERENCE, case_overrides
from tests.backend.test_forensics_datasets import _table_digest, imported
from tests.backend.test_graph_datasets import SC

EXPECTED = {
    "exact_match": 7104,
    "probable_match": 144,
    "grouped_match": 2,
    "partial_match": 8,
    "ambiguous": 3,
    "duplicate_candidate": 0,
    "unmatched_ledger": 3,
    "unmatched_bank": 3,
    "insufficient_data": 0,
    "not_applicable": 0,
}
SCENARIO_STATUS = {  # P5 bank-mismatch scenarios → the status each affected row must get
    "S-S-01": ("unmatched_ledger", None),
    "S-S-02": (None, "unmatched_bank"),
    "S-S-03": ("partial_match", "partial_match"),  # cleared 8–11 working days late: date discrepancy
    "S-S-04": ("grouped_match", "grouped_match"),
    "S-S-05": ("grouped_match", "grouped_match"),
    "S-S-06": ("partial_match", "partial_match"),  # bank fee: amount discrepancy
    "S-S-07": ("partial_match", "partial_match"),  # payee mismatch: counterparty discrepancy
}


def bank_import(h, path):
    ds = h.upload(path, kind="bank_statement")
    h.confirm(ds)
    from ledgerspy.quality.engine import run_integrity

    run_integrity(h.conn, h.cfg, ds["dataset_id"], "t")
    return ds["dataset_id"]


def request_from_setup(su, led, bank):
    return service.RunRequest(
        ledger_dataset_id=led,
        bank_dataset_id=bank,
        scopes=[
            service.ScopeIn(
                account_key=p["account_key"], ledger_column=p["ledger_column"], ledger_value=p["ledger_value"]
            )
            for p in su["proposals"]["scopes"]
        ],
        ledger_debit_means=su["proposals"]["ledger_debit_means"]["value"],
    )


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    pay = imported(h, PAYSIM)
    pay_bank = bank_import(h, REFERENCE / "SYNTHETIC_bank_statement.csv")  # same case as PaySim
    h.case_id = store.create_case(
        h.conn, name="syn", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    led = imported(h, REFERENCE / "SYNTHETIC_ledger.csv")
    bank = bank_import(h, REFERENCE / "SYNTHETIC_bank_statement.csv")
    for etype, (fname, mapping) in validation.P5_FILES.items():
        masters.import_master(
            h.conn, h.cfg, h.workspace, h.case_id, REFERENCE / fname, etype, "t", mapping=mapping
        )
    cfg = load_config(
        overrides={"workspace": {"path": str(h.workspace)}, "server": {"port": PORT}, **case_overrides()}
    )
    risk_service.clear_cache()
    p9 = risk_service.run_risk(h.conn, cfg, led).result_sha256
    digest = _table_digest(h.conn)
    service.clear_cache()
    su = service.setup(h.conn, cfg, led, bank)
    req = request_from_setup(su, led, bank)
    t = time.perf_counter()
    run = service.start(h.conn, cfg, req, "t")
    seconds = time.perf_counter() - t
    yield {
        "h": h,
        "cfg": cfg,
        "led": led,
        "bank": bank,
        "pay": pay,
        "pay_bank": pay_bank,
        "su": su,
        "req": req,
        "run": run,
        "p9": p9,
        "digest": digest,
        "seconds": seconds,
    }
    h.conn.close()


def _status(run):
    out = {}
    for r in run.results:
        for t in r["ledger_txn_ids"]:
            out[("l", t)] = r["status"]
        for t in r["bank_txn_ids"]:
            out[("b", t)] = r["status"]
    return out


# ------------------------------------------------------------------------------------------ P5


def test_setup_proposes_evidence_backed_scopes_and_semantics(env):
    su = env["su"]
    assert [(p["ledger_column"], p["ledger_value"]) for p in su["proposals"]["scopes"]] == [
        ("Company Code", "LVE"),
        ("Company Code", "LVC"),
    ]
    assert all("group-company master" in p["basis"] for p in su["proposals"]["scopes"])
    assert su["proposals"]["ledger_debit_means"]["value"] == "outflow"
    assert {i["item"] for i in su["requires_confirmation"]} == {"scopes", "ledger_debit_means"}
    assert all("…" in a["account"] and len(a["account"]) == 5 for a in su["bank"]["accounts"])  # masked


def test_p5_exact_classification_counts(env):
    s = env["run"].summary
    assert s["results_by_status"] == EXPECTED
    assert s["ledger_rows"] == 7271 and s["bank_rows"] == 7267
    assert (
        s["rows_by_status"]["ledger"]["grouped_match"] == 6
        and s["rows_by_status"]["bank"]["grouped_match"] == 2
    )
    assert env["run"].truncation["limits_reached"] is False


def test_every_p5_bank_mismatch_scenario_is_classified(env):
    st = _status(env["run"])
    for sid, (ls, bs) in SCENARIO_STATUS.items():
        a = SC[sid]["affected"]
        if ls:
            assert [st[("l", t)] for t in a["ledger_txn_ids"]] == [ls] * len(a["ledger_txn_ids"]), sid
        if bs:
            assert [st[("b", t)] for t in a["bank_txn_ids"]] == [bs] * len(a["bank_txn_ids"]), sid
    by = {r["ledger_txn_ids"][0]: r for r in env["run"].results if r["status"] == "partial_match"}
    assert (
        sorted(by[t]["discrepancies"][0] for t in SC["S-S-06"]["affected"]["ledger_txn_ids"])
        == ["amount"] * 3
    )
    assert {by[t]["amount_difference_minor"] for t in SC["S-S-06"]["affected"]["ledger_txn_ids"]} == {
        590_00,
        -236_00,
    }
    assert by[SC["S-S-07"]["affected"]["ledger_txn_ids"][0]]["discrepancies"] == ["counterparty"]


def test_matches_agree_with_the_generators_ledger_to_bank_mapping(env):
    """Evaluation only: every exact / probable / grouped / partial pairing is the generator's true pairing."""
    g, _ = generate(params_from(env["cfg"].settings.synth))
    lid = {r.key: r.txn_id for r in g.rows}
    bid = {b.key: b.txn_id for b in g.bank_rows}
    truth = {lid[k]: {bid[x] for x in v} for k, v in g.special["bank_of"].items()}
    right = wrong = 0
    for r in env["run"].results:
        if r["status"] in ("exact_match", "probable_match", "grouped_match", "partial_match"):
            for t in r["ledger_txn_ids"]:
                if truth.get(t) and truth[t] <= set(r["bank_txn_ids"]):
                    right += 1
                else:
                    wrong += 1
    assert wrong == 0 and right == 7262
    for r in env["run"].results:  # ambiguous groups contain their true partners: nothing was mis-assigned
        if r["status"] == "ambiguous":
            assert all(truth[t] <= set(r["bank_txn_ids"]) for t in r["ledger_txn_ids"])


def test_row_and_summary_totals_agree_per_currency(env):
    run = env["run"]
    s = run.summary
    seen = Counter(
        (side, t) for r in run.results for side in ("ledger", "bank") for t in r[side + "_txn_ids"]
    )
    assert len(seen) == 7271 + 7267 and set(seen.values()) == {1}
    for side in ("ledger", "bank"):
        total = Counter()
        for r in run.results:
            for row in r[side + "_rows"]:
                total[(row["currency"], r["status"])] += row["amount_minor"]
        flat = {(c, st): v for c, by in s["amounts_by_status_minor"][side].items() for st, v in by.items()}
        assert dict(total) == flat
        matched = sum(v for (c, st), v in flat.items() if st in engine.MATCHED)
        assert s["matched_amount_minor"][side] == {"INR": matched}
    assert (
        s["matched_amount_minor"]["ledger"] == s["matched_amount_minor"]["bank"]
    )  # exact / grouped are equal


def test_duplicates_balances_and_findings(env):
    run = env["run"]
    assert run.summary["duplicates"]["by_type"] == {
        "bank_near": 3,
        "ledger_near": 2,
        "ledger_same_reference": 3,
    }
    assert all(g["confirmed"] is False and g["status"] == "candidate" for g in run.duplicates)
    b = run.balance
    assert b["status"] == "consistent" and {a["status"] for a in b["accounts"]} == {"consistent"}
    lve = next(a for a in b["accounts"] if a["account"] == "…6964")
    assert lve["opening_balance_minor"] == 60_00_000_00 and lve["opening_plus_movement_equals_closing"]
    rules = Counter(s["rule_id"] for s in run.findings)
    assert rules["recon_unmatched_ledger"] == 3 and rules["recon_unmatched_bank"] == 3
    assert rules["recon_amount_mismatch"] == 3 and rules["recon_date_discrepancy"] == 4
    assert rules["recon_counterparty_mismatch"] == 1 and "recon_balance_discontinuity" not in rules
    assert all(s["fused_in_p9"] is False and s["weight"] is None for s in run.findings)


def test_determinism_persistence_isolation_and_no_mutation(env):
    h, cfg, run = env["h"], env["cfg"], env["run"]
    again = service.start(h.conn, cfg, env["req"], "t")
    assert again.run_id == run.run_id and again.result_sha256 == run.result_sha256
    n = h.conn.execute("SELECT COUNT(*) FROM reconciliation_runs WHERE run_id = ?", (run.run_id,)).fetchone()[
        0
    ]
    assert n == 1  # recorded once
    service.clear_cache()
    rebuilt = service.get_run(h.conn, cfg, run.run_id)  # recomputed from the register and verified
    assert rebuilt.result_sha256 == run.result_sha256 and rebuilt.results == run.results
    audit = h.conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'reconciliation.run' AND object = ?",
        (f"reconciliation:{run.run_id}",),
    ).fetchone()[0]
    assert audit == 1
    assert _table_digest(h.conn) == env["digest"]  # canonical rows, labels and overlays untouched
    risk_service.clear_cache()
    assert risk_service.run_risk(h.conn, cfg, env["led"]).result_sha256 == env["p9"]  # P9 unchanged


def test_scopes_kinds_and_semantics_are_validated(env):
    from ledgerspy.errors import LedgerSpyError

    h, cfg = env["h"], env["cfg"]
    with pytest.raises(LedgerSpyError) as e:
        service.start(
            h.conn,
            cfg,
            service.RunRequest(
                ledger_dataset_id=env["led"], bank_dataset_id=env["bank"], ledger_debit_means="outflow"
            ),
            "t",
        )
    assert e.value.code == "scopes_required"  # two accounts, no scope confirmed
    with pytest.raises(LedgerSpyError) as e:
        service.start(h.conn, cfg, env["req"].model_copy(update={"ledger_debit_means": None}), "t")
    assert e.value.code == "ledger_semantics_required"
    with pytest.raises(LedgerSpyError) as e:
        service.setup(h.conn, cfg, env["bank"], env["led"])
    assert e.value.code == "wrong_dataset_kind"
    with pytest.raises(LedgerSpyError) as e:
        service.setup(h.conn, cfg, env["led"], env["pay_bank"])
    assert e.value.code == "different_cases"
    bad = env["req"].model_copy(update={"scopes": [service.ScopeIn(account_key="A-000000000000")]})
    with pytest.raises(LedgerSpyError) as e:
        service.start(h.conn, cfg, bad, "t")
    assert e.value.code == "invalid_scopes"


def test_one_account_scope_reconciles_only_that_company(env):
    h, cfg = env["h"], env["cfg"]
    lve = next(p for p in env["su"]["proposals"]["scopes"] if p["ledger_value"] == "LVE")
    req = env["req"].model_copy(
        update={
            "scopes": [
                service.ScopeIn(**{k: lve[k] for k in ("account_key", "ledger_column", "ledger_value")})
            ]
        }
    )
    run = service.start(h.conn, cfg, req, "t")
    rows = run.summary["rows_by_status"]
    assert rows["ledger"]["not_applicable"] == 6486 and rows["bank"]["not_applicable"] == 6483
    assert (
        run.summary["matched_rows"]["ledger"]
        + sum(rows["ledger"][s] for s in ("partial_match", "ambiguous", "unmatched_ledger"))
        == 785
    )


def test_paysim_has_no_currency_and_is_never_reconciled(env):
    """PaySim (no currency, no statement of its own) against a statement of its case: insufficient data."""
    h, cfg = env["h"], env["cfg"]
    su = service.setup(h.conn, cfg, env["pay"], env["pay_bank"])
    assert su["proposals"]["scopes"] == [] and su["missing_currency_rows"]["ledger"] == su["ledger"]["rows"]
    acc = su["bank"]["accounts"][0]["account_key"]
    run = service.start(
        h.conn,
        cfg,
        service.RunRequest(
            ledger_dataset_id=env["pay"],
            bank_dataset_id=env["pay_bank"],
            scopes=[service.ScopeIn(account_key=acc, ledger_column="payer_raw", ledger_value="C1231006815")],
        ),
        "t",
    )
    s = run.summary
    assert sum(s["results_by_status"][k] for k in engine.MATCHED) == 0 and s["partial_match"] == 0
    ins = [r for r in run.results if r["status"] == "insufficient_data" and r["ledger_txn_ids"]]
    assert ins and all("missing_currency" in r["reasons"] for r in ins)


# ------------------------------------------------------------------------------------------ scale


def test_large_generated_reconciliation_is_fast_and_exact():
    """39,500 ledger rows: 1:1 with 0–2 day lags, 1,000 grouped settlements of 3, 500 fees, 1,000 missing
    rows, 500 bank-only lines; expected counts are known by construction."""
    rc = load_config().settings.reconciliation
    d0 = date(2024, 1, 1)
    lv, bv = [], []
    vendors = [
        f"Vendor {chr(65 + i % 26)}{chr(65 + i // 26 % 26)}{i:04d} Industries Pvt Ltd" for i in range(800)
    ]

    def led(i, d, amt, v):
        lv.append(
            {
                "txn_id": f"L{i:06d}",
                "source_row": len(lv) + 1,
                "posted_at": (d0 + timedelta(d)).isoformat(),
                "amount_minor": amt,
                "currency": "INR",
                "direction": "debit",
                "payer_raw": "Us Ltd",
                "payee_raw": v,
                "reference": None,
                "description": None,
                "extra": {},
                "normalized_fields": [],
            }
        )

    def bk(i, d, amt, v):
        bv.append(
            {
                "txn_id": f"B{i:06d}",
                "source_row": len(bv) + 1,
                "posted_at": (d0 + timedelta(d)).isoformat(),
                "amount_minor": amt,
                "currency": "INR",
                "direction": "out",
                "account": "ACC1",
                "reference": f"UTR{i:09d}",
                "description": f"NEFT DR/UTR{i:09d}/{v.upper()}",
                "extra": {},
                "normalized_fields": [],
            }
        )

    i = 0
    for k in range(36_500):
        d, v, a = 10 + k % 700, vendors[k % 800], 1_000_00 + k * 37
        led(i, d, a, v)
        if k < 35_000:
            bk(i, d + k % 3, a, v)
        elif k < 35_500:
            bk(i, d + 1, a + 50_00, v)  # fee: partial (amount)
        # else: 1,000 ledger-only rows → 500 here
        i += 1
    for g in range(1_000):  # grouped: three rows of one vendor settled together
        v, d = vendors[g % 800], 20 + g % 650
        amts = [2_000_00 + g * 3 + j for j in range(3)]
        for j, a in enumerate(amts):
            led(i, d + j, a + 7_000_000, v)
            i += 1
        bk(i, d + 4, sum(amts) + 21_000_000, v)
        i += 1
    for j in range(500):
        bk(i, 30 + j, 99_00 + j, "Unknown Payee")
        i += 1
    ledger_ids = {r["txn_id"] for r in lv}
    assert len(ledger_ids) == len(lv) == 39_500
    scopes = [Scope("SC-1", account_key(alnum("ACC1")), None, None)]
    t = time.perf_counter()
    names = sorted({(fallback_entity(v), v) for v in vendors})
    idx = NameIndex(names, rc.counterparty.min_name_chars, rc.counterparty.min_prefix_chars)
    L = ledger_rows(lv, scopes, "outflow", fallback_entity)
    B = bank_rows(bv, True, scopes, idx)
    out = engine.reconcile(L, B, rc, duplicates.find(L, B, rc))
    seconds = time.perf_counter() - t
    by = Counter(r["status"] for r in out.results)
    assert by["exact_match"] == 35_000 and by["grouped_match"] == 1_000 and by["partial_match"] == 500
    assert by["unmatched_ledger"] + by["insufficient_data"] == 1_000 and by["unmatched_bank"] == 500
    assert by["ambiguous"] == 0 and not out.truncation
    assert seconds < 120, seconds


# ------------------------------------------------------------------------------------------ API


@pytest.fixture(scope="module")
def api(env):
    token = secrets.token_urlsafe(32)
    with TestClient(create_app(env["cfg"], db_path_for(env["cfg"].settings), token), base_url=BASE_URL) as c:
        yield c, {"Authorization": f"Bearer {token}"}


def test_reconciliation_endpoints_require_the_token(api, env):
    c, _ = api
    rid = env["run"].run_id
    for p in (
        f"/api/reconciliation/setup?ledger_dataset_id={env['led']}&bank_dataset_id={env['bank']}",
        "/api/reconciliation/runs",
        f"/api/reconciliation/runs/{rid}",
        f"/api/reconciliation/runs/{rid}/results",
        f"/api/reconciliation/runs/{rid}/duplicates",
        f"/api/reconciliation/runs/{rid}/balances",
        f"/api/reconciliation/runs/{rid}/findings",
        f"/api/reconciliation/runs/{rid}/results/{env['run'].results[0]['result_id']}",
    ):
        assert c.get(p).status_code == 401, p
        assert c.get(p, headers={"Authorization": "Bearer wrong"}).status_code == 401, p
    assert c.post("/api/reconciliation/runs", json=env["req"].model_dump()).status_code == 401


def test_reconciliation_api_run_pages_filters_and_detail(api, env):
    c, auth = api
    r = c.post("/api/reconciliation/runs", json=env["req"].model_dump(), headers=auth)
    assert r.status_code == 201
    s = r.json()
    assert s["run_id"] == env["run"].run_id and s["summary"]["results_by_status"] == EXPECTED
    assert "not probabilities" in s["note"] and "P14b" in s["formats"]
    base = f"/api/reconciliation/runs/{s['run_id']}"
    p = c.get(base + "/results?status=partial_match&size=5", headers=auth).json()
    assert (
        p["total"] == 8 and len(p["items"]) == 5 and all(x["status"] == "partial_match" for x in p["items"])
    )
    p2 = c.get(base + "/results?status=partial_match&size=5&page=2", headers=auth).json()
    assert len(p2["items"]) == 3 and not {x["result_id"] for x in p["items"]} & {
        x["result_id"] for x in p2["items"]
    }
    lve = next(a for a in s["scopes"] if a["account"] == "…6964")["account_key"]
    acc = c.get(base + f"/results?account={lve}&size=500", headers=auth).json()
    assert acc["total"] and all(x["scope"]["account_key"] == lve for x in acc["items"])
    win = c.get(base + "/results?from=2024-01-01&to=2024-01-31&size=500", headers=auth).json()
    assert win["total"] and all(
        x["first_date"] <= "2024-01-31" and x["last_date"] >= "2024-01-01" for x in win["items"]
    )
    assert c.get(base + "/results?currency=USD", headers=auth).json()["total"] == 0
    grouped = c.get(base + "/results?status=grouped_match", headers=auth).json()["items"][0]
    d = c.get(base + "/results/" + grouped["result_id"], headers=auth).json()
    assert len(d["source_evidence"]) == len(grouped["ledger_txn_ids"]) + 1
    ev = d["source_evidence"][0]
    assert len(ev["evidence_sha256"]) == 64 and ev["source_row"] >= 1 and "p4_normalized_values" in ev
    bank_ev = next(e for e in d["source_evidence"] if e["side"] == "bank")
    assert bank_ev["canonical"]["account"].startswith("…") and set(bank_ev["statement_columns"]) == {
        "Balance"
    }
    assert c.get(base + "/duplicates?side=bank", headers=auth).json()["total"] == 3
    assert c.get(base + "/balances", headers=auth).json()["status"] == "consistent"
    f = c.get(base + "/findings?rule=recon_counterparty_mismatch", headers=auth).json()
    assert f["total"] == 1 and f["items"][0]["severity"] == "high"
    runs = c.get(f"/api/reconciliation/runs?ledger_dataset_id={env['led']}", headers=auth).json()["items"]
    assert s["run_id"] in {x["run_id"] for x in runs}


def test_reconciliation_api_rejects_bad_parameters(api, env):
    c, auth = api
    base = f"/api/reconciliation/runs/{env['run'].run_id}"
    assert c.get(base + "/results?size=501", headers=auth).status_code == 422  # above max_page_size
    assert c.get(base + "/results?status=bogus", headers=auth).status_code == 422
    assert c.get(base + "/results?account=../../etc", headers=auth).status_code == 422
    assert c.get(base + "/results/RM-0000000000000000", headers=auth).status_code == 404
    assert c.get(base + "/results/not-an-id", headers=auth).status_code == 422
    assert c.get("/api/reconciliation/runs/RR-0000000000000000", headers=auth).status_code == 404
    assert c.get("/api/reconciliation/runs/C:/x", headers=auth).status_code in (404, 422)
    assert (
        c.post(
            "/api/reconciliation/runs", json={**env["req"].model_dump(), "path": "/tmp"}, headers=auth
        ).status_code
        == 422
    )  # unknown fields (e.g. a file path) are refused
    r = c.post(
        "/api/reconciliation/runs",
        json={**env["req"].model_dump(), "bank_dataset_id": env["led"]},
        headers=auth,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "wrong_dataset_kind"
