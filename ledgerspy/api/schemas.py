"""Request/response contracts of the API."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    schema_version: int
    config_sha256: str


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    client: str | None = Field(default=None, max_length=200)
    period_start: date | None = None
    period_end: date | None = None

    @model_validator(mode="after")
    def _period_order(self) -> "CaseCreate":
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("period_end is before period_start")
        return self


class CaseOut(BaseModel):
    case_id: str
    name: str
    client: str | None
    period_start: date | None
    period_end: date | None
    status: Literal["open", "in_review", "closed"]
    created_by: str
    created_at: str
