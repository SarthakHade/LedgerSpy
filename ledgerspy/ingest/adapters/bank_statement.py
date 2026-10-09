"""Bank statement adapter (P14): mapped statement lines → canonical Transactions.

Same canonical schema as every other adapter. The direction is `in` / `out` from the ACCOUNT HOLDER's view and
comes only from the confirmed mapping (`Mapping.bank`):

* a debit / credit column pair, or a debit/credit indicator column: `debit_means` says whether a debit is
  money out ("outflow") or money in ("inflow");
* one signed amount column without a direction column: `amount_sign` says whether a positive amount is money
  in ("positive_inflow") or out ("positive_outflow"); the stored amount is the absolute value;
* an in/out indicator column or a declared options.direction ('in' / 'out') is used as is.

Nothing is inferred. A zero signed amount has no direction (issue `zero_amount_no_direction`); a negative
value in a directional (debit / credit) column is not reinterpreted (issue `negative_in_directional_column`,
amount and direction left empty). The running-balance and statement columns stay verbatim in `extra`.
"""

from ledgerspy.canonical.models import Direction
from ledgerspy.ingest.adapters.generic_ledger import GenericLedgerConverter, Issue
from ledgerspy.ingest.mapping import BankOptions
from ledgerspy.ingest.values import Cell, is_blank


class BankStatementConverter(GenericLedgerConverter):
    name = "bank_statement"
    version = "1"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.bank = self.mapping.bank or BankOptions()

    def _holder(self, d: Direction | None) -> Direction | None:
        """debit/credit → out/in by the confirmed `debit_means`; in/out unchanged."""
        if d in (Direction.DEBIT, Direction.CREDIT):
            if self.bank.debit_means is None:  # pragma: no cover — validate_mapping requires it
                return None
            debit_out = self.bank.debit_means == "outflow"
            return Direction.OUT if (d is Direction.DEBIT) == debit_out else Direction.IN
        return d

    def direction(self, cells: list[Cell], row: int, issues: list[Issue]) -> Direction | None:
        return self._holder(super().direction(cells, row, issues))

    def amount_and_direction(
        self, cells: list[Cell], row: int, currency: str | None, issues: list[Issue]
    ) -> tuple[int | None, Direction | None]:
        if "amount" in self.col and self.bank.amount_sign is not None:
            value = self._cell(cells, "amount")
            amount = self._minor(value, "amount", currency, row, issues)
            if amount is None:
                if is_blank(value):
                    self._issue(issues, row, "amount", "missing", value)
                return None, None
            if amount == 0:
                self._issue(issues, row, "direction", "zero_amount_no_direction", value)
                return 0, None
            inflow = (amount > 0) == (self.bank.amount_sign == "positive_inflow")
            return abs(amount), Direction.IN if inflow else Direction.OUT
        if "amount" in self.col:
            return super().amount_and_direction(cells, row, currency, issues)
        amount, d = super().amount_and_direction(cells, row, currency, issues)
        if amount is not None and amount < 0:
            cols = f"{self.mapping.fields['debit_amount']} / {self.mapping.fields['credit_amount']}"
            self._issue(issues, row, "amount", "negative_in_directional_column", amount, column=cols)
            return None, None
        return amount, self._holder(d)
