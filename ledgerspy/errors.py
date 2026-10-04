"""Base error type: stable machine code, human message, structured details, matching HTTP status."""

from typing import Any


class LedgerSpyError(Exception):
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
