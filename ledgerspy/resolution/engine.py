"""Entity resolution run: blocking → pair evidence and decisions → safe clustering → canonical entities.

1. Records are sorted by key, so input order never matters.
2. Blocking (docs/entity-resolution.md §4). Each record gets block keys:
   - name keys: the first `name_prefix_chars` letters of the core name, every core word of ≥ 3 letters, the
     sorted words, and a consonant skeleton of the first word;
   - identifier keys: tax ID, PAN, bank account, e-mail, phone;
   - a non-generic e-mail domain and the postal code.
   Records sharing a key are candidate pairs. Name and address blocks larger than `max_block_size` are not
   expanded (reported). Identifier blocks are always expanded: a value shared by many records is evidence
   that must stay visible. At or below `exhaustive_max_records` every pair is compared.
3. Every candidate pair is scored and decided (`matching.evaluate`). Two follow-up rules use master data:
   - a ledger name linked exactly to a master record is not also a review candidate for other master records;
   - two ledger names exactly linked to master records take the decision of those master records (a look-alike
     vendor's ledger name stays apart from the original's).
4. Clustering. Confirmed matches are merged in descending evidence order, but a merge of two groups happens
   only when no cross pair contradicts it. Each cross pair is evaluated, even if blocking skipped it, and
   the
   merge is refused (recorded as a blocked merge) when a cross pair:
   - has a hard identifier conflict;
   - is `conflicting_identity`;
   - joins a person and an organisation;
   - has names below `review_name` without a shared identifier.
   So A≈B and B≈C never put A and C together on their own.
5. Canonical entity ID = "CE-" + 16 hex digits of SHA-256 of the cluster's anchor record key. The anchor is
   the cluster's first master record by key, else its first ledger name. The ID is stable while the anchor
   stays in the cluster, whatever else is added.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ledgerspy.config.schema import ResolutionConfig
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.resolution import matching
from ledgerspy.resolution.records import PartyRecord

RESOLUTION_VERSION = 1
# When ledger names inherit the decision of their master records, the most cautious decision wins.
CAUTION = (
    "conflicting_identity",
    "shared_attribute",
    "no_match",
    "ambiguous",
    "probable_match",
    "confirmed_match",
)
_VOWELS = set("aeiou")


@dataclass
class Resolution:
    records: dict[str, PartyRecord]
    pairs: dict[str, dict[str, Any]]  # scored candidate pairs by pair ID
    entities: list[dict[str, Any]]
    entity_of: dict[str, str]  # record key → canonical entity ID
    record_status: dict[str, str]
    blocked_merges: list[dict[str, Any]]
    stats: dict[str, Any]
    sha256: str
    pairs_of: dict[str, list[str]] = field(default_factory=dict)  # record key → pair IDs

    def entity(self, canonical_id: str) -> dict[str, Any] | None:
        return next((e for e in self.entities if e["canonical_entity_id"] == canonical_id), None)


def _skeleton(word: str) -> str:
    return (word[0] + "".join(c for c in word[1:] if c not in _VOWELS))[:4] if word else ""


def block_keys(r: PartyRecord, cfg: ResolutionConfig) -> tuple[list[str], list[str]]:
    """(name/address keys, identifier keys)."""
    if r.entity_class == "account_code":
        return [], [f"code:{r.name.value}"] if r.name.value else []
    soft, hard = [], []
    core = r.name.core
    if core:
        compact = core.replace(" ", "")
        if len(compact) >= 3:
            soft.append("pfx:" + compact[: cfg.blocking.name_prefix_chars])
        soft += [f"tok:{t}" for t in sorted(set(r.name.tokens)) if len(t) >= 3]
        soft.append("sorted:" + " ".join(r.name.tokens_sorted))
        sk = _skeleton(r.name.tokens[0]) if r.name.tokens else ""
        if len(sk) >= 3:
            soft.append("skel:" + sk)
    n = r.norm
    for f in ("tax_id", "pan", "bank_account", "email", "phone"):
        if n.get(f):
            hard.append(f"id:{f}:{n[f]}")
    if n.get("email"):
        dom = n["email"].split("@", 1)[1]
        if dom not in cfg.normalization.generic_email_domains:
            soft.append("dom:" + dom)
    if n.get("postal_code"):
        soft.append("pin:" + n["postal_code"])
    return sorted(set(soft)), sorted(set(hard))


def candidates(records: list[PartyRecord], cfg: ResolutionConfig) -> tuple[list[tuple[str, str]], dict]:
    keys = [r.key for r in records]
    n = len(keys)
    if n <= cfg.blocking.exhaustive_max_records:
        pairs = [(keys[i], keys[j]) for i in range(n) for j in range(i + 1, n)]
        return pairs, {"mode": "exhaustive", "blocks": 0, "skipped_blocks": []}
    blocks: dict[str, list[str]] = defaultdict(list)
    hard_keys = set()
    for r in records:
        soft, hard = block_keys(r, cfg)
        for k in soft:
            blocks[k].append(r.key)
        for k in hard:
            blocks[k].append(r.key)
            hard_keys.add(k)
    pairs: set[tuple[str, str]] = set()
    skipped = []
    for k in sorted(blocks):
        members = sorted(set(blocks[k]))
        if len(members) < 2:
            continue
        if k not in hard_keys and len(members) > cfg.blocking.max_block_size:
            skipped.append({"key": k, "size": len(members)})
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))
    return sorted(pairs), {
        "mode": "blocking",
        "blocks": sum(1 for v in blocks.values() if len(set(v)) >= 2),
        "skipped_blocks": skipped,
    }


def _context(records: list[PartyRecord], cfg: ResolutionConfig) -> matching.Context:
    ctx = matching.Context(cfg)
    names: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in records:
        for f in ("email", "phone"):
            if r.norm.get(f):
                names[(f, r.norm[f])].add(r.name.core or "")
    ctx.contact_names = dict(names)
    by_core: dict[str, list[PartyRecord]] = defaultdict(list)
    for r in records:
        if r.source == "master" and r.name.core:
            by_core[r.name.core].append(r)
    for core, rs in by_core.items():
        consistent = True
        for f in ("tax_id", "bank_account"):
            vals = {r.norm.get(f) for r in rs if r.norm.get(f)}
            if len(vals) > 1:
                consistent = False
        classes = {r.entity_class for r in rs}
        # Organisations with one core name and no differing tax ID / bank account are one identity. Persons
        # share common names, so several person records count as one identity only when every record carries
        # the same tax ID or the same bank account.
        if len(rs) == 1:
            unique = True
        elif classes == {"organisation"}:
            unique = consistent
        elif classes == {"person"}:
            unique = consistent and any(
                all(r.norm.get(f) for r in rs) and len({r.norm.get(f) for r in rs}) == 1
                for f in ("tax_id", "bank_account")
            )
        else:
            unique = False
        ctx.unique_master_core[core] = unique
    return ctx


def resolve(records: list[PartyRecord], cfg: ResolutionConfig) -> Resolution:
    t0 = time.perf_counter()
    recs = sorted(records, key=lambda r: r.key)
    by_key = {r.key: r for r in recs}
    if len(by_key) != len(recs):
        raise ValueError("duplicate record keys")
    ctx = _context(recs, cfg)
    cand, block_info = candidates(recs, cfg)
    t_block = time.perf_counter() - t0

    pairs: dict[str, dict[str, Any]] = {}
    cache: dict[tuple[str, str], dict[str, Any]] = {}

    def ev(x: str, y: str) -> dict[str, Any]:
        k = (x, y) if x < y else (y, x)
        if k not in cache:
            cache[k] = matching.evaluate(by_key[k[0]], by_key[k[1]], ctx)
        return cache[k]

    for x, y in cand:
        p = ev(x, y)
        pairs[p["pair_id"]] = p
    t_score = time.perf_counter() - t0 - t_block

    # Follow-up rules that use master data (see module docstring, step 3).
    linked: dict[str, set[str]] = defaultdict(set)
    for p in pairs.values():
        a, b = by_key[p["a"]], by_key[p["b"]]
        if p["status"] == "confirmed_match" and {a.source, b.source} == {"ledger", "master"}:
            led, mas = (a, b) if a.source == "ledger" else (b, a)
            linked[led.key].add(mas.key)
    for p in pairs.values():
        a, b = by_key[p["a"]], by_key[p["b"]]
        if {a.source, b.source} == {"ledger", "master"} and p["status"] in matching.REVIEW:
            led, mas = (a, b) if a.source == "ledger" else (b, a)
            if linked[led.key] and mas.key not in linked[led.key]:
                p["assessment_before_rule"] = p["status"]
                p["status"], p["rule"] = "no_match", "ledger_name_linked_exactly_to_another_master_record"
                p["explanation"] = (
                    "No match: the ledger name is linked exactly to another master record ("
                    + ", ".join(sorted(linked[led.key]))
                    + f"). Name-only assessment before this rule: {p['assessment_before_rule']}."
                )
        elif a.source == b.source == "ledger" and linked[a.key] and linked[b.key]:
            decided = [ev(m1, m2) for m1 in sorted(linked[a.key]) for m2 in sorted(linked[b.key]) if m1 != m2]
            if decided:
                worst = min(decided, key=lambda d: (CAUTION.index(d["status"]), d["pair_id"]))
                p["assessment_before_rule"] = p["status"]
                p["status"], p["rule"] = worst["status"], "decided_by_linked_master_records"
                p["decided_by"] = worst["pair_id"]
                p["explanation"] = (
                    f"Decided by the master records these ledger names are linked to ({worst['pair_id']}): "
                    + worst["explanation"]
                )
            elif linked[a.key] == linked[b.key] and p["status"] != "confirmed_match":
                p["assessment_before_rule"] = p["status"]
                p["status"], p["rule"] = "confirmed_match", "linked_to_the_same_master_record"
                p["explanation"] = (
                    "Confirmed match because both ledger names are linked exactly to the same master "
                    "record "
                    f"({', '.join(sorted(linked[a.key]))}). Name-only assessment: "
                    f"{p['assessment_before_rule']}."
                )

    # Safe clustering.
    th = cfg.thresholds
    parent = {k: k for k in by_key}
    members = {k: [k] for k in by_key}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    blocked = []
    edges = sorted(
        (p for p in pairs.values() if p["status"] == "confirmed_match"),
        key=lambda p: (-p["evidence_score"], p["pair_id"]),
    )
    used_edges: dict[str, list[str]] = defaultdict(list)
    for p in edges:
        ra, rb = find(p["a"]), find(p["b"])
        if ra == rb:
            used_edges[ra].append(p["pair_id"])
            continue
        reason = None
        for x in members[ra]:
            for y in members[rb]:
                c = ev(x, y)
                bad = (
                    c["status"] == "conflicting_identity"
                    or "conflict" in (c["identifiers"]["tax_id"], c["identifiers"]["bank_account"])
                    or {by_key[x].entity_class, by_key[y].entity_class} == {"person", "organisation"}
                    or (
                        c["name"]["name_similarity"] < th.review_name
                        and not c["name"]["same_core_name"]
                        and "match" not in c["identifiers"].values()
                    )
                )
                if bad:
                    reason = {"cross_pair": c["pair_id"], "cross_status": c["status"], "records": [x, y]}
                    break
            if reason:
                break
        if reason:
            blocked.append({"edge": p["pair_id"], "records": [p["a"], p["b"]], **reason})
            continue
        parent[rb] = ra
        members[ra] = sorted(members[ra] + members[rb])
        used_edges[ra] += used_edges.pop(rb, []) + [p["pair_id"]]
        del members[rb]

    pairs_of: dict[str, list[str]] = defaultdict(list)
    for pid, p in sorted(pairs.items()):
        pairs_of[p["a"]].append(pid)
        pairs_of[p["b"]].append(pid)

    entities = []
    entity_of: dict[str, str] = {}
    record_status: dict[str, str] = {}
    for k in sorted({find(k) for k in by_key}):
        mem = members[k]
        anchor = min(mem, key=lambda m: (by_key[m].source != "master", m))
        cid = "CE-" + sha256_hex(anchor)[:16]
        for m in mem:
            entity_of[m] = cid
        entities.append({"canonical_entity_id": cid, "anchor": anchor, "members": mem, "_root": k})

    for e in entities:
        mem = e["members"]
        inside = set(mem)
        review, conflicts, links = [], [], []
        for m in mem:
            for pid in pairs_of.get(m, []):
                p = pairs[pid]
                other = p["b"] if p["a"] == m else p["a"]
                if other in inside:
                    continue
                if p["status"] in matching.REVIEW:
                    (conflicts if p["status"] == "conflicting_identity" else review).append(pid)
                if p["status"] in ("shared_attribute", "conflicting_identity"):
                    links.append(pid)
        edges_used = sorted(set(used_edges.get(e["_root"], [])))
        anchor = by_key[e["anchor"]]
        ledger = [by_key[m] for m in mem if by_key[m].source == "ledger"]
        display = anchor.name.raw if anchor.source == "master" else None
        if display is None:
            display = sorted(ledger, key=lambda r: (-r.behaviour.get("transactions", 0), r.key))[0].raw[
                "name"
            ]
        if conflicts:
            status = "conflicting_identity"
        elif review:
            status = "needs_review"
        elif len(mem) > 1:
            status = "resolved"
        else:
            status = "unique"
        e.pop("_root")
        e.update(
            {
                "display_name": display,
                "entity_class": sorted({by_key[m].entity_class for m in mem} - {"unknown"} or {"unknown"})[0],
                "entity_types": sorted({by_key[m].entity_type for m in mem}),
                "member_count": len(mem),
                "master_records": sum(1 for m in mem if by_key[m].source == "master"),
                "ledger_names": len(ledger),
                "resolution_status": status,
                "confidence": min((pairs[x]["evidence_score"] for x in edges_used), default=None),
                "merge_evidence": edges_used,
                "review_pairs": sorted(set(review)),
                "conflict_pairs": sorted(set(conflicts)),
                "link_pairs": sorted(set(links)),
            }
        )
        for m in mem:
            mine = [
                pairs[x]
                for x in pairs_of.get(m, [])
                if (pairs[x]["b"] if pairs[x]["a"] == m else pairs[x]["a"]) not in inside
            ]
            if any(p["status"] == "conflicting_identity" for p in mine):
                record_status[m] = "conflicting_identity"
            elif any(p["status"] in matching.REVIEW for p in mine):
                record_status[m] = "needs_review"
            else:
                record_status[m] = "resolved" if len(mem) > 1 else "unique"

    by_status = defaultdict(int)
    for p in pairs.values():
        by_status[p["status"]] += 1
    n = len(recs)
    possible = n * (n - 1) // 2
    stats = {
        "records": n,
        "records_by_source": dict(sorted(_count(r.source for r in recs).items())),
        "records_by_type": dict(sorted(_count(r.entity_type for r in recs).items())),
        "records_by_class": dict(sorted(_count(r.entity_class for r in recs).items())),
        "possible_pairs": possible,
        "candidate_pairs": len(cand),
        "candidate_reduction_ratio": round(1 - len(cand) / possible, 6) if possible else None,
        "blocking": block_info,
        "pairs_by_status": {s: by_status.get(s, 0) for s in matching.STATUSES},
        "extra_pair_evaluations_for_merge_safety": len(cache) - len(cand),
        "entities": len(entities),
        "multi_record_entities": sum(1 for e in entities if e["member_count"] > 1),
        "entities_by_status": dict(sorted(_count(e["resolution_status"] for e in entities).items())),
        "blocked_merges": len(blocked),
        "timings_seconds": {
            "blocking": round(t_block, 4),
            "scoring": round(t_score, 4),
            "total": round(time.perf_counter() - t0, 4),
        },
    }
    digest = sha256_hex(
        canonical_json(
            {
                "version": RESOLUTION_VERSION,
                "entities": [
                    [e["canonical_entity_id"], e["members"], e["resolution_status"]] for e in entities
                ],
                "pairs": [
                    [pid, p["status"], p["rule"], p["evidence_score"]] for pid, p in sorted(pairs.items())
                ],
                "blocked": blocked,
            }
        )
    )
    return Resolution(
        records=by_key,
        pairs=pairs,
        entities=entities,
        entity_of=entity_of,
        record_status=record_status,
        blocked_merges=blocked,
        stats=stats,
        sha256=digest,
        pairs_of=dict(pairs_of),
    )


def _count(xs) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for x in xs:
        out[x] += 1
    return out
