"""P8 endpoint: the dataset anomaly report (Isolation Forest, robust peer z, tail percentiles).

Token-protected, read-only.
"""

from typing import Any

from fastapi import APIRouter, Request

from ledgerspy.anomaly import service
from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import DatasetId, _rejected
from ledgerspy.errors import LedgerSpyError

router = APIRouter()


@router.post("/api/datasets/{dataset_id}/anomaly")
def run_anomaly(dataset_id: DatasetId, request: Request, db: Db) -> Any:
    """Compute the anomaly report for the dataset's current data version (nothing is stored)."""
    try:
        return service.run_anomaly(db, request.app.state.config, dataset_id)
    except LedgerSpyError as exc:
        return _rejected(exc)
