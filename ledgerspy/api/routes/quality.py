"""P4 endpoints: integrity reports, issues, fix decisions, normalisation and reverts. All token-protected."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ledgerspy.api.limits import Page
from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import DatasetId, _rejected
from ledgerspy.errors import LedgerSpyError
from ledgerspy.normalize import service as normalize
from ledgerspy.normalize.view import load_view
from ledgerspy.quality import engine

router = APIRouter()


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fix_id: str = Field(pattern=r"^X-[0-9a-f]{16}$")
    decision: Literal["accept", "reject"]
    value: Any = None
    reason: str | None = Field(default=None, max_length=500)


class Decisions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[Decision] = Field(min_length=1, max_length=5000)


class Revert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


@router.get("/api/datasets")
def list_datasets(db: Db) -> Any:
    """Datasets with their latest readiness (for the integrity dashboard's picker)."""
    rows = db.execute(
        "SELECT d.dataset_id, d.case_id, c.name AS case_name, d.original_filename, d.format, d.status, "
        "d.kind, d.row_count, "
        "d.adapter, d.created_at, (SELECT readiness FROM integrity_runs r WHERE r.dataset_id = d.dataset_id "
        "ORDER BY r.created_at DESC, r.run_id DESC LIMIT 1) AS latest_readiness "
        "FROM datasets d JOIN cases c ON c.case_id = d.case_id ORDER BY d.created_at DESC"
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/datasets/{dataset_id}/integrity", status_code=201)
def run_integrity(dataset_id: DatasetId, request: Request, db: Db) -> Any:
    try:
        return engine.run_integrity(db, request.app.state.config, dataset_id, request.app.state.actor)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/integrity")
def latest_integrity(dataset_id: DatasetId, db: Db) -> Any:
    try:
        load_view(db, dataset_id)  # 404 / 409 for unknown or unmapped datasets
        return engine.latest_report(db, dataset_id)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/integrity/issues")
def integrity_issues(
    dataset_id: DatasetId,
    db: Db,
    run_id: Annotated[str | None, Query(pattern=r"^Q-[0-9A-HJKMNP-TV-Z]{26}$")] = None,
    check: Annotated[str | None, Query(max_length=40)] = None,
    severity: Annotated[Literal["info", "warning", "error", "critical"] | None, Query()] = None,
    page: Page = 1,
    size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Any:
    try:
        return engine.list_issues(db, dataset_id, run_id, check, severity, page, size)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/fixes")
def list_fixes(
    dataset_id: DatasetId,
    db: Db,
    status: Annotated[
        Literal["proposed", "accepted", "rejected", "applied", "reverted"] | None, Query()
    ] = None,
) -> Any:
    try:
        load_view(db, dataset_id)
        return {"items": normalize.list_fixes(db, dataset_id, status)}
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.post("/api/datasets/{dataset_id}/fixes/decisions")
def decide(dataset_id: DatasetId, body: Decisions, request: Request, db: Db) -> Any:
    try:
        load_view(db, dataset_id)
        items = normalize.decide(
            db, dataset_id, [d.model_dump() for d in body.decisions], request.app.state.actor
        )
        return {"items": items}
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.post("/api/datasets/{dataset_id}/normalize")
def apply_fixes(dataset_id: DatasetId, request: Request, db: Db) -> Any:
    """Apply every accepted fix as a logged transformation, then re-run the integrity check."""
    state = request.app.state
    try:
        load_view(db, dataset_id)
        result = normalize.apply_accepted(db, dataset_id, state.actor)
        report = engine.run_integrity(db, state.config, dataset_id, state.actor)
        return {**result, "integrity_run_id": report["run_id"], "readiness": report["readiness"]}
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/transformations")
def transformations(dataset_id: DatasetId, db: Db) -> Any:
    try:
        load_view(db, dataset_id)
        return {"items": normalize.list_transformations(db, dataset_id)}
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.post("/api/datasets/{dataset_id}/transformations/{transformation_id}/revert")
def revert(dataset_id: DatasetId, transformation_id: str, body: Revert, request: Request, db: Db) -> Any:
    try:
        load_view(db, dataset_id)
        return normalize.revert(db, dataset_id, transformation_id, request.app.state.actor, body.reason)
    except LedgerSpyError as exc:
        return _rejected(exc)
