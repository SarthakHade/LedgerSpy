"""Canonical data models (docs/architecture.md §4, §5.4, §5.5, §12).

These are the contracts later phases fill in: ingestion produces Transactions, entity resolution produces
Entities, detectors emit Signals, the risk engine produces Scores, the explain module produces Explanations
and case management persists Findings. Fields are limited to what the architecture defines.
"""

import math
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"
    OUT = "out"  # bank statement lines
    IN = "in"


class EntityKind(StrEnum):
    VENDOR = "vendor"
    EMPLOYEE = "employee"
    CUSTOMER = "customer"
    BANK_ACCOUNT = "bank_account"
    COMPANY_ACCOUNT = "company_account"
    UNKNOWN = "unknown"


class SubjectType(StrEnum):
    TXN = "txn"
    ENTITY = "entity"
    PAIR = "pair"


class SignalFamily(StrEnum):
    AMOUNT = "amount"
    TIMING = "timing"
    DUPLICATION = "duplication"
    STRUCTURING = "structuring"
    DIGIT = "digit"
    ENTITY = "entity"
    RELATIONSHIP = "relationship"
    RECONCILIATION = "reconciliation"
    MODEL_ANOMALY = "model_anomaly"
    MODEL_SUPERVISED = "model_supervised"
    DATA_QUALITY = "data_quality"


class Tier(StrEnum):
    HIGH = "high"
    REVIEW = "review"
    LOW = "low"


class FindingStatus(StrEnum):
    NEW = "new"
    UNDER_REVIEW = "under_review"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    ESCALATED = "escalated"


class Transaction(_Model):
    """A canonical ledger or bank line. Label columns (isFraud, …) are never part of it."""

    txn_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    source_row: int = Field(ge=1, description="1-based data row in the evidence file")
    posted_at: datetime
    document_date: date | None = None
    amount_minor: int = Field(description="Amount in minor units (e.g. paise)")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    direction: Direction
    account: str | None = None
    payer_raw: str | None = None
    payee_raw: str | None = None
    payer_entity_id: str | None = None
    payee_entity_id: str | None = None
    reference: str | None = None
    description: str | None = None
    txn_type: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict, description="Unmapped columns, verbatim")


class Entity(_Model):
    entity_id: str = Field(pattern=r"^E-[0-9A-HJKMNP-TV-Z]{26}$")
    kind: EntityKind
    canonical_name: str = Field(min_length=1)
    attributes: dict[str, str] = Field(default_factory=dict, description="address, PAN/GST, bank account, …")


class Signal(_Model):
    """Uniform detector output (architecture §5.4)."""

    subject: SubjectType
    subject_id: str = Field(min_length=1)
    code: str = Field(pattern=r"^[a-z_]+(\.[a-z0-9_]+)+$", description="e.g. reconciliation.missing_in_bank")
    family: SignalFamily
    strength: float = Field(ge=0.0, le=1.0)
    weight: float = Field(gt=0.0, le=1.0)
    raw_value: float | int | str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    template_id: str = Field(min_length=1)


class Score(_Model):
    """Fused risk for one subject, with the exact per-family contribution shares (sum to 1 when risk > 0)."""

    subject: SubjectType
    subject_id: str = Field(min_length=1)
    risk: float = Field(ge=0.0, le=1.0)
    tier: Tier
    contributions: dict[SignalFamily, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _contributions_decompose_risk(self) -> "Score":
        if any(not 0.0 <= c <= 1.0 for c in self.contributions.values()):
            raise ValueError("each contribution share must be in [0, 1]")
        if self.risk > 0 and not math.isclose(sum(self.contributions.values()), 1.0, abs_tol=1e-9):
            raise ValueError("contribution shares must sum to 1 when risk > 0")
        if self.risk == 0 and self.contributions:
            raise ValueError("a zero risk has no contributions")
        return self


class Contribution(_Model):
    family: SignalFamily
    signal_code: str = Field(min_length=1)
    share: float = Field(ge=0.0, le=1.0)
    sentence: str = Field(min_length=1, description="Rendered explanation template")


class Attribution(_Model):
    """Model-component attribution, named honestly by method (architecture §5.5 layer 2)."""

    method: Literal["shap", "feature_deviation"]
    feature: str = Field(min_length=1)
    value: float


class Explanation(_Model):
    """Produced by the backend and rendered verbatim by the frontend."""

    subject: SubjectType
    subject_id: str = Field(min_length=1)
    risk: float = Field(ge=0.0, le=1.0)
    tier: Tier
    contributions: list[Contribution] = Field(default_factory=list)
    attributions: list[Attribution] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict, description="source rows, peers, match, cycle …")


class Finding(_Model):
    finding_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    finding_type: str = Field(min_length=1)
    subject: SubjectType
    subject_id: str = Field(min_length=1)
    status: FindingStatus = FindingStatus.NEW
    dismissal_reason: str | None = None

    @model_validator(mode="after")
    def _dismissal_needs_reason(self) -> "Finding":
        has_reason = bool(self.dismissal_reason and self.dismissal_reason.strip())
        if self.status is FindingStatus.DISMISSED and not has_reason:
            raise ValueError("a dismissed finding requires a reason")
        if self.status is not FindingStatus.DISMISSED and self.dismissal_reason is not None:
            raise ValueError("dismissal_reason is only valid for dismissed findings")
        return self

    @property
    def stable_key(self) -> str:
        """Deduplicates the same finding across runs (type + subject)."""
        return f"{self.finding_type}:{self.subject.value}:{self.subject_id}"
