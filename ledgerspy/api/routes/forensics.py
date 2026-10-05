"""P7 endpoint: the dataset forensic report (rule signals + Benford). Token-protected, read-only."""

from typing import Any

from fastapi import APIRouter, Request

from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import DatasetId, _rejected
from ledgerspy.errors import LedgerSpyError
from ledgerspy.forensics import service

router = APIRouter()


@router.post("/api/datasets/{dataset_id}/forensics")
def run_forensics(dataset_id: DatasetId, request: Request, db: Db) -> Any:
    """Compute the forensic report for the dataset's current data version (nothing is stored)."""
    try:
        return service.run_forensics(db, request.app.state.config, dataset_id)
    except LedgerSpyError as exc:
        return _rejected(exc)
