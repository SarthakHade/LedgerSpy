"""Bank balance checks: only what the statement itself supports (docs/reconciliation.md §7).

Needs the confirmed running-balance column (`Mapping.bank.balance_column`) and in/out directions. Per account
and currency, rows are taken in FILE order (the statement's own order). The order is accepted only if the
balance transitions it implies, `balance[i] = balance[i-1] ± amount[i]` (in raises, out lowers), hold for at
least `balance.min_order_share` of transitions; newest-first statements are recognised the same way in
reverse. Otherwise the check is `unavailable` with the measured shares; nothing is guessed.

Reported per account / currency: the implied opening balance (first row's balance minus its own movement —
derived from the source, never invented), total movement, closing balance (last row's balance), whether
opening + movement = closing, every discontinuity (expected vs stated balance, the difference, the two rows),
and, when a statement-identifier column is confirmed, each statement period's opening / closing and whether
it continues the previous period's closing balance.
"""

from collections import defaultdict
from decimal import Decimal
from typing import Any

from ledgerspy.config.schema import LedgerSpyConfig
from ledgerspy.ingest.values import parse_decimal, to_minor_units
from ledgerspy.reconciliation.records import account_key, account_label, alnum


def _minor(value: Any, sep: str, digits: int) -> int | None:
    try:
        n = parse_decimal(value, sep)
        return None if n is None else to_minor_units(Decimal(n), digits)
    except ValueError:
        return None


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "accounts": []}


def check(
    view_rows: list[dict[str, Any]], mapping: dict[str, Any], settings: LedgerSpyConfig
) -> dict[str, Any]:
    bank = mapping.get("bank") or {}
    col = bank.get("balance_column")
    if not col:
        return unavailable("the statement has no confirmed running-balance column (bank.balance_column)")
    stmt_col = bank.get("statement_column")
    sep = mapping["options"].get("decimal_separator") or "."
    mu = settings.ingest.minor_units
    min_share = settings.reconciliation.balance.min_order_share
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in sorted(view_rows, key=lambda r: r["source_row"]):
        groups[(alnum(r.get("account")), r.get("currency"))].append(r)
    accounts = []
    for (acc, cur), rows in sorted(groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        accounts.append(
            _account(
                acc,
                cur,
                rows,
                col,
                stmt_col,
                sep,
                mu.by_currency.get(cur, mu.default_digits) if cur else mu.default_digits,
                min_share,
            )
        )
    statuses = {a["status"] for a in accounts}
    overall = (
        "consistent"
        if statuses == {"consistent"}
        else "discontinuities"
        if "discontinuities" in statuses
        else "partial"
        if "consistent" in statuses
        else "unavailable"
    )
    return {"status": overall, "column": col, "statement_column": stmt_col, "accounts": accounts}


def _account(acc, cur, rows, col, stmt_col, sep, digits, min_share) -> dict[str, Any]:
    head = {
        "account_key": account_key(acc),
        "account": account_label(acc),
        "currency": cur,
        "rows": len(rows),
    }
    if cur is None:
        return head | {
            "status": "unavailable",
            "reason": "rows without a currency: balances are not compared",
        }
    seq = []
    unusable = 0
    for r in rows:
        bal = _minor(r["extra"].get(col), sep, digits)
        amt, d = r.get("amount_minor"), r.get("direction")
        mv = None if amt is None or d not in ("in", "out") else (amt if d == "in" else -amt)
        if bal is None or mv is None:
            unusable += 1
        seq.append({"row": r, "balance": bal, "movement": mv})
    usable = [x for x in seq if x["balance"] is not None and x["movement"] is not None]
    if len(usable) < 2:
        return head | {
            "status": "unavailable",
            "reason": "fewer than two rows with a balance and a movement",
            "rows_without_balance_or_movement": unusable,
        }

    def ok(x: dict) -> bool:
        return x["balance"] is not None and x["movement"] is not None

    def pairs(order: list[dict]) -> list[tuple[dict, dict]]:
        # transitions between ADJACENT statement rows only; a row without balance or movement breaks the chain
        return [(a, b) for a, b in zip(order, order[1:], strict=False) if ok(a) and ok(b)]

    def fits(order: list[dict]) -> int:
        return sum(1 for a, b in pairs(order) if b["balance"] == a["balance"] + b["movement"])

    fwd, rev = fits(seq), fits(seq[::-1])
    n = len(pairs(seq))
    if n == 0:
        return head | {
            "status": "unavailable",
            "reason": "no two adjacent rows both have a balance and a movement",
            "rows_without_balance_or_movement": unusable,
        }
    share = {"file_order": round(fwd / n, 6), "reverse_file_order": round(rev / n, 6)}
    if max(fwd, rev) / n < min_share or fwd == rev:
        return head | {
            "status": "unavailable",
            "reason": "neither the file order nor its reverse explains the running balance "
            f"(at least {min_share:.0%} of transitions required)",
            "transition_fit": share,
        }
    full = seq if fwd > rev else seq[::-1]
    order = [x for x in full if ok(x)]
    breaks = []
    for a, b in pairs(full):
        expected = a["balance"] + b["movement"]
        if b["balance"] != expected:
            breaks.append(
                {
                    "previous_txn_id": a["row"]["txn_id"],
                    "previous_source_row": a["row"]["source_row"],
                    "txn_id": b["row"]["txn_id"],
                    "source_row": b["row"]["source_row"],
                    "date": b["row"].get("posted_at", "")[:10] or None,
                    "previous_balance_minor": a["balance"],
                    "movement_minor": b["movement"],
                    "expected_balance_minor": expected,
                    "stated_balance_minor": b["balance"],
                    "difference_minor": b["balance"] - expected,
                }
            )
    opening = order[0]["balance"] - order[0]["movement"]
    movement = sum(x["movement"] for x in order)
    closing = order[-1]["balance"]
    dates = [x["row"].get("posted_at") or "" for x in order]
    out_of_order = sum(1 for a, b in zip(dates, dates[1:], strict=False) if a and b and b < a)
    out = head | {
        "status": "discontinuities" if breaks else "consistent",
        "order": "file_order" if fwd > rev else "reverse_file_order",
        "transition_fit": share,
        "opening_balance_minor": opening,
        "opening_basis": "implied: the first row's stated balance minus its own movement",
        "movement_minor": movement,
        "closing_balance_minor": closing,
        "opening_plus_movement_equals_closing": opening + movement == closing,
        "complete": unusable == 0,
        "unexplained_difference_minor": closing - (opening + movement),
        "discontinuities": breaks,
        "rows_without_balance_or_movement": unusable,
        "dates_out_of_order": out_of_order,
        "first_date": (dates[0] or "")[:10] or None,
        "last_date": (dates[-1] or "")[:10] or None,
    }
    if stmt_col:
        periods = []
        for x in order:
            sid = x["row"]["extra"].get(stmt_col)
            sid = None if sid is None else str(sid)
            if not periods or periods[-1]["statement"] != sid:
                periods.append(
                    {
                        "statement": sid,
                        "first_txn_id": x["row"]["txn_id"],
                        "rows": 0,
                        "opening_balance_minor": x["balance"] - x["movement"],
                        "movement_minor": 0,
                    }
                )
            p = periods[-1]
            p["rows"] += 1
            p["movement_minor"] += x["movement"]
            p["closing_balance_minor"] = x["balance"]
            p["last_txn_id"] = x["row"]["txn_id"]
        for prev, cur_p in zip(periods, periods[1:], strict=False):
            cur_p["continues_previous"] = cur_p["opening_balance_minor"] == prev["closing_balance_minor"]
            cur_p["gap_minor"] = cur_p["opening_balance_minor"] - prev["closing_balance_minor"]
        if periods:
            periods[0]["continues_previous"] = None
        out["periods"] = {"status": "checked", "items": periods}
    else:
        out["periods"] = {
            "status": "not_applicable",
            "reason": "no statement-identifier column confirmed; continuity is checked row by row only",
        }
    return out
