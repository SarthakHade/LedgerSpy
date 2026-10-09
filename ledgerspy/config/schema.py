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


# ------------------------------------------------------------------------------------- P12 entity resolution


class ResolutionNormalization(_Section):
    phone_country_code: str = Field(pattern=r"^\d{0,3}$")
    generic_email_domains: list[str]


class ResolutionBlocking(_Section):
    name_prefix_chars: int = Field(ge=2, le=12)
    max_block_size: int = Field(ge=2)  # larger blocks are not expanded (counted in the report)
    exhaustive_max_records: int = Field(ge=0)  # compare all pairs at or below this many records (0: never)


class ResolutionThresholds(_Section):
    probable_name: float = Field(gt=0.0, le=1.0)  # name-only: probable match (review) at or above
    review_name: float = Field(gt=0.0, le=1.0)  # name-only: ambiguous (review) at or above; below: no match
    contact_name_min: float = Field(gt=0.0, le=1.0)  # a shared contact identifier needs this name similarity
    tax_name_min: float = Field(gt=0.0, le=1.0)  # the same tax ID needs this name similarity to confirm
    typo_token: float = Field(gt=0.0, le=1.0)  # differing words this similar are spelling variants
    address_same: float = Field(gt=0.0, le=1.0)  # address similarity treated as the same address

    @model_validator(mode="after")
    def _ordered(self) -> "ResolutionThresholds":
        if not self.review_name <= self.probable_name:
            raise ValueError("resolution.thresholds: review_name must not exceed probable_name")
        return self


class ResolutionConfig(_Section):
    version: int = Field(ge=1)
    normalization: ResolutionNormalization
    blocking: ResolutionBlocking
    thresholds: ResolutionThresholds
    shared_contact_generic_min: int = Field(
        ge=2
    )  # a phone / e-mail used by this many names: a shared contact
    behaviour_bonus_max: float = Field(
        ge=0.0, le=0.2
    )  # supporting evidence never decides; it adds at most this


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
    resolution: ResolutionConfig


# ------------------------------------------------------------------------------------- P7 rules and Benford

Severity = Literal["low", "medium", "high"]


class _Rule(_Section):
    enabled: bool
    severity: Severity
    weight: float = Field(gt=0.0, le=1.0)


class DuplicateExactRule(_Rule):
    pass


class DuplicateNearRule(_Rule):
    window_days: int = Field(ge=1)
    amount_relative_tolerance: float = Field(ge=0.0, lt=0.1)


class RoundAmountRule(_Rule):
    min_rows: int = Field(ge=2)
    min_round_share: float = Field(gt=0.0, le=1.0)
    min_distinct_round_amounts: int = Field(ge=1)
    max_p_value: float = Field(gt=0.0, lt=1.0)


class StructuringRule(_Rule):
    window_days: int = Field(ge=1)
    min_parts: int = Field(ge=2)
    min_proximity: float = Field(gt=0.0, lt=1.0)


class TemporalRule(_Rule):
    routine_after_months: int = Field(ge=1)


class NewVendorLargeRule(_Rule):
    max_prior_count: int = Field(ge=0)
    min_robust_z: float = Field(gt=0.0)
    min_days_observed: int = Field(ge=0)
    peer_bases: list[Literal["counterparty", "account", "txn_type", "global"]] = Field(min_length=1)


class BalanceContinuityRule(_Rule):
    ignore_kinds: list[str]


class DormancyRule(_Rule):
    min_dormant_days: int = Field(ge=1)
    min_amount_ratio: float = Field(gt=0.0)


class PeriodEndRule(_Rule):
    period: Literal["fiscal_year", "month"]
    window_days: int = Field(ge=1, le=31)
    min_stream_rows: int = Field(ge=2)
    min_cluster: int = Field(ge=2)
    min_concentration: float = Field(gt=0.0)
    min_amount_ratio: float = Field(gt=0.0)


class VelocityRule(_Rule):
    min_velocity_ratio: float = Field(gt=1.0)
    min_count_30d: int = Field(ge=1)
    max_p_value: float = Field(gt=0.0, lt=1.0)


class RulesConfig(_Section):
    duplicate_exact: DuplicateExactRule
    duplicate_near: DuplicateNearRule
    round_amount: RoundAmountRule
    structuring: StructuringRule
    temporal: TemporalRule
    new_vendor_large: NewVendorLargeRule
    balance_continuity: BalanceContinuityRule
    dormancy: DormancyRule
    period_end: PeriodEndRule
    velocity: VelocityRule


BenfordTest = Literal["first", "first_two", "last_two"]


class BenfordSubgroups(_Section):
    dimensions: list[Literal["counterparty", "account", "txn_type", "fiscal_year"]]
    tests: list[BenfordTest] = Field(min_length=1)
    min_n: int = Field(ge=10)
    signal_bands: list[Literal["acceptable", "marginal", "nonconformity"]]
    max_example_rows: int = Field(ge=0)


class BenfordConfig(_Section):
    min_value_major: int = Field(ge=1)
    last_two_min_major: int = Field(ge=100)
    negative_amounts: Literal["exclude", "absolute"]
    min_n: dict[BenfordTest, int]
    warn_n: dict[BenfordTest, int]
    min_distinct_values: int = Field(ge=2)
    warn_range_ratio: float = Field(ge=1.0)
    z_critical: float = Field(gt=0.0)
    mad_bands: dict[Literal["first", "first_two"], list[float]]
    subgroups: BenfordSubgroups
    severity: Severity
    weight: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _complete(self) -> "BenfordConfig":
        tests = {"first", "first_two", "last_two"}
        if set(self.min_n) != tests or set(self.warn_n) != tests:
            raise ValueError("min_n and warn_n need first, first_two and last_two")
        if any(len(b) != 3 or b != sorted(b) for b in self.mad_bands.values()):
            raise ValueError("mad_bands are three increasing upper bounds (close, acceptable, marginal)")
        return self


# ------------------------------------------------------------------------------------------- P8 anomaly

# Monotone transforms applied to a P6 feature value before it is scored. signed_log1p(x) = sign(x)·ln(1 + |x|)
# compresses heavy tails (counts, days, ratios) and is defined for every real value.
AnomalyTransform = Literal["none", "signed_log1p"]
PeerDimension = Literal["counterparty", "account", "txn_type"]


class AnomalyReference(_Section):
    mode: Literal["dataset", "chronological"]
    chronological_fraction: float = Field(gt=0.0, lt=1.0)
    warmup_days: int = Field(ge=0)


class AnomalyEligibility(_Section):
    max_missing_share: float = Field(ge=0.0, lt=1.0)
    min_distinct_values: int = Field(ge=2)
    min_row_present_share: float = Field(gt=0.0, le=1.0)


class ModelFeature(_Section):
    name: str = Field(min_length=1)
    transform: AnomalyTransform


class _Detector(_Section):
    enabled: bool
    severity: Severity
    weight: float = Field(gt=0.0, le=1.0)


def _unique(names: list[str], what: str) -> None:
    if len(names) != len(set(names)):
        raise ValueError(f"{what}: a feature is listed twice")


class IsolationForestConfig(_Detector):
    random_state: int = Field(ge=0)
    n_estimators: int = Field(ge=10, le=1000)
    max_samples: int = Field(ge=16)
    min_rows: int = Field(ge=16)
    min_features: int = Field(ge=1)
    tail_fraction: float = Field(gt=0.0, lt=0.5)
    max_flagged: int = Field(ge=1)
    min_score: float = Field(ge=0.0, lt=1.0)
    max_explained_features: int = Field(ge=1)
    features: list[ModelFeature] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid(self) -> "IsolationForestConfig":
        _unique([f.name for f in self.features], "isolation_forest.features")
        return self


class RobustFeature(_Section):
    name: str = Field(min_length=1)
    transform: AnomalyTransform
    direction: Literal["higher", "lower", "both"]
    peers: list[PeerDimension] = Field(
        min_length=1
    )  # most specific first; the first sufficient group is used
    min_scale: float = Field(gt=0.0)  # resolution floor of the robust scale, in transformed units

    @model_validator(mode="after")
    def _valid(self) -> "RobustFeature":
        if len(set(self.peers)) != len(self.peers):
            raise ValueError(f"robust_peer feature {self.name}: a peer dimension is listed twice")
        return self


class RobustPeerConfig(_Detector):
    min_group_size: int = Field(ge=5)
    z_threshold: float = Field(gt=0.0)
    features: list[RobustFeature] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid(self) -> "RobustPeerConfig":
        _unique([f.name for f in self.features], "robust_peer.features")
        return self


class TailFeature(_Section):
    name: str = Field(min_length=1)
    direction: Literal["higher", "lower"]


class TailConfig(_Detector):
    fractions: list[float] = Field(min_length=1)
    min_population: int = Field(ge=10)
    features: list[TailFeature] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid(self) -> "TailConfig":
        _unique([f.name for f in self.features], "tail.features")
        if not all(0.0 < q < 0.5 for q in self.fractions) or len(set(self.fractions)) != len(self.fractions):
            raise ValueError("tail.fractions are distinct shares in (0, 0.5)")
        if self.min_population * min(self.fractions) < 1:
            raise ValueError("tail.min_population must be large enough for the smallest tail to hold a row")
        return self


class AnomalyEvaluation(_Section):
    review_capacity: list[int] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid(self) -> "AnomalyEvaluation":
        if any(k < 1 for k in self.review_capacity):
            raise ValueError("review_capacity values are positive row counts")
        return self


class AnomalyConfig(_Section):
    reference: AnomalyReference
    eligibility: AnomalyEligibility
    isolation_forest: IsolationForestConfig
    robust_peer: RobustPeerConfig
    tail: TailConfig
    evaluation: AnomalyEvaluation


# ------------------------------------------------------------------------------------------- P9 risk engine


class RiskFamily(_Section):
    detectors: list[str] = Field(min_length=1)  # P7 rule IDs / P8 detector IDs whose signals form the family
    group: str = Field(pattern=r"^[a-z_]+$")  # independent evidence source the family belongs to
    cap: float = Field(ge=0.0, lt=1.0)  # maximum family evidence (0–1)
    scored: bool  # false: reported beside the risk, never part of it (data quality)
    label: str = Field(min_length=1, max_length=60)


class RiskGroup(_Section):
    cap: float = Field(gt=0.0, lt=1.0)


class RiskTiers(_Section):
    medium: float = Field(gt=0.0, lt=100.0)
    high: float = Field(gt=0.0, lt=100.0)
    critical: float = Field(gt=0.0, lt=100.0)
    min_sources_high: int = Field(ge=1)
    min_sources_critical: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> "RiskTiers":
        if not self.medium < self.high < self.critical:
            raise ValueError("risk tiers must increase: medium < high < critical")
        if self.min_sources_critical < self.min_sources_high:
            raise ValueError("critical needs at least as many independent sources as high")
        return self


class RiskReview(_Section):
    max_high_share: float = Field(gt=0.0, le=1.0)  # high + critical rows as a share of scored rows
    max_critical_share: float = Field(gt=0.0, le=1.0)
    top_k: int = Field(ge=1)  # review queue length


class RiskEntities(_Section):
    top_k: int = Field(ge=1)
    decay: float = Field(gt=0.0, le=1.0)
    # P12: "resolved" groups counterparty streams whose names resolve to one canonical entity (confirmed
    # matches only); "name" keeps the P9 grouping by P6 party key.
    grouping: Literal["resolved", "name"]


class RiskConfig(_Section):
    families: dict[str, RiskFamily] = Field(min_length=1)
    groups: dict[str, RiskGroup]
    within_family_decay: float = Field(gt=0.0, le=1.0)
    tiers: RiskTiers
    review: RiskReview
    entities: RiskEntities
    max_contributors: int = Field(ge=1)

    @model_validator(mode="after")
    def _valid(self) -> "RiskConfig":
        seen: dict[str, str] = {}
        for name, fam in self.families.items():
            if not re.match(r"^[a-z_]+$", name):
                raise ValueError(f"risk family names are lower_snake_case: {name}")
            for d in fam.detectors:
                if d in seen:
                    raise ValueError(f"detector {d} is in two risk families ({seen[d]}, {name})")
                seen[d] = name
            if not fam.scored and fam.cap != 0.0:
                raise ValueError(f"unscored family {name} must have cap 0")
        unknown = set(self.groups) - {f.group for f in self.families.values()}
        if unknown:
            raise ValueError(f"risk groups without a family: {sorted(unknown)}")
        return self


# ------------------------------------------------------------------------------------------- P10 supervised

SplitStrategy = Literal["random_stratified", "chronological", "group"]


class SupervisedTarget(_Section):
    paysim_label_column: str = Field(min_length=1)
    synthetic_min_severity: Literal["low", "medium", "high", "critical"]
    synthetic_exclude_scenario_types: list[str]


class SupervisedFeatures(_Section):
    version: int = Field(ge=1)  # bump when the list or a transform changes
    numeric: list[str] = Field(min_length=1)
    log_transform: list[str]  # subset of numeric: signed log1p before scaling
    categorical: list[str]  # one-hot encoded with categories learnt on the training split
    max_missing_share: float = Field(ge=0.0, lt=1.0)
    ablations: dict[str, list[str]]  # name → features removed

    @model_validator(mode="after")
    def _valid(self) -> "SupervisedFeatures":
        names = self.numeric + self.categorical
        if len(names) != len(set(names)):
            raise ValueError("supervised.features: a feature is listed twice")
        if not set(self.log_transform) <= set(self.numeric):
            raise ValueError("supervised.features.log_transform must be numeric features")
        for name, removed in self.ablations.items():
            if not set(removed) <= set(names):
                raise ValueError(f"ablation {name} removes features that are not in the list")
        return self


class SupervisedSplits(_Section):
    train: float = Field(gt=0.0, lt=1.0)
    validation: float = Field(gt=0.0, lt=1.0)
    test: float = Field(gt=0.0, lt=1.0)
    strategies: list[SplitStrategy] = Field(min_length=1)

    @model_validator(mode="after")
    def _sum(self) -> "SupervisedSplits":
        if abs(self.train + self.validation + self.test - 1.0) > 1e-9:
            raise ValueError("supervised.splits: train + validation + test must be 1")
        return self


class LogisticRegressionConfig(_Section):
    l2: float = Field(gt=0.0)
    max_iter: int = Field(ge=1)
    tol: float = Field(gt=0.0)


class GradientBoostingConfig(_Section):
    n_estimators: int = Field(ge=1, le=2000)
    learning_rate: float = Field(gt=0.0, le=1.0)
    max_depth: int = Field(ge=1, le=8)
    max_bins: int = Field(ge=4, le=255)
    min_samples_leaf: int = Field(ge=1)
    l2: float = Field(ge=0.0)


class SupervisedModels(_Section):
    logistic_regression: LogisticRegressionConfig
    gradient_boosting: GradientBoostingConfig


class SupervisedBootstrap(_Section):
    resamples: int = Field(ge=10)
    seed: int = Field(ge=0)
    confidence: float = Field(gt=0.5, lt=1.0)


class SupervisedEvaluation(_Section):
    review_share: float = Field(gt=0.0, lt=0.5)  # operating point: top share of validation rows
    top_k: list[int] = Field(min_length=1)
    bootstrap: SupervisedBootstrap
    calibration_bins: int = Field(ge=2)


class SupervisedCalibration(_Section):
    method: Literal["platt"]
    min_validation_positives: int = Field(ge=1)
    max_ece: float = Field(gt=0.0, lt=1.0)


class SupervisedInference(_Section):
    batch_size: int = Field(ge=1, le=1_000_000)  # rows scored per batch (speed / memory only)


class SupervisedConfig(_Section):
    seed: int = Field(ge=0)
    target: SupervisedTarget
    features: SupervisedFeatures
    splits: SupervisedSplits
    imbalance: list[Literal["none", "balanced"]] = Field(min_length=1)
    models: SupervisedModels
    evaluation: SupervisedEvaluation
    calibration: SupervisedCalibration
    permutation_repeats: int = Field(ge=1)
    inference: SupervisedInference


# ------------------------------------------------------------------------------------------ P13 graph


class GraphCycles(_Section):
    min_length: int = Field(ge=2, le=12)
    max_length: int = Field(ge=2, le=12)  # bounded enumeration: longer cycles are never searched
    max_cycles: int = Field(ge=1)  # enumeration stops here (reported as truncated)
    max_search_steps: int = Field(ge=1000)  # DFS expansions; reaching it is reported as truncated
    window_days: int = Field(ge=1)  # a time-ordered chain must complete within this many days
    min_edge_txns: int = Field(ge=1)  # money edges with fewer transactions are not searched
    min_edge_amount_minor: int = Field(ge=0)
    retention_min: float = Field(gt=0.0, le=1.0)  # min/max chain amount at or above: "value retained"
    max_chains: int = Field(ge=1)  # ordered chains evaluated per cycle

    @model_validator(mode="after")
    def _bounds(self) -> "GraphCycles":
        if self.min_length > self.max_length:
            raise ValueError("graph.cycles.min_length must not exceed max_length")
        return self


class GraphCentrality(_Section):
    pagerank_damping: float = Field(gt=0.0, lt=1.0)
    pagerank_tol: float = Field(gt=0.0)
    pagerank_max_iter: int = Field(ge=1)
    betweenness_max_nodes: int = Field(ge=0)  # above this, betweenness is reported unavailable (not sampled)
    betweenness_max_work: int = Field(ge=0)  # nodes × money edges above this: unavailable (Brandes is O(n·m))
    high_percentile: float = Field(gt=0.5, lt=1.0)  # graph.high_betweenness: at or above this peer percentile


class GraphSignals(_Section):
    reciprocal_window_days: int = Field(ge=1)
    reciprocal_amount_ratio: float = Field(gt=0.0, le=1.0)
    pass_through_window_days: int = Field(ge=1)
    pass_through_ratio_low: float = Field(gt=0.0, le=1.0)
    pass_through_ratio_high: float = Field(ge=1.0)
    pass_through_min_new_counterparties: int = Field(ge=2)
    pass_through_warmup_days: int = Field(ge=0)  # counterparties first seen this early are not "new"
    shared_identifier_cluster_min: int = Field(ge=2)


class GraphLimits(_Section):
    max_page_size: int = Field(ge=1, le=5000)
    visualization_max_nodes: int = Field(ge=1)
    visualization_max_edges: int = Field(ge=1)
    neighbourhood_max_edges: int = Field(ge=1)
    edge_max_transactions: int = Field(ge=1)  # transactions listed per edge detail page (all remain counted)


class GraphConfig(_Section):
    version: int = Field(ge=1)
    cycles: GraphCycles
    centrality: GraphCentrality
    signals: GraphSignals
    limits: GraphLimits


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
    rules: RulesConfig
    benford: BenfordConfig
    anomaly: AnomalyConfig
    risk: RiskConfig
    supervised: SupervisedConfig
    graph: GraphConfig
