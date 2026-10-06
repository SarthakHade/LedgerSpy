"""P8 result types.

`AnomalyResult` is one triggered anomaly on one subject, with the score, the population it was judged against
and the evidence. `forensic()` turns it into the P7 `ForensicSignal` (and from there the canonical `Signal`),
so P9 consumes P7 and P8 signals through one contract.
"""

from dataclasses import dataclass, field
from typing import Any

from ledgerspy.canonical import Signal, SignalFamily
from ledgerspy.forensics.models import ForensicSignal, signal_id

# Detector status for a dataset.
EVALUATED = "evaluated"  # the detector ran; rows are scored, insufficient or reference-only
INSUFFICIENT_DATA = "insufficient_data"  # too few usable rows or features for a meaningful result
NOT_AVAILABLE = "not_available"  # an input the detector needs is missing (the reason says which)
DISABLED = "disabled"  # switched off in configuration

# Row status within a detector.
SCORED = "scored"
ROW_INSUFFICIENT = "insufficient_data"
REFERENCE_ONLY = "reference_only"  # chronological mode: a reference row is a baseline, not scored


@dataclass(frozen=True)
class AnomalyResult:
    detector_id: str
    detector_version: int
    subject_type: str  # transaction (P8 scores transactions)
    subject_id: str
    txn_ids: tuple[str, ...]
    anomaly_score: float  # higher = more unusual, for every detector
    percentile: float | None  # of the score (or value) in the reference population, if defined
    tail: float | None  # the smallest configured tail share the subject falls in, if any
    population: dict[str, Any]  # what the subject was compared with: kind, size, peer group, …
    features: dict[str, Any]  # the P6 feature values used (raw, as in the matrix)
    evidence: dict[str, Any]
    explanation: str
    severity: str
    strength: float  # provisional for P9 (architecture §5.3): how deep in the tail, in [0, 1]
    weight: float  # placeholder for P9: from configuration
    metadata: dict[str, Any] = field(default_factory=dict)  # detector run identity (versions, seed, hashes)

    @property
    def signal_id(self) -> str:
        return signal_id(self.detector_id, self.detector_version, self.subject_type, self.subject_id)

    def forensic(self) -> ForensicSignal:
        return ForensicSignal(
            rule_id=self.detector_id,
            rule_version=self.detector_version,
            family=SignalFamily.MODEL_ANOMALY,
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            txn_ids=self.txn_ids,
            severity=self.severity,
            strength=self.strength,
            weight=self.weight,
            evidence={
                "value": self.anomaly_score,
                "anomaly_score": self.anomaly_score,
                "percentile": self.percentile,
                "tail": self.tail,
                "population": self.population,
                **self.evidence,
            },
            features=self.features,
            explanation=self.explanation,
        )

    def to_canonical(self) -> Signal:
        return self.forensic().to_canonical()

    def to_dict(self) -> dict[str, Any]:
        out = self.forensic().to_dict()
        out.pop("evidence")
        return {
            **out,
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "status": "triggered",
            "anomaly_score": self.anomaly_score,
            "percentile": self.percentile,
            "tail": self.tail,
            "population": self.population,
            "evidence": self.evidence,
            "metadata": self.metadata,
        }


@dataclass
class DetectorOutcome:
    detector_id: str
    version: int
    status: str
    reason: str = ""
    rows_total: int = 0
    rows_scored: int = 0
    rows_insufficient: int = 0
    rows_reference_only: int = 0
    insufficient_reasons: dict[str, int] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)  # used / excluded with reasons
    parameters: dict[str, Any] = field(default_factory=dict)  # threshold policy and model settings
    details: dict[str, Any] = field(default_factory=dict)  # detector-specific summary (groups, cutoffs, …)
    results: list[AnomalyResult] = field(default_factory=list)
    scores: dict[str, list[Any]] = field(default_factory=dict)  # per-row columns, matrix order

    def summary(self) -> dict[str, Any]:
        return {
            "detector_id": self.detector_id,
            "version": self.version,
            "family": SignalFamily.MODEL_ANOMALY.value,
            "status": self.status,
            "reason": self.reason,
            "rows_total": self.rows_total,
            "rows_scored": self.rows_scored,
            "rows_insufficient": self.rows_insufficient,
            "rows_reference_only": self.rows_reference_only,
            "insufficient_reasons": dict(sorted(self.insufficient_reasons.items())),
            "triggered": len(self.results),
            "features": self.features,
            "parameters": self.parameters,
            "details": self.details,
        }
