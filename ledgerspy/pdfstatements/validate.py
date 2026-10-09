"""Field parsing, review rules and statement-level validation for extracted rows.

Three kinds of confidence are kept apart and never merged into one number:
- TEXT: was the text read from the PDF's embedded text layer (exact glyph codes; replacement characters are
  counted) or recognised by OCR, or taken from a text layer lying over a page image (someone else's OCR)?
- OCR: Tesseract's per-word recognition confidence (0–100) where OCR ran — glyph recognition only.
- PARSE: per field, whether the cell text is a valid value of its kind (`ok`, `missing`, `invalid`).
None of them says the value is financially correct. A row is REVIEW-REQUIRED when any reason applies; every
OCR-derived row is review-required, whatever its OCR confidence.

Statement validation (balance continuity, stated opening / closing balances) runs only when the direction
semantics are known (headers stating withdrawal / deposit, or the confirmed mapping) and balances carry no
Dr/Cr indicators; otherwise it is `not_evaluated` with the reason.
"""

from decimal import Decimal
from typing import Any

from ledgerspy.ingest.values import parse_decimal
from ledgerspy.pdfstatements.layout import NUMBER, is_date

AMOUNT_ROLES = ("debit", "credit", "amount", "balance")


def number(text: str | None, sep: str) -> Decimal | None:
    if not text:
        return None
    t = text.strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    if not NUMBER.match(t) or t.lower().endswith(("cr", "dr")):
        return None
    try:
        v = parse_decimal(t, sep)
    except ValueError:
        return None
    return None if v is None else (-Decimal(v) if neg else Decimal(v))


def indicator(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip().lower()
    return "cr" if t.endswith("cr") else "dr" if t.endswith("dr") else None


def decimal_separator(items: list[dict[str, Any]], min_rate: float) -> str | None:
    """The statement's decimal separator from the evidence of the amount cells: values that parse under
    exactly one reading vote for it, and one reading must hold at least `min_rate` (P3's
    `mapping.min_parse_rate`) of those votes. Values that parse under both must then agree (e.g. '10.00'
    cannot be read with ','; '1,234' is ambiguous). Values that parse under neither are flagged on their own
    rows. None when ambiguous."""
    texts = [c for it in items for r, c in it["fields"].items() if r in AMOUNT_ROLES and c]
    texts = [t[:-2].strip() if indicator(t) else t for t in texts]
    if not texts:
        return "."

    fit = {t: {s for s in (".", ",") if number(t, s) is not None} for t in texts}
    only = {s: sum(1 for f in fit.values() if f == {s}) for s in (".", ",")}  # values only one reading parses
    both = [t for t, f in fit.items() if len(f) == 2]
    # values parsing under neither are left out of the decision and flagged invalid on their rows
    if not only["."] and not only[","]:
        if not both:
            return None
        return "." if all(number(t, ".") == number(t, ",") for t in both) else None
    for s, other in ((".", ","), (",", ".")):
        if only[s] / (only[s] + only[other]) >= min_rate:
            return s
    return None


def review_row(it: dict[str, Any], formats: list[str], sep: str | None, min_conf: float, auto_accept: bool):
    """Sets it['parse'], it['reasons'], it['review_required'] for a transaction item."""
    reasons: list[str] = []
    parse: dict[str, str] = {}
    f = it["fields"]
    for role in ("date", "value_date"):
        if role in it["roles_present"]:
            parse[role] = "missing" if not f.get(role) else "ok" if is_date(f[role], formats) else "invalid"
    for role in AMOUNT_ROLES:
        if role not in it["roles_present"]:
            continue
        txt = f.get(role)
        core = txt[:-2].strip() if role == "balance" and indicator(txt) else txt
        if not txt:
            parse[role] = "missing"
        elif sep is None:
            parse[role] = "unknown_separator"
        else:
            parse[role] = "ok" if number(core, sep) is not None else "invalid"
    if parse.get("value_date") == "invalid":
        reasons.append("invalid_value_date")
    for role in AMOUNT_ROLES:
        if parse.get(role) == "invalid":
            reasons.append(f"invalid_{role}")
        if parse.get(role) == "unknown_separator":
            reasons.append("decimal_separator_ambiguous")
    has_dc = f.get("debit") or f.get("credit")
    if f.get("debit") and f.get("credit"):
        reasons.append("debit_and_credit_both_set")
    if not has_dc and not f.get("amount"):
        reasons.append("no_amount")
    if it["method"] == "ocr":
        reasons.append("ocr_derived")
        low = [w for w in it.get("ocr_words", []) if w["conf"] < min_conf]
        if low:
            reasons.append("low_ocr_confidence")
            it["low_confidence_words"] = [{"text": w["text"], "conf": w["conf"]} for w in low]
    if it["method"] == "text_over_image":
        reasons.append("text_layer_over_image")
    if it.get("replacement_chars"):
        reasons.append("unmapped_glyphs")
    if it.get("uncertain_words"):
        reasons.append("column_assignment_uncertain")
    if it.get("columns_source") == "carried":
        reasons.append("columns_carried_from_previous_page")
    if not auto_accept and not reasons:
        reasons.append("review_policy_all_rows")
    it["parse"] = parse
    it["reasons"] = sorted(set(reasons))
    it["review_required"] = bool(it["reasons"])


def statement_checks(
    txns: list[dict[str, Any]],
    stated: dict[str, Any],
    debit_means: str | None,
    sep: str | None,
) -> dict[str, Any]:
    """Balance continuity and stated opening / closing balances, only when the semantics are known."""
    if debit_means is None:
        return {
            "status": "not_evaluated",
            "reason": "the debit / credit meaning is not established (the headers do "
            "not state withdrawal / deposit and no mapping is confirmed yet)",
        }
    if sep is None:
        return {"status": "not_evaluated", "reason": "the decimal separator of the amounts is ambiguous"}
    if any(indicator(t["fields"].get("balance")) for t in txns):
        return {
            "status": "not_evaluated",
            "reason": "balances carry Dr/Cr indicators: their sign convention is not established",
        }
    if any("amount" in t["roles_present"] and "debit" not in t["roles_present"] for t in txns):
        return {
            "status": "not_evaluated",
            "reason": "a single amount column: its sign convention must be confirmed in the mapping",
        }
    out: dict[str, Any] = {"status": "evaluated", "debit_means": debit_means, "breaks": [], "checks": None}
    sign_debit = -1 if debit_means == "outflow" else 1
    groups: dict[Any, list[dict[str, Any]]] = {}
    for t in txns:  # continuity is per account: one PDF may hold several statements
        groups.setdefault((t.get("context") or {}).get("account"), []).append(t)
    out["accounts"] = []
    for account, rows in groups.items():
        chain = _chain(rows, sign_debit, sep, out["breaks"])
        out["accounts"].append(
            {"account": account, "rows": len(rows)}
            | {k: (None if v is None else str(v)) if k != "rows_complete" else v for k, v in chain.items()}
        )
    if len(groups) > 1:
        out["note"] = (
            "several accounts in one PDF: continuity is checked per account; stated opening / closing "
            "balances are not attributed to an account and are not compared"
        )
        return out
    chain = _chain([dict(t) for t in txns], sign_debit, sep, [])
    op = number((stated.get("opening_balance") or {}).get("value"), sep)
    cl = number((stated.get("closing_balance") or {}).get("value"), sep)
    first_implied, last_bal = chain["implied_opening"], chain["last_balance"]
    out["checks"] = {
        "rows_complete": chain["rows_complete"],
        "implied_opening": None if first_implied is None else str(first_implied),
        "stated_opening": None if op is None else str(op),
        "opening_matches": None if op is None or first_implied is None else op == first_implied,
        "last_balance": None if last_bal is None else str(last_bal),
        "stated_closing": None if cl is None else str(cl),
        "closing_matches": None if cl is None or last_bal is None else cl == last_bal,
        "opening_plus_movement_equals_closing": None
        if op is None or cl is None or not chain["rows_complete"]
        else op + chain["movement"] == cl,
        "movement": str(chain["movement"]),
    }
    return out


def _chain(
    txns: list[dict[str, Any]], sign_debit: int, sep: str, breaks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Walk one account's rows in statement order; record breaks (and flag those rows) into `breaks`."""
    prev_bal = None
    movement = Decimal(0)
    first_implied = None
    last_bal = None
    complete = True
    for t in txns:
        d, c, b = (number(t["fields"].get(r), sep) for r in ("debit", "credit", "balance"))
        mv = (d or 0) * sign_debit + (c or 0) * -sign_debit
        if d is None and c is None:
            complete = False
            prev_bal = b
            continue
        movement += mv
        if b is None:
            complete = False
            prev_bal = None
            continue
        if first_implied is None:
            first_implied = b - mv
        if prev_bal is not None and prev_bal + mv != b:
            breaks.append(
                {
                    "item_no": t["item_no"],
                    "page": t["page"],
                    "expected": str(prev_bal + mv),
                    "stated": str(b),
                    "difference": str(b - (prev_bal + mv)),
                }
            )
            t["reasons"] = sorted(set(t["reasons"]) | {"balance_discontinuity"})
            t["review_required"] = True
        prev_bal = b
        last_bal = b
    return {
        "rows_complete": complete,
        "implied_opening": first_implied,
        "last_balance": last_bal,
        "movement": movement,
    }
