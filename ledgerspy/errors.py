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


class InvariantError(LedgerSpyError):
    """An internal consistency check failed (e.g. a row counted twice in a total). Raised explicitly, not by
    `assert`, so it also holds under `python -O`: the operation is refused rather than return wrong totals."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("internal_invariant_failed", message, details, http_status=500)


def require(condition: bool, message: str, **details: Any) -> None:
    if not condition:
        raise InvariantError(message, details or None)
