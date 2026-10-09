"""P12 entity-resolution features: one value per counterparty stream, from the dataset's entity resolution.

Resolution reads the whole dataset (every ledger name) and the case's imported master records, so these are
POST-PERIOD entity features: analytical context, never "known at" a transaction (the supervised contract
therefore excludes them). The stream's party record is its ledger name (`l:<party key>`); resolution runs
once per feature input (memoised) with `features.resolution` settings. See docs/entity-resolution.md.
"""

from typing import Any

from ledgerspy.features.data import FeatureInput
from ledgerspy.features.entity import ent, streams
from ledgerspy.features.spec import Kind, Missing, Unusual
from ledgerspy.resolution import ledger
from ledgerspy.resolution.engine import Resolution
from ledgerspy.resolution.records import ledger_key

RESOLUTION_INPUTS = ("payer_raw/payee_raw", "direction", "master records (when imported)")


def resolution_of(inp: FeatureInput) -> Resolution:
    """The dataset's entity resolution (memoised on the input; also used by P9 and the API)."""
    return inp.memo(
        "resolution",
        lambda: ledger.resolve_dataset(inp.rows, inp.masters, inp.cfg.resolution, inp.adapter == "paysim"),
    )


def _per_stream(inp: FeatureInput, fn) -> list[Any]:
    res = resolution_of(inp)
    return [fn(res, ledger_key(stream.split(":", 1)[1])) for stream in streams(inp)]


def _entity(res: Resolution, key: str) -> dict[str, Any]:
    return res.entity(res.entity_of[key])  # type: ignore[return-value]


def _other_entity_pairs(res: Resolution, key: str) -> list[dict[str, Any]]:
    mine = res.entity_of[key]
    out = []
    for pid in res.pairs_of.get(key, []):
        p = res.pairs[pid]
        other = p["b"] if p["a"] == key else p["a"]
        if res.entity_of[other] != mine:
            out.append(p)
    return out


def _links(res: Resolution, key: str) -> list[tuple[dict[str, Any], str]]:
    """(pair, other record) for identifier / address links of the stream's whole canonical entity."""
    e = _entity(res, key)
    inside = set(e["members"])
    out = []
    for m in e["members"]:
        for pid in res.pairs_of.get(m, []):
            p = res.pairs[pid]
            other = p["b"] if p["a"] == m else p["a"]
            if other in inside or p["status"] not in ("shared_attribute", "conflicting_identity"):
                continue
            shared = "match" in p["identifiers"].values() or bool(
                p["address"] and p["address"]["same_address"]
            )
            if shared:
                out.append((p, other))
    return out


@ent(
    name="ent_canonical_entity_id",
    version=1,
    family="relationship",
    description="Canonical entity (P12) of the stream's counterparty name: streams whose names are "
    "confirmed matches share it; ambiguous or conflicting candidates never do.",
    inputs=RESOLUTION_INPUTS,
    kind=Kind.CATEGORY,
    units="canonical entity ID (CE-…)",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null for a stream",
    unusual=Unusual.CONTEXT,
    requires=("direction", "counterparty"),
    params=("resolution",),
)
def ent_canonical_entity_id(inp: FeatureInput) -> list[Any]:
    return _per_stream(inp, lambda res, k: res.entity_of[k])


@ent(
    name="ent_resolution_status",
    version=1,
    family="relationship",
    description="Resolution status of the stream's counterparty name: unique (no candidate needing "
    "review), resolved (merged with confirmed matches), needs_review (a probable or ambiguous "
    "candidate exists) or conflicting_identity (a candidate with contradictory identity evidence "
    "exists).",
    inputs=RESOLUTION_INPUTS,
    kind=Kind.CATEGORY,
    units="status",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null for a stream",
    unusual=Unusual.CONTEXT,
    requires=("direction", "counterparty"),
    params=("resolution",),
)
def ent_resolution_status(inp: FeatureInput) -> list[Any]:
    return _per_stream(inp, lambda res, k: res.record_status[k])


@ent(
    name="ent_linked_record_count",
    version=1,
    family="relationship",
    description="Number of records (ledger names and master records) in the stream's canonical entity.",
    inputs=RESOLUTION_INPUTS,
    kind=Kind.INT,
    units="records",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null for a stream (at least 1: its own name)",
    unusual=Unusual.CONTEXT,
    requires=("direction", "counterparty"),
    params=("resolution",),
)
def ent_linked_record_count(inp: FeatureInput) -> list[Any]:
    return _per_stream(inp, lambda res, k: _entity(res, k)["member_count"])


@ent(
    name="ent_match_confidence",
    version=1,
    family="relationship",
    description="Evidence score (0–1, not a probability) of the weakest confirmed match that formed the "
    "stream's canonical entity.",
    inputs=RESOLUTION_INPUTS,
    kind=Kind.FLOAT,
    units="evidence score",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="null when the canonical entity has a single record (nothing was matched)",
    unusual=Unusual.LOWER,
    requires=("direction", "counterparty"),
    params=("resolution",),
)
def ent_match_confidence(inp: FeatureInput) -> list[Any]:
    return _per_stream(inp, lambda res, k: _entity(res, k)["confidence"])


@ent(
    name="ent_name_similarity_max",
    version=1,
    family="relationship",
    description="Highest name similarity between the stream's counterparty name and a candidate record of "
    "a DIFFERENT canonical entity (a near-identical name that was not merged: a possible look-alike).",
    inputs=RESOLUTION_INPUTS,
    kind=Kind.FLOAT,
    units="similarity 0–1",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="null when blocking found no candidate in another canonical entity",
    unusual=Unusual.HIGHER,
    requires=("direction", "counterparty"),
    params=("resolution",),
)
def ent_name_similarity_max(inp: FeatureInput) -> list[Any]:
    def f(res: Resolution, k: str) -> float | None:
        sims = [p["name"]["name_similarity"] for p in _other_entity_pairs(res, k)]
        return max(sims) if sims else None

    return _per_stream(inp, f)


@ent(
    name="shared_identifier_count",
    version=1,
    family="relationship",
    description="Number of other canonical entities that share a tax ID, bank account, phone, e-mail or "
    "address with the master records of the stream's canonical entity (links, never merges).",
    inputs=RESOLUTION_INPUTS,
    kind=Kind.INT,
    units="entities",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="null when the stream's canonical entity has no master record",
    unusual=Unusual.HIGHER,
    requires=("direction", "counterparty", "master_data"),
    params=("resolution",),
)
def shared_identifier_count(inp: FeatureInput) -> list[Any]:
    def f(res: Resolution, k: str) -> int | None:
        if not _entity(res, k)["master_records"]:
            return None
        return len({res.entity_of[o] for _, o in _links(res, k)})

    return _per_stream(inp, f)


@ent(
    name="employee_link_indicator",
    version=1,
    family="relationship",
    description="True when the stream's canonical entity (an organisation) shares a bank account, phone, "
    "e-mail, tax ID or address with an employee master record.",
    inputs=RESOLUTION_INPUTS,
    kind=Kind.BOOL,
    units="flag",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="null when the canonical entity has no master record or is itself a person",
    unusual=Unusual.TRUE,
    requires=("direction", "counterparty", "master_data"),
    params=("resolution",),
)
def employee_link_indicator(inp: FeatureInput) -> list[Any]:
    def f(res: Resolution, k: str) -> bool | None:
        e = _entity(res, k)
        if not e["master_records"] or e["entity_class"] == "person":
            return None
        return any(res.records[o].entity_type == "employee" for _, o in _links(res, k))

    return _per_stream(inp, f)
