"""P12 on real imports: master import, resolution of the P5 ledger with its masters, P6 features, P9 grouping
before/after, P13-ready identities, PaySim, the API, performance.

The P5 ground truth and the ledger's unmapped `Counterparty ID` column are read only to SCORE results here.
"""

import csv
import secrets
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import date

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases import store
from ledgerspy.cases.db import db_path_for
from ledgerspy.cli import main
from ledgerspy.config import load_config
from ledgerspy.features import service as feature_service
from ledgerspy.features.data import party_key
from ledgerspy.resolution import engine, masters, service, validation
from ledgerspy.resolution.masters import MasterImportError
from ledgerspy.resolution.records import from_master
from ledgerspy.risk import service as risk_service
from tests.backend.conftest import BASE_URL, PORT, make_ingest
from tests.backend.test_features_datasets import PAYSIM, REFERENCE, case_overrides
from tests.backend.test_forensics_datasets import _table_digest, imported


def _master_digest(conn) -> str:
    import hashlib

    h = hashlib.sha256()
    for t in ("master_files", "master_records"):
        for row in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2"):  # noqa: S608 — fixed names
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def _stream_truth(path) -> dict[str, set[str]]:
    truth = defaultdict(set)
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            s = ("out:" + party_key(r["Payee"])) if r["Dr/Cr"] == "Dr" else ("in:" + party_key(r["Payer"]))
            if r["Counterparty ID"]:
                truth[s].add(r["Counterparty ID"])
    return truth


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    inputs = tmp_path_factory.mktemp("inputs")
    shutil.copy(REFERENCE / "SYNTHETIC_ledger.csv", inputs / "SYNTHETIC_ledger.csv")
    info = validation.ledger_with_name_variants(
        REFERENCE / "SYNTHETIC_ledger.csv", inputs / "SYNTHETIC_ledger_variants.csv"
    )
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    pay = imported(h, PAYSIM)
    h.case_id = store.create_case(
        h.conn, name="syn", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    syn = imported(h, inputs / "SYNTHETIC_ledger.csv")
    var = imported(h, inputs / "SYNTHETIC_ledger_variants.csv")
    over = {"workspace": {"path": str(h.workspace)}, "server": {"port": PORT}, **case_overrides()}
    cfg = load_config(overrides=over)
    cfg_name = load_config(overrides={**over, "risk": {"entities": {"grouping": "name"}}})
    risk_service.clear_cache()
    p9_no_masters = risk_service.run_risk(h.conn, cfg, syn)
    digest = _table_digest(h.conn)
    files = {}
    for etype, (fname, mapping) in validation.P5_FILES.items():
        files[etype] = masters.import_master(
            h.conn, h.cfg, h.workspace, h.case_id, REFERENCE / fname, etype, "tester", mapping=mapping
        )
    service.clear_cache()
    yield {
        "h": h,
        "pay": pay,
        "syn": syn,
        "var": var,
        "cfg": cfg,
        "cfg_name": cfg_name,
        "files": files,
        "variant_info": info,
        "variant_truth": _stream_truth(inputs / "SYNTHETIC_ledger_variants.csv"),
        "p9_no_masters": p9_no_masters,
        "digest": digest,
    }
    h.conn.close()


# ------------------------------------------------------------------------------------------ master import


def test_master_import_is_evidence_backed_explicit_and_immutable(env, tmp_path):
    h = env["h"]
    v = env["files"]["vendor"]
    assert v["record_count"] == 99 and v["entity_type"] == "vendor" and v["mapping"]["tax_id"] == "tax_id"
    stored = h.conn.execute(
        "SELECT 1 FROM evidence_files WHERE sha256 = ?", (v["evidence_sha256"],)
    ).fetchone()
    assert stored is not None
    raw = h.conn.execute(
        "SELECT name, phone, tax_id FROM master_records WHERE master_file_id = ? AND source_row = 1",
        (v["master_file_id"],),
    ).fetchone()
    assert tuple(raw) == ("Hessarin Estates Pvt Ltd", "+91-04722-10118", "SYN-TX-8884111R")  # verbatim
    with pytest.raises(sqlite3.DatabaseError):
        h.conn.execute("UPDATE master_records SET name = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        h.conn.execute("DELETE FROM master_files")
    # A mapping is never applied silently; the suggestion is offered, and accepted only explicitly.
    with pytest.raises(MasterImportError) as e:
        masters.import_master(
            h.conn, h.cfg, h.workspace, h.case_id, REFERENCE / "SYNTHETIC_vendor_master.csv", "vendor", "t"
        )
    assert e.value.code == "mapping_required"
    sug = e.value.details["suggestion"]
    assert sug["mapping"]["name"] == "vendor_name" and sug["mapping"]["tax_id"] == "tax_id"
    with pytest.raises(MasterImportError):
        masters.import_master(
            h.conn,
            h.cfg,
            h.workspace,
            h.case_id,
            REFERENCE / "SYNTHETIC_vendor_master.csv",
            "vendor",
            "t",
            mapping={"name": "no_such_column"},
        )
    again = masters.import_master(
        h.conn,
        h.cfg,
        h.workspace,
        h.case_id,
        REFERENCE / "SYNTHETIC_vendor_master.csv",
        "vendor",
        "t",
        mapping=validation.P5_FILES["vendor"][1],
    )
    assert again["already_imported"] and again["master_file_id"] == v["master_file_id"]
    actions = [r[0] for r in h.conn.execute("SELECT action FROM audit_log WHERE action = 'master.imported'")]
    assert len(actions) == 4


# ------------------------------------------------------------------------------------------ resolution


def test_p5_resolution_surfaces_relations_and_links_ledger_names(env):
    rr = service.run(env["h"].conn, env["cfg"], env["syn"])
    res = rr.res
    st = res.stats
    assert st["records_by_source"] == {"ledger": 153, "master": 164}
    assert st["candidate_reduction_ratio"] > 0.9 and st["blocked_merges"] == 0
    # Natural P5 relations: surfaced for review / as links, never merged (scored with the ground truth here).
    names = {r.key: r.raw.get("record_id") for r in res.records.values() if r.source == "master"}
    key = {v: k for k, v in names.items()}
    for a, b in (
        ("V0006", "V0083"),
        ("V0024", "V0084"),
        ("V0089", "V0090"),
        ("V0089", "V0091"),
        ("V0027", "V0039"),
    ):
        assert res.entity_of[key[a]] != res.entity_of[key[b]]
        p = next(p for p in res.pairs.values() if {p["a"], p["b"]} == {key[a], key[b]})
        assert p["status"] == "conflicting_identity", (a, b)
    for v, emp in (("V0077", "E0018"), ("V0078", "E0002"), ("V0082", "E0032")):
        p = next(p for p in res.pairs.values() if {p["a"], p["b"]} == {key[v], key[emp]})
        assert p["status"] == "shared_attribute" and res.entity_of[key[v]] != res.entity_of[key[emp]]
    # Ledger names link to their master record (scored against the ledger's unmapped Counterparty ID).
    truth = _stream_truth(REFERENCE / "SYNTHETIC_ledger.csv")
    wrong = 0
    for s, ids in truth.items():
        members = res.entity(res.entity_of["l:" + s.split(":", 1)[1]])["members"]
        got = {names[m] for m in members if m in names}
        wrong += bool(got and not got & ids)
    assert wrong == 0


def test_resolution_reads_and_writes_nothing_it_should_not(env):
    h = env["h"]
    before = (_table_digest(h.conn), _master_digest(h.conn))
    service.clear_cache()
    service.run(h.conn, env["cfg"], env["syn"])
    assert (_table_digest(h.conn), _master_digest(h.conn)) == before
    assert _table_digest(h.conn) == env["digest"]  # canonical rows untouched by import and resolution


def test_p6_resolution_features_with_master_data(env):
    run = feature_service.build_for_dataset(env["h"].conn, env["cfg"], env["syn"])
    ent = run.entities
    col = {
        n: dict(zip(ent.subjects, ent.columns[n], strict=True))
        for n in (
            "employee_link_indicator",
            "shared_identifier_count",
            "ent_resolution_status",
            "ent_linked_record_count",
        )
    }
    assert ent.status["employee_link_indicator"]["status"] == "computed"
    flagged = {s for s, v in col["employee_link_indicator"].items() if v}
    assert "out:mardale systems llp" in flagged and "out:wyndel advisors private limited" in flagged
    assert col["ent_resolution_status"]["out:ulvanee alloy pvt ltd"] == "conflicting_identity"
    assert col["ent_linked_record_count"]["out:hessarin estates pvt ltd"] == 2  # ledger name + vendor record


# ------------------------------------------------------------------------------------------ P9


def test_p9_transaction_risk_is_unchanged_by_resolution(env):
    h = env["h"]
    risk_service.clear_cache()
    resolved = risk_service.run_risk(h.conn, env["cfg"], env["syn"])
    risk_service.clear_cache()
    by_name = risk_service.run_risk(h.conn, env["cfg_name"], env["syn"])
    before = env["p9_no_masters"]
    for t in before.by_txn:
        assert before.by_txn[t]["score"] == resolved.by_txn[t]["score"] == by_name.by_txn[t]["score"]
    assert resolved.summary["entity_grouping"]["merged_entities"] == 0  # the P5 ledger has no name variants
    assert len(resolved.entities) == len(by_name.entities) == 159
    assert resolved.summary["entity_grouping"]["master_records"] == 164
    statuses = {e["resolution"]["resolution_status"] for e in resolved.entities}
    assert statuses <= {"unique", "resolved", "needs_review", "conflicting_identity"}


def test_p9_resolved_grouping_on_name_variants_reduces_splits_without_false_merges(env):
    h, truth = env["h"], env["variant_truth"]
    risk_service.clear_cache()
    by_name = risk_service.run_risk(h.conn, env["cfg_name"], env["var"])
    risk_service.clear_cache()
    resolved = risk_service.run_risk(h.conn, env["cfg"], env["var"])
    qn = validation.grouping_quality(by_name.entities, truth)
    qr = validation.grouping_quality(resolved.entities, truth)
    assert qn["false_merge_entities"] == qr["false_merge_entities"] == 0
    assert qr["split_counterparties"] < qn["split_counterparties"] and qr["entities"] < qn["entities"]
    typo = {v for v, k in env["variant_info"]["variant_vendors"].items() if k == "typo"}
    assert {s.split(":", 1)[1] for s in qr["split_counterparty_ids"]} == typo  # name-only typos stay apart
    for t in by_name.by_txn:
        assert by_name.by_txn[t]["score"] == resolved.by_txn[t]["score"]
    # The entity formula is unchanged: a merged entity's score is the P9 noisy-OR of its own top transactions.
    ec = env["cfg"].settings.risk.entities
    for e in resolved.entities:
        if len(e["member_streams"]) > 1:
            txns = sorted(
                (r for r in resolved.rows if r["entity_id"] == e["entity_id"] and r["score"]),
                key=lambda r: (-r["score"], r["txn_id"]),
            )[: ec.top_k]
            surv = 1.0
            for j, r in enumerate(txns):
                surv *= 1 - r["score"] / 100 * ec.decay**j
            assert e["score"] == pytest.approx(round(100 * (1 - surv), 2), abs=0.011)
            assert e["resolution"]["merged_streams"] == len(e["member_streams"])


def test_p13_ready_canonical_identities(env):
    h = env["h"]
    service.clear_cache()
    a = service.run(h.conn, env["cfg"], env["syn"])
    service.clear_cache()
    b = service.run(h.conn, env["cfg"], env["syn"])
    assert a.res.sha256 == b.res.sha256 and a.res.entity_of == b.res.entity_of
    risk_service.clear_cache()
    r = risk_service.run_risk(h.conn, env["cfg"], env["syn"])
    ids = {e["canonical_entity_id"] for e in a.res.entities}
    assert all(e["resolution"]["canonical_entity_id"] in ids for e in r.entities)
    # Cross-type links (employee → vendor) are explicit for the graph; endpoints are canonical entities.
    links = [p for p in a.res.pairs.values() if p["status"] == "shared_attribute"]
    assert links and all(a.res.entity_of[p["a"]] != a.res.entity_of[p["b"]] for p in links)
    assert {tuple(sorted(p["entity_types"])) for p in links} >= {("employee", "vendor")}


def test_paysim_is_exact_account_identity_only(env):
    rr = service.run(
        env["h"].conn, load_config(overrides={"workspace": {"path": str(env["h"].workspace)}}), env["pay"]
    )
    s = service.summary(rr)
    assert (
        s["stats"]["records_by_class"] == {"account_code": s["stats"]["records"]}
        and s["stats"]["records"] > 10_000
    )
    assert s["stats"]["candidate_pairs"] == 0 and s["stats"]["multi_record_entities"] == 0
    assert "account codes" in s["unavailable"]["legal_entity_resolution"]
    assert s["stats"]["timings_seconds"]["total"] < 5


def test_scale_with_blocking(env):
    cfg = env["cfg"].settings.features.resolution
    recs = [from_master(m, cfg) for m in validation.population(3000)]
    t = time.perf_counter()
    res = engine.resolve(recs, cfg)
    assert time.perf_counter() - t < 30
    truth = {r.key: r.raw["record_id"].split("~")[0] for r in recs}
    sc = validation.score(res, truth, {(p["a"], p["b"]) for p in res.pairs.values()})
    assert res.stats["candidate_reduction_ratio"] > 0.99 and sc["candidate_recall"] == 1.0
    assert sc["false_merges"] == 0


# ------------------------------------------------------------------------------------------ API and CLI


@pytest.fixture(scope="module")
def api(env):
    token = secrets.token_urlsafe(32)
    with TestClient(create_app(env["cfg"], db_path_for(env["cfg"].settings), token), base_url=BASE_URL) as c:
        yield c, {"Authorization": f"Bearer {token}"}, f"/api/datasets/{env['syn']}/resolution"


def test_resolution_endpoints_require_the_token(api, env):
    c, _, base = api
    for path in (
        base,
        base + "/entities",
        base + "/entities/CE-0000000000000000",
        base + "/pairs",
        base + "/pairs/P-0000000000000000",
        f"/api/cases/{env['h'].case_id}/masters",
    ):
        assert c.get(path).status_code == 401, path
        assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401, path


def test_resolution_api_pages_filters_and_details(api, env):
    c, auth, base = api
    s = c.get(base, headers=auth).json()
    assert s["stats"]["records"] == 317 and s["policy"]["merged"] == "confirmed_match only"
    p1 = c.get(base + "/entities?size=50", headers=auth).json()
    p2 = c.get(base + "/entities?size=50&page=2", headers=auth).json()
    assert p1["total"] == s["stats"]["entities"] and not {x["canonical_entity_id"] for x in p1["items"]} & {
        x["canonical_entity_id"] for x in p2["items"]
    }
    conf = c.get(base + "/entities?status=conflicting_identity", headers=auth).json()
    assert conf["total"] > 0 and {x["resolution_status"] for x in conf["items"]} == {"conflicting_identity"}
    d = c.get(base + f"/entities/{conf['items'][0]['canonical_entity_id']}", headers=auth).json()
    assert d["members"] and d["review"] and all("fields" in m for m in d["members"])
    q = c.get(base + "/pairs", headers=auth).json()
    assert q["total"] > 0 and q["items"][0]["status"] == "conflicting_identity"
    assert all(x["suggested_action"] and x["explanation"] for x in q["items"])
    pd = c.get(base + f"/pairs/{q['items'][0]['pair_id']}", headers=auth).json()
    assert pd["name"]["components"] and len(pd["records"]) == 2 and "conflicts" in pd
    assert c.get(base + "/pairs?status=confirmed_match&size=5", headers=auth).json()["total"] > 0
    lookalike = c.get(base + "/pairs?q=ulvane", headers=auth).json()
    assert any(x["status"] == "conflicting_identity" for x in lookalike["items"])
    assert c.get(base + "/entities/CE-0000000000000000", headers=auth).status_code == 404
    assert c.get(base + "/entities/../../x", headers=auth).status_code in (404, 422)
    assert c.get(base + "/entities/not-an-id", headers=auth).status_code == 422
    assert c.get(base + "/pairs?status=bogus", headers=auth).status_code == 422
    assert c.get(base + "/entities?size=501", headers=auth).status_code == 422
    ms = c.get(f"/api/cases/{env['h'].case_id}/masters", headers=auth).json()
    assert len(ms["master_files"]) == 4
    risk = c.get(f"/api/datasets/{env['syn']}/risk/entities?size=5", headers=auth).json()
    assert all(e["resolution"]["canonical_entity_id"].startswith("CE-") for e in risk["items"])


def test_cli_entities(env, tmp_path, capsys):
    h = env["h"]
    common = ["--workspace", str(h.workspace)]
    assert main(["entities", "masters", "--case-id", h.case_id, *common]) == 0
    assert "MF-" in capsys.readouterr().out
    assert main(["entities", "resolve", "--dataset-id", env["syn"], *common]) == 0
    assert "Entity resolution of" in capsys.readouterr().out
    out = tmp_path / "val.json"
    assert main(["entities", "validate", "--out", str(out), *common]) == 0
    assert "false merges 0" in capsys.readouterr().out and out.is_file()
    assert (
        main(
            [
                "entities",
                "import-master",
                "--case-id",
                h.case_id,
                "--file",
                str(REFERENCE / "SYNTHETIC_customer_master.csv"),
                "--entity-type",
                "customer",
                *common,
            ]
        )
        == 1
    )
    assert "mapping is required" in capsys.readouterr().err
