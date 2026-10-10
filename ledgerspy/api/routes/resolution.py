"""P12 endpoints: entity resolution of a dataset (summary, canonical entities, candidate pairs) and the master
files imported for a case.

All token-protected (LocalSecurityMiddleware) and read-only. Identifiers are validated by pattern (CE-…,
P-…); no endpoint takes a file path. Master files are imported with `ledgerspy entities import-master`
(CLI).
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam

from ledgerspy.api.limits import Page
from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import CaseId, DatasetId, _rejected
from ledgerspy.errors import LedgerSpyError
from ledgerspy.resolution import masters, service

router = APIRouter()

CanonicalId = Annotated[str, PathParam(pattern=r"^CE-[0-9a-f]{16}$")]
PairId = Annotated[str, PathParam(pattern=r"^P-[0-9a-f]{16}$")]
EntityStatus = Literal["unique", "resolved", "needs_review", "conflicting_identity"]
PairStatus = Literal[
    "confirmed_match", "probable_match", "ambiguous", "conflicting_identity", "shared_attribute", "no_match"
]
Size = Annotated[int, Query(ge=1, le=500)]
Search = Annotated[str | None, Query(min_length=1, max_length=100)]


def _run(request: Request, db, dataset_id: str):
    return service.run(db, request.app.state.config, dataset_id)


@router.get("/api/datasets/{dataset_id}/resolution")
def resolution_summary(dataset_id: DatasetId, request: Request, db: Db) -> Any:
    try:
        return service.summary(_run(request, db, dataset_id))
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/resolution/entities")
def resolution_entities(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Page = 1,
    size: Size = 50,
    status: Annotated[EntityStatus | None, Query()] = None,
    q: Search = None,
) -> Any:
    try:
        return service.entities_page(_run(request, db, dataset_id), page, size, status, q)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/resolution/entities/{canonical_id}")
def resolution_entity(dataset_id: DatasetId, canonical_id: CanonicalId, request: Request, db: Db) -> Any:
    try:
        return service.entity_detail(_run(request, db, dataset_id), canonical_id)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/resolution/pairs")
def resolution_pairs(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Page = 1,
    size: Size = 50,
    status: Annotated[PairStatus | None, Query()] = None,
    q: Search = None,
) -> Any:
    """Candidate pairs; by default the review queue (probable, ambiguous, conflicting) and shared links."""
    try:
        return service.pairs_page(_run(request, db, dataset_id), page, size, status, q)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/resolution/pairs/{pair_id}")
def resolution_pair(dataset_id: DatasetId, pair_id: PairId, request: Request, db: Db) -> Any:
    try:
        return service.pair_detail(_run(request, db, dataset_id), pair_id)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/cases/{case_id}/masters")
def case_masters(case_id: CaseId, db: Db) -> Any:
    return {"case_id": case_id, "master_files": masters.list_master_files(db, case_id)}
