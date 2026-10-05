"""P7 forensic signal and result types.

`ForensicSignal` is the rich, explainable record a detector emits. `to_canonical()` turns it into the uniform
`ledgerspy.canonical.Signal` that P9's risk fusion consumes (architecture §5.4); the extra fields (rule
version, severity, feature values, explanation) travel with it as evidence.
"""

from dataclasses import dataclass, field
from typing import Any

from ledgerspy.canonical import Signal, SignalFamily, SubjectType
from ledgerspy.hashing import canonical_json, sha256_hex

SUBJECTS = {
    "transaction": SubjectType.TXN,
    "entity": SubjectType.ENTITY,
    "group": SubjectType.GROUP,
}

# Rule / analysis status for a dataset.
EVALUATED = "evaluated"  # the rule ran; per-subject outcomes are triggered, not triggered or insufficient
NOT_AVAILABLE = "not_available"  # an input the rule needs is missing for this dataset (the reason says which)
NOT_APPLICABLE = "not_applicable"  # the concept does not apply to this dataset (e.g. no running balance)
DISABLED = "disabled"  # switched off in configuration


def signal_id(rule_id: str, version: int, subject_type: str, subject_id: str) -> str:
    """Deterministic: the same rule version on the same subject always has the same ID."""
    return "S-" + sha256_hex(canonical_json([rule_id, version, subject_type, subject_id]))[:20]


@dataclass(frozen=True)
class ForensicSignal:
    rule_id: str
    rule_version: int
    family: SignalFamily
    subject_type: str  # transaction | entity | group
    subject_id: str
    txn_ids: tuple[str, ...]  # every transaction the evidence points at
    severity: str
    strength: float  # placeholder for P9: 1.0 when a rule fires
    weight: float  # placeholder for P9: from configuration
    evidence: dict[str, Any]
    features: dict[str, Any]  # the P6 feature values the decision used
    explanation: str
    signal_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.signal_id:
            object.__setattr__(
                self,
                "signal_id",
                signal_id(self.rule_id, self.rule_version, self.subject_type, self.subject_id),
            )
        if self.subject_type not in SUBJECTS:
            raise ValueError(f"unknown subject type {self.subject_type}")

    @property
    def code(self) -> str:
        return f"{self.family.value}.{self.rule_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "code": self.code,
            "family": self.family.value,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "txn_ids": list(self.txn_ids),
            "triggered": True,
            "severity": self.severity,
            "strength": self.strength,
            "weight": self.weight,
            "evidence": self.evidence,
            "features": self.features,
            "explanation": self.explanation,
        }

    def to_canonical(self) -> Signal:
        return Signal(
            subject=SUBJECTS[self.subject_type],
            subject_id=self.subject_id,
            code=self.code,
            family=self.family,
            strength=self.strength,
            weight=self.weight,
            raw_value=self.evidence.get("value"),
            evidence={
                "signal_id": self.signal_id,
                "rule_version": self.rule_version,
                "severity": self.severity,
                "txn_ids": list(self.txn_ids),
                "features": self.features,
                "explanation": self.explanation,
                **self.evidence,
            },
            template_id=f"p7.{self.rule_id}.v{self.rule_version}",
        )


@dataclass
class RuleOutcome:
    rule_id: str
    version: int
    family: SignalFamily
    status: str
    reason: str = ""
    evaluated: int = 0  # subjects the rule could judge
    insufficient: int = (
        0  # subjects skipped because a needed value was null (insufficient history / not applicable)
    )
    signals: list[ForensicSignal] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "family": self.family.value,
            "status": self.status,
            "reason": self.reason,
            "evaluated": self.evaluated,
            "insufficient": self.insufficient,
            "triggered": len(self.signals),
            "affected_transactions": len({t for s in self.signals for t in s.txn_ids}),
        }
