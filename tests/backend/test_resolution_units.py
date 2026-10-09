"""P12 units: normalisation, identifiers, fuzzy names, blocking, conflicts, clustering, determinism and
safety."""

import random
import re
from pathlib import Path

import pytest

from ledgerspy.config import load_config
from ledgerspy.resolution import engine, matching, normalize, similarity, validation
from ledgerspy.resolution.records import MasterRecord, from_master, ledger_party

ROOT = Path(__file__).resolve().parents[2]
REF = ROOT / "data" / "synthetic" / "reference"
CFG = load_config().settings.features.resolution


def m(etype: str, row: int, **values) -> object:
    rec = MasterRecord("MF-0000000000000abc", etype, row, tuple(sorted(values.items())))
    return from_master(rec, CFG)


def decide(a, b) -> dict:
    res = engine.resolve(
        [a, b],
        CFG.model_copy(update={"blocking": CFG.blocking.model_copy(update={"exhaustive_max_records": 10})}),
    )
    (p,) = res.pairs.values()
    return p


# ------------------------------------------------------------------------------------------ normalisation


def test_name_normalisation_case_punctuation_whitespace_and_legal_suffixes():
    for raw in (
        "ABC Pvt Ltd",
        "ABC PRIVATE LIMITED",
        "Abc Pvt. Ltd.",
        "  abc   private   ltd ",
        "A.B.C. Pvt Ltd",
    ):
        n = normalize.name(raw)
        assert (n.core, n.legal_form) == ("abc", "pvt ltd"), raw
        assert n.raw == raw  # raw kept verbatim
    stationery = normalize.name("Stationery L.T.D").value
    assert stationery == normalize.name("Stationery Ltd").value == "stationery ltd"
    n = normalize.name("Quor-ven Carton L.L.P.")
    assert (n.core, n.legal_form) == ("quorven carton", "llp")
    assert normalize.name("Verrin Electricals & Co").legal_form == "and co"
    # meaningful words are never removed
    assert (
        normalize.name("ABC Trading Ltd").core == "abc trading"
        and normalize.name("ABC Traders Ltd").core == "abc traders"
    )
    assert "legal_form_canonicalised" in normalize.name("X Pvt Ltd").rules
    assert normalize.name("Javrel​  Office").value == "javrel office"  # zero-width removed
    assert (
        normalize.name(None).value is None
        and normalize.name("  ").value is None
        and normalize.name("...").value is None
    )


def test_contact_and_identifier_normalisation():
    assert (
        normalize.phone("+91-04722-10118", "91").value
        == normalize.phone("04722 10118", "91").value
        == "472210118"
    )
    assert normalize.phone("0091 22 4000 0000", "91").value == "2240000000"
    assert normalize.phone("123", "91").value is None and normalize.phone(None, "91").value is None
    assert normalize.email(" Accounts.Desk@X.Example ").value == "accounts.desk@x.example"
    assert normalize.email("john.doe+tag@gmail.com").value == "john.doe+tag@gmail.com"  # never rewritten
    assert normalize.email("not-an-email").value is None and normalize.email("a@b").value is None
    assert normalize.bank_account("SYN 7608-3725-9888").value == "SYN760837259888"
    assert normalize.bank_account("12").value is None
    tax, pan = normalize.tax_id("27ABCDE1234F1Z5")
    assert tax.value == "27ABCDE1234F1Z5" and pan == "ABCDE1234F"
    assert normalize.tax_id("SYN-TX-8884111R") == (
        normalize.Normalized("SYN-TX-8884111R", "SYNTX8884111R", ("alphanumeric_upper",)),
        None,
    )


def test_address_normalisation():
    a, pin, words = normalize.address("Plot 154, Silverline Indl. Est., Kesrigarh Rd, RJ 722635")
    b, pin2, words2 = normalize.address("plot 154 silverline industrial estate kesrigarh road rj 722635")
    assert pin == pin2 == "722635" and words == words2 and "postal_code_extracted" in a.rules
    assert normalize.address(None)[0].value is None and normalize.address(None)[1] is None


def test_similarity_components_are_reported_and_symmetric():
    c = similarity.name_components(
        "ulvane alloys", "ulvanee alloy", ("ulvane", "alloys"), ("ulvanee", "alloy"), 0.8
    )
    assert set(c) == {"edit", "jaro_winkler", "token_jaccard", "soft_token", "token_set", "trigram"}
    assert c["token_jaccard"] == 0 and c["soft_token"] == 1.0  # spelling variants of both words
    assert similarity.edit("abc", "abd") == similarity.edit("abd", "abc")
    assert similarity.jaro_winkler("martha", "marhta") == pytest.approx(0.961, abs=1e-3)
    d = similarity.token_differences(("abc", "trading"), ("abc", "traders"), 0.8)
    assert d["distinguishing_words"] == ["traders", "trading"]


# ------------------------------------------------------------------------------------------ exact identifiers


def test_strong_identifier_match_confirms():
    p = decide(
        m("vendor", 1, name="Ulvane Alloys Pvt Ltd", tax_id="27ABCDE1234F1Z5"),
        m("vendor", 2, name="Ulvane Alloys", tax_id="ABCDE1234F"),
    )
    assert p["status"] == "confirmed_match" and p["rule"] == "same_tax_id"  # a GSTIN and its PAN
    assert "same tax ID" in p["explanation"] and "No conflicting identifier found" in p["explanation"]


def test_identifier_conflict_is_never_merged_even_with_identical_names():
    p = decide(
        m("vendor", 1, name="Northwind Logistics Pvt Ltd", tax_id="TAX-0001"),
        m("vendor", 2, name="Northwind Logistics Private Limited", tax_id="TAX-0002"),
    )
    assert p["status"] == "conflicting_identity" and "different tax ID" in p["conflicts"]
    p = decide(
        m("vendor", 1, name="Acme Pvt Ltd", bank_account="BANK000001"),
        m("vendor", 2, name="Acme Pvt Ltd", bank_account="BANK000002"),
    )
    assert p["status"] == "conflicting_identity" and "different bank account" in p["conflicts"]


def test_missing_identifiers_fall_back_to_names_only():
    p = decide(m("vendor", 1, name="Acme Pvt Ltd"), m("vendor", 2, name="ACME Private Limited"))
    assert p["status"] == "confirmed_match" and p["rule"] == "same_name_modulo_formatting"
    assert set(p["identifiers"].values()) == {"missing"}


def test_same_number_at_different_banks_is_not_a_match():
    p = decide(
        m("vendor", 1, name="Acme Pvt Ltd", bank_account="000123456789", bank_code="SBIN0000001"),
        m("vendor", 2, name="Acme Pvt Ltd", bank_account="000123456789", bank_code="HDFC0000001"),
    )
    assert p["identifiers"]["bank_account"] == "differs"


# ------------------------------------------------------------------------------------------ fuzzy names


def test_fuzzy_true_near_false_and_ambiguous_pairs():
    near = decide(
        m("vendor", 1, name="Kesrin Housekeeping Pvt Ltd"),
        m("vendor", 2, name="Kesrinn Housekeeping Pvt Ltd"),
    )
    assert near["status"] in ("probable_match", "ambiguous")  # name-only: review, never merged
    false = decide(m("vendor", 1, name="ABC Trading Ltd"), m("vendor", 2, name="ABC Traders Ltd"))
    assert false["status"] == "no_match" and "traders" in false["name"]["distinguishing_words"]
    amb = decide(
        m("customer", 1, name="Lotus Foods Pvt Ltd", city="Indore"),
        m("customer", 2, name="Lotus Foods Pvt Ltd", city="Kochi"),
    )
    assert amb["status"] == "ambiguous" and "different city" in amb["conflicts"]
    persons = decide(m("employee", 1, name="Priya Sharma"), m("employee", 2, name="Priya Sharma"))
    assert persons["status"] == "ambiguous" and persons["rule"] == "same_personal_name_without_identifier"


def test_person_and_organisation_are_never_merged():
    p = decide(
        m("employee", 1, name="Devika Nair", bank_account="SYN861888842204"),
        m("vendor", 2, name="Wyndel Advisors Pvt Ltd", bank_account="SYN861888842204"),
    )
    assert p["status"] == "shared_attribute" and p["conflicts"] == []
    assert "never merged" in p["explanation"]


# ------------------------------------------------------------------------------ conflicts and shared contacts


def test_shared_contact_details_are_not_identity():
    recs = [
        m("vendor", i, name=n, phone="+91 22 4000 0000")
        for i, n in enumerate(["Tower A Facilities", "Tower A Security", "Tower A Canteen"], 1)
    ]
    res = engine.resolve(recs, CFG)
    assert {p["status"] for p in res.pairs.values()} == {"shared_attribute"}
    assert all(p["identifiers"]["phone"] == "shared_contact" for p in res.pairs.values())
    assert len({res.entity_of[r.key] for r in recs}) == 3


def test_same_email_with_conflicting_names_is_a_conflict():
    p = decide(
        m("vendor", 1, name="Ravel Paper Mills", email="ap@ravel.example"),
        m("vendor", 2, name="Zentor Chemicals", email="ap@ravel.example"),
    )
    assert p["status"] == "conflicting_identity" and p["rule"] == "shared_identifier_different_names"


def test_generic_email_domain_and_bank_code_are_not_evidence():
    a = m("vendor", 1, name="Kiran Electricals", email="kiranelec@gmail.com", bank_code="SBIN0000001")
    b = m("vendor", 2, name="Shree Ganesh Hardware", email="sghardware@gmail.com", bank_code="SBIN0000001")
    soft, hard = engine.block_keys(a, CFG)
    assert not any(k.startswith("dom:gmail") for k in soft) and not any("bank_code" in k for k in hard)
    assert engine.resolve([a, b], CFG).pairs == {}


# ------------------------------------------------------------------------------------------ blocking


def test_blocking_is_deterministic_and_keeps_identifier_blocks():
    recs = [
        m("vendor", i, name=f"Vendor Number {i} Pvt Ltd", bank_account="SHARED0000001") for i in range(1, 61)
    ]
    pairs, info = engine.candidates(recs, CFG)
    assert len(pairs) == 60 * 59 // 2  # a shared bank account is always expanded, however large
    assert any(b["key"] == "tok:vendor" for b in info["skipped_blocks"])  # big name blocks are not
    assert pairs == sorted(pairs) and engine.candidates(list(reversed(recs)), CFG) == (pairs, info)


def test_exhaustive_mode_compares_every_pair():
    recs = [m("vendor", i, name=n) for i, n in enumerate(["Alpha", "Beta", "Gamma"], 1)]
    cfg = CFG.model_copy(update={"blocking": CFG.blocking.model_copy(update={"exhaustive_max_records": 5})})
    assert (
        len(engine.candidates(recs, cfg)[0]) == 3 and engine.candidates(recs, cfg)[1]["mode"] == "exhaustive"
    )


def test_candidate_recall_on_the_validation_set():
    r = validation.run_validation(CFG, REF)
    assert r["master_records"]["candidate_recall"] == 1.0
    assert r["candidates"]["true_pairs_found_by_blocking"] == r["candidates"]["true_pairs"]
    assert r["candidates"]["reduction_ratio"] > 0.9


# ------------------------------------------------------------------------------------------ clustering


def test_no_transitive_over_merge():
    a = m("vendor", 1, name="Orion Metals Pvt Ltd", tax_id="T-ORION-1")
    b = m("vendor", 2, name="Orion Metals Private Limited", tax_id="T-ORION-1", bank_account="BANK000077")
    c = m("vendor", 3, name="Orion Metal Pvt Ltd", bank_account="BANK000077", tax_id="T-ORION-2")
    res = engine.resolve([a, b, c], CFG)
    assert res.entity_of[a.key] == res.entity_of[b.key]  # same tax ID
    assert res.entity_of[c.key] != res.entity_of[a.key]  # B≈C by bank account, but A and C conflict on tax ID
    st = {frozenset((p["a"], p["b"])): p["status"] for p in res.pairs.values()}
    assert st[frozenset((a.key, c.key))] == "conflicting_identity"


def test_blocked_merge_is_recorded():
    # A≈B and A≈C are confirmed (same name modulo formatting); B and C conflict on bank account.
    a = m("vendor", 1, name="Helix Pumps Pvt Ltd")
    b = m("vendor", 2, name="HELIX PUMPS PRIVATE LIMITED", bank_account="BANK000001")
    c = m("vendor", 3, name="Helix Pumps P. Ltd.", bank_account="BANK000002")
    res = engine.resolve([a, b, c], CFG)
    assert res.entity_of[b.key] != res.entity_of[c.key]
    assert res.blocked_merges and res.blocked_merges[0]["cross_status"] == "conflicting_identity"


def test_canonical_ids_are_stable():
    recs = [
        m("vendor", 1, name="Acme Pvt Ltd"),
        m("vendor", 2, name="ACME Private Limited"),
        m("vendor", 3, name="Zeta Ltd"),
    ]
    res = engine.resolve(recs, CFG)
    cid = res.entity_of[recs[0].key]
    assert cid == res.entity_of[recs[1].key] and re.fullmatch(r"CE-[0-9a-f]{16}", cid)
    more = engine.resolve([*recs, m("vendor", 4, name="Unrelated Textiles LLP")], CFG)
    assert more.entity_of[recs[0].key] == cid  # adding an unrelated record changes no existing ID


# ------------------------------------------------------------------------------------------ determinism


def _validation_records():
    base = validation.load_p5_masters(REF)
    var, _ = validation.variants(base)
    adv, _ = validation.adversarial()
    return [from_master(x, CFG) for x in base + var + adv]


def test_shuffled_input_and_repeated_runs_give_identical_results():
    recs = _validation_records()
    ref = engine.resolve(recs, CFG)
    for seed in (1, 2):
        shuffled = recs[:]
        random.Random(seed).shuffle(shuffled)  # noqa: S311
        again = engine.resolve(shuffled, CFG)
        assert again.sha256 == ref.sha256 and again.entity_of == ref.entity_of
        assert [p for _, p in sorted(again.pairs.items())] == [p for _, p in sorted(ref.pairs.items())]
    assert engine.resolve(recs, CFG).sha256 == ref.sha256


def test_normalisation_and_decisions_are_deterministic():
    a, b = (
        m("vendor", 1, name="Quorven Cartons LLP", tax_id="TAX-0001"),
        m("vendor", 2, name="Quor-ven Carton L.L.P.", tax_id="TAX-0002"),
    )
    assert decide(a, b) == decide(a, b) == decide(b, a)


# ------------------------------------------------------------------------------------------ PaySim and safety


def test_account_codes_are_exact_only():
    recs = [
        ledger_party(c, ("payee",), {}, {}, account_codes=True) for c in ("C123456789", "C123456788", "M555")
    ]
    res = engine.resolve(recs, CFG)
    assert res.pairs == {} and len({res.entity_of[r.key] for r in recs}) == 3
    assert all(r.entity_class == "account_code" for r in recs)


def test_resolution_never_reads_labels_ground_truth_or_supervised_output():
    pkg = ROOT / "ledgerspy" / "resolution"
    for p in pkg.glob("*.py"):
        code = p.read_text(encoding="utf-8")
        assert "ledgerspy.supervised" not in code and "FROM labels" not in code, p
        if p.name != "validation.py":  # evaluation reads the ground truth AFTER resolution ran
            assert "ground_truth" not in code and "Counterparty ID" not in code, p
    assert "Counterparty ID" in (pkg / "validation.py").read_text(encoding="utf-8")


def test_explanations_name_the_evidence_and_never_claim_ai():
    p = decide(
        m("vendor", 1, name="Ulvane Alloys Private Limited", tax_id="TAX-0001", bank_account="BANK000001"),
        m("vendor", 2, name="Ulvanee Alloy Pvt Ltd", tax_id="TAX-0002", bank_account="BANK000002"),
    )
    assert p["status"] == "conflicting_identity"
    assert re.search(r"names are \d+% similar", p["explanation"]) and "different tax ID" in p["explanation"]
    assert not re.search(r"\bAI\b|model thinks|machine learning", p["explanation"])
    assert set(p["name"]["components"]) >= set(similarity.AVERAGED)


def test_validation_report_measures_false_merges():
    r = validation.run_validation(CFG, REF, REF / "ground_truth" / "SYNTHETIC_ground_truth.json")
    mr = r["master_records"]
    assert mr["false_merges"] == 0 and mr["false_merge_clusters"] == 0 and mr["auto_match_precision"] == 1.0
    assert mr["false_splits"] == mr["false_splits_left_for_review"]  # every split is in the review queue
    assert r["variant_categories"]["spelling_name_only"]["merged"] == 0  # name-only typos are never merged
    for cat, v in r["variant_categories"].items():
        if validation.EXPECTED_MERGE[cat]:
            assert v["merged"] == v["variants"], cat
    assert not any(v["merged"] for v in r["adversarial"].values())
    assert all(v["surfaced"] and not v["merged"] for v in r["p5_relations"].values())
    assert r["ledger_links"]["wrong_links"] == 0 and r["ledger_links"]["link_precision"] == 1.0
    assert set(matching.STATUSES) == set(r["pairs_by_status"])
