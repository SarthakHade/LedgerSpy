"""Canonical serialisation and hashing shared by config hashing, the audit log and IDs."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, NaN/Infinity rejected."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    """UTC timestamp with microseconds, e.g. 2026-10-04T12:00:00.123456Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
