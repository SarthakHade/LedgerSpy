"""P12 validation: entity resolution on the P5 masters, deterministic variants, adversarial cases and the P5
ledger (`ledgerspy entities validate`).

Inputs:
- **P5 masters** (vendor, customer, employee, group company) with the documented column mappings below. Every
  P5 master record is its own entity, except where the masters themselves record one company twice (a vendor
  and a customer record with the same tax ID: one legal entity).
- **Variants** (truth by construction, seed-fixed): every k-th master record gets one of these copies:
  - an exact duplicate;
  - case / punctuation changes;
  - the legal suffix spelled differently;
  - a one-letter spelling change;
  - address formatting;
  - the name varied while e-mail, phone and bank account stay the same.
- **Adversarial negatives** (all distinct entities):
  - "ABC Trading Ltd" / "ABC Traders Ltd";
  - one legal name in two cities;
  - two employees with the same common name;
  - a shared generic e-mail domain;
  - a shared office address;
  - a shared bank (IFSC) code;
  - a shared switchboard phone;
  - near-identical names with conflicting tax IDs.
- **P5 relations** that must surface but never merge (read from the P5 ground truth ONLY to score the result,
  after resolution ran): look-alike vendors (S-E-01/02), three vendors sharing a bank account (S-P-01),
  sister concerns sharing contacts (S-N-04), employee–vendor shared attributes (S-F-01, S-M-01/02,
  S-N-01/02/03/05).
- **Ledger links**: P5 ledger names → master records. Scored against the ledger's own unmapped
  `Counterparty ID` column (evaluation only; matching never reads it).

Metrics (pairwise over records):
- candidate recall: true pairs that blocking proposed / all true pairs;
- auto-match precision: same-cluster pairs that are true / all same-cluster pairs;
- false merges: same-cluster pairs of different entities (and the clusters containing them);
- false splits: true pairs not in one cluster, split into "left for review" and "missed";
- ambiguous rate: review-status pairs / candidate pairs.
"""

import csv
import json
import random
import time
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

from ledgerspy.config.schema import ResolutionConfig
from ledgerspy.features.data import party_key
from ledgerspy.resolution import engine, matching, records
from ledgerspy.resolution.records import MasterRecord

P5_FILES: dict[str, tuple[str, dict[str, str]]] = {
    "vendor": (
        "SYNTHETIC_vendor_master.csv",
        {
            "record_id": "vendor_id",
            "name": "vendor_name",
            "address": "address",
            "city": "city",
            "phone": "phone",
            "email": "email",
            "bank_account": "bank_account",
            "bank_code": "bank_code",
            "tax_id": "tax_id",
        },
    ),
    "customer": (
        "SYNTHETIC_customer_master.csv",
        {
            "record_id": "customer_id",
            "name": "customer_name",
            "address": "address",
            "city": "city",
            "phone": "phone",
            "tax_id": "tax_id",
        },
    ),
    "employee": (
        "SYNTHETIC_employee_master.csv",
        {
            "record_id": "employee_id",
            "name": "name",
            "address": "home_address",
            "phone": "phone",
            "email": "email",
            "bank_account": "bank_account",
            "bank_code": "bank_code",
            "tax_id": "tax_id",
        },
    ),
    "group_company": (
        "SYNTHETIC_group_entities.csv",
        {
            "record_id": "entity_code",
            "name": "legal_name",
            "address": "address",
            "bank_account": "bank_account",
            "bank_code": "bank_code",
            "tax_id": "tax_id",
        },
    ),
}
# Variant categories and the evidence each variant keeps (identifiers not listed are dropped):
#   exact_duplicate              every field                       → expected: merged
#   case_punctuation             every field, reformatted          → expected: merged
#   legal_suffix_name_only       name (other suffix), address      → expected: merged (same core name)
#   spelling_with_tax_id         name (one letter changed), tax ID → expected: merged (same tax ID)
#   spelling_name_only           name (one letter changed), address → expected: review, never merged
#   address_format               name, address reformatted, phone  → expected: merged
#   shared_contact_name_variant  name varied, e-mail, phone, bank  → expected: merged
CATEGORIES = (
    "exact_duplicate",
    "case_punctuation",
    "legal_suffix_name_only",
    "spelling_with_tax_id",
    "spelling_name_only",
    "address_format",
    "shared_contact_name_variant",
)
EXPECTED_MERGE = {c: c != "spelling_name_only" for c in CATEGORIES}
_KEEP = {
    "legal_suffix_name_only": ("record_id", "name", "address", "city"),
    "spelling_with_tax_id": ("record_id", "name", "tax_id"),
    "spelling_name_only": ("record_id", "name", "address", "city"),
    "address_format": ("record_id", "name", "address", "city", "phone"),
    "shared_contact_name_variant": ("record_id", "name", "email", "phone", "bank_account", "bank_code"),
}
SUFFIX_SWAP = (
    (" Private Limited", " Pvt. Ltd."),
    (" Pvt Ltd", " Private Limited"),
    (" L.L.P.", " LLP"),
    (" LLP", " L.L.P."),
    (" & Co", " and Company"),
)
ADDRESS_SWAP = (("Road", "Rd."), ("Industrial", "Indl."), ("Estate", "Est."), (",", ""))


def _mr(file_id: str, etype: str, row: int, values: dict[str, str | None]) -> MasterRecord:
    return MasterRecord(file_id, etype, row, tuple(sorted((k, v) for k, v in values.items())))


def load_p5_masters(ref: Path) -> list[MasterRecord]:
    out = []
    for etype, (fname, mapping) in P5_FILES.items():
        with (ref / fname).open(encoding="utf-8", newline="") as f:
            for i, row in enumerate(csv.DictReader(f), 1):
                out.append(
                    _mr(f"MF-p5{etype[:9]:0<9}", etype, i, {k: row[c] or None for k, c in mapping.items()})
                )
    return out


def _vary(rec: MasterRecord, cat: str, rng: random.Random) -> dict[str, str | None]:
    v = dict(rec.values)
    name = v["name"] or ""
    if cat == "case_punctuation":
        v["name"] = name.upper().replace(" ", "  ", 1) + "."
        if v.get("phone"):
            v["phone"] = v["phone"].replace("-", " ")
        if v.get("email"):
            v["email"] = v["email"].upper()
    elif cat == "legal_suffix_name_only":
        for a, b in SUFFIX_SWAP:
            if name.endswith(a):
                v["name"] = name[: -len(a)] + b
                break
        else:
            v["name"] = name + " Pvt Ltd" if rec.entity_type != "employee" else name.upper()
    elif cat in ("spelling_with_tax_id", "spelling_name_only"):
        words = name.split()
        j = max(range(len(words)), key=lambda k: (len(words[k]), -k))
        w = words[j]
        p = 1 + rng.randrange(max(1, len(w) - 2))
        words[j] = w[:p] + w[p + 1 :] if rng.random() < 0.5 else w[:p] + w[p] + w[p:]
        v["name"] = " ".join(words)
    elif cat == "address_format":
        addr = v.get("address") or ""
        for a, b in ADDRESS_SWAP:
            addr = addr.replace(a, b)
        v["address"] = addr.lower()
        v["name"] = name.lower()
    elif cat == "shared_contact_name_variant":
        words = name.split()
        v["name"] = " ".join(words[:-1] + [words[-1].upper()]) if len(words) > 1 else name.lower()
    v["record_id"] = (v.get("record_id") or "") + f"~{cat}"
    if cat in _KEEP:
        v = {k: x for k, x in v.items() if k in _KEEP[cat]}
    return v


def variants(base: list[MasterRecord], every: int = 3, seed: int = 12) -> tuple[list[MasterRecord], dict]:
    rng = random.Random(seed)  # noqa: S311 — deterministic test data
    out, cat_of = [], {}
    for i, rec in enumerate(base):
        if i % every:
            continue
        cat = CATEGORIES[(i // every) % len(CATEGORIES)]
        if rec.entity_type == "employee" and cat == "legal_suffix_name_only":
            cat = "case_punctuation"  # persons have no legal form
        needs = {
            "shared_contact_name_variant": "bank_account",
            "spelling_with_tax_id": "tax_id",
            "address_format": "address",
        }
        if cat in needs and not rec.get(needs[cat]):
            cat = "exact_duplicate"
        v = (
            _vary(rec, cat, rng)
            if cat != "exact_duplicate"
            else dict(rec.values) | {"record_id": (rec.get("record_id") or "") + "~exact_duplicate"}
        )
        m = _mr("MF-variants000", rec.entity_type, i + 1, v)
        out.append(m)
        cat_of[records.master_key(m)] = (cat, records.master_key(rec))
    return out, cat_of


ADVERSARIAL: list[tuple[str, str, dict[str, str]]] = [
    (
        "trading_vs_traders",
        "vendor",
        {
            "name": "ABC Trading Ltd",
            "tax_id": "ADV-TX-0001",
            "bank_account": "ADV000000001",
            "phone": "+91-02000-00001",
            "address": "12 Market Road, Pune 411001",
        },
    ),
    (
        "trading_vs_traders",
        "vendor",
        {
            "name": "ABC Traders Ltd",
            "tax_id": "ADV-TX-0002",
            "bank_account": "ADV000000002",
            "phone": "+91-02000-00002",
            "address": "88 Station Road, Nashik 422001",
        },
    ),
    (
        "same_name_two_cities",
        "vendor",
        {
            "name": "Northwind Logistics Pvt Ltd",
            "city": "Pune",
            "address": "5 Ring Road, Pune 411014",
            "tax_id": "ADV-TX-0003",
            "bank_account": "ADV000000003",
        },
    ),
    (
        "same_name_two_cities",
        "vendor",
        {
            "name": "Northwind Logistics Private Limited",
            "city": "Chennai",
            "address": "71 Mount Road, Chennai 600002",
            "tax_id": "ADV-TX-0004",
            "bank_account": "ADV000000004",
        },
    ),
    (
        "same_name_two_cities_no_ids",
        "customer",
        {"name": "Lotus Foods Pvt Ltd", "city": "Indore", "address": "3 MG Road, Indore 452001"},
    ),
    (
        "same_name_two_cities_no_ids",
        "customer",
        {"name": "Lotus Foods Pvt Ltd", "city": "Kochi", "address": "19 Marine Drive, Kochi 682031"},
    ),
    (
        "common_employee_name",
        "employee",
        {
            "name": "Priya Sharma",
            "email": "priya.sharma1@corp.example",
            "bank_account": "ADV000000005",
            "tax_id": "ADV-TX-0005",
        },
    ),
    (
        "common_employee_name",
        "employee",
        {
            "name": "Priya Sharma",
            "email": "priya.sharma2@corp.example",
            "bank_account": "ADV000000006",
            "tax_id": "ADV-TX-0006",
        },
    ),
    (
        "generic_email_domain",
        "vendor",
        {"name": "Kiran Electricals", "email": "kiranelec@gmail.com", "tax_id": "ADV-TX-0007"},
    ),
    (
        "generic_email_domain",
        "vendor",
        {"name": "Shree Ganesh Hardware", "email": "sghardware@gmail.com", "tax_id": "ADV-TX-0008"},
    ),
    (
        "shared_office_address",
        "vendor",
        {
            "name": "Apex Consulting LLP",
            "address": "Unit 4, Business Centre, MG Road, Pune 411001",
            "tax_id": "ADV-TX-0009",
        },
    ),
    (
        "shared_office_address",
        "vendor",
        {
            "name": "Zenith Advisory LLP",
            "address": "Unit 4, Business Centre, MG Road, Pune 411001",
            "tax_id": "ADV-TX-0010",
        },
    ),
    (
        "shared_office_address",
        "vendor",
        {
            "name": "Orbit Tax Services LLP",
            "address": "Unit 4 Business Centre MG Rd Pune 411001",
            "tax_id": "ADV-TX-0011",
        },
    ),
    (
        "shared_bank_code",
        "vendor",
        {
            "name": "Sterling Paper Mills Pvt Ltd",
            "bank_account": "ADV000000012",
            "bank_code": "SBIN0000001",
            "tax_id": "ADV-TX-0012",
        },
    ),
    (
        "shared_bank_code",
        "vendor",
        {
            "name": "Sterling Papers Pvt Ltd",
            "bank_account": "ADV000000013",
            "bank_code": "SBIN0000001",
            "tax_id": "ADV-TX-0013",
        },
    ),
    ("shared_switchboard_phone", "vendor", {"name": "Tower A Facilities", "phone": "+91-022-4000-0000"}),
    (
        "shared_switchboard_phone",
        "vendor",
        {"name": "Tower A Security Services", "phone": "+91 022 40000000"},
    ),
    ("shared_switchboard_phone", "vendor", {"name": "Tower A Canteen", "phone": "02240000000"}),
    ("similar_names_conflicting_tax", "vendor", {"name": "Meridian Steel Pvt Ltd", "tax_id": "ADV-TX-0014"}),
    ("similar_names_conflicting_tax", "vendor", {"name": "Meridien Steel Pvt Ltd", "tax_id": "ADV-TX-0015"}),
]


def adversarial() -> tuple[list[MasterRecord], dict[str, str]]:
    out, case_of = [], {}
    for i, (case, etype, vals) in enumerate(ADVERSARIAL, 1):
        m = _mr("MF-adversarial", etype, i, {"record_id": f"ADV{i:03d}", **vals})
        out.append(m)
        case_of[records.master_key(m)] = case
    return out, case_of


def ledger_parties(ref: Path) -> tuple[list, dict[str, set[str]]]:
    """P5 ledger names as party records (canonical-like) and, for scoring only, their counterparty IDs."""

    class Row:
        __slots__ = ("payer", "payee", "reference", "account", "amount")

    rows, truth = [], defaultdict(set)
    with (ref / "SYNTHETIC_ledger.csv").open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            x = Row()
            x.payer, x.payee = party_key(r["Payer"]), party_key(r["Payee"])
            x.reference, x.account = r["Invoice No"] or None, r["GL Account"] or None
            x.amount = round(float(r["Amount"]) * 100) if r["Amount"] else None
            rows.append(x)
            cp = x.payee if r["Dr/Cr"] == "Dr" else x.payer
            if cp and r["Counterparty ID"]:
                truth[cp].add(r["Counterparty ID"])
    from ledgerspy.resolution.ledger import parties

    return parties(rows, account_codes=False), truth


def score(res: engine.Resolution, truth: dict[str, str], cand_pairs: set[tuple[str, str]]) -> dict[str, Any]:
    keys = sorted(truth)
    by_truth: dict[str, list[str]] = defaultdict(list)
    for k in keys:
        by_truth[truth[k]].append(k)
    true_pairs = {(a, b) for ms in by_truth.values() for a, b in combinations(sorted(ms), 2)}
    same_cluster = set()
    by_cluster: dict[str, list[str]] = defaultdict(list)
    for k in keys:
        by_cluster[res.entity_of[k]].append(k)
    for ms in by_cluster.values():
        same_cluster |= set(combinations(sorted(ms), 2))
    false_merge = sorted(p for p in same_cluster if truth[p[0]] != truth[p[1]])
    split = sorted(true_pairs - same_cluster)
    status = {}
    for p in res.pairs.values():
        status[(p["a"], p["b"])] = p["status"]
    in_review = [p for p in split if status.get(p) in matching.REVIEW]
    cand_true = true_pairs & cand_pairs
    return {
        "records": len(keys),
        "true_pairs": len(true_pairs),
        "candidate_recall": round(len(cand_true) / len(true_pairs), 4) if true_pairs else None,
        "same_cluster_pairs": len(same_cluster),
        "auto_match_precision": round(1 - len(false_merge) / len(same_cluster), 4) if same_cluster else None,
        "false_merges": len(false_merge),
        "false_merge_pairs": false_merge[:20],
        "false_merge_clusters": sum(1 for ms in by_cluster.values() if len({truth[m] for m in ms}) > 1),
        "false_splits": len(split),
        "false_splits_left_for_review": len(in_review),
        "false_splits_missed": len(split) - len(in_review),
        "merged_true_pairs": len(true_pairs & same_cluster),
    }


def run_validation(cfg: ResolutionConfig, ref: Path, ground_truth: Path | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    base = load_p5_masters(ref)
    var, cat_of = variants(base)
    adv, case_of = adversarial()
    parties, ledger_truth = ledger_parties(ref)
    recs = [records.from_master(m, cfg) for m in base + var + adv] + parties
    res = engine.resolve(recs, cfg)
    cand_pairs = {(p["a"], p["b"]) for p in res.pairs.values()}

    # Truth (evaluation only). Master records: own record ID; variants: their base; vendor/customer records of
    # one company (the same tax ID in the masters themselves) form one entity.
    truth: dict[str, str] = {}
    tax_entity: dict[str, str] = {}
    for m in base:
        k = records.master_key(m)
        tid = (m.get("tax_id") or "").strip()
        if tid and m.entity_type in ("vendor", "customer", "group_company"):
            tax_entity.setdefault(tid, k)
            truth[k] = tax_entity[tid]
        else:
            truth[k] = k
    for k, (_, b) in cat_of.items():
        truth[k] = truth[b]
    for m in adv:
        k = records.master_key(m)
        truth[k] = k
    overall = score(res, truth, cand_pairs)

    per_cat = {}
    for cat in CATEGORIES:
        ks = [k for k, (c, _) in cat_of.items() if c == cat]
        ok = sum(1 for k in ks if res.entity_of[k] == res.entity_of[cat_of[k][1]])
        review = sum(
            1
            for k in ks
            if res.entity_of[k] != res.entity_of[cat_of[k][1]]
            and any(
                res.pairs[pid]["status"] in matching.REVIEW
                and cat_of[k][1] in (res.pairs[pid]["a"], res.pairs[pid]["b"])
                for pid in res.pairs_of.get(k, [])
            )
        )
        per_cat[cat] = {
            "variants": len(ks),
            "expected": "merged" if EXPECTED_MERGE[cat] else "review (not merged)",
            "merged": ok,
            "left_for_review": review,
            "missed": len(ks) - ok - review,
        }

    adv_out = {}
    for case in dict.fromkeys(c for c, _, _ in ADVERSARIAL):
        ks = [k for k, c in case_of.items() if c == case]
        clusters = {res.entity_of[k] for k in ks}
        statuses = sorted(
            {
                res.pairs[pid]["status"]
                for k in ks
                for pid in res.pairs_of.get(k, [])
                if {res.pairs[pid]["a"], res.pairs[pid]["b"]} <= set(ks)
            }
        )
        adv_out[case] = {"records": len(ks), "merged": len(clusters) < len(ks), "pair_statuses": statuses}

    # Ledger names → master records (scored against the ledger's unmapped Counterparty ID column).
    master_id = {records.master_key(m): m.get("record_id") for m in base}
    linked = correct = wrong = 0
    with_truth = 0
    unlinked = []
    for p in parties:
        ids = ledger_truth.get(p.key[2:])
        if not ids:
            continue
        with_truth += 1
        members = res.entity(res.entity_of[p.key])["members"]
        got = {master_id[m] for m in members if m in master_id}
        if got:
            linked += 1
            if got & ids:
                correct += 1
            else:
                wrong += 1
        else:
            unlinked.append(
                {
                    "ledger_name": p.key[2:],
                    "counterparty_ids": sorted(ids),
                    "status": res.record_status[p.key],
                }
            )
    ledger_links = {
        "ledger_names_with_counterparty_id": with_truth,
        "linked_to_a_master_record": linked,
        "correct_links": correct,
        "wrong_links": wrong,
        "link_precision": round(correct / linked, 4) if linked else None,
        "link_recall": round(correct / with_truth, 4) if with_truth else None,
        "not_linked": unlinked,
    }

    relations = None
    if ground_truth is not None:
        relations = _p5_relations(res, base, ground_truth)

    # Blocking vs all pairs on the master records (recall of blocking; exhaustive comparison is cheap here).
    master_keys = sorted(truth)
    exhaustive_true = {(a, b) for a, b in combinations(master_keys, 2) if truth[a] == truth[b]}
    return {
        "resolution_sha256": res.sha256,
        "records": res.stats["records_by_source"] | {"variants": len(var), "adversarial": len(adv)},
        "candidates": {
            "possible_pairs": res.stats["possible_pairs"],
            "candidate_pairs": res.stats["candidate_pairs"],
            "reduction_ratio": res.stats["candidate_reduction_ratio"],
            "true_pairs_found_by_blocking": len(exhaustive_true & cand_pairs),
            "true_pairs": len(exhaustive_true),
        },
        "pairs_by_status": res.stats["pairs_by_status"],
        "ambiguous_rate": round(
            sum(res.stats["pairs_by_status"][s] for s in matching.REVIEW)
            / max(1, res.stats["candidate_pairs"]),
            4,
        ),
        "master_records": overall,
        "variant_categories": per_cat,
        "adversarial": adv_out,
        "ledger_links": ledger_links,
        "p5_relations": relations,
        "blocked_merges": len(res.blocked_merges),
        "timings_seconds": res.stats["timings_seconds"]
        | {"validation_total": round(time.perf_counter() - t0, 3)},
    }


def _p5_relations(res: engine.Resolution, base: list[MasterRecord], gt_path: Path) -> dict[str, Any]:
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    key_of = {(m.entity_type, m.get("record_id")): records.master_key(m) for m in base}
    out = {}
    for s in gt["scenarios"]:
        if s["scenario_id"] not in (
            "S-E-01",
            "S-E-02",
            "S-P-01",
            "S-N-04",
            "S-F-01",
            "S-M-01",
            "S-M-02",
            "S-N-01",
            "S-N-02",
            "S-N-03",
            "S-N-05",
        ):
            continue
        a = s["affected"]
        ks = [key_of.get(("vendor", v)) for v in a["vendor_ids"]] + [
            key_of.get(("employee", e)) for e in a["employee_ids"]
        ]
        ks = [k for k in ks if k]
        pair_st = sorted(
            {
                res.pairs[pid]["status"]
                for k in ks
                for pid in res.pairs_of.get(k, [])
                if {res.pairs[pid]["a"], res.pairs[pid]["b"]} <= set(ks)
            }
        )
        out[s["scenario_id"]] = {
            "type": s["scenario_type"],
            "records": len(ks),
            "merged": len({res.entity_of[k] for k in ks}) < len(ks),
            "surfaced": bool(
                set(pair_st) & {"conflicting_identity", "shared_attribute", "probable_match", "ambiguous"}
            ),
            "pair_statuses": pair_st,
        }
    return out


def population(n: int, seed: int = 5) -> list[MasterRecord]:
    """A larger synthetic organisation population for performance tests: n base records + ~10% variants."""
    rng = random.Random(seed)  # noqa: S311
    syl = ["ka", "ro", "vel", "tan", "mor", "zen", "lu", "dra", "pi", "ser", "qua", "nor", "bel", "ix", "ost"]
    trades = [
        "Steel",
        "Cartons",
        "Logistics",
        "Foods",
        "Textiles",
        "Chemicals",
        "Paper",
        "Plastics",
        "Motors",
        "Agro",
    ]
    forms = ["Pvt Ltd", "LLP", "Private Limited", "& Co", "Ltd"]
    out = []
    for i in range(n):
        stem = "".join(rng.choice(syl) for _ in range(rng.randint(2, 4))).capitalize()
        name = f"{stem} {rng.choice(trades)} {rng.choice(forms)}"
        vals = {
            "record_id": f"P{i:06d}",
            "name": name,
            "tax_id": f"PT{i:08d}",
            "bank_account": f"BA{rng.randrange(10**11):011d}",
            "phone": f"+91-0{rng.randint(20, 99)}-{rng.randrange(10**7):07d}",
            "address": f"Plot {rng.randint(1, 400)}, Sector {rng.randint(1, 60)}, City{rng.randint(1, 80)} "
            f"{rng.randint(400000, 799999)}",
        }
        out.append(_mr("MF-population0", "vendor", i + 1, vals))
        if i % 10 == 0:
            v = dict(vals)
            v["record_id"] += "~v"
            v["name"] = name.upper().replace(" PVT LTD", " PRIVATE LIMITED")
            out.append(_mr("MF-population1", "vendor", i + 1, v))
    return out


LEDGER_VARIANTS = ("legal_suffix", "upper_case", "punctuation", "typo")


def ledger_with_name_variants(src: Path, dst: Path, every: int = 9) -> dict[str, Any]:
    """Copy the P5 ledger, re-spelling the counterparty name on every 2nd row of every `every`-th vendor.

    The variant kind cycles through LEDGER_VARIANTS (a typo is name-only evidence: it must stay apart).
    Returns, for scoring only, the vendor → variant kind map; the ledger's own `Counterparty ID` column is
    the truth.
    """
    with src.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    vendors = sorted({r["Counterparty ID"] for r in rows if r["Counterparty ID"].startswith("V")})
    chosen = {v: LEDGER_VARIANTS[i % len(LEDGER_VARIANTS)] for i, v in enumerate(vendors[::every])}
    seen: dict[str, int] = defaultdict(int)
    for r in rows:
        v = r["Counterparty ID"]
        if v not in chosen or r["Dr/Cr"] != "Dr":
            continue
        seen[v] += 1
        if seen[v] % 2:
            continue
        name, kind = r["Payee"], chosen[v]
        if kind == "legal_suffix":
            for a, b in SUFFIX_SWAP:
                if name.endswith(a):
                    name = name[: -len(a)] + b
                    break
            else:
                name += " Ltd"
        elif kind == "upper_case":
            name = name.upper()
        elif kind == "punctuation":
            name = name.replace(" ", ". ", 1)
        else:
            w = name.split()
            w[0] = w[0][:2] + w[0][3:] if len(w[0]) > 4 else w[0] + "e"
            name = " ".join(w)
        r["Payee"] = name
    with dst.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return {"variant_vendors": chosen}


def grouping_quality(entities: list[dict[str, Any]], stream_truth: dict[str, set[str]]) -> dict[str, Any]:
    """False merges / splits of P9 entities against a stream → true counterparty ID map (scoring only).

    P9 entities are per flow (a group company paid and paying is two entities by design), so a split is a
    counterparty ID spread over several entities OF ONE FLOW.
    """
    merged_wrong = 0
    owner: dict[tuple[str, str], set[str]] = defaultdict(set)
    for e in entities:
        ids = set().union(*(stream_truth.get(s, set()) for s in e["member_streams"]))
        if len(ids) > 1:
            merged_wrong += 1
        for i in ids:
            owner[(e["flow"], i)].add(e["entity_id"])
    split = sorted(f"{f}:{i}" for (f, i), es in owner.items() if len(es) > 1)
    return {
        "entities": len(entities),
        "false_merge_entities": merged_wrong,
        "split_counterparties": len(split),
        "split_counterparty_ids": split[:20],
    }
