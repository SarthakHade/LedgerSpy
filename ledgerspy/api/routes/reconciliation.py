"""P14 endpoints: ledger ↔ bank statement reconciliation (token-protected, like every /api route).

Bank statements are imported with the P3 endpoints (`POST /api/cases/{case_id}/files?kind=bank_statement`,
then `PUT /api/datasets/{id}/mapping` with the confirmed `bank` semantics). Here: what must be confirmed
(`setup`), starting a run, and read-only views of a run. Lists are paginated with `size` ≤
`reconciliation.limits.max_page_size`. Identifiers are validated by pattern; no endpoint takes a file path.
Bank account numbers are returned masked (last 4) with a stable `account_key`.
"""

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam

from ledgerspy.api.limits import Page
from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import _rejected
from ledgerspy.errors import LedgerSpyError
from ledgerspy.reconciliation import engine, findings, service

router = APIRouter()

DatasetQ = Annotated[str, Query(pattern=service.DATASET_ID)]
OptDatasetQ = Annotated[str | None, Query(pattern=service.DATASET_ID)]
RunId = Annotated[str, PathParam(pattern=r"^RR-[0-9a-f]{16}$")]
ResultId = Annotated[str, PathParam(pattern=r"^RM-[0-9a-f]{16}$")]
Status = Literal[engine.STATUSES]  # type: ignore[valid-type]
Rule = Literal[findings.RULES]  # type: ignore[valid-type]
Severity = Literal["low", "medium", "high"]
Size = Annotated[int, Query(ge=1, le=5000)]
From = Annotated[date | None, Query(alias="from")]
To = Annotated[date | None, Query(alias="to")]
Account = Annotated[str | None, Query(pattern=service.ACCOUNT_KEY)]
Currency = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
Q = Annotated[str | None, Query(min_length=1, max_length=128)]


def _size(request: Request, size: int) -> int:
    lim = request.app.state.config.settings.reconciliation.limits.max_page_size
    if size > lim:
        raise LedgerSpyError("page_too_large", f"size must be at most {lim}", {"max_page_size": lim})
    return size


def _run(request: Request, db, run_id: str):
    return service.get_run(db, request.app.state.config, run_id)


@router.get("/api/reconciliation/setup")
def reconciliation_setup(
    request: Request, db: Db, ledger_dataset_id: DatasetQ, bank_dataset_id: DatasetQ
) -> Any:
    try:
        return service.setup(db, request.app.state.config, ledger_dataset_id, bank_dataset_id)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.post("/api/reconciliation/runs", status_code=201)
def start_run(body: service.RunRequest, request: Request, db: Db) -> Any:
    state = request.app.state
    try:
        return service.summary(service.start(db, state.config, body, state.actor))
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/reconciliation/runs")
def list_runs(db: Db, ledger_dataset_id: OptDatasetQ = None, bank_dataset_id: OptDatasetQ = None) -> Any:
    return service.list_runs(db, ledger_dataset_id, bank_dataset_id)


@router.get("/api/reconciliation/runs/{run_id}")
def run_summary(run_id: RunId, request: Request, db: Db) -> Any:
    try:
        return service.summary(_run(request, db, run_id))
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/reconciliation/runs/{run_id}/results")
def run_results(
    run_id: RunId,
    request: Request,
    db: Db,
    status: Status | None = None,
    account: Account = None,
    currency: Currency = None,
    frm: From = None,
    to: To = None,
    q: Q = None,
    page: Page = 1,
    size: Size = 100,
) -> Any:
    try:
        return service.results_page(
            _run(request, db, run_id), status, account, currency, frm, to, q, page, _size(request, size)
        )
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/reconciliation/runs/{run_id}/results/{result_id}")
def run_result(run_id: RunId, result_id: ResultId, request: Request, db: Db) -> Any:
    try:
        return service.result_detail(db, _run(request, db, run_id), result_id)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/reconciliation/runs/{run_id}/duplicates")
def run_duplicates(
    run_id: RunId,
    request: Request,
    db: Db,
    side: Literal["ledger", "bank"] | None = None,
    page: Page = 1,
    size: Size = 100,
) -> Any:
    try:
        return service.duplicates_page(_run(request, db, run_id), side, page, _size(request, size), db)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/reconciliation/runs/{run_id}/balances")
def run_balances(run_id: RunId, request: Request, db: Db) -> Any:
    try:
        run = _run(request, db, run_id)
        return {"run_id": run.run_id, **run.balance}
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/reconciliation/runs/{run_id}/findings")
def run_findings(
    run_id: RunId,
    request: Request,
    db: Db,
    rule: Rule | None = None,
    severity: Severity | None = None,
    page: Page = 1,
    size: Size = 100,
) -> Any:
    try:
        return service.findings_page(_run(request, db, run_id), rule, severity, page, _size(request, size))
    except LedgerSpyError as exc:
        return _rejected(exc)
