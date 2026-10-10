"""P14b endpoints: PDF statement extraction, review and page images (token-protected like every /api route).

Upload with the P3 endpoint (`POST /api/cases/{case_id}/files?filename=x.pdf&kind=bank_statement`); confirm
with `PUT /api/datasets/{id}/mapping` once the extraction is complete and reviewed. Lists are paginated with
`size` ≤ 500; identifiers are validated by pattern; no endpoint takes a file path. Page images are PNG
rendered locally at a bounded resolution.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import DatasetId, _rejected
from ledgerspy.cases.db import connect
from ledgerspy.errors import LedgerSpyError
from ledgerspy.pdfstatements import service

router = APIRouter()

ItemId = Annotated[str, PathParam(pattern=r"^PX-[0-9a-f]{16}$")]
PageNo = Annotated[int, PathParam(ge=1, le=100_000)]
Kind = Literal[
    "transaction",
    "continuation",
    "table_header",
    "statement_info",
    "page_header",
    "page_footer",
    "opening_balance",
    "closing_balance",
    "carry_forward",
    "total",
    "unparsed",
]
ReviewFilter = Literal["required", "pending", "confirmed", "excluded", "auto_accepted", "not_reviewable"]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(pattern=r"^PX-[0-9a-f]{16}$")
    decision: Literal["confirm", "exclude"]
    reason: str | None = Field(default=None, max_length=500)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decisions: list[Decision] = Field(min_length=1, max_length=500)


@router.get("/api/pdf/ocr-status")
def ocr_status(request: Request) -> Any:
    try:
        return service.ocr_status(request.app.state.config)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/pdf")
def pdf_summary(dataset_id: DatasetId, db: Db) -> Any:
    try:
        return service.summary(db, dataset_id)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/pdf/items")
def pdf_items(
    dataset_id: DatasetId,
    db: Db,
    kind: Kind | None = None,
    review: ReviewFilter | None = None,
    pdf_page: Annotated[int | None, Query(ge=1, le=100_000)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Any:
    try:
        return service.items_page(db, dataset_id, kind, review, pdf_page, page, size)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/pdf/items/{item_id}")
def pdf_item(dataset_id: DatasetId, item_id: ItemId, db: Db) -> Any:
    try:
        return service.item_detail(db, dataset_id, item_id)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.post("/api/datasets/{dataset_id}/pdf/review")
def pdf_review(dataset_id: DatasetId, body: ReviewRequest, request: Request, db: Db) -> Any:
    try:
        return service.review(
            db, dataset_id, [d.model_dump() for d in body.decisions], request.app.state.actor
        )
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.post("/api/datasets/{dataset_id}/pdf/extract")
async def pdf_reextract(dataset_id: DatasetId, request: Request) -> Any:
    state = request.app.state

    def work() -> Any:
        conn = connect(state.db_path)
        try:
            return service.reextract(conn, state.config, state.workspace, dataset_id, state.actor)
        finally:
            conn.close()

    try:
        return await run_in_threadpool(work)
    except LedgerSpyError as exc:
        return _rejected(exc)


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: Literal[
        "date", "value_date", "description", "reference", "debit", "credit", "amount", "drcr", "balance"
    ]
    value: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=3, max_length=2000)


@router.post("/api/datasets/{dataset_id}/pdf/items/{item_id}/corrections", status_code=201)
def pdf_correct(dataset_id: DatasetId, item_id: ItemId, body: CorrectionIn, request: Request, db: Db) -> Any:
    """P15: correct one extracted field as an overlay (original kept; see pdfstatements.corrections)."""
    from ledgerspy.pdfstatements import corrections

    try:
        return corrections.correct(
            db,
            request.app.state.config,
            dataset_id,
            item_id,
            body.field,
            body.value,
            body.rationale,
            request.app.state.actor,
        )
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/pdf/corrections")
def pdf_corrections(
    dataset_id: DatasetId, db: Db, item_id: Annotated[str | None, Query(pattern=r"^PX-[0-9a-f]{16}$")] = None
) -> Any:
    from ledgerspy.pdfstatements import corrections

    return corrections.list_for(db, dataset_id, item_id)


@router.get("/api/datasets/{dataset_id}/pdf/pages/{page_no}/image")
async def pdf_page_image(
    dataset_id: DatasetId,
    page_no: PageNo,
    request: Request,
    dpi: Annotated[int | None, Query(ge=36, le=600)] = None,
) -> Any:
    state = request.app.state

    def work() -> bytes:
        conn = connect(state.db_path)
        try:
            return service.page_image(conn, state.config, state.workspace, dataset_id, page_no, dpi)
        finally:
            conn.close()

    try:
        png = await run_in_threadpool(work)
    except LedgerSpyError as exc:
        return _rejected(exc)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})
