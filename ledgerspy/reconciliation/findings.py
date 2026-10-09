"""Reconciliation findings: an INDEPENDENT evidence family (`reconciliation`).

Same signal shape as P7 / P13 (deterministic `signal_id` from rule, version and subject). P9 does not fuse
them (weight null, `fused_in_p9` false): no P9 score changes because a row is unmatched. Integration into
risk is a later, separately validated change (P9 / P16).

Rules (version 1):
- `recon_unmatched_ledger`, `recon_unmatched_bank`: a reconcilable row with no counterpart;
- `recon_amount_mismatch`, `recon_date_discrepancy`, `recon_counterparty_mismatch`,
  `recon_reference_conflict`: the discrepancies of a partial match;
- `recon_duplicate_bank_posting` (identical statement lines), `recon_repeated_settlement` (same counterparty
  and amount settled again within the window), `recon_duplicate_ledger_posting`: duplicate candidates;
- `recon_balance_discontinuity`: a stated running balance that does not follow from the previous one.
"""

from typing import Any

from ledgerspy.forensics.models import signal_id

VERSION = 1
RULES = (
    "recon_unmatched_ledger",
    "recon_unmatched_bank",
    "recon_amount_mismatch",
    "recon_date_discrepancy",
    "recon_counterparty_mismatch",
    "recon_reference_conflict",
    "recon_duplicate_bank_posting",
    "recon_repeated_settlement",
    "recon_duplicate_ledger_posting",
    "recon_balance_discontinuity",
)
DISCREPANCY_RULE = {
    "amount": "recon_amount_mismatch",
    "date": "recon_date_discrepancy",
    "counterparty": "recon_counterparty_mismatch",
    "reference": "recon_reference_conflict",
}
SEVERITY = {
    "recon_amount_mismatch": "medium",
    "recon_date_discrepancy": "low",
    "recon_counterparty_mismatch": "high",
    "recon_reference_conflict": "medium",
    "recon_duplicate_bank_posting": "high",
    "recon_repeated_settlement": "medium",
    "recon_duplicate_ledger_posting": "medium",
    "recon_balance_discontinuity": "high",
}
LIMITS = {
    "recon_unmatched_ledger": "The payment may clear in a later statement, through another account, or "
    "net with other items; check the bank and the remittance before concluding.",
    "recon_unmatched_bank": "The line may be booked in another ledger, period or account, or be a bank "
    "charge not yet booked; check before concluding.",
    "recon_amount_mismatch": "Fees, short payments and FX are common legitimate causes.",
    "recon_date_discrepancy": "Late clearing has legitimate causes (holidays, cheques, batch runs).",
    "recon_counterparty_mismatch": "Narrations are abbreviated by banks; confirm the beneficiary account on "
    "the payment record.",
    "recon_reference_conflict": "Reference formats differ between systems; confirm which one is wrong.",
    "recon_duplicate_bank_posting": "A candidate only: identical lines can be legitimate (equal invoices).",
    "recon_repeated_settlement": "A candidate only: repeated equal payments can be legitimate instalments.",
    "recon_duplicate_ledger_posting": "A candidate only: confirm against the invoices.",
    "recon_balance_discontinuity": "A statement page may be missing, or the export may be incomplete.",
}


def _signal(
    rule, subject_type, subject_id, ledger, bank, severity, metrics, evidence, explanation, result_id=None
):
    return {
        "signal_id": signal_id(rule, VERSION, subject_type, subject_id),
        "rule_id": rule,
        "rule_version": VERSION,
        "code": f"reconciliation.{rule}",
        "family": "reconciliation",
        "subject_type": subject_type,
        "subject_id": subject_id,
        "ledger_txn_ids": sorted(ledger),
        "bank_txn_ids": sorted(bank),
        "result_id": result_id,
        "severity": severity,
        "strength": 1.0,
        "weight": None,
        "fused_in_p9": False,
        "metrics": metrics,
        "evidence": evidence,
        "explanation": explanation,
        "limitations": [LIMITS[rule]],
    }


def compute(
    results: list[dict[str, Any]], duplicates: list[dict[str, Any]], balance: dict[str, Any]
) -> list[dict[str, Any]]:
    out = []
    for r in results:
        st = r["status"]
        if st in ("unmatched_ledger", "unmatched_bank"):
            side = "ledger" if st == "unmatched_ledger" else "bank"
            row = r[side + "_rows"][0]
            rule = "recon_" + st
            sev = (
                ("medium" if row["flow"] == "out" else "low")
                if side == "ledger"
                else ("high" if row["flow"] == "out" else "medium")
            )
            out.append(
                _signal(
                    rule,
                    "txn",
                    row["txn_id"],
                    r["ledger_txn_ids"],
                    r["bank_txn_ids"],
                    sev,
                    {"amount_minor": row["amount_minor"], "currency": row["currency"], "flow": row["flow"]},
                    {"row": row, "scope": r["scope"]},
                    r["explanation"],
                    r["result_id"],
                )
            )
        elif st == "partial_match":
            for d in r["discrepancies"]:
                rule = DISCREPANCY_RULE[d]
                metric = {
                    "amount": {"amount_difference_minor": r["amount_difference_minor"]},
                    "date": {"date_difference_days": r["date_difference_days"]},
                    "counterparty": {"bank_named_parties": r["link"].get("bank_named_parties", [])},
                    "reference": {
                        "ledger_reference": r["ledger_rows"][0]["reference"],
                        "bank_reference": r["bank_rows"][0]["reference"],
                    },
                }[d]
                out.append(
                    _signal(
                        rule,
                        "result",
                        r["result_id"],
                        r["ledger_txn_ids"],
                        r["bank_txn_ids"],
                        SEVERITY[rule],
                        metric | {"currency": r["scope"]["currency"]},
                        {"ledger_rows": r["ledger_rows"], "bank_rows": r["bank_rows"], "link": r["link"]},
                        r["explanation"],
                        r["result_id"],
                    )
                )
    for g in duplicates:
        rule = (
            "recon_duplicate_bank_posting"
            if (g["side"], g["type"]) == ("bank", "exact")
            else "recon_repeated_settlement"
            if g["side"] == "bank"
            else "recon_duplicate_ledger_posting"
        )
        ids = g["txn_ids"]
        out.append(
            _signal(
                rule,
                "group",
                g["group_id"],
                ids if g["side"] == "ledger" else [],
                ids if g["side"] == "bank" else [],
                SEVERITY[rule],
                {"rows": len(ids), "amount_minor": g["amount_minor"], "currency": g["currency"]},
                {"group": g},
                f"{len(ids)} {'ledger rows' if g['side'] == 'ledger' else 'bank lines'} "
                f"{g['first_date']} … {g['last_date']}: {g['basis']}. Member statuses: "
                + ", ".join(f"{t} {s}" for t, s in sorted(g.get("member_status", {}).items()))
                + ".",
            )
        )
    for acc in balance.get("accounts", []):
        for b in acc.get("discontinuities", []):
            out.append(
                _signal(
                    "recon_balance_discontinuity",
                    "balance",
                    f"{acc['account_key']}:{acc['currency']}:{b['txn_id']}",
                    [],
                    [b["previous_txn_id"], b["txn_id"]],
                    SEVERITY["recon_balance_discontinuity"],
                    {"difference_minor": b["difference_minor"], "currency": acc["currency"]},
                    {"break": b, "account": acc["account"]},
                    f"Account {acc['account']}: the balance after line {b['txn_id']} is "
                    f"{b['stated_balance_minor']} but the previous balance {b['previous_balance_minor']} "
                    f"plus its movement {b['movement_minor']} gives {b['expected_balance_minor']} "
                    f"(difference {b['difference_minor']}, minor units, {acc['currency']}).",
                )
            )
    return sorted(out, key=lambda s: (s["rule_id"], s["signal_id"]))
