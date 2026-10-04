"""Pure normalisation transforms.

Some transforms lose information (collapsing whitespace cannot be undone from the result alone). That is why
every applied transformation stores the exact original value in the transformation log: reversal always
restores the logged original, never a re-computation.
"""

import re
import unicodedata
from datetime import datetime

from ledgerspy.ingest.values import parse_decimal, to_minor_units

_WS = re.compile(r"\s+")  # Unicode-aware: includes non-breaking and other exotic spaces


def collapse_whitespace(text: str) -> str:
    """'Acme\\u00a0  Ltd ' → 'Acme Ltd'."""
    return _WS.sub(" ", text).strip()


def has_control_chars(text: str) -> bool:
    """Control (Cc) or invisible format (Cf, e.g. zero-width space) characters."""
    return any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text)


def clean_identifier(text: str) -> str:
    """Identifier normal form: Unicode NFKC, without control/zero-width characters, single spaces."""
    nfkc = unicodedata.normalize("NFKC", text)
    visible = "".join(ch for ch in nfkc if unicodedata.category(ch) not in ("Cc", "Cf"))
    return collapse_whitespace(visible)


def apply_case(text: str, mode: str) -> str:
    return text.upper() if mode == "upper" else text.lower()


def safe_pipeline(
    value: str, *, whitespace: bool, identifier: bool, case: str | None
) -> tuple[str, list[str]]:
    """Apply the configured safe transforms in a fixed order.

    Returns the result and the names of the transforms that changed it.
    """
    steps: list[str] = []
    current = value
    if identifier:
        cleaned = clean_identifier(current)
        if cleaned != current:
            steps.append("identifier_normalize")
            current = cleaned
    elif whitespace:
        collapsed = collapse_whitespace(current)
        if collapsed != current:
            steps.append("collapse_whitespace")
            current = collapsed
    if case:
        cased = apply_case(current, case)
        if cased != current:
            steps.append(f"case_{case}")
            current = cased
    return current, steps


def date_candidates(raw: str, formats: list[str]) -> list[str]:
    """Distinct ISO datetimes the raw text can mean under the configured formats (in format order)."""
    found: list[str] = []
    for fmt in formats:
        try:
            iso = datetime.strptime(raw.strip(), fmt).isoformat()
        except ValueError:
            continue
        if iso not in found:
            found.append(iso)
    return found


def amount_candidates(raw: str, digits: int) -> list[int]:
    """Distinct minor-unit amounts the raw text can mean, ignoring spaces/apostrophes used as group marks."""
    compact = raw.replace(" ", "").replace(" ", "").replace("'", "").replace("’", "")
    found: list[int] = []
    for sep in (".", ","):
        try:
            number = parse_decimal(compact, sep)
            minor = None if number is None else to_minor_units(number, digits)
        except ValueError:
            continue
        if minor is not None and minor not in found:
            found.append(minor)
    return found
