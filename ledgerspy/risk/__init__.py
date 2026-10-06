"""P9 risk engine v1.

Fuses P7 rule signals and P8 anomaly signals into a bounded review-risk score with an exact, traceable
contribution breakdown, tiers, a review-volume guard, entity risk and explanation objects.

The score is a forensic review priority on a 0–100 scale, never a probability of fraud. See
docs/risk-engine.md.
"""
