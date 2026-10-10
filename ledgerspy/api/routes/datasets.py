"""P3 ingestion endpoints: upload a file into a case, inspect the dataset, confirm its mapping, page rows.

The upload body is the raw file (`Content-Type: application/octet-stream`). It is streamed to a staging file
with the size limit enforced chunk by chunk; the original filename travels as a query parameter and is
metadata only (never a path).
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ledgerspy.api.limits import Page
from ledgerspy.api.routes.cases import Db
from ledgerspy.cases import evidence
from ledgerspy.cases.db import connect
from ledgerspy.errors import LedgerSpyError
from ledgerspy.ingest import service
from ledgerspy.ingest.errors import IngestError
from ledgerspy.ingest.mapping import Mapping

router = APIRouter()

CaseId = Annotated[str, PathParam(pattern=r"^C-[0-9A-HJKMNP-TV-Z]{26}$")]
DatasetId = Annotated[str, PathParam(pattern=r"^D-[0-9A-HJKMNP-TV-Z]{26}$")]


def _rejected(exc: LedgerSpyError) -> JSONResponse:
    return JSONResponse({"status": "rejected", "error": exc.to_dict()}, status_code=exc.http_status)


@router.post("/api/cases/{case_id}/files", status_code=201)
async def upload_file(
    request: Request,
    case_id: CaseId,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    kind: Annotated[str, Query(max_length=32)] = "ledger",
    sheet: Annotated[str | None, Query(min_length=1, max_length=31)] = None,
    delimiter: Annotated[str | None, Query(min_length=1, max_length=1)] = None,
) -> Any:
    state = request.app.state
    limit = state.config.settings.ingest.max_file_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return _rejected(
            IngestError(
                "file_too_large",
                f"The file exceeds the {limit}-byte upload limit.",
                {"limit_bytes": limit},
                http_status=413,
            )
        )
    try:
        staged = await evidence.stage_stream(request.stream(), state.workspace, limit)
    except LedgerSpyError as exc:
        return _rejected(exc)

    def work() -> dict[str, Any]:
        conn = connect(state.db_path)
        try:
            return service.ingest_upload(
                conn,
                state.config,
                state.workspace,
                case_id,
                staged,
                filename,
                kind,
                state.actor,
                sheet,
                delimiter,
            )
        finally:
            conn.close()

    try:
        return await run_in_threadpool(work)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: DatasetId, db: Db) -> Any:
    try:
        return service.get_dataset(db, dataset_id)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.put("/api/datasets/{dataset_id}/mapping")
def confirm_mapping(dataset_id: DatasetId, mapping: Mapping, request: Request, db: Db) -> Any:
    state = request.app.state
    try:
        return service.confirm_mapping(db, state.config, state.workspace, dataset_id, mapping, state.actor)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/transactions")
def list_transactions(
    dataset_id: DatasetId,
    db: Db,
    page: Page = 1,
    size: Annotated[int, Query(ge=1, le=500)] = 100,
    view: Annotated[Literal["normalized", "canonical"], Query()] = "normalized",
) -> Any:
    try:
        return service.get_transactions(db, dataset_id, page, size, view)
    except LedgerSpyError as exc:
        return _rejected(exc)
