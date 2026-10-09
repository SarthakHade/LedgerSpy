"""Pair evidence and the decision policy (docs/entity-resolution.md §5–§7).

For a candidate pair the evidence is computed field by field:
- name: five similarity components on the core names (legal form excluded) and their mean; the words that
  differ, split into spelling variants and distinguishing words; the legal forms;
- identifiers (master data only, when mapped): tax ID (and a GSTIN's PAN), bank account (+ bank code), e-mail,
  phone, each `match` / `conflict` / `differs` / `missing`; a phone or e-mail used by many differently named
  records is a `shared_contact` (a switchboard, a shared mailbox), not an identity;
- address: word similarity, postal code, city;
- supporting behaviour (ledger names only): shared reference patterns, counterparties, accounts, amount
  profile.

Statuses: `confirmed_match` (merged), `probable_match` and `ambiguous` (review; never merged),
`conflicting_identity` (contradictory identity evidence; review; never merged), `shared_attribute` (records
of different kinds or names sharing a contact, address or account: a link for P13, never a merge),
`no_match`.

Decision rules, in order (first that applies):
 1. different entity classes (person vs organisation): never a match; shared identifiers or address →
    `shared_attribute`, else `no_match`.
 2. account codes (PaySim): equal codes only (never fuzzy).
 3. same tax ID: `confirmed_match` when names are at least `tax_name_min` similar and no bank-account
    conflict, else `conflicting_identity`.
 4. a hard conflict (both have a tax ID, or both a bank account, and they differ): `conflicting_identity` when
    any identifier matches or the names are at least `review_name` similar, else `no_match`. A high name
    similarity never hides a conflict.
 5. the same core name (only case, punctuation, spacing or the legal form's spelling differ) and no soft
    conflict (different legal-form family, different city): organisations → `confirmed_match`; persons →
    `confirmed_match` only with a matching identifier, else `ambiguous` (common personal names); a ledger
    name
    → a master record: `confirmed_match` only when it is the one compatible master record with that core name.
 6. a shared bank account, e-mail or phone (not a shared contact): with names ≥ `contact_name_min` →
    `confirmed_match` if a bank account or two identifiers match and nothing soft conflicts, else
    `probable_match`; with less similar names → `conflicting_identity` (one identifier, different names).
 7. names only: ≥ `probable_name` with no distinguishing word and no soft conflict → `probable_match`;
    ≥ `review_name` → `ambiguous`; shared contact or address → `shared_attribute`; else `no_match`.
Supporting behaviour never changes a status; it adds at most `behaviour_bonus_max` to the evidence score.

The evidence score (0–1) orders the review queue; it is not a probability.
"""

from dataclasses import dataclass, field
from typing import Any

from ledgerspy.config.schema import ResolutionConfig
from ledgerspy.hashing import sha256_hex
from ledgerspy.resolution import similarity
from ledgerspy.resolution.records import PartyRecord

STATUSES = (
    "confirmed_match",
    "probable_match",
    "ambiguous",
    "conflicting_identity",
    "shared_attribute",
    "no_match",
)
REVIEW = ("probable_match", "ambiguous", "conflicting_identity")
LEGAL_FAMILY = {
    "pvt ltd": "ltd",
    "ltd": "ltd",
    "plc": "plc",
    "llp": "llp",
    "llc": "llc",
    "inc": "corp",
    "corp": "corp",
    "gmbh": "gmbh",
    "co": "co",
    "and co": "co",
}
_STATUS_TEXT = {
    "confirmed_match": "Confirmed match",
    "probable_match": "Probable match (needs review)",
    "ambiguous": "Ambiguous (needs review)",
    "conflicting_identity": "Conflicting identity (needs review)",
    "shared_attribute": "Shared attribute (linked, not the same entity)",
    "no_match": "No match",
}
_FIELD_TEXT = {"tax_id": "tax ID", "bank_account": "bank account", "email": "e-mail", "phone": "phone"}


@dataclass
class Context:
    cfg: ResolutionConfig
    contact_names: dict[tuple[str, str], set[str]] = field(
        default_factory=dict
    )  # (field, value) → core names
    # core name → True when every master record with that core name is one identity (one record, or records
    # without a differing tax ID or bank account): a ledger name with that core then links unambiguously.
    unique_master_core: dict[str, bool] = field(default_factory=dict)


def pair_id(a: str, b: str) -> str:
    x, y = sorted((a, b))
    return "P-" + sha256_hex(x + "\x1f" + y)[:16]


def _legal_family(form: str | None) -> str | None:
    if not form:
        return None
    return LEGAL_FAMILY.get(form.split()[-1] if form not in LEGAL_FAMILY else form, form)


def _identifiers(a: PartyRecord, b: PartyRecord, ctx: Context) -> dict[str, str]:
    out = {}
    na, nb = a.norm, b.norm
    for f in ("tax_id", "bank_account", "email", "phone"):
        va, vb = na.get(f), nb.get(f)
        if f == "tax_id" and va and vb and va != vb:
            pa, pb = na.get("pan"), nb.get("pan")
            if (pa and pa in (pb, vb)) or (pb and pb == va):
                out[f] = "match"  # a GSTIN and the PAN it embeds
                continue
        if not va or not vb:
            out[f] = "missing"
        elif va == vb:
            bank_codes = na.get("bank_code"), nb.get("bank_code")
            if f == "bank_account" and all(bank_codes) and bank_codes[0] != bank_codes[1]:
                out[f] = "differs"  # the same number at different banks is a different account
                continue
            if (
                f in ("email", "phone")
                and len(ctx.contact_names.get((f, va), ())) >= ctx.cfg.shared_contact_generic_min
            ):
                out[f] = "shared_contact"
            else:
                out[f] = "match"
        else:
            out[f] = "conflict" if f in ("tax_id", "bank_account") else "differs"
    return out


def _address(a: PartyRecord, b: PartyRecord, cfg: ResolutionConfig) -> dict[str, Any] | None:
    """Address evidence: compared when both records have an address or both have a city (else None)."""
    wa, wb = a.norm.get("address_words"), b.norm.get("address_words")
    ca, cb = a.norm.get("city"), b.norm.get("city")
    if not (wa and wb) and not (ca and cb):
        return None
    sim = similarity.token_jaccard(tuple(wa), tuple(wb)) if wa and wb else None
    pa, pb = a.norm.get("postal_code"), b.norm.get("postal_code")
    same = sim is not None and sim >= cfg.thresholds.address_same and (pa == pb or not pa or not pb)
    return {
        "similarity": round(sim, 6) if sim is not None else None,
        "postal_code": "match" if pa and pa == pb else ("differs" if pa and pb else "missing"),
        "city": "match" if ca and ca == cb else ("differs" if ca and cb else "missing"),
        "same_address": same,
    }


def _behaviour(a: PartyRecord, b: PartyRecord) -> list[dict[str, Any]]:
    """Supporting evidence from ledger behaviour (both records must be ledger names)."""
    if a.source != "ledger" or b.source != "ledger":
        return []
    out = []
    ba, bb = a.behaviour, b.behaviour
    for key, label in (
        ("reference_patterns", "shared invoice / reference pattern"),
        ("counterparties", "shared counterparties (the same payers / payees)"),
        ("accounts", "shared ledger accounts"),
    ):
        sa, sb = set(ba.get(key, ())), set(bb.get(key, ()))
        if sa and sb:
            j = len(sa & sb) / len(sa | sb)
            if j >= 0.5:
                out.append(
                    {"evidence": key, "label": label, "jaccard": round(j, 4), "shared": sorted(sa & sb)[:5]}
                )
    ma, mb = ba.get("median_amount_minor"), bb.get("median_amount_minor")
    if ma and mb and max(ma, mb) > 0 and min(ma, mb) / max(ma, mb) >= 0.9:
        out.append(
            {
                "evidence": "amount_profile",
                "label": "similar typical amount (within 10%)",
                "ratio": round(min(ma, mb) / max(ma, mb), 4),
            }
        )
    return out


def evaluate(a: PartyRecord, b: PartyRecord, ctx: Context) -> dict[str, Any]:
    if b.key < a.key:
        a, b = b, a
    cfg = ctx.cfg
    th = cfg.thresholds
    ca, cb = a.name.core or "", b.name.core or ""
    comps = similarity.name_components(ca, cb, a.name.tokens, b.name.tokens, th.typo_token)
    name_sim = similarity.mean(comps)
    if "person" in (a.entity_class, b.entity_class) and a.name.tokens_sorted and b.name.tokens_sorted:
        sa, sb = " ".join(a.name.tokens_sorted), " ".join(b.name.tokens_sorted)
        alt = similarity.mean(
            similarity.name_components(sa, sb, a.name.tokens_sorted, b.name.tokens_sorted, th.typo_token)
        )
        if alt > name_sim:
            comps["word_order_insensitive"] = alt
            name_sim = alt
    diff = similarity.token_differences(a.name.tokens, b.name.tokens, th.typo_token)
    exact_core = bool(ca) and (
        ca == cb
        or (a.name.tokens_sorted == b.name.tokens_sorted and "person" in (a.entity_class, b.entity_class))
    )
    fa, fb = _legal_family(a.name.legal_form), _legal_family(b.name.legal_form)
    ids = _identifiers(a, b, ctx)
    addr = _address(a, b, cfg)
    behaviour = _behaviour(a, b)

    evidence: list[str] = []
    conflicts: list[str] = []
    soft: list[str] = []
    matched = [f for f, s in ids.items() if s == "match"]
    shared_contacts = [f for f, s in ids.items() if s == "shared_contact"]
    hard = [f for f in ("tax_id", "bank_account") if ids[f] == "conflict"]
    for f in matched:
        evidence.append(f"same {_FIELD_TEXT[f]}")
    for f in shared_contacts:
        evidence.append(f"same {_FIELD_TEXT[f]}, but it is shared by many differently named records")
    for f in hard:
        conflicts.append(f"different {_FIELD_TEXT[f]}")
    if fa and fb and fa != fb:
        soft.append(f"different legal form ({a.name.legal_form} vs {b.name.legal_form})")
    if addr:
        if addr["city"] == "differs":
            soft.append("different city")
        if addr["same_address"]:
            evidence.append("same address")
    if diff["distinguishing_words"] and not exact_core:
        soft_words = f"names differ by distinguishing words: {', '.join(diff['distinguishing_words'][:4])}"
    else:
        soft_words = None
    shared_any = bool(matched or shared_contacts or (addr and addr["same_address"]))

    status, rule = "no_match", "insufficient_evidence"
    if "account_code" in (a.entity_class, b.entity_class):
        status, rule = (
            ("confirmed_match", "same_account_code")
            if a.name.value == b.name.value
            else ("no_match", "different_account_code")
        )
    elif {a.entity_class, b.entity_class} == {"person", "organisation"}:
        status, rule = (
            ("shared_attribute", "person_and_organisation_share_attributes")
            if shared_any
            else ("no_match", "person_and_organisation")
        )
    elif ids["tax_id"] == "match":
        if name_sim >= th.tax_name_min and ids["bank_account"] != "conflict":
            status, rule = "confirmed_match", "same_tax_id"
        else:
            status, rule = "conflicting_identity", "same_tax_id_conflicting_names_or_accounts"
    elif hard:
        if matched or name_sim >= th.review_name or exact_core:
            status, rule = "conflicting_identity", "identifier_conflict"
        else:
            status, rule = "no_match", "identifier_conflict_different_names"
    elif exact_core and not soft:
        persons = "person" in (a.entity_class, b.entity_class)
        ledger_master = {a.source, b.source} == {"ledger", "master"}
        if ledger_master:
            m = a if a.source == "master" else b
            if ctx.unique_master_core.get(m.name.core or "", False):
                status, rule = "confirmed_match", "ledger_name_equals_unique_master_name"
            else:
                status, rule = "ambiguous", "ledger_name_equals_several_master_names"
        elif persons and not matched:
            status, rule = "ambiguous", "same_personal_name_without_identifier"
        else:
            status, rule = "confirmed_match", "same_name_modulo_formatting"
    elif matched:
        strong = "bank_account" in matched or len(matched) >= 2
        if name_sim >= th.contact_name_min:
            if strong and not soft and not soft_words:
                status, rule = "confirmed_match", "shared_identifiers_and_similar_names"
            else:
                status, rule = "probable_match", "shared_identifier_and_similar_names"
        else:
            status, rule = "conflicting_identity", "shared_identifier_different_names"
    elif name_sim >= th.probable_name and not soft and not soft_words:
        status, rule = "probable_match", "very_similar_names"
    elif name_sim >= th.review_name:
        status, rule = "ambiguous", "similar_names"
    elif shared_any:
        status, rule = "shared_attribute", "shared_contact_or_address"

    if soft_words:
        soft.append(soft_words)
    cross_class = {a.entity_class, b.entity_class} == {"person", "organisation"}
    if cross_class:  # a person and an organisation differ by nature: only what they share is evidence
        conflicts, soft = [], []
    weights = {"tax_id": 0.35, "bank_account": 0.25, "email": 0.15, "phone": 0.1}
    score = 0.5 * name_sim + (0.15 if exact_core else 0.0)
    score += sum(weights[f] for f in matched) + 0.02 * len(shared_contacts)
    if addr and addr["similarity"] is not None:
        score += 0.1 * addr["similarity"]
    if not cross_class:
        score -= (
            0.4 * (ids["tax_id"] == "conflict") + 0.3 * (ids["bank_account"] == "conflict") + 0.1 * len(soft)
        )
    else:
        score = sum(weights[f] for f in matched) + (0.1 if addr and addr["same_address"] else 0.0)
    bonus = min(cfg.behaviour_bonus_max, cfg.behaviour_bonus_max / 4 * len(behaviour))
    score = round(min(1.0, max(0.0, score + bonus)), 4)

    rec = {
        "pair_id": pair_id(a.key, b.key),
        "a": a.key,
        "b": b.key,
        "entity_types": [a.entity_type, b.entity_type],
        "entity_classes": [a.entity_class, b.entity_class],
        "status": status,
        "rule": rule,
        "evidence_score": score,
        "name": {
            "a": a.name.raw,
            "b": b.name.raw,
            "normalized": [a.name.value, b.name.value],
            "core": [ca, cb],
            "legal_form": [a.name.legal_form, b.name.legal_form],
            "components": comps,
            "name_similarity": name_sim,
            "same_core_name": exact_core,
            **diff,
        },
        "identifiers": ids,
        "address": addr,
        "evidence": evidence,
        "conflicts": conflicts + soft,
        "supporting_behaviour": behaviour,
    }
    rec["explanation"] = explain(rec)
    return rec


def explain(p: dict[str, Any]) -> str:
    parts = [f"normalised names are {round(100 * p['name']['name_similarity'])}% similar"]
    if p["name"]["same_core_name"]:
        parts[0] = "the names are the same apart from case, punctuation, spacing or the legal form's spelling"
    parts += p["evidence"]
    head = f"{_STATUS_TEXT[p['status']]} because " + ", ".join(parts) + "."
    if p["conflicts"]:
        tail = " Conflicting evidence: " + "; ".join(p["conflicts"]) + "."
    else:
        tail = " No conflicting identifier found."
    if p["supporting_behaviour"]:
        tail += (
            " Supporting (not decisive): " + "; ".join(x["label"] for x in p["supporting_behaviour"]) + "."
        )
    note = {
        "ledger_name_equals_several_master_names": " Several master records carry this name; the ledger "
        "cannot tell which.",
        "same_personal_name_without_identifier": " Personal names are common; an identifier is needed.",
        "person_and_organisation_share_attributes": " A person and an organisation are never merged.",
    }.get(p["rule"], "")
    return head + tail + note
