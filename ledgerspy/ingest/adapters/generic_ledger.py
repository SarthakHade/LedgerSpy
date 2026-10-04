"""Generic ledger adapter: converts mapped rows into canonical Transactions.

Never invents values. A blank or unconvertible cell in a mapped field becomes `None` on the transaction and
an ingestion issue (source_row, field, column, code, raw value). Unmapped columns go to `extra` verbatim.
Label columns go to the separate label output, and never to the transaction.
"""

from datetime import datetime
from typing import Any

from ledgerspy.canonical.ids import txn_id
from ledgerspy.canonical.models import Direction, Transaction
from ledgerspy.config.schema import LedgerSpyConfig, normalize_header
from ledgerspy.ingest.mapping import Mapping
from ledgerspy.ingest.values import (
    Cell,
    cell_text,
    is_blank,
    json_cell,
    parse_datetime,
    parse_decimal,
    to_minor_units,
)

Issue = dict[str, Any]
_RAW_CHARS = 200
# Fields where a blank cell is itself a problem worth recording (the others are optional by nature).
CORE_FIELDS = ("posted_at", "amount", "direction")


class GenericLedgerConverter:
    name = "generic_ledger"
    version = "1"
    synthetic: bool | None = None  # unknown provenance

    def __init__(
        self,
        mapping: Mapping,
        header: list[str],
        settings: LedgerSpyConfig,
        dataset_id: str,
        evidence_sha256: str,
        use_source_ids: bool,
    ) -> None:
        self.mapping = mapping
        self.settings = settings
        self.dataset_id = dataset_id
        self.evidence_sha256 = evidence_sha256
        self.use_source_ids = use_source_ids
        self.idx = {name: i for i, name in enumerate(header)}
        self.col = {f: self.idx[c] for f, c in mapping.fields.items()}
        self.labels = [(c, self.idx[c]) for c in mapping.label_columns]
        used = set(mapping.fields.values()) | set(mapping.label_columns)
        self.extra = [(c, i) for c, i in self.idx.items() if c not in used]
        self.direction_values = {
            normalize_header(v): d for d, vs in settings.mapping.direction_values.items() for v in vs
        }
        self.saw_time = False

    # ------------------------------------------------------------------ helpers

    def _cell(self, cells: list[Cell], f: str) -> Cell:
        i = self.col.get(f)
        return None if i is None else cells[i]

    def _issue(
        self, issues: list[Issue], row: int, f: str, code: str, value: Cell, column: str | None = None
    ) -> None:
        raw = cell_text(value)
        issues.append(
            {
                "source_row": row,
                "field": f,
                "column": column if column is not None else self.mapping.fields.get(f),
                "code": code,
                "raw_value": raw[:_RAW_CHARS] if raw else None,
            }
        )

    def _digits(self, currency: str | None) -> int:
        mu = self.settings.ingest.minor_units
        return mu.by_currency.get(currency, mu.default_digits) if currency else mu.default_digits

    def _minor(self, value: Cell, f: str, currency: str | None, row: int, issues: list[Issue]) -> int | None:
        sep = self.mapping.options.decimal_separator or "."
        try:
            number = parse_decimal(value, sep)
        except ValueError:
            self._issue(issues, row, f, "unparseable", value)
            return None
        if number is None:
            return None
        try:
            return to_minor_units(number, self._digits(currency))
        except ValueError:
            self._issue(issues, row, f, "too_many_decimals", value)
            return None

    def _date(
        self, f: str, fmt: str | None, cells: list[Cell], row: int, issues: list[Issue]
    ) -> datetime | None:
        value = self._cell(cells, f)
        if f not in self.col:
            return None
        try:
            parsed = parse_datetime(value, fmt)
        except ValueError:
            self._issue(issues, row, f, "unparseable", value)
            return None
        if parsed is None and f in CORE_FIELDS:
            self._issue(issues, row, f, "missing", value)
        if parsed is not None and parsed.time() != datetime.min.time():
            self.saw_time = True
        return parsed

    def _text(self, cells: list[Cell], f: str) -> str | None:
        return cell_text(self._cell(cells, f)) if f in self.col else None

    # ------------------------------------------------------------------ overridable fields

    def posted_at(self, cells: list[Cell], row: int, issues: list[Issue]) -> datetime | None:
        return self._date("posted_at", self.mapping.options.date_format, cells, row, issues)

    def currency(self, cells: list[Cell], row: int, issues: list[Issue]) -> str | None:
        if "currency" not in self.col:
            return self.mapping.options.currency
        value = self._cell(cells, "currency")
        code = (cell_text(value) or "").upper()
        if not code:
            return None
        if len(code) != 3 or not code.isalpha() or not code.isascii():
            self._issue(issues, row, "currency", "unparseable", value)
            return None
        return code

    def amount_and_direction(
        self, cells: list[Cell], row: int, currency: str | None, issues: list[Issue]
    ) -> tuple[int | None, Direction | None]:
        if "amount" in self.col:
            value = self._cell(cells, "amount")
            amount = self._minor(value, "amount", currency, row, issues)
            if amount is None and is_blank(value):
                self._issue(issues, row, "amount", "missing", value)
            return amount, self.direction(cells, row, issues)
        debit_v, credit_v = self._cell(cells, "debit_amount"), self._cell(cells, "credit_amount")
        debit = self._minor(debit_v, "debit_amount", currency, row, issues)
        credit = self._minor(credit_v, "credit_amount", currency, row, issues)
        unusable = (debit is None and not is_blank(debit_v)) or (credit is None and not is_blank(credit_v))
        if unusable:
            return None, None
        nonzero = [(a, d) for a, d in ((debit, Direction.DEBIT), (credit, Direction.CREDIT)) if a]
        if len(nonzero) == 1:
            return nonzero[0]
        if len(nonzero) == 2:
            self._issue(
                issues,
                row,
                "amount",
                "debit_and_credit_both_set",
                f"{debit_v} / {credit_v}",
                column=f"{self.mapping.fields['debit_amount']} / {self.mapping.fields['credit_amount']}",
            )
            return None, None
        if debit is None and credit is None:
            self._issue(
                issues,
                row,
                "amount",
                "missing",
                None,
                column=f"{self.mapping.fields['debit_amount']} / {self.mapping.fields['credit_amount']}",
            )
            return None, None
        # an explicit zero line: no direction can be stated
        self._issue(
            issues,
            row,
            "direction",
            "zero_amount_no_direction",
            f"{debit_v} / {credit_v}",
            column=f"{self.mapping.fields['debit_amount']} / {self.mapping.fields['credit_amount']}",
        )
        return 0, None

    def direction(self, cells: list[Cell], row: int, issues: list[Issue]) -> Direction | None:
        if "direction" not in self.col:
            return Direction(self.mapping.options.direction) if self.mapping.options.direction else None
        value = self._cell(cells, "direction")
        if is_blank(value):
            self._issue(issues, row, "direction", "missing", value)
            return None
        found = self.direction_values.get(normalize_header(cell_text(value) or ""))
        if found is None:
            self._issue(issues, row, "direction", "unknown_value", value)
            return None
        return Direction(found)

    # ------------------------------------------------------------------ row

    def convert(self, row: int, cells: list[Cell]) -> tuple[Transaction, dict[str, Any], list[Issue]]:
        issues: list[Issue] = []
        source_id = self._text(cells, "txn_id") if self.use_source_ids else None
        currency = self.currency(cells, row, issues)
        amount, direction = self.amount_and_direction(cells, row, currency, issues)
        document = self._date("document_date", self.mapping.options.document_date_format, cells, row, issues)
        txn = Transaction(
            txn_id=txn_id(self.evidence_sha256, row, source_id=source_id),
            dataset_id=self.dataset_id,
            source_row=row,
            posted_at=self.posted_at(cells, row, issues),
            document_date=document.date() if document else None,
            amount_minor=amount,
            currency=currency,
            direction=direction,
            account=self._text(cells, "account"),
            payer_raw=self._text(cells, "payer_raw"),
            payee_raw=self._text(cells, "payee_raw"),
            reference=self._text(cells, "reference"),
            description=self._text(cells, "description"),
            txn_type=self._text(cells, "txn_type"),
            extra={c: json_cell(cells[i]) for c, i in self.extra},
        )
        labels = {c: json_cell(cells[i]) for c, i in self.labels if not is_blank(cells[i])}
        return txn, labels, issues

    def time_basis(self) -> str:
        return "datetime" if self.saw_time else "date"
