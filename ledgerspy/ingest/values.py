"""Strict value parsing. Nothing is guessed: a value either parses under the declared convention or fails."""

import math
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

Cell = str | int | float | bool | datetime | date | time | None

_CURRENCY_SYMBOLS = "₹$€£¥"


def is_blank(value: Cell) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def cell_text(value: Cell) -> str | None:
    """Text form of a cell for text fields (XLSX numbers like 1001.0 become "1001")."""
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value).strip()


def json_cell(value: Cell) -> str | int | float | bool | None:
    """JSON-safe, otherwise verbatim, representation of a cell (for `extra` and labels)."""
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _number_pattern(decimal_sep: str) -> re.Pattern[str]:
    d = re.escape(decimal_sep)
    t = re.escape("," if decimal_sep == "." else ".")
    # plain digits, western grouping (1,234,567) or Indian grouping (12,34,567); optional fraction
    return re.compile(rf"^(?:\d+|\d{{1,3}}(?:{t}\d{{3}})+|\d{{1,2}}(?:{t}\d{{2}})*{t}\d{{3}})(?:{d}\d+)?$")


_PATTERNS = {".": _number_pattern("."), ",": _number_pattern(",")}


def parse_decimal(value: Cell, decimal_sep: str) -> Decimal | None:
    """Parse an amount. Returns None for blanks; raises ValueError when the value is not a number.

    Accepts a sign, accounting negatives "(1,234.50)", grouping separators and a leading currency symbol.
    """
    if is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return Decimal(repr(value))
    if not isinstance(value, str):
        raise ValueError(f"{type(value).__name__} is not an amount")
    s = value.strip().replace(" ", "")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s[:1] in "+-":
        negative, s = negative != (s[0] == "-"), s[1:].strip()
    if s[:1] and s[0] in _CURRENCY_SYMBOLS:
        s = s[1:].strip()
    if not _PATTERNS[decimal_sep].match(s):
        raise ValueError(f"not a number under decimal separator {decimal_sep!r}")
    thousands = "," if decimal_sep == "." else "."
    s = s.replace(thousands, "").replace(decimal_sep, ".")
    try:
        number = Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover — the pattern already guarantees a number
        raise ValueError("not a number") from exc
    return -number if negative else number


def to_minor_units(amount: Decimal, digits: int) -> int:
    """Exact conversion to minor units. Raises ValueError rather than rounding."""
    scaled = amount.scaleb(digits)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"has more than {digits} decimal places")
    return int(scaled)


def parse_datetime(value: Cell, fmt: str | None) -> datetime | None:
    """Parse a posting date. Excel date cells are used as-is; text needs the declared strptime format."""
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time())
    if isinstance(value, str):
        if fmt is None:
            raise ValueError("no date format declared for text dates")
        return datetime.strptime(value.strip(), fmt)
    raise ValueError(f"{type(value).__name__} is not a date (Excel serial numbers are not guessed)")


def fits_format(text: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), fmt)
    except ValueError:
        return None
