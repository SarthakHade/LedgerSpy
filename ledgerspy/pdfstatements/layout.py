"""Statement layout: words → lines → classified items, with one explicit, testable strategy.

Strategy `header_columns` (the only one in P14b):
1. Words are grouped into lines by vertical centre (`layout.line_tolerance` × median word height).
2. A TABLE HEADER is a line whose cells (words closer than `layout.header_gap_factor` × height) name known
   columns: a date, an amount source (debit + credit, or amount) and at least `layout.min_header_roles` roles
   in all. A role named twice makes the header ambiguous (rejected, reported).
3. Column boundaries come from the page's own body text: between two header cells, the boundary is the middle
   of the widest vertical whitespace channel (x positions covered by the fewest body words) between the end
   of the left header cell and the start of the right one — each header stays inside its own column, while a
   left-aligned narration may run far past its header and a right-aligned amount may start left of its own.
   Without body text, or when header cells touch, the gap midpoint is used. Each word goes to the column
   holding its centre; a word reaching more than `layout.straddle_share` of its width across a boundary is
   UNCERTAIN.
4. Lines below a header are classified: opening / closing / carried-forward balance lines, totals,
   transactions (a date in the date column), continuations (narration-only lines directly below a
   transaction, inside the narration column), page footers, statement information (account, currency, period,
   stated balances, by label) and `unparsed` — kept with a reason, never dropped.
5. A page without its own header reuses the previous page's columns (`columns_carried`, review-required).

Pages without any recognised header: every line is kept, statement information is still read, and the page is
reported as `layout_not_recognised` (no transactions are guessed from free text).
"""

import re
from statistics import median
from typing import Any

from ledgerspy.ingest.values import fits_format

ROLE_PHRASES: dict[str, list[str]] = {
    "date": [
        "date",
        "txn date",
        "tran date",
        "transaction date",
        "posting date",
        "post date",
        "trans date",
        "book date",
        "txn dt",
        "tran dt",
    ],
    "value_date": ["value date", "value dt", "val date", "val dt"],
    "description": [
        "narration",
        "description",
        "particulars",
        "details",
        "transaction details",
        "remarks",
        "transaction description",
        "transaction remarks",
    ],
    "reference": [
        "ref",
        "ref no",
        "reference",
        "reference no",
        "chq no",
        "cheque no",
        "chq/ref no",
        "chq ref no",
        "cheque/ref no",
        "utr",
        "utr no",
        "instrument no",
        "chq/ref number",
    ],
    "debit": [
        "withdrawal",
        "withdrawals",
        "withdrawal amt",
        "withdrawal amount",
        "debit",
        "debits",
        "debit amount",
        "debit amt",
        "dr",
        "dr amount",
        "paid out",
        "money out",
        "payments",
    ],
    "credit": [
        "deposit",
        "deposits",
        "deposit amt",
        "deposit amount",
        "credit",
        "credits",
        "credit amount",
        "credit amt",
        "cr",
        "cr amount",
        "paid in",
        "money in",
        "receipts",
    ],
    "amount": ["amount", "transaction amount", "txn amount"],
    "drcr": ["dr/cr", "cr/dr", "type", "dr cr"],
    "balance": [
        "balance",
        "closing balance",
        "running balance",
        "balance amt",
        "balance amount",
        "available balance",
    ],
}
OUTFLOW_LITERALS = {
    "withdrawal",
    "withdrawals",
    "withdrawal amt",
    "withdrawal amount",
    "paid out",
    "money out",
    "payments",
}
INFLOW_LITERALS = {"deposit", "deposits", "deposit amt", "deposit amount", "paid in", "money in", "receipts"}

_KEY = re.compile(r"[^a-z0-9/]+")
_CUR_IN_HEADER = re.compile(r"\(([A-Z]{3})\)")
OPENING = re.compile(r"\b(opening balance|balance b/?f|balance brought forward|brought forward|b/f)\b", re.I)
CLOSING = re.compile(r"\b(closing balance|balance c/?f|carried forward|c/f)\b", re.I)
CARRIED = re.compile(r"\b(b/?f|c/?f|brought forward|carried forward)\b", re.I)
TOTAL = re.compile(r"^\s*(grand\s+)?totals?\b", re.I)
PAGE_FOOTER = re.compile(r"\bpage\s+\d+(\s*(of|/)\s*\d+)?\b", re.I)
NUMBER = re.compile(r"-?\(?\d[\d,]*(\.\d+)?\)?(\s*(cr|dr))?$", re.I)
LABELS = {
    "account": re.compile(
        r"\b(?:account|a/c|acct)\.?\s*(?:no\.?|number|#)?\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9\-]{3,34})", re.I
    ),
    "currency": re.compile(r"\b(?i:currency)\s*[:\-]\s*([A-Z]{3})\b"),
    "period": re.compile(
        r"\b(?:statement\s+)?period\s*[:\-]?\s*(?:from\s+)?(\S+)\s+(?:to|-|–)\s+(\S+)", re.I
    ),
    "opening_balance": re.compile(r"\bopening\s+balance\s*[:\-]\s*(-?[\d,]+(?:\.\d+)?)(\s*(?:cr|dr))?", re.I),
    "closing_balance": re.compile(r"\bclosing\s+balance\s*[:\-]\s*(-?[\d,]+(?:\.\d+)?)(\s*(?:cr|dr))?", re.I),
}


def key(text: str) -> str:
    k = _KEY.sub(" ", text.casefold()).strip()
    return re.sub(r"\s*/\s*", "/", k)


_PHRASE_ROLE = {key(p): role for role, ps in ROLE_PHRASES.items() for p in ps}


def role_of(literal: str) -> str | None:
    return _PHRASE_ROLE.get(key(_CUR_IN_HEADER.sub("", literal)))


# --------------------------------------------------------------------------------------------- lines


def lines_of(words: list[dict[str, Any]], tol: float) -> list[dict[str, Any]]:
    if not words:
        return []
    mh = median(max(w["bottom"] - w["top"], 0.5) for w in words)
    out: list[dict[str, Any]] = []
    for w in sorted(words, key=lambda w: ((w["top"] + w["bottom"]) / 2, w["x0"])):
        c = (w["top"] + w["bottom"]) / 2
        if out and abs(c - out[-1]["center"]) <= tol * mh:
            ln = out[-1]
            ln["words"].append(w)
            n = len(ln["words"])
            ln["center"] = ln["center"] + (c - ln["center"]) / n
        else:
            out.append({"words": [w], "center": c})
    for ln in out:
        ln["words"].sort(key=lambda w: w["x0"])
        ln["text"] = " ".join(w["text"] for w in ln["words"])
        ln["bbox"] = [
            round(min(w["x0"] for w in ln["words"]), 2),
            round(min(w["top"] for w in ln["words"]), 2),
            round(max(w["x1"] for w in ln["words"]), 2),
            round(max(w["bottom"] for w in ln["words"]), 2),
        ]
        ln["height"] = max(ln["bbox"][3] - ln["bbox"][1], 0.5)
    return out


def cells_of(line: dict[str, Any], gap_factor: float) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for w in line["words"]:
        h = max(w["bottom"] - w["top"], 0.5)
        if cells and w["x0"] - cells[-1]["x1"] <= gap_factor * h:
            cells[-1]["text"] += " " + w["text"]
            cells[-1]["x1"] = w["x1"]
        else:
            cells.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"]})
    return cells


# --------------------------------------------------------------------------------------------- header


def detect_header(
    line: dict[str, Any], cfg: dict[str, Any]
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Columns [{role, literal, x0, x1, currency}] or (None, why-not) for a candidate header line."""
    cells = cells_of(line, cfg["header_gap_factor"])
    cols, roles = [], []
    for c in cells:
        role = role_of(c["text"])
        m = _CUR_IN_HEADER.search(c["text"])
        cols.append(
            {
                "role": role or "other",
                "literal": c["text"],
                "x0": c["x0"],
                "x1": c["x1"],
                "currency": m.group(1) if m else None,
            }
        )
        if role:
            roles.append(role)
    found = set(roles)
    amount_source = "amount" in found or {"debit", "credit"} <= found
    if "date" not in found or not amount_source or len(found) < cfg["min_header_roles"]:
        return None, None
    dup = sorted(r for r in found if roles.count(r) > 1)
    if dup:
        return None, f"header names {dup} more than once: column roles are ambiguous"
    return cols, None


def boundaries(cols: list[dict[str, Any]], body: list[dict[str, Any]] | None = None) -> list[float]:
    """Right boundary of every column but the last (see the module docstring, step 3)."""
    spans = [(w["x0"], w["x1"]) for ln in (body or []) for w in ln["words"]]
    out = []
    for i in range(len(cols) - 1):
        lo, hi = cols[i]["x1"], cols[i + 1]["x0"]
        mid = (lo + hi) / 2
        if not spans or hi - lo < 1.0:
            out.append(mid)
            continue
        step = 0.5
        n = int((hi - lo) / step) + 1
        cover = [0] * n
        for x0, x1 in spans:
            a, b = max(int((x0 - lo) / step), 0), min(int((x1 - lo) / step), n - 1)
            for k in range(a, b + 1):
                cover[k] += 1
        least = min(cover)
        best, run_start = (
            None,
            None,
        )  # widest run of minimal coverage; ties → the run nearest the gap midpoint
        for k in range(n + 1):
            if k < n and cover[k] == least:
                run_start = k if run_start is None else run_start
                continue
            if run_start is not None:
                width, centre = k - run_start, lo + (run_start + k - 1) / 2 * step
                cand = (width, -abs(centre - mid), centre)
                best = cand if best is None or cand[:2] > best[:2] else best
                run_start = None
        out.append(round(best[2], 2) if best else mid)
    return out


def with_boundaries(cols: list[dict[str, Any]], body: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bounds = boundaries(cols, body)
    return [c | {"right": bounds[i] if i < len(bounds) else None} for i, c in enumerate(cols)]


def assign(line: dict[str, Any], cols: list[dict[str, Any]], straddle: float) -> dict[str, Any]:
    """Cells of a body line by column, with per-cell boxes, OCR confidences and uncertain words."""
    bounds = [c["right"] for c in cols[:-1]] if "right" in cols[0] else boundaries(cols)
    cells: dict[int, dict[str, Any]] = {}
    uncertain = []
    for w in line["words"]:
        cx = (w["x0"] + w["x1"]) / 2
        i = sum(1 for b in bounds if cx > b)
        lo = bounds[i - 1] if i > 0 else float("-inf")
        hi = bounds[i] if i < len(bounds) else float("inf")
        width = max(w["x1"] - w["x0"], 0.1)
        over = max(0.0, lo - w["x0"]) + max(0.0, w["x1"] - hi)
        if over / width > straddle:
            uncertain.append(w["text"])
        c = cells.setdefault(i, {"words": []})
        c["words"].append(w)
    out = {}
    for i, c in cells.items():
        ws = c["words"]
        confs = [w["conf"] for w in ws if w["conf"] is not None]
        out[i] = {
            "text": " ".join(w["text"] for w in ws),
            "bbox": [
                min(w["x0"] for w in ws),
                min(w["top"] for w in ws),
                max(w["x1"] for w in ws),
                max(w["bottom"] for w in ws),
            ],
            "min_conf": min(confs) if confs else None,
        }
    return {"cells": out, "uncertain_words": uncertain}


# --------------------------------------------------------------------------------------------- classify


def is_date(text: str | None, formats: list[str]) -> bool:
    return bool(text) and any(fits_format(text.strip(), f) is not None for f in formats)


def statement_info(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for name, rx in LABELS.items():
        m = rx.search(text)
        if not m:
            continue
        if name == "period":
            info[name] = {"from": m.group(1), "to": m.group(2)}
        elif name in ("opening_balance", "closing_balance"):
            info[name] = {"value": m.group(1), "indicator": (m.group(2) or "").strip().upper() or None}
        else:
            info[name] = m.group(1).strip()
    return info


def classify_page(
    lines: list[dict[str, Any]],
    carried: list[dict[str, Any]] | None,
    cfg: dict[str, Any],
    formats: list[str],
    seen_transaction: bool,
) -> dict[str, Any]:
    """Items of one page (every line once) + the columns in force at its end."""
    items: list[dict[str, Any]] = []
    cols = None
    cols_source = None
    warnings: list[str] = []
    for k, ln in enumerate(lines):
        cand, why = detect_header(ln, cfg)
        if cand is not None:
            cols, cols_source = with_boundaries(cand, _body_below(lines, k + 1, cfg)), "page"
            items.append({"kind": "table_header", "line": ln, "columns": cols})
            continue
        if why:
            warnings.append(why)
        if cols is None and carried is not None and _plausible_body(ln, carried, formats):
            cols, cols_source = with_boundaries(carried, _body_below(lines, k, cfg)), "carried"
        if PAGE_FOOTER.search(ln["text"]) and len(ln["text"]) < 40:
            items.append({"kind": "page_footer", "line": ln})
            continue
        if cols is None:
            info = statement_info(ln["text"])
            items.append({"kind": "statement_info" if info else "page_header", "line": ln, "info": info})
            continue
        a = assign(ln, cols, cfg["straddle_share"])
        by_role = {cols[i]["role"]: c for i, c in a["cells"].items()}
        text = ln["text"]
        date_txt = by_role.get("date", {}).get("text")
        prev = next((it for it in reversed(items) if it["kind"] in ("transaction", "continuation")), None)
        base = {
            "line": ln,
            "cells": a["cells"],
            "roles": by_role,
            "uncertain_words": a["uncertain_words"],
            "columns_source": cols_source,
        }
        info = statement_info(text)
        if OPENING.search(text) and not is_date(date_txt, formats) or info.get("opening_balance"):
            kind = "carry_forward" if (seen_transaction and CARRIED.search(text)) else "opening_balance"
            items.append({"kind": kind, **base, "info": info})
        elif CLOSING.search(text) and not is_date(date_txt, formats) or info.get("closing_balance"):
            kind = "carry_forward" if CARRIED.search(text) else "closing_balance"
            items.append({"kind": kind, **base, "info": info})
        elif TOTAL.search(text):
            items.append({"kind": "total", **base})
        elif is_date(date_txt, formats):
            items.append({"kind": "transaction", **base})
            seen_transaction = True
        elif _continuation(ln, by_role, prev, cols):
            items.append({"kind": "continuation", **base})
        elif info:
            items.append({"kind": "statement_info", "line": ln, "info": info})
        else:
            reason = "no date in the date column" if by_role else "no recognisable cells"
            if date_txt:
                reason = f"date column value {date_txt!r} is not a date in any configured format"
            items.append({"kind": "unparsed", **base, "reason": reason})
    return {"items": items, "columns": cols, "warnings": warnings, "seen_transaction": seen_transaction}


def _body_below(lines: list[dict[str, Any]], start: int, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Lines from `start` up to the next header line (the body the boundaries are measured on)."""
    out = []
    for ln in lines[start:]:
        if detect_header(ln, cfg)[0] is not None:
            break
        out.append(ln)
    return out


def _plausible_body(ln: dict[str, Any], cols: list[dict[str, Any]], formats: list[str]) -> bool:
    """A line that looks like a transaction under the carried columns (a date where the date column is)."""
    a = assign(ln, cols, 1.0)
    by_role = {cols[i]["role"]: c for i, c in a["cells"].items()}
    return is_date(by_role.get("date", {}).get("text"), formats)


def _continuation(ln, by_role, prev, cols) -> bool:
    if prev is None or prev["kind"] not in ("transaction", "continuation"):
        return False
    if set(by_role) - {"description", "reference", "other"} or "description" not in by_role:
        return False
    gap = ln["bbox"][1] - prev["line"]["bbox"][3]
    return gap <= 1.5 * ln["height"]
