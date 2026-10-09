"""Entity resolution for one canonicalised dataset (read-only): the API / CLI view of the P6 resolution run.

The resolution is the SAME run the P6 resolution features and the P9 entity grouping use: it is built from
the dataset's feature input (`features.service.input_for`: canonical ledger names + the case's imported
master records) and memoised there. Results are cached by identity (dataset, data version, master records,
features configuration, resolution version). Nothing is written: canonical rows and master records are never
modified.
"""

import threading
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ledgerspy.config.loader import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.features import service as feature_service
from ledgerspy.features.data import party_key
from ledgerspy.features.resolution import resolution_of
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.normalize.view import load_view
from ledgerspy.resolution import matching, normalize
from ledgerspy.resolution.engine import RESOLUTION_VERSION, Resolution
from ledgerspy.resolution.masters import records_sha256

STATUS_ORDER = ("conflicting_identity", "needs_review", "resolved", "unique")
PAIR_ORDER = (
    "conflicting_identity",
    "probable_match",
    "ambiguous",
    "shared_attribute",
    "confirmed_match",
    "no_match",
)
UNSUPPORTED_TYPES = {
    "bank_counterparty": "bank statements are not ingested yet (P14); bank counterparties cannot be resolved",
}


@dataclass
class ResolutionResult:
    dataset_id: str
    identity: dict[str, Any]
    res: Resolution
    adapter: str
    variants: dict[str, Counter] = field(default_factory=dict)  # party key → raw spellings in the ledger
    streams: dict[str, list[str]] = field(default_factory=dict)  # party key → P6 streams (flow:key)
    phone_country_code: str = ""


_CACHE: "OrderedDict[str, ResolutionResult]" = OrderedDict()
_LOCK = threading.Lock()


def run(conn, cfg: EffectiveConfig, dataset_id: str) -> ResolutionResult:
    view = load_view(conn, dataset_id)
    masters = feature_service.masters_for(conn, dataset_id)
    ident = {
        "dataset_id": dataset_id,
        "data_version": view.data_version,
        "masters_sha256": records_sha256(masters),
        "features_config_sha256": sha256_hex(canonical_json(cfg.settings.features.model_dump(mode="json"))),
        "resolution_version": RESOLUTION_VERSION,
    }
    key = sha256_hex(canonical_json(ident))
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    inp = feature_service.input_for(conn, cfg, view)
    res = resolution_of(inp)
    variants: dict[str, Counter] = defaultdict(Counter)
    for r in view.rows:
        for f in ("payer_raw", "payee_raw"):
            raw = r.get(f)
            k = party_key(raw)
            if k:
                variants[k][raw] += 1
    streams: dict[str, set[str]] = defaultdict(set)
    for fr in inp.rows:
        if fr.cp and fr.flow:
            streams[fr.cp].add(f"{fr.flow}:{fr.cp}")
    out = ResolutionResult(
        dataset_id,
        ident | {"resolution_sha256": res.sha256},
        res,
        view.adapter,
        dict(variants),
        {k: sorted(v) for k, v in streams.items()},
        cfg.settings.features.resolution.normalization.phone_country_code,
    )
    with _LOCK:
        _CACHE[key] = out
        while len(_CACHE) > 4:
            _CACHE.popitem(last=False)
    return out


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _not_found(what: str, ident: str) -> LedgerSpyError:
    return LedgerSpyError("not_found", f"No {what} {ident!r} in this resolution.", http_status=404)


def _record(rr: ResolutionResult, key: str, full: bool = False) -> dict[str, Any]:
    r = rr.res.records[key]
    out: dict[str, Any] = {
        "record_key": key,
        "source": r.source,
        "entity_type": r.entity_type,
        "entity_class": r.entity_class,
        "name": r.name.raw,
        "canonical_entity_id": rr.res.entity_of[key],
        "record_status": rr.res.record_status[key],
    }
    if r.source == "ledger":
        pk = key[2:]
        out["roles"] = list(r.roles)
        out["transactions"] = r.behaviour.get("transactions", 0)
        out["name_variants"] = [
            {"raw": raw, "rows": n}
            for raw, n in sorted(rr.variants.get(pk, {}).items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        out["streams"] = rr.streams.get(pk, [])
    else:
        out["master_file_id"] = r.origin["master_file_id"]
        out["source_row"] = r.origin["source_row"]
        out["record_id"] = r.raw.get("record_id")
    if full:
        out["fields"] = _fields(r, rr.phone_country_code)
        if r.source == "ledger":
            out["behaviour"] = r.behaviour
    return out


def _fields(r, cc: str) -> list[dict[str, Any]]:
    """Raw value, normalised value and the rules applied, per field (normalisation re-run for display)."""
    rows = [
        {
            "field": "name",
            "raw": r.name.raw,
            "normalized": r.name.value,
            "core": r.name.core,
            "legal_form": r.name.legal_form,
            "rules": list(r.name.rules),
        }
    ]
    if r.source != "master":
        return rows
    for f, fn in (
        ("phone", lambda v: normalize.phone(v, cc)),
        ("email", normalize.email),
        ("bank_account", normalize.bank_account),
        ("bank_code", normalize.bank_code),
        ("tax_id", lambda v: normalize.tax_id(v)[0]),
        ("address", lambda v: normalize.address(v)[0]),
    ):
        raw = r.raw.get(f)
        n = fn(raw)
        rows.append({"field": f, "raw": raw, "normalized": r.norm.get(f, n.value), "rules": list(n.rules)})
    rows.append({"field": "city", "raw": r.raw.get("city"), "normalized": r.norm.get("city"), "rules": []})
    rows.append(
        {
            "field": "postal_code",
            "raw": None,
            "normalized": r.norm.get("postal_code"),
            "rules": ["extracted from the address"] if r.norm.get("postal_code") else [],
        }
    )
    return rows


def _entity_row(rr: ResolutionResult, e: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_entity_id": e["canonical_entity_id"],
        "display_name": e["display_name"],
        "entity_class": e["entity_class"],
        "entity_types": e["entity_types"],
        "resolution_status": e["resolution_status"],
        "member_count": e["member_count"],
        "master_records": e["master_records"],
        "ledger_names": e["ledger_names"],
        "confidence": e["confidence"],
        "review_pairs": len(e["review_pairs"]),
        "conflict_pairs": len(e["conflict_pairs"]),
        "links": len(e["link_pairs"]),
    }


def _pair_row(rr: ResolutionResult, p: dict[str, Any]) -> dict[str, Any]:
    return {
        "pair_id": p["pair_id"],
        "status": p["status"],
        "rule": p["rule"],
        "evidence_score": p["evidence_score"],
        "name_similarity": p["name"]["name_similarity"],
        "a": {
            "record_key": p["a"],
            "name": p["name"]["a"],
            "entity_type": p["entity_types"][0],
            "canonical_entity_id": rr.res.entity_of[p["a"]],
        },
        "b": {
            "record_key": p["b"],
            "name": p["name"]["b"],
            "entity_type": p["entity_types"][1],
            "canonical_entity_id": rr.res.entity_of[p["b"]],
        },
        "conflicts": p["conflicts"],
        "explanation": p["explanation"],
        "suggested_action": SUGGESTED[p["status"]],
    }


SUGGESTED = {
    "confirmed_match": "none: merged automatically on explicit evidence",
    "probable_match": "review: confirm the merge only with an identifier or document that proves one entity",
    "ambiguous": "review: obtain an identifier (tax ID, bank account) before treating these as one entity",
    "conflicting_identity": "investigate: identity evidence contradicts itself (possible look-alike, shared "
    "account or data error); never merge without resolving the conflict",
    "shared_attribute": "investigate the relationship (P13 graph): different entities sharing contact "
    "details, an address or an account",
    "no_match": "none",
}


def summary(rr: ResolutionResult) -> dict[str, Any]:
    res = rr.res
    masters = [r for r in res.records.values() if r.source == "master"]
    coverage = {}
    for f in ("tax_id", "bank_account", "email", "phone", "address", "postal_code", "city"):
        coverage[f] = round(sum(1 for r in masters if r.norm.get(f)) / len(masters), 4) if masters else None
    review = sum(res.stats["pairs_by_status"][s] for s in matching.REVIEW)
    paysim = rr.adapter == "paysim"
    return {
        "dataset_id": rr.dataset_id,
        "identity": rr.identity,
        "resolution_sha256": res.sha256,
        "stats": res.stats,
        "review_queue": review,
        "field_coverage": {
            "ledger_names": "name only (a ledger carries no identifiers; mapped counterparty IDs are not "
            "canonical yet)",
            "master_records": coverage,
        },
        "unsupported_entity_types": UNSUPPORTED_TYPES,
        "unavailable": (
            {
                "legal_entity_resolution": "PaySim party names are simulator account codes: only exact code "
                "identity applies; there are no legal names, addresses, contacts, bank accounts or tax IDs",
            }
            if paysim
            else {}
        )
        | (
            {}
            if masters or paysim
            else {
                "master_data": "no master data is imported for the case: ledger names "
                "are resolved by name only (confirmed only when formatting differs)"
            }
        ),
        "policy": {
            "merged": "confirmed_match only",
            "never_merged": ["probable_match", "ambiguous", "conflicting_identity", "shared_attribute"],
            "note": "Behavioural similarity is supporting evidence only. A person and an organisation are "
            "never merged. Ground truth and labels are never read.",
        },
    }


def entities_page(
    rr: ResolutionResult, page: int, size: int, status: str | None, q: str | None
) -> dict[str, Any]:
    items = rr.res.entities
    if status:
        items = [e for e in items if e["resolution_status"] == status]
    if q:
        needle = q.casefold()
        items = [
            e
            for e in items
            if needle in (e["display_name"] or "").casefold() or needle in e["canonical_entity_id"].casefold()
        ]
    items = sorted(
        items,
        key=lambda e: (
            STATUS_ORDER.index(e["resolution_status"]),
            -e["member_count"],
            (e["display_name"] or "").casefold(),
            e["canonical_entity_id"],
        ),
    )
    start = (page - 1) * size
    return {
        "total": len(items),
        "page": page,
        "size": size,
        "items": [_entity_row(rr, e) for e in items[start : start + size]],
        "resolution_sha256": rr.res.sha256,
    }


def entity_detail(rr: ResolutionResult, cid: str) -> dict[str, Any]:
    e = rr.res.entity(cid)
    if e is None:
        raise _not_found("canonical entity", cid)
    inside = set(e["members"])
    return _entity_row(rr, e) | {
        "anchor": e["anchor"],
        "members": [_record(rr, m, full=True) for m in e["members"]],
        "merge_evidence": [rr.res.pairs[p] for p in e["merge_evidence"]],
        "review": [_pair_row(rr, rr.res.pairs[p]) for p in e["review_pairs"] + e["conflict_pairs"]],
        "links": [_pair_row(rr, rr.res.pairs[p]) for p in e["link_pairs"]],
        "blocked_merges": [b for b in rr.res.blocked_merges if set(b["records"]) & inside],
        "resolution_sha256": rr.res.sha256,
    }


def pairs_page(
    rr: ResolutionResult, page: int, size: int, status: str | None, q: str | None
) -> dict[str, Any]:
    pairs = list(rr.res.pairs.values())
    if status:
        pairs = [p for p in pairs if p["status"] == status]
    else:  # default: the review queue and links (no_match / confirmed are available by status filter)
        pairs = [p for p in pairs if p["status"] in (*matching.REVIEW, "shared_attribute")]
    if q:
        needle = q.casefold()
        pairs = [
            p
            for p in pairs
            if needle in (p["name"]["a"] or "").casefold() or needle in (p["name"]["b"] or "").casefold()
        ]
    pairs.sort(key=lambda p: (PAIR_ORDER.index(p["status"]), -p["evidence_score"], p["pair_id"]))
    start = (page - 1) * size
    return {
        "total": len(pairs),
        "page": page,
        "size": size,
        "items": [_pair_row(rr, p) for p in pairs[start : start + size]],
        "resolution_sha256": rr.res.sha256,
    }


def pair_detail(rr: ResolutionResult, pid: str) -> dict[str, Any]:
    p = rr.res.pairs.get(pid)
    if p is None:
        raise _not_found("candidate pair", pid)
    return {
        **p,
        "suggested_action": SUGGESTED[p["status"]],
        "records": [_record(rr, p["a"], full=True), _record(rr, p["b"], full=True)],
        "resolution_sha256": rr.res.sha256,
    }
