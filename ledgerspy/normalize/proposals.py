"""Detect fix proposals on the normalised view. Proposing never changes data.

Two classes:

* **safe** (requires_confirmation = 0): whitespace collapse, identifier normal form, configured case. The
  result is fully determined; an analyst still has to accept it before it is applied.
* **reinterpretation** (requires_confirmation = 1): a value P3 could not convert under the declared convention
  (date format, decimal separator) that another configured convention can read. With one candidate the
  analyst confirms it; with several the analyst must choose one. Never applied without that decision.

Missing values, rounding and duplicate removal are never proposed: they would invent or destroy data.
"""

from dataclasses import dataclass
from typing import Any

from ledgerspy.config.schema import LedgerSpyConfig
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.normalize.transforms import amount_candidates, date_candidates, safe_pipeline
from ledgerspy.normalize.view import DatasetView, encode


@dataclass(frozen=True)
class Proposal:
    fix_id: str
    txn_id: str
    source_row: int
    field: str
    transform: str
    original_value: str  # encoded (JSON) current value
    proposed_value: str | None  # encoded; None when the analyst must choose among candidates
    candidates: list[str]  # encoded
    requires_confirmation: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fix_id": self.fix_id,
            "txn_id": self.txn_id,
            "source_row": self.source_row,
            "field": self.field,
            "transform": self.transform,
            "original_value": self.original_value,
            "proposed_value": self.proposed_value,
            "candidates": self.candidates,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
        }


def fix_id(dataset_id: str, txn_id: str, field: str, transform: str, original: str) -> str:
    """Deterministic: the same proposal on the same data always has the same ID across integrity runs."""
    return "X-" + sha256_hex(canonical_json([dataset_id, txn_id, field, transform, original]))[:16]


def _make(
    view: DatasetView,
    row: dict,
    field: str,
    transform: str,
    original: Any,
    proposed: list[Any],
    confirm: bool,
    reason: str,
) -> Proposal:
    enc_original = encode(original)
    candidates = [encode(v) for v in proposed]
    return Proposal(
        fix_id=fix_id(view.dataset_id, row["txn_id"], field, transform, enc_original),
        txn_id=row["txn_id"],
        source_row=row["source_row"],
        field=field,
        transform=transform,
        original_value=enc_original,
        proposed_value=candidates[0] if len(candidates) == 1 else None,
        candidates=candidates,
        requires_confirmation=confirm,
        reason=reason,
    )


def propose(view: DatasetView, settings: LedgerSpyConfig) -> list[Proposal]:
    n = settings.normalize
    formats = settings.mapping.date_formats
    declared = view.mapping["options"]
    mu = settings.ingest.minor_units
    out: list[Proposal] = []
    text_fields = sorted(set(n.whitespace_fields) | set(n.identifier_fields) | set(n.case))
    for row in view.rows:
        for f in text_fields:
            value = row.get(f)
            if not isinstance(value, str) or not view.mapped(f):
                continue
            new, steps = safe_pipeline(
                value,
                whitespace=f in n.whitespace_fields,
                identifier=f in n.identifier_fields,
                case=n.case.get(f),
            )
            if steps and new:
                out.append(
                    _make(view, row, f, "+".join(steps), value, [new], False, f"{' + '.join(steps)} ({f})")
                )
        for f, opt in (("posted_at", "date_format"), ("document_date", "document_date_format")):
            if row.get(f) is not None:
                continue
            for issue in view.p3(row["source_row"], f):
                if issue["code"] != "unparseable" or not issue["raw_value"]:
                    continue
                found = date_candidates(issue["raw_value"], formats)
                if found:
                    out.append(
                        _make(
                            view,
                            row,
                            f,
                            "reinterpret_date",
                            None,
                            found,
                            True,
                            f"{issue['raw_value']!r} does not match the declared format "
                            f"{declared.get(opt)!r}; "
                            + (
                                "it matches another configured format"
                                if len(found) == 1
                                else f"it is ambiguous under {len(found)} configured formats"
                            ),
                        )
                    )
        if row.get("amount_minor") is None and "amount" in view.mapping["fields"]:
            for issue in view.p3(row["source_row"], "amount_minor"):
                if issue["code"] != "unparseable" or not issue["raw_value"]:
                    continue
                currency = row.get("currency")
                digits = mu.by_currency.get(currency, mu.default_digits) if currency else mu.default_digits
                found = amount_candidates(issue["raw_value"], digits)
                if found:
                    out.append(
                        _make(
                            view,
                            row,
                            "amount_minor",
                            "reinterpret_amount",
                            None,
                            found,
                            True,
                            f"{issue['raw_value']!r} is not a number under the declared decimal separator "
                            f"{declared.get('decimal_separator')!r}; "
                            + ("one other reading exists" if len(found) == 1 else "several readings exist"),
                        )
                    )
    return out
