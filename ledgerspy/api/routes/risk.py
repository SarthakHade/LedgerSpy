"""P9 endpoints: the dataset risk result (summary, transactions, explanations, entities, detectors, scatter).

All token-protected and read-only. Every endpoint reads the same cached analysis of the dataset's current
state (`ledgerspy.risk.service.run_risk`), so their numbers agree; each response carries the result's
`result_sha256`.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse

from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import DatasetId, _rejected
from ledgerspy.errors import LedgerSpyError
from ledgerspy.risk import service

router = APIRouter()

TierFilter = Literal["low", "medium", "high", "critical", "insufficient_data"]
TxnId = Annotated[str, PathParam(min_length=1, max_length=128)]
EntityId = Annotated[str, PathParam(pattern=r"^E-[0-9a-f]{16}$")]


def _not_found(what: str, ident: str) -> JSONResponse:
    return _rejected(LedgerSpyError("not_found", f"No {what} {ident!r} in this dataset.", http_status=404))


def _result(request: Request, db, dataset_id: str):
    return service.run_risk(db, request.app.state.config, dataset_id)


@router.post("/api/datasets/{dataset_id}/risk")
def run_risk(dataset_id: DatasetId, request: Request, db: Db) -> Any:
    """Analyse the dataset's current state (or reuse the identical cached analysis) and return the summary."""
    try:
        return _result(request, db, dataset_id).summary
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/risk")
def risk_summary(dataset_id: DatasetId, request: Request, db: Db) -> Any:
    try:
        return _result(request, db, dataset_id).summary
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/risk/transactions")
def risk_transactions(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=500)] = 50,
    tier: Annotated[TierFilter | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    sort: Annotated[Literal["priority", "source_row", "amount", "score"], Query()] = "priority",
) -> Any:
    try:
        return service.transactions_page(_result(request, db, dataset_id), page, size, tier, q, sort)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/risk/transactions/{txn_id}")
def risk_transaction(dataset_id: DatasetId, txn_id: TxnId, request: Request, db: Db) -> Any:
    try:
        detail = service.transaction_detail(
            _result(request, db, dataset_id), txn_id, request.app.state.config.settings
        )
    except LedgerSpyError as exc:
        return _rejected(exc)
    return detail if detail is not None else _not_found("transaction", txn_id)


@router.get("/api/datasets/{dataset_id}/risk/entities")
def risk_entities(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=500)] = 50,
    tier: Annotated[Literal["low", "medium", "high", "critical"] | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> Any:
    try:
        return service.entities_page(_result(request, db, dataset_id), page, size, tier, q)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/risk/entities/{entity_id}")
def risk_entity(dataset_id: DatasetId, entity_id: EntityId, request: Request, db: Db) -> Any:
    try:
        detail = service.entity_detail(_result(request, db, dataset_id), entity_id)
    except LedgerSpyError as exc:
        return _rejected(exc)
    return detail if detail is not None else _not_found("entity", entity_id)


@router.get("/api/datasets/{dataset_id}/risk/detectors")
def risk_detectors(dataset_id: DatasetId, request: Request, db: Db) -> Any:
    try:
        res = _result(request, db, dataset_id)
    except LedgerSpyError as exc:
        return _rejected(exc)
    return res.detectors | {"result_sha256": res.result_sha256}


@router.get("/api/datasets/{dataset_id}/risk/scatter")
def risk_scatter(dataset_id: DatasetId, request: Request, db: Db) -> Any:
    try:
        res = _result(request, db, dataset_id)
    except LedgerSpyError as exc:
        return _rejected(exc)
    return res.scatter | {"result_sha256": res.result_sha256}
