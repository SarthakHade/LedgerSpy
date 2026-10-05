"""Configuration schema.

The schema has no default values: config/default.yaml is the only place values are set, so there is
exactly one source for each of them. Unknown keys and wrong types are rejected.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LOOPBACK_HOST = "127.0.0.1"

# Canonical transaction fields a source column can be mapped to (used by ledgerspy.ingest.mapping).
MAPPABLE_FIELDS = (
    "txn_id",
    "posted_at",
    "document_date",
    "amount",
    "debit_amount",
    "credit_amount",
    "direction",
    "currency",
    "account",
    "payer_raw",
    "payee_raw",
    "reference",
    "description",
    "txn_type",
)
MappableField = Literal[
    "txn_id",
    "posted_at",
    "document_date",
    "amount",
    "debit_amount",
    "credit_amount",
    "direction",
    "currency",
    "account",
    "payer_raw",
    "payee_raw",
    "reference",
    "description",
    "txn_type",
]
DirectionValue = Literal["debit", "credit", "in", "out"]


def normalize_header(name: str) -> str:
    """Header comparison key: case-folded, letters and digits only ("Posting_Date" → "postingdate")."""
    return "".join(ch for ch in str(name).casefold() if ch.isalnum())


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ServerConfig(_Section):
    host: Literal["127.0.0.1"] = Field(description="Bind address. Loopback only, by design.")
    port: int = Field(ge=1024, le=65535)


class WorkspaceConfig(_Section):
    path: str = Field(min_length=1)
    db_filename: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+\.db$")


class CsvConfig(_Section):
    encodings: list[Literal["utf-8", "cp1252"]] = Field(min_length=1)
    delimiters: list[Literal[",", ";", "\t", "|"]] = Field(min_length=1)
    sniff_bytes: int = Field(ge=1024, le=16 * 1024 * 1024)


class XlsxConfig(_Section):
    max_uncompressed_bytes: int = Field(ge=1)
    max_compression_ratio: int = Field(ge=1)
    ratio_check_min_bytes: int = Field(ge=0)
    max_zip_entries: int = Field(ge=1)


class MinorUnitsConfig(_Section):
    default_digits: int = Field(ge=0, le=4)
    by_currency: dict[str, int]

    @model_validator(mode="after")
    def _codes(self) -> "MinorUnitsConfig":
        for code, digits in self.by_currency.items():
            if not re.fullmatch(r"[A-Z]{3}", code) or not 0 <= digits <= 4:
                raise ValueError(f"invalid minor-unit entry {code}: {digits}")
        return self


class IngestConfig(_Section):
    max_file_bytes: int = Field(ge=1)
    max_rows: int = Field(ge=1)
    max_columns: int = Field(ge=2)
    max_cell_chars: int = Field(ge=1)
    csv: CsvConfig
    xlsx: XlsxConfig
    minor_units: MinorUnitsConfig


class MappingConfig(_Section):
    min_parse_rate: float = Field(gt=0.0, le=1.0)
    max_unconvertible_share: float = Field(ge=0.0, lt=1.0)
    profile_distinct_cap: int = Field(ge=10)
    sample_values: int = Field(ge=0, le=20)
    label_columns: list[str] = Field(min_length=1)
    aliases: dict[MappableField, list[str]]
    direction_values: dict[DirectionValue, list[str]]
    date_formats: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _aliases_are_unambiguous(self) -> "MappingConfig":
        labels = {normalize_header(n) for n in self.label_columns}
        seen: dict[str, str] = {}
        for field, names in self.aliases.items():
            for name in names:
                key = normalize_header(name)
                if not key:
                    raise ValueError(f"alias {name!r} for {field} is empty after normalisation")
                if key in labels:
                    raise ValueError(f"alias {name!r} for {field} is a label column")
                if seen.get(key, field) != field:
                    raise ValueError(f"alias {name!r} is listed for both {seen[key]} and {field}")
                seen[key] = field
        values: dict[str, str] = {}
        for direction, names in self.direction_values.items():
            for name in names:
                key = normalize_header(name)
                if values.get(key, direction) != direction:
                    raise ValueError(f"direction value {name!r} is listed for {values[key]} and {direction}")
                values[key] = direction
        return self


class PaySimBalanceColumns(_Section):
    before: str = Field(min_length=1)
    after: str = Field(min_length=1)


class PaySimConfig(_Section):
    step_epoch: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
    step_hours: int = Field(ge=1)
    direction_by_type: dict[str, DirectionValue]
    balance_columns: PaySimBalanceColumns


class AdaptersConfig(_Section):
    paysim: PaySimConfig


# ---------------------------------------------------------------------------------------------- P4 quality

# Every integrity check, in report order. Issues carry the code of the check that raised them.
QUALITY_CHECKS = (
    "missing_required",
    "missing_optional",
    "invalid_value",
    "unparseable_date",
    "implausible_date",
    "malformed_identifier",
    "exact_duplicate",
    "near_duplicate",
    "negative_amount",
    "zero_amount",
    "out_of_period",
    "balance_continuity",
    "duplicate_source_id",
    "unmapped_columns",
    "field_coverage",
    "needs_normalization",
    "ingestion_issues",
)
QualityCheck = Literal[
    "missing_required",
    "missing_optional",
    "invalid_value",
    "unparseable_date",
    "implausible_date",
    "malformed_identifier",
    "exact_duplicate",
    "near_duplicate",
    "negative_amount",
    "zero_amount",
    "out_of_period",
    "balance_continuity",
    "duplicate_source_id",
    "unmapped_columns",
    "field_coverage",
    "needs_normalization",
    "ingestion_issues",
]
Severity = Literal["info", "warning", "error", "critical"]
CanonicalField = Literal[
    "posted_at",
    "document_date",
    "amount_minor",
    "currency",
    "direction",
    "account",
    "payer_raw",
    "payee_raw",
    "reference",
    "description",
    "txn_type",
]
TextField = Literal["account", "payer_raw", "payee_raw", "reference", "description", "txn_type", "currency"]


class EscalationRule(_Section):
    check: QualityCheck
    min_share: float = Field(gt=0.0, le=1.0, description="share of all rows affected by the check")
    severity: Severity


class SeverityConfig(_Section):
    base: dict[QualityCheck, Severity]
    synthetic_overrides: dict[QualityCheck, Severity]
    escalation: list[EscalationRule]

    @model_validator(mode="after")
    def _every_check_has_a_base(self) -> "SeverityConfig":
        missing = [c for c in QUALITY_CHECKS if c not in self.base]
        if missing:
            raise ValueError(f"quality.severity.base lacks: {missing}")
        return self


class ReadinessComponent(_Section):
    weight: float = Field(gt=0.0, le=1.0)
    checks: list[QualityCheck] = Field(min_length=1)


class ReadinessConfig(_Section):
    min_severity: Severity
    blocking_severity: Severity
    max_score_when_blocking: float = Field(ge=0.0, lt=100.0)
    components: dict[str, ReadinessComponent]

    @model_validator(mode="after")
    def _checks_in_one_component(self) -> "ReadinessConfig":
        seen: dict[str, str] = {}
        for name, comp in self.components.items():
            for c in comp.checks:
                if c in seen:
                    raise ValueError(f"check {c} is in readiness components {seen[c]} and {name}")
                seen[c] = name
        return self


class DateBounds(_Section):
    earliest: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    latest: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class BalanceConfig(_Section):
    running_balance_aliases: list[str] = Field(min_length=1)
    min_convention_share: float = Field(gt=0.0, le=1.0)


class QualityConfig(_Section):
    required_fields: list[CanonicalField] = Field(min_length=1)
    optional_fields: list[CanonicalField]
    expected_ledger_fields: list[CanonicalField]
    date_bounds: DateBounds
    near_duplicate_window_days: int = Field(ge=0)
    identifier_fields: list[TextField]
    identifier_max_length: int = Field(ge=1)
    balance: BalanceConfig
    severity: SeverityConfig
    readiness: ReadinessConfig

    @model_validator(mode="after")
    def _field_lists_disjoint(self) -> "QualityConfig":
        if set(self.required_fields) & set(self.optional_fields):
            raise ValueError("a field cannot be both required and optional")
        return self


class NormalizeConfig(_Section):
    whitespace_fields: list[TextField]
    identifier_fields: list[TextField]
    case: dict[TextField, Literal["upper", "lower"]]


# ------------------------------------------------------------------------------------- P5 synthetic data


class ApprovalLimits(_Section):
    expense_claim_minor: int = Field(ge=1, description="claims at or above this need Finance approval")
    vendor_payment_minor: int = Field(ge=1, description="payments at or above this need CFO approval")


class SynthConfig(_Section):
    seed: int = Field(ge=0)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    years: int = Field(ge=2, le=5)
    scale: float = Field(gt=0.0, le=20.0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    approval_limits: ApprovalLimits
    business_hours: list[str] = Field(min_length=2, max_length=2)


# ------------------------------------------------------------------------------------- P6 feature registry

_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class FeatureCalendar(_Section):
    business_hours: list[str] = Field(min_length=2, max_length=2)
    non_working_weekdays: list[int] = Field(max_length=7)
    fiscal_year_start_month: int = Field(ge=1, le=12)
    holidays: list[str]

    @model_validator(mode="after")
    def _valid(self) -> "FeatureCalendar":
        if (
            not all(re.match(_HHMM, t) for t in self.business_hours)
            or self.business_hours[0] >= self.business_hours[1]
        ):
            raise ValueError("business_hours must be two HH:MM times, start before end")
        if not all(0 <= d <= 6 for d in self.non_working_weekdays):
            raise ValueError("non_working_weekdays are 0 (Monday) … 6 (Sunday)")
        if not all(re.match(r"^\d{4}-\d{2}-\d{2}$", d) for d in self.holidays):
            raise ValueError("holidays are YYYY-MM-DD dates")
        return self


class ApprovalThreshold(_Section):
    name: str = Field(min_length=1, max_length=64)
    amount_minor: int = Field(ge=1)


class FeatureAdapterProfile(_Section):
    counterparty: Literal["by_direction", "payee"]
    account_is_category: bool
    corporate: bool


class FeatureReportConfig(_Section):
    max_missing_share: float = Field(ge=0.0, le=1.0)
    min_distinct_values: int = Field(ge=1)


class FeaturesConfig(_Section):
    baseline_window: int = Field(ge=5)
    min_entity_history: int = Field(ge=1)
    min_peer_history: int = Field(ge=1)
    min_peers: int = Field(ge=1)
    mad_scale_floor: float = Field(gt=0.0)
    round_level_min: int = Field(ge=1, le=5)
    velocity_min_history_days: int = Field(ge=1)
    trend_window_days: int = Field(ge=7)
    trend_min_points: int = Field(ge=3)
    recent_days: int = Field(ge=1)
    calendar: FeatureCalendar
    approval_thresholds: list[ApprovalThreshold]
    adapters: dict[str, FeatureAdapterProfile]
    report: FeatureReportConfig


class LedgerSpyConfig(_Section):
    config_version: Literal[1]
    server: ServerConfig
    workspace: WorkspaceConfig
    ingest: IngestConfig
    mapping: MappingConfig
    adapters: AdaptersConfig
    quality: QualityConfig
    normalize: NormalizeConfig
    synth: SynthConfig
    features: FeaturesConfig
