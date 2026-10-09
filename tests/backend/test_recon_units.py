"""P14 reconciliation units: matching stages, ambiguity, scope / currency isolation, duplicates, balances,
determinism, the bank-statement adapter and mapping, and the schema migration."""

import copy
import json
import random
import sqlite3
from datetime import date, timedelta

import pytest

from ledgerspy.cases.db import MIGRATIONS, SCHEMA_VERSION, connect, current_version, init_db
from ledgerspy.config import load_config
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.ingest.mapping import Mapping, mapping_json
from ledgerspy.reconciliation import balances, duplicates, engine, findings
from ledgerspy.reconciliation.records import (
    NameIndex,
    Scope,
    account_key,
    alnum,
    bank_rows,
    fallback_entity,
    ledger_rows,
    shape,
)

CFG = load_config()
RC = CFG.settings.reconciliation
D0 = date(2024, 3, 1)
OWN = "Our Company Pvt Ltd"


def day(n: int) -> str:
    return (D0 + timedelta(days=n)).isoformat() + "T10:00:00"


def L(tid, d, amt, flow="out", cp="Acme Traders Pvt Ltd", ref=None, cur="INR", **extra):
    out = flow == "out"
    return {
        "txn_id": tid,
        "source_row": 0,
        "posted_at": day(d),
        "amount_minor": amt,
        "currency": cur,
        "direction": "debit" if out else "credit",
        "payer_raw": OWN if out else cp,
        "payee_raw": cp if out else OWN,
        "reference": ref,
        "description": "posting",
        "account": "6100",
        "txn_type": None,
        "extra": dict(extra),
        "normalized_fields": [],
    }


def B(
    tid,
    d,
    amt,
    flow="out",
    desc="NEFT DR/UTR/ACME TRADERS PVT LTD",
    ref=None,
    acc="ACC1",
    cur="INR",
    bal=None,
):
    return {
        "txn_id": tid,
        "source_row": 0,
        "posted_at": day(d),
        "amount_minor": amt,
        "currency": cur,
        "direction": flow,
        "payer_raw": None,
        "payee_raw": None,
        "reference": ref,
        "description": desc,
        "account": acc,
        "txn_type": None,
        "extra": {} if bal is None else {"Balance": bal},
        "normalized_fields": [],
    }


def number(rows):
    for i, r in enumerate(rows, start=1):
        r["source_row"] = i
    return rows


def anchors(lv, bv):
    """Matched rows far before and after the test rows, so both sides cover the test period (rows whose
    clearing window lies outside the other side's coverage are insufficient data, by design). Dropped from
    the output."""
    for d, flow in ((-90, "out"), (-90, "in"), (300, "out"), (300, "in")):
        tag = f"anchor-{d}-{flow}"
        lv.append(L(tag, d, 1_234_567, flow=flow, cp="Anchor Holdings Pvt Ltd"))
        bv.append(B(tag + "-b", d, 1_234_567, flow=flow, desc="X/ANCHOR HOLDINGS PVT LTD"))
        for acc in ("ACC2",):
            bv.append(B(tag + "-b2", d, 1_234_567, flow=flow, acc=acc, desc="X/ANCHOR HOLDINGS PVT LTD"))
    return lv, bv


def recon(lv, bv, scopes=None, rc=RC, debit_means="outflow", anchor=True):
    if anchor:
        lv, bv = anchors(list(lv), list(bv))
    lv, bv = number(lv), number(bv)
    scopes = scopes or [Scope("SC-1", account_key(alnum("ACC1")), None, None)]
    names = sorted(
        {(fallback_entity(n), n) for r in lv for n in (r["payer_raw"], r["payee_raw"]) if n}
        | {(fallback_entity("Bob Employee"), "Bob Employee")}
    )
    idx = NameIndex(names, rc.counterparty.min_name_chars, rc.counterparty.min_prefix_chars)
    lr = ledger_rows(lv, scopes, debit_means, fallback_entity)
    br = bank_rows(bv, True, scopes, idx)
    dups = duplicates.find(lr, br, rc)
    out = engine.reconcile(lr, br, rc, dups)
    if anchor:
        keep = lambda ids: not any(t.startswith("anchor") for t in ids)  # noqa: E731
        out.results = [r for r in out.results if keep(r["ledger_txn_ids"] + r["bank_txn_ids"])]
        dups = [g for g in dups if keep(g["txn_ids"])]
    return out, dups, lr, br


def status_of(out):
    s = {}
    for r in out.results:
        for t in r["ledger_txn_ids"] + r["bank_txn_ids"]:
            s[t] = r["status"]
    return s


def with_(rc, **sections):
    return rc.model_copy(update={k: getattr(rc, k).model_copy(update=v) for k, v in sections.items()})


# ------------------------------------------------------------------------------------------ one-to-one


def test_exact_match_by_counterparty_and_by_reference():
    out, *_ = recon(
        [L("l1", 0, 10_000_00), L("l2", 3, 7_777_00, cp="Zed Ltd", ref="INV-2024-0042")],
        [B("b1", 1, 10_000_00), B("b2", 4, 7_777_00, desc="CHQ 000123 INV2024-0042")],
    )
    st = status_of(out)
    assert st == {"l1": "exact_match", "b1": "exact_match", "l2": "exact_match", "b2": "exact_match"}
    r1 = next(r for r in out.results if r["ledger_txn_ids"] == ["l1"])
    assert r1["link"]["counterparty"] == "agree" and r1["date_difference_days"] == 1
    assert r1["amount_difference_minor"] == 0 and r1["detector"] == engine.DETECTOR
    r2 = next(r for r in out.results if r["ledger_txn_ids"] == ["l2"])
    assert r2["link"]["reference"] == "agree"


def test_probable_match_without_reference_or_counterparty():
    out, *_ = recon([L("l1", 0, 5_000_00, cp="Payroll")], [B("b1", 2, 5_000_00, desc="SALARY BULK UPLOAD")])
    r = out.results[0]
    assert r["status"] == "probable_match" and r["evidence_score"] >= RC.scoring.probable_min_score
    assert set(r["score_components"]) == {"amount", "date"} and r["limitations"]


def test_no_match_outside_the_date_window_without_a_link():
    out, *_ = recon([L("l1", 0, 5_000_00, cp="Payroll")], [B("b1", 20, 5_000_00, desc="SALARY BULK UPLOAD")])
    assert status_of(out) == {"l1": "unmatched_ledger", "b1": "unmatched_bank"}


def test_ambiguous_candidates_are_never_resolved_by_order():
    lv = [L("l1", 0, 9_999_00), L("l2", 0, 9_999_00)]
    bv = [B("b1", 1, 9_999_00), B("b2", 1, 9_999_00)]
    out, *_ = recon(lv, bv)
    assert len(out.results) == 1 and out.results[0]["status"] == "ambiguous"
    assert len(out.results[0]["candidates"]) == 4 and "none was selected" in out.results[0]["explanation"]
    # one ledger row, two equally good bank lines: still ambiguous
    out, *_ = recon([L("l1", 0, 9_999_00)], bv)
    assert out.results[0]["status"] == "ambiguous" and out.results[0]["bank_txn_ids"] == ["b1", "b2"]


def test_clear_winner_beats_a_weaker_candidate():
    out, *_ = recon([L("l1", 0, 9_999_00)], [B("b1", 0, 9_999_00), B("b2", 5, 9_999_00, desc="NEFT DR/X")])
    assert status_of(out) == {"l1": "exact_match", "b1": "exact_match", "b2": "unmatched_bank"}


# ------------------------------------------------------------------------------------------ grouped


def test_one_bank_line_settles_several_ledger_rows():
    lv = [
        L("l1", 0, 1_000_00),
        L("l2", 2, 2_500_00),
        L("l3", 4, 3_000_00),
        L("other", 3, 6_500_00, cp="Zed Ltd"),
    ]
    out, *_ = recon(lv, [B("b1", 6, 6_500_00, desc="NEFT DR/UTR/ACME TRADERS PVT LTD/3 INVOICES")])
    g = next(r for r in out.results if r["status"] == "grouped_match")
    assert g["ledger_txn_ids"] == ["l1", "l2", "l3"] and g["bank_txn_ids"] == ["b1"]
    assert [c["txn_id"] for c in g["components"]] == ["l1", "l2", "l3"] and g["amount_difference_minor"] == 0
    assert g["date_difference_days"] == {"min": 2, "max": 6}
    assert status_of(out)["other"] == "unmatched_ledger"  # same amount, other counterparty: not used


def test_one_ledger_row_settled_by_several_bank_lines():
    out, *_ = recon(
        [L("l1", 0, 9_000_00, flow="in")],
        [
            B("b1", 1, 4_000_00, "in", "RTGS CR/ACME TRADERS PVT LTD"),
            B("b2", 3, 5_000_00, "in", "RTGS CR/ACME TRADERS PVT LTD"),
        ],
    )
    g = out.results[0]
    assert g["status"] == "grouped_match" and g["link"]["type"] == "ledger_settled_by_bank"
    assert g["bank_txn_ids"] == ["b1", "b2"]


def test_competing_groupings_are_ambiguous_and_search_limits_are_reported():
    lv = [L(f"l{i}", i, 1_000_00) for i in range(4)]
    out, *_ = recon(lv, [B("b1", 5, 2_000_00, desc="NEFT/ACME TRADERS PVT LTD")])
    assert {r["status"] for r in out.results} >= {"ambiguous"}
    amb = next(r for r in out.results if r["status"] == "ambiguous")
    assert len(amb["candidates"]) == 3  # runs l0-l1, l1-l2, l2-l3
    rc = with_(RC, grouping={"max_candidates": 2})
    out, *_ = recon([L(f"l{i}", i, 1_000_00 + i) for i in range(5)], [B("b1", 6, 99_00)], rc=rc)
    assert out.truncation["grouping_candidates"] == ["b1"]


# ------------------------------------------------------------------------------------------ partial


def test_partial_matches_report_the_difference_and_are_not_reconciled():
    lv = [
        L("fee", 0, 100_000_00),
        L("late", 0, 50_000_00, cp="Latecomer Industries Pvt Ltd"),
        L("wrong", 0, 20_000_00, cp="Vendor X Pvt Ltd"),
    ]
    bv = [
        B("bf", 1, 100_590_00),
        B("bl", 12, 50_000_00, desc="NEFT/LATECOMER INDUSTRIES PVT LTD"),
        B("bw", 1, 20_000_00, desc="NEFT DR/BOB EMPLOYEE/A-C XX0001"),
    ]
    out, *_ = recon(lv, bv)
    by = {r["ledger_txn_ids"][0]: r for r in out.results if r["ledger_txn_ids"]}
    assert by["fee"]["status"] == "partial_match" and by["fee"]["discrepancies"] == ["amount"]
    assert by["fee"]["amount_difference_minor"] == 590_00
    assert by["late"]["discrepancies"] == ["date"] and by["late"]["date_difference_days"] == 12
    assert by["wrong"]["discrepancies"] == ["counterparty"] and "bob employee" in by["wrong"]["explanation"]
    assert all("Not reconciled" in r["explanation"] for r in by.values())


def test_amount_outside_tolerance_or_without_a_link_stays_unmatched():
    out, *_ = recon([L("l1", 0, 10_000_00)], [B("b1", 1, 11_000_00)])  # 10% > 5%
    assert status_of(out) == {"l1": "unmatched_ledger", "b1": "unmatched_bank"}
    out, *_ = recon([L("l1", 0, 10_000_00, cp="Payroll")], [B("b1", 1, 10_050_00, desc="SALARY")])
    assert status_of(out)["l1"] == "unmatched_ledger"  # close amount but nothing links them


def test_conflicting_reference_blocks_automatic_matching():
    out, *_ = recon(
        [L("l1", 0, 3_000_00, cp="Payroll", ref="UTR000111")],
        [B("b1", 0, 3_000_00, desc="TRANSFER", ref="UTR000999")],
    )
    r = out.results[0]
    assert r["status"] == "partial_match" and r["discrepancies"] == ["reference"]
    assert shape("UTR000111") == shape("UTR000999") == "A3N6"


# ------------------------------------------------------------------------------------------ scope / data


def test_no_matching_across_accounts_or_currencies():
    scopes = [
        Scope("SC-1", account_key("ACC1"), "Entity", "A"),
        Scope("SC-2", account_key("ACC2"), "Entity", "B"),
    ]
    lv = [
        L("la", 0, 1_000_00, Entity="A"),
        L("lb", 0, 2_000_00, Entity="B"),
        L("lusd", 0, 3_000_00, cur="USD", Entity="A"),
    ]
    bv = [
        B("b-acc2", 0, 1_000_00, acc="ACC2"),
        B("b-acc1", 0, 2_000_00, acc="ACC1"),
        B("b-inr", 0, 3_000_00, acc="ACC1"),
    ]
    out, _, lr, br = recon(lv, bv, scopes=scopes, anchor=False)
    linked = ("exact_match", "probable_match", "grouped_match", "partial_match", "ambiguous")
    assert not [s for s in status_of(out).values() if s in linked]
    s = engine.summarize(out.results, lr, br)
    assert set(s["amounts_by_status_minor"]["ledger"]) == {"INR", "USD"}  # never added together


def test_insufficient_and_not_applicable_rows_are_kept_with_reasons():
    lv = [L("nocur", 0, 1_000_00, cur=None), L("zero", 0, 0), L("ok", 0, 5_000_00)]
    lv[0]["currency"] = None
    bv = [B("other-acc", 0, 5_000_00, acc="ZZZ"), B("noacc", 0, 5_000_00, acc=None), B("ok", 0, 5_000_00)]
    out, *_ = recon(lv, bv)
    by = {t: r for r in out.results for t in r["ledger_txn_ids"] + r["bank_txn_ids"]}
    assert by["nocur"]["status"] == "insufficient_data" and by["nocur"]["reasons"] == ["missing_currency"]
    assert by["zero"]["status"] == "not_applicable" and by["zero"]["reasons"] == ["zero_amount"]
    assert by["other-acc"]["status"] == "not_applicable" and by["other-acc"]["reasons"] == [
        "account_not_in_scope"
    ]
    assert by["noacc"]["status"] == "insufficient_data" and by["noacc"]["reasons"] == ["missing_account"]


def test_ledger_debit_semantics_are_applied_not_assumed():
    lv = number([L("l1", 0, 1_000_00)])
    sc = [Scope("SC-1", account_key("ACC1"), None, None)]
    assert ledger_rows(lv, sc, None, fallback_entity)[0].reasons == ["missing_direction"]
    assert ledger_rows(lv, sc, "outflow", fallback_entity)[0].flow == "out"
    assert ledger_rows(lv, sc, "inflow", fallback_entity)[0].flow == "in"


def test_rows_outside_the_other_sides_coverage_are_insufficient():
    out, *_ = recon(
        [L("early", 0, 1_000_00, cp="Q"), L("l", 30, 2_000_00), L("late", 60, 3_000_00, cp="Q")],
        [B("b", 31, 2_000_00)],
        anchor=False,
    )
    s = status_of(out)
    assert s["early"] == "insufficient_data" and s["late"] == "insufficient_data"


# ------------------------------------------------------------------------------------------ duplicates


def test_duplicate_bank_posting_is_a_candidate_not_excluded():
    lv = [L("l1", 0, 4_321_00)]
    bv = [B("b1", 0, 4_321_00, ref="UTR1"), B("b2", 0, 4_321_00, ref="UTR1"), B("b3", 20, 4_321_00)]
    out, dups, *_ = recon(lv, bv)
    exact = [g for g in dups if g["type"] == "exact"]
    assert len(exact) == 1 and exact[0]["txn_ids"] == ["b1", "b2"] and exact[0]["confirmed"] is False
    s = status_of(out)
    assert s["l1"] == "ambiguous" and s["b1"] == s["b2"] == "ambiguous"  # identical candidates: no pick
    assert s["b3"] == "unmatched_bank"
    # once one side is matched, the repeat of a matched line becomes a duplicate candidate
    out, dups, *_ = recon([L("l1", 0, 4_321_00)], [B("b1", 0, 4_321_00), B("b2", 3, 4_321_00)])
    s = status_of(out)
    assert s["b1"] == "exact_match" and s["b2"] == "duplicate_candidate"
    dc = next(r for r in out.results if r["status"] == "duplicate_candidate")
    assert dc["duplicate_group_ids"] and "CANDIDATE" in dc["limitations"][0]


def test_ledger_duplicates_by_reference_and_window():
    lv = [L("a", 0, 1_000_00, ref="INV1"), L("b", 9, 1_000_00, ref="INV1"), L("c", 1, 1_000_00)]
    _, dups, *_ = recon(lv, [])
    kinds = {(g["type"], tuple(g["txn_ids"])) for g in dups}
    assert ("same_reference", ("a", "b")) in kinds and ("near", ("a", "c")) in kinds


# ------------------------------------------------------------------------------- summary / determinism


def test_every_row_once_and_totals_reconcile():
    rng = random.Random(7)
    lv, bv = [], []
    for i in range(300):
        a = rng.randrange(1_00, 50_000_00)
        lv.append(
            L(
                f"l{i:03d}",
                rng.randrange(0, 200),
                a,
                flow=rng.choice(["in", "out"]),
                cp=rng.choice(["Acme Traders Pvt Ltd", "Zed Ltd", "Payroll"]),
            )
        )
        if rng.random() < 0.9:
            x = dict(lv[-1])
            bv.append(
                B(
                    f"b{i:03d}",
                    int(x["posted_at"][8:10]) % 28,
                    a + rng.choice([0, 0, 0, 100]),
                    flow="out" if x["direction"] == "debit" else "in",
                )
            )
    out, _, lr, br = recon(lv, bv, anchor=False)
    s = engine.summarize(out.results, lr, br)
    assert sum(s["rows_by_status"]["ledger"].values()) == 300 and sum(
        s["rows_by_status"]["bank"].values()
    ) == len(bv)
    for side, rows in (("ledger", lv), ("bank", bv)):
        total = sum(v for by in s["amounts_by_status_minor"][side].values() for v in by.values())
        assert total == sum(r["amount_minor"] for r in rows)
    seen = [t for r in out.results for t in r["ledger_txn_ids"]]
    assert len(seen) == len(set(seen)) == 300


def test_shuffled_inputs_give_identical_results_and_inputs_are_not_mutated():
    rng = random.Random(3)
    lv = [
        L(f"l{i}", i % 40, (i % 17 + 1) * 1_000_00, cp=["Acme Traders Pvt Ltd", "Zed Ltd"][i % 2])
        for i in range(120)
    ]
    bv = [B(f"b{i}", i % 40 + i % 3, (i % 17 + 1) * 1_000_00) for i in range(110)]
    number(lv), number(bv)
    before = copy.deepcopy((lv, bv))
    a, *_ = recon(copy.deepcopy(lv), copy.deepcopy(bv), anchor=False)
    assert {r["status"] for r in a.results} >= {"exact_match", "unmatched_ledger"}
    sl, sb = copy.deepcopy(lv), copy.deepcopy(bv)
    rng.shuffle(sl), rng.shuffle(sb)
    # keep each row's own source_row: shuffling changes the order rows arrive in, not their identity
    out = engine.reconcile(*_rows(sl, sb), RC, duplicates.find(*_rows(sl, sb), RC))
    assert sha256_hex(canonical_json(a.results)) == sha256_hex(canonical_json(out.results))
    recon(lv, bv, anchor=False)
    assert (lv, bv) == before


def _rows(lv, bv):
    scopes = [Scope("SC-1", account_key("ACC1"), None, None)]
    names = sorted(
        {(fallback_entity(n), n) for r in lv for n in (r["payer_raw"], r["payee_raw"]) if n}
        | {(fallback_entity("Bob Employee"), "Bob Employee")}
    )
    idx = NameIndex(names, RC.counterparty.min_name_chars, RC.counterparty.min_prefix_chars)
    return ledger_rows(lv, scopes, "outflow", fallback_entity), bank_rows(bv, True, scopes, idx)


def test_findings_are_a_separate_unfused_family():
    out, dups, *_ = recon(
        [L("l1", 0, 1_000_00), L("l2", 0, 100_000_00)], [B("b1", 0, 7_000_00), B("b2", 1, 100_590_00)]
    )
    sig = findings.compute(out.results, dups, {"accounts": []})
    rules = {s["rule_id"] for s in sig}
    assert rules == {"recon_unmatched_ledger", "recon_unmatched_bank", "recon_amount_mismatch"}
    assert all(
        s["family"] == "reconciliation" and s["weight"] is None and s["fused_in_p9"] is False for s in sig
    )
    assert all(s["explanation"] and s["limitations"] and s["signal_id"].startswith("S-") for s in sig)


# ------------------------------------------------------------------------------------------ names


def test_truncated_and_aliased_names_are_found_in_descriptions():
    idx = NameIndex(
        [
            ("e1", "Krishnamurthy Engineering Services Pvt Ltd"),
            ("e2", "Quor-ven Carton L.L.P."),
            ("e3", "Javrel Office"),
            ("e4", "Javrel Office Supplies"),
            ("e5", "Abc"),
        ],
        6,
        12,
    )
    from ledgerspy.reconciliation.records import segments

    assert idx.find(segments("NEFT DR/UTR1/KRISHNAMURTHY ENGINEERING SERVIC"))[0] == {"e1"}
    assert idx.find(segments("NEFT DR/UTR1/QUOR-VEN CARTON L.L.P./A-C XX0005"))[0] == {"e2"}
    assert idx.find(segments("NEFT/JAVREL OFFICE SUPPLIES"))[0] == {"e4"}  # the longer name only
    assert idx.find(segments("NEFT/ABC"))[0] == set()  # too short to search
    assert idx.find(segments("NEFT/KRISHNA"))[0] == set()  # below min_prefix_chars


# ------------------------------------------------------------------------------------------ balances


def _bal_rows(balances_, order=1):
    rows = []
    for i, (amt, flow, bal) in enumerate(balances_):
        r = B(f"s{i}", i, amt, flow, bal=bal)
        rows.append(r)
    rows = rows[::order]
    return number(rows)


MAP = {"options": {"decimal_separator": "."}, "bank": {"balance_column": "Balance"}}


def test_balance_continuity_and_breaks():
    seq, bal = [], 1000_00
    for i in range(12):
        flow = "in" if i % 3 else "out"
        bal += 10_00 if flow == "in" else -10_00
        if i == 8:
            bal += 100_00  # the stated balance jumps by 100.00 with no movement
        seq.append((10_00, flow, f"{bal // 100}.{bal % 100:02d}"))
    rep = balances.check(_bal_rows(seq), MAP, CFG.settings)
    a = rep["accounts"][0]
    assert a["status"] == "discontinuities" and a["opening_balance_minor"] == 1000_00
    br = a["discontinuities"]
    assert len(br) == 1 and br[0]["txn_id"] == "s8" and br[0]["difference_minor"] == 100_00
    assert a["unexplained_difference_minor"] == 100_00 and a["periods"]["status"] == "not_applicable"
    sig = findings.compute([], [], rep)
    assert [s["rule_id"] for s in sig] == ["recon_balance_discontinuity"]


def test_newest_first_statements_and_unavailable_cases():
    seq = [
        (100_00, "in", "1100.00"),
        (50_00, "out", "1050.00"),
        (25_00, "out", "1025.00"),
        (5_00, "in", "1030.00"),
    ]
    rev = balances.check(_bal_rows(seq, order=-1), MAP, CFG.settings)["accounts"][0]
    assert rev["status"] == "consistent" and rev["order"] == "reverse_file_order"
    assert rev["opening_balance_minor"] == 1000_00 and rev["closing_balance_minor"] == 1030_00
    assert (
        balances.check(_bal_rows(seq), {"options": {}, "bank": {}}, CFG.settings)["status"] == "unavailable"
    )
    junk = _bal_rows([(1_00, "in", "5.00"), (1_00, "in", "9.00"), (1_00, "in", "2.00")])
    assert balances.check(junk, MAP, CFG.settings)["accounts"][0]["status"] == "unavailable"


def test_statement_periods_continuity():
    rows = _bal_rows([(100_00, "in", "1100.00"), (50_00, "out", "1050.00"), (25_00, "out", "1025.00")])
    for r, s in zip(rows, ["M1", "M1", "M2"], strict=True):
        r["extra"]["Stmt"] = s
    rep = balances.check(
        rows, {"options": {}, "bank": {"balance_column": "Balance", "statement_column": "Stmt"}}, CFG.settings
    )
    p = rep["accounts"][0]["periods"]["items"]
    assert [x["statement"] for x in p] == ["M1", "M2"] and p[1]["continues_previous"] is True


# ------------------------------------------------------------------------------------------ ingestion


BANK_CSV = (
    "Txn Date,Narration,Ref No,Withdrawal,Deposit,Balance,Currency,Account No\n"
    "2024-01-02,NEFT DR/ACME,R1,100.00,,900.00,INR,ACC1\n"
    "2024-01-03,RTGS CR/ZED,R2,,50.00,950.00,INR,ACC1\n"
)


def test_bank_statement_ingestion_with_withdrawal_deposit_headers(ingest):
    ds = ingest.upload(BANK_CSV.encode(), "stmt.csv", kind="bank_statement")
    s = ds["mapping_suggestion"]
    assert ds["kind"] == "bank_statement" and s["adapter"]["suggested"] == "bank_statement"
    assert s["proposed_mapping"]["bank"]["debit_means"] == "outflow" and s["requires_confirmation"] == []
    assert s["proposed_mapping"]["bank"]["balance_column"] == "Balance"
    r = ingest.confirm(ds)
    rows = ingest.transactions(ds["dataset_id"])
    assert [(x["direction"], x["amount_minor"]) for x in rows] == [("out", 10000), ("in", 5000)]
    assert rows[0]["extra"] == {"Balance": "900.00"} and r["adapter"] == "bank_statement"


def test_debit_credit_headers_and_signed_amounts_need_confirmation(ingest):
    csv = "Date,Description,Debit,Credit,Currency\n2024-01-02,X,100.00,,INR\n2024-01-03,Y,,7.00,INR\n"
    ds = ingest.upload(csv.encode(), "s.csv", kind="bank_statement")
    items = [i["item"] for i in ds["mapping_suggestion"]["requires_confirmation"]]
    assert "bank.debit_means" in items
    from ledgerspy.ingest.errors import IngestError

    with pytest.raises(IngestError) as e:
        ingest.confirm(ds)
    assert {x["code"] for x in e.value.details["errors"]} == {"bank_semantics"}
    m = copy.deepcopy(ds["mapping_suggestion"]["proposed_mapping"])
    m["bank"]["debit_means"] = "inflow"  # an unusual bank: confirmed, then applied as stated
    ingest.confirm(ds, m)
    assert [x["direction"] for x in ingest.transactions(ds["dataset_id"])] == ["in", "out"]

    signed = "Date,Description,Amount\n2024-01-02,X,-100.00\n2024-01-03,Y,40.00\n2024-01-04,Z,0\n"
    ds = ingest.upload(signed.encode(), "t.csv", kind="bank_statement")
    items = [i["item"] for i in ds["mapping_suggestion"]["requires_confirmation"]]
    assert "bank.amount_sign" in items and "currency" in items
    m = copy.deepcopy(ds["mapping_suggestion"]["proposed_mapping"])
    m["bank"]["amount_sign"] = "positive_inflow"
    r = ingest.confirm(ds, m)
    rows = ingest.transactions(ds["dataset_id"])
    assert [(x["direction"], x["amount_minor"], x["currency"]) for x in rows] == [
        ("out", 10000, None),
        ("in", 4000, None),
        (None, 0, None),
    ]
    assert r["issues"]["by_field"]["direction"] == {"zero_amount_no_direction": 1}


def test_kind_and_adapter_must_agree_and_ledger_mappings_keep_their_hash(ingest):
    ds = ingest.upload(BANK_CSV.encode(), "stmt.csv")  # uploaded as a ledger
    assert any("bank statement" in n for n in ds["mapping_suggestion"]["notes"])
    m = copy.deepcopy(ds["mapping_suggestion"]["proposed_mapping"])
    m["adapter"] = "bank_statement"
    from ledgerspy.ingest.errors import IngestError

    with pytest.raises(IngestError):
        ingest.confirm(ds, m)
    plain = Mapping(adapter="generic_ledger", fields={"posted_at": "D"})
    assert "bank" not in mapping_json(plain)
    assert mapping_json(plain) == {k: v for k, v in plain.model_dump(mode="json").items() if k != "bank"}


def test_negative_value_in_a_directional_column_is_not_reinterpreted(ingest):
    csv = (
        "Date,Description,Withdrawal,Deposit,Currency\n"
        "2024-01-02,X,-100.00,,INR\n2024-01-03,Y,5.00,,INR\n2024-01-04,Z,,6.00,INR\n"
    )
    ds = ingest.upload(csv.encode(), "n.csv", kind="bank_statement")
    ingest.confirm(ds)
    rows = ingest.transactions(ds["dataset_id"])
    assert (rows[0]["amount_minor"], rows[0]["direction"]) == (None, None)
    issues = ingest.conn.execute(
        "SELECT code FROM ingest_issues WHERE dataset_id = ?", (ds["dataset_id"],)
    ).fetchall()
    assert [i[0] for i in issues] == ["negative_in_directional_column"]


def test_pdf_is_accepted_only_as_a_bank_statement_and_malformed_pdfs_are_refused(ingest):
    """P14 deferred PDFs to P14b; since P14b a PDF is a bank statement (ledgers stay CSV / XLSX)."""
    from ledgerspy.ingest.errors import IngestError

    with pytest.raises(IngestError) as e:
        ingest.upload(b"%PDF-1.7 ...", "statement.pdf")  # as a ledger
    assert e.value.code == "unsupported_format" and "bank statement" in e.value.message
    with pytest.raises(IngestError) as e:
        ingest.upload(b"%PDF-1.7 ...", "statement.pdf", kind="bank_statement")
    assert e.value.code == "pdf_malformed"
    assert ingest.conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0  # nothing registered


# ------------------------------------------------------------------------------------------ schema


def test_migration_6_keeps_existing_datasets_and_triggers(tmp_path):
    path = tmp_path / "v5.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN")
    for version, desc, statements in MIGRATIONS[:5]:
        for s in statements:
            conn.execute(s)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)", (version, desc))
    conn.execute(
        "INSERT INTO cases (case_id, name, status, created_at, created_by) "
        "VALUES ('C-1', 'c', 'open', 't', 'a')"
    )
    conn.execute("INSERT INTO evidence_files VALUES (?, 1, 'x.csv', 't')", ("a" * 64,))
    conn.execute(
        "INSERT INTO datasets (dataset_id, case_id, evidence_sha256, original_filename, kind, format, "
        "format_details, header, row_count, profile, suggestion, upload_config_sha256, status, created_by, "
        "created_at) VALUES ('D-1', 'C-1', ?, 'x.csv', 'ledger', 'csv', '{}', '[]', 1, '{}', '{}', 'h', "
        "'canonicalised', 'a', 't')",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO transactions (dataset_id, txn_id, source_row, extra) VALUES ('D-1', 'T1', 1, '{}')"
    )
    conn.execute("COMMIT")
    conn.close()
    assert (
        init_db(path, "tester") == SCHEMA_VERSION and SCHEMA_VERSION >= 6
    )  # v5 → latest (6 = P14, 7 = P14b)
    c = connect(path)
    assert current_version(c) == SCHEMA_VERSION
    assert c.execute("SELECT kind, status FROM datasets").fetchall()[0][:] == ("ledger", "canonicalised")
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        c.execute("UPDATE datasets SET original_filename = 'y' WHERE dataset_id = 'D-1'")
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        c.execute("DELETE FROM datasets")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO transactions (dataset_id, txn_id, source_row, extra) VALUES ('D-9', 'T', 1, '{}')"
        )
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute(
            "INSERT INTO reconciliation_runs VALUES "
            "('RR-0', 'C-1', 'D-1', 'D-1', '{}', '{}', ?, '{}', 'a', 't')",
            ("b" * 64,),
        )
        c.execute("DELETE FROM reconciliation_runs")
    c.close()
    assert json.dumps(MIGRATIONS[5][1]).count("P14") == 1


# ------------------------------------------------------------------------------------------ safety


def test_reconciliation_reads_no_labels_ground_truth_or_models_and_p9_never_imports_it():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "ledgerspy"
    for p in (root / "reconciliation").glob("*.py"):
        code = p.read_text(encoding="utf-8")
        for banned in (
            "ledgerspy.supervised",
            "ledgerspy.risk",
            "ledgerspy.synth",
            "FROM labels",
            "ground_truth",
        ):
            assert banned not in code, (p.name, banned)
    for sub in ("risk", "anomaly", "forensics", "features", "resolution", "supervised", "graph"):
        for p in (root / sub).rglob("*.py"):
            assert "ledgerspy.reconciliation" not in p.read_text(encoding="utf-8"), p
