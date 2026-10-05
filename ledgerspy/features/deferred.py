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
    "Vendor masters are not ingested as canonical data yet (P3 accepts transaction files only); planned with "
    "entity resolution (P12). Until then cp_days_since_first gives the relationship age seen in the ledger.",
    ("vendor master: onboarding_date",),
)
deferred(
    "shared_identifier_count",
    "relationship",
    Level.ENTITY,
    "Number of other master records (vendors, employees) sharing the entity's address, phone, bank account "
    "or tax ID.",
    "Needs vendor/employee masters as canonical entities with attributes (P12 entity resolution).",
    ("vendor master", "employee master"),
)
deferred(
    "employee_link_indicator",
    "relationship",
    Level.ENTITY,
    "True when a vendor shares an identifier with an employee (address, phone, bank account).",
    "Needs employee and vendor masters as canonical entities (P12) and the attribute graph (P13).",
    ("vendor master", "employee master"),
)
deferred(
    "entity_linkage_count",
    "relationship",
    Level.ENTITY,
    "Number of entities linked to this one through shared attributes or resolved aliases.",
    "Needs resolved entities and the relationship graph (P12, P13).",
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
