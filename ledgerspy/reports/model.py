"""Report vocabulary, limits and fixed wording (P16).

The limits are versioned constants, not configuration: adding a configuration section would change the
configuration hash that every P9 risk result is keyed on, and P16 must leave P9 results bit-identical.
"""

FORMAT_VERSION = 1

REPORT_TYPES = {
    "case_investigation": "Case investigation report",
    "bank_reconciliation": "Bank reconciliation report",
    "audit_memo": "Audit memo (draft)",
    "evidence_manifest": "Evidence manifest",
}
FORMATS = {
    "json": "application/json",
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}

MAX_INVESTIGATIONS = 20  # per memo / manifest
MAX_LISTED_ROWS = 500  # rows listed per section; totals always cover every row (truncation is reported)
MAX_ACTIVITY = 500  # activity events listed per case
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024  # a rendered artifact larger than this is refused, never cut silently

# Claim origins and types: the origin says WHO asserted it, the type WHAT it is.
ORIGINS = {
    "automated": "machine-generated indicator (an engine's output); a lead requiring review, not proof",
    "analyst": "recorded by an analyst in the investigation workflow (decision, note or conclusion)",
    "computed": "computed by the report from the listed records (counts and totals)",
}
CLAIM_TYPES = (
    "automated_finding",
    "linked_record",
    "reconciliation_result",
    "extraction_value",
    "analyst_decision",
    "analyst_note",
    "case_conclusion",
    "computed_total",
)
TRACEABILITY = {
    "traceable": "every reference resolves to the current record and its evidence verifies; this shows where "
    "the claim comes from, not that it is true",
    "unverified": "at least one reference is stale, changed, missing or unavailable, or its evidence does "
    "not verify; do not rely on this claim without re-checking",
    "no_reference": "the claim has no linked record (e.g. a case-level decision); it rests on the analyst's "
    "rationale only",
}

# Link kinds whose record is an automated finding (an indicator), see investigations.model.LINK_TYPES.
FINDING_KIND = "finding"

TERMINOLOGY = {
    "finding": "An automated output (risk row, detector signal, graph cycle, reconciliation finding): an "
    "indicator and a lead requiring review, never proof of fraud.",
    "assessment": "An analyst's recorded decision with its rationale. It never changes the automated output.",
    "conclusion": "The analyst's documented outcome of an investigation, only if one was recorded.",
    "review_risk_score": "A 0–100 review-priority score (P9). It ranks rows for review; it is not a "
    "probability of fraud and not an amount.",
    "gross_value": "The sum of the amounts of the listed transactions, per dataset and currency. It is not a "
    "loss, not an exposure that has been confirmed, and different currencies are never added.",
}

DRAFT_NOTICE = (
    "DRAFT — generated from recorded case data by deterministic templates. It has not been reviewed or "
    "approved: LedgerSpy has no report-approval workflow, so this memo stays a draft until it is reviewed "
    "outside the tool. It states indicators and recorded analyst assessments; it does not establish fraud."
)
RECOMMENDATION_NOTICE = (
    "Recommendations are generated from the recorded case state (open questions, stale or changed records, "
    "missing conclusions). They are suggestions for the reviewer, not findings or facts."
)
