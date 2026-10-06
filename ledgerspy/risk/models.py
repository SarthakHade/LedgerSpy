"""P9 constants shared by the risk engine, its explanations and the API."""

RISK_VERSION = 1

TIERS = ("low", "medium", "high", "critical")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

SCORED = "scored"
INSUFFICIENT = "insufficient_data"

# Plain names for detectors in user-facing text (the detector's own explanation carries the evidence).
DETECTOR_LABELS = {
    "duplicate_exact": "Exact duplicate payment",
    "duplicate_near": "Near-duplicate payment",
    "round_amount": "Round amounts from this counterparty",
    "structuring": "Split below an approval limit",
    "temporal": "Posted outside working time",
    "new_vendor_large": "Large amount to a new counterparty",
    "balance_continuity": "Running-balance discontinuity",
    "dormancy": "Reactivated after a dormant period",
    "period_end": "Period-end cluster",
    "velocity": "Payment velocity spike",
    "benford_subgroup": "Benford deviation in its subgroup",
    "isolation_forest": "Statistically unusual combination",
    "robust_peer": "Far from its peer group",
    "tail_percentile": "Extreme value in its flow",
}

NOT_PROBABILITY = (
    "The risk score ranks transactions for review on a 0–100 scale. It is not a probability or a "
    "percentage chance of fraud: it has not been calibrated against confirmed outcomes."
)


def detector_label(detector: str) -> str:
    return DETECTOR_LABELS.get(detector, detector.replace("_", " "))
