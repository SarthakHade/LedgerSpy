"""PaySim adapter.

PaySim is a public mobile-money SIMULATION. It maps into the same canonical Transaction as any ledger; only
two fields need PaySim-specific rules, both taken from config (`adapters.paysim`):

* posted_at = step_epoch + (step − 1) × step_hours. `step` is a simulated hour, not a date, so the epoch is
  deliberately artificial (1970-01-01) and the dataset's time basis is recorded as "simulation_step".
* direction comes from `type`, relative to the nameOrig account (CASH_IN raises that balance; the other
  types lower it), as verified on data/raw/paysim_sample_10k.csv.

There is no currency in PaySim, so currency stays None.
"""

from datetime import datetime, timedelta

from ledgerspy.canonical.models import Direction
from ledgerspy.ingest.adapters.generic_ledger import GenericLedgerConverter, Issue
from ledgerspy.ingest.values import Cell, cell_text, is_blank


class PaySimConverter(GenericLedgerConverter):
    name = "paysim"
    version = "1"
    synthetic = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        p = self.settings.adapters.paysim
        self.epoch = datetime.fromisoformat(p.step_epoch)
        self.step = timedelta(hours=p.step_hours)
        self.by_type = {k.upper(): Direction(v) for k, v in p.direction_by_type.items()}

    def posted_at(self, cells: list[Cell], row: int, issues: list[Issue]) -> datetime | None:
        value = self._cell(cells, "posted_at")
        if is_blank(value):
            self._issue(issues, row, "posted_at", "missing", value)
            return None
        text = cell_text(value) or ""
        if not text.isdigit() or int(text) < 1:
            self._issue(issues, row, "posted_at", "unparseable", value)
            return None
        return self.epoch + (int(text) - 1) * self.step

    def direction(self, cells: list[Cell], row: int, issues: list[Issue]) -> Direction | None:
        value = self._cell(cells, "txn_type")
        if is_blank(value):
            self._issue(issues, row, "direction", "missing", value, column=self.mapping.fields["txn_type"])
            return None
        found = self.by_type.get((cell_text(value) or "").upper())
        if found is None:
            self._issue(
                issues, row, "direction", "unknown_value", value, column=self.mapping.fields["txn_type"]
            )
        return found

    def time_basis(self) -> str:
        return "simulation_step"
