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


class PaySimConfig(_Section):
    step_epoch: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
    step_hours: int = Field(ge=1)
    direction_by_type: dict[str, DirectionValue]


class AdaptersConfig(_Section):
    paysim: PaySimConfig


class LedgerSpyConfig(_Section):
    config_version: Literal[1]
    server: ServerConfig
    workspace: WorkspaceConfig
    ingest: IngestConfig
    mapping: MappingConfig
    adapters: AdaptersConfig
