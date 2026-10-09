"""Features the current canonical data cannot support yet.

They are registered so the inventory is honest about what is missing and why, but they have no implementation,
are never computed and never appear as matrix columns. Each names the phase that will make it possible.
"""

from ledgerspy.features.spec import Level, deferred

deferred(
    "vendor_master_age_days",
    "frequency",
    Level.TRANSACTION,
    "Days between the vendor-master onboarding date and the transaction.",
    "P12 imports masters for entity resolution with identity fields only (name, address, contacts, bank "
    "account, tax ID); onboarding dates are not a mapped master field yet. Until then cp_days_since_first "
    "gives the relationship age seen in the ledger.",
    ("vendor master: onboarding_date",),
)
deferred(
    "entity_linkage_count",
    "relationship",
    Level.ENTITY,
    "Number of entities linked to this one through shared attributes or resolved aliases.",
    "Needs the relationship graph (P13); P12 provides the resolved entities and their attribute links "
    "(see shared_identifier_count).",
    ("entities", "entity_aliases"),
)
deferred(
    "approver_is_poster",
    "behaviour",
    Level.TRANSACTION,
    "True when the same user posted and approved the transaction (segregation of duties).",
    "Approver and poster are not canonical fields: in the synthetic ledger they are unmapped columns "
    "('Approved By', 'Posted By') kept verbatim in `extra`, which features never read. Needs canonical user "
    "fields first.",
    ("approved_by", "posted_by"),
)
deferred(
    "user_after_hours_share",
    "behaviour",
    Level.ENTITY,
    "Share of a posting user's transactions made after hours (per-user time-pattern profile).",
    "Needs a canonical posting-user field (see approver_is_poster).",
    ("posted_by",),
)
deferred(
    "bank_match_status",
    "relationship",
    Level.TRANSACTION,
    "Whether a ledger payment has a matching bank line (matched / date-shifted / fee / missing).",
    "Needs ledger-to-bank reconciliation (P14).",
    ("ledger", "bank statement"),
)
deferred(
    "benford_subgroup_deviation",
    "amount",
    Level.ENTITY,
    "How far a subgroup's leading-digit distribution departs from Benford's law.",
    "A dataset/subgroup statistical test, not a per-row feature; it belongs to the Benford module (P7).",
    ("amount_minor",),
)
deferred(
    "amount_to_balance_ratio",
    "amount",
    Level.TRANSACTION,
    "Amount relative to the account's running balance (PaySim).",
    "Balances are not canonical fields (PaySim keeps them in `extra`) and are known simulator artefacts; the "
    "decision belongs to the supervised PaySim adapter (P10).",
    ("oldbalanceOrg",),
)
deferred(
    "geo_distance",
    "relationship",
    Level.TRANSACTION,
    "Distance between the counterparty's location and the company's operating locations.",
    "No location data in the canonical transaction (the vendor master city is not ingested; no geocoding "
    "offline).",
    ("vendor master: city",),
)
