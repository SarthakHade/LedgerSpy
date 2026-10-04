"""Explicit, structured ingestion errors. Every rejection carries a stable code and a human message."""

from typing import Any


class IngestError(Exception):
    """A file (or mapping) is rejected. `http_status` is the API status the error maps to."""

    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None, http_status: int = 422
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}
