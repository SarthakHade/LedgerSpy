"""Investigation vocabulary: lifecycle, priorities, outcomes, link types and the decisions allowed on each.

Terminology: an automated output is a FINDING or INDICATOR (a lead that requires review), never proof. An
analyst ASSESSMENT is a recorded decision with a rationale; it never changes the automated output, a P9
score, a detector, a model or source data. A case CONCLUSION is the analyst's documented outcome, which may be
inconclusive.
"""

STATUSES = ("open", "in_review", "awaiting_evidence", "concluded", "closed")
PRIORITIES = ("low", "medium", "high", "critical")
OUTCOMES = ("concerns_substantiated", "no_issue_identified", "inconclusive", "referred")

# status → allowed next statuses. Concluding needs an outcome + conclusion; closing and re-opening need a
# reason.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("in_review", "awaiting_evidence", "closed"),
    "in_review": ("awaiting_evidence", "concluded", "open"),
    "awaiting_evidence": ("in_review",),
    "concluded": ("closed", "in_review"),
    "closed": ("in_review",),
}
NEEDS_RATIONALE = {
    ("open", "closed"),
    ("concluded", "in_review"),
    ("closed", "in_review"),
    ("concluded", "closed"),
}

# link type → (needs dataset, needs run, ref pattern description). Patterns are enforced by refs.py.
LINK_TYPES: dict[str, dict[str, object]] = {
    "transaction": {"dataset": True, "run": False, "kind": "record"},
    "risk_transaction": {"dataset": True, "run": False, "kind": "finding"},
    "risk_entity": {"dataset": True, "run": False, "kind": "finding"},
    "signal": {"dataset": True, "run": False, "kind": "finding"},
    "entity": {"dataset": True, "run": False, "kind": "entity"},
    "entity_pair": {"dataset": True, "run": False, "kind": "entity"},
    "graph_node": {"dataset": True, "run": False, "kind": "graph"},
    "graph_edge": {"dataset": True, "run": False, "kind": "graph"},
    "graph_cycle": {"dataset": True, "run": False, "kind": "finding"},
    "graph_signal": {"dataset": True, "run": False, "kind": "finding"},
    "reconciliation_run": {"dataset": False, "run": True, "kind": "record"},
    "reconciliation_result": {"dataset": False, "run": True, "kind": "reconciliation"},
    "reconciliation_finding": {"dataset": False, "run": True, "kind": "finding"},
    "duplicate_group": {"dataset": False, "run": True, "kind": "duplicate"},
    "evidence_file": {"dataset": False, "run": False, "kind": "evidence"},
    "pdf_page": {"dataset": True, "run": False, "kind": "evidence"},
    "pdf_item": {"dataset": True, "run": False, "kind": "ocr"},
}

GENERAL_ACTIONS = ("reviewed_no_issue", "further_investigation_required", "evidence_insufficient")
FINDING_ACTIONS = ("finding_under_review", "finding_confirmed", "finding_dismissed", "finding_escalated")
ACTIONS_BY_KIND: dict[str, tuple[str, ...]] = {
    "case": GENERAL_ACTIONS,
    "record": GENERAL_ACTIONS,
    "evidence": GENERAL_ACTIONS,
    "graph": GENERAL_ACTIONS,
    "finding": GENERAL_ACTIONS + FINDING_ACTIONS,
    "entity": GENERAL_ACTIONS + ("entity_link_accepted", "entity_link_rejected"),
    "duplicate": GENERAL_ACTIONS + ("duplicate_confirmed", "duplicate_rejected"),
    "reconciliation": GENERAL_ACTIONS + ("match_accepted", "match_rejected", "ambiguity_resolved"),
    "ocr": GENERAL_ACTIONS + ("ocr_row_confirmed", "ocr_row_excluded", "ocr_row_corrected"),
}
# The finding status the latest decision implies (roadmap: New / Under review / Confirmed / Dismissed /
# Escalated).
FINDING_STATUS = {
    "finding_under_review": "under_review",
    "further_investigation_required": "under_review",
    "evidence_insufficient": "under_review",
    "finding_confirmed": "confirmed",
    "finding_dismissed": "dismissed",
    "reviewed_no_issue": "dismissed",
    "finding_escalated": "escalated",
}
ACTION_TEXT = {
    "reviewed_no_issue": "Reviewed: no issue identified",
    "further_investigation_required": "Further investigation required",
    "evidence_insufficient": "Evidence insufficient to assess",
    "finding_under_review": "Finding under review",
    "finding_confirmed": "Finding confirmed by the analyst (indicator substantiated; not a fraud finding)",
    "finding_dismissed": "Finding dismissed by the analyst (kept, with the reason)",
    "finding_escalated": "Finding escalated",
    "entity_link_accepted": "Entity link accepted by the analyst (recorded only: entities are not merged)",
    "entity_link_rejected": "Entity link rejected by the analyst",
    "duplicate_confirmed": "Duplicate confirmed by the analyst (nothing is deleted)",
    "duplicate_rejected": "Not a duplicate (analyst)",
    "match_accepted": "Reconciliation match accepted by the analyst",
    "match_rejected": "Reconciliation match rejected by the analyst",
    "ambiguity_resolved": "Ambiguous match resolved by the analyst with a justification",
    "ocr_row_confirmed": "Extracted row confirmed against the PDF page",
    "ocr_row_excluded": "Extracted row excluded (not used)",
    "ocr_row_corrected": "Extracted field corrected (overlay; the extraction is unchanged)",
}
