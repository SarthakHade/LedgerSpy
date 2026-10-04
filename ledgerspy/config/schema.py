"""Configuration schema.

The schema has no default values: config/default.yaml is the only place values are set, so there is
exactly one source for each of them. Unknown keys and wrong types are rejected.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LOOPBACK_HOST = "127.0.0.1"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ServerConfig(_Section):
    host: Literal["127.0.0.1"] = Field(description="Bind address. Loopback only, by design.")
    port: int = Field(ge=1024, le=65535)


class WorkspaceConfig(_Section):
    path: str = Field(min_length=1)
    db_filename: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+\.db$")


class LedgerSpyConfig(_Section):
    config_version: Literal[1]
    server: ServerConfig
    workspace: WorkspaceConfig
