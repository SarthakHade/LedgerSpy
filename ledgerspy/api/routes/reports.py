"""P16 endpoints: report preview, generation, listing, retrieval, download and verification.

Token-protected like every /api route. Bodies forbid unknown fields; identifiers are validated by pattern;
no endpoint takes a file name or path (artefacts are addressed by report ID and format only). A report never
changes its sources: preview writes nothing, generation appends one register row, its files and an audit
entry. Memos are drafts: there is no approval endpoint because no report-approval workflow exists.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import _rejected
from ledgerspy.errors import LedgerSpyError
from ledgerspy.reports import model, store

router = APIRouter()

ReportId = Annotated[str, PathParam(pattern=r"^RP-[0-9A-HJKMNP-TV-Z]{26}$")]
INV = r"^IV-[0-9A-HJKMNP-TV-Z]{26}$"
ReportType = Literal[tuple(model.REPORT_TYPES)]  # type: ignore[valid-type]
Format = Literal[tuple(model.FORMATS)]  # type: ignore[valid-type]


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: ReportType
    investigation_ids: list[Annotated[str, Field(pattern=INV)]] = Field(
        default_factory=list, max_length=model.MAX_INVESTIGATIONS
    )
    run_id: str | None = Field(default=None, pattern=r"^RR-[0-9a-f]{16}$")

    def params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.investigation_ids:
            out["investigation_ids"] = sorted(self.investigation_ids)
        if self.run_id:
            out["run_id"] = self.run_id
        return out


def _call(fn, *args, **kw) -> Any:
    try:
        return fn(*args, **kw)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/reports/vocabulary")
def vocabulary() -> Any:
    return {
        "report_types": model.REPORT_TYPES,
        "formats": sorted(model.FORMATS),
        "deferred_formats": {
            "pdf": "no PDF library is installed (see docs/reporting.md)",
            "docx": "no DOCX library is installed",
        },
        "origins": model.ORIGINS,
        "traceability": model.TRACEABILITY,
        "terminology": model.TERMINOLOGY,
        "draft_notice": model.DRAFT_NOTICE,
        "approval_supported": False,
        "limits": {
            "investigations": model.MAX_INVESTIGATIONS,
            "listed_rows": model.MAX_LISTED_ROWS,
            "artifact_bytes": model.MAX_ARTIFACT_BYTES,
        },
    }


@router.post("/api/reports/preview")
def preview(body: ReportIn, request: Request, db: Db) -> Any:
    return _call(
        store.preview,
        db,
        request.app.state.config,
        request.app.state.workspace,
        body.report_type,
        body.params(),
    )


@router.post("/api/reports", status_code=201)
def generate(body: ReportIn, request: Request, db: Db) -> Any:
    return _call(
        store.generate,
        db,
        request.app.state.config,
        request.app.state.workspace,
        body.report_type,
        body.params(),
        request.app.state.actor,
    )


@router.get("/api/reports")
def list_reports(
    db: Db,
    case_id: Annotated[str | None, Query(pattern=r"^C-[0-9A-HJKMNP-TV-Z]{26}$")] = None,
    report_type: ReportType | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Any:
    return store.list_page(db, case_id, report_type, page, size)


@router.get("/api/reports/{report_id}")
def get_report(report_id: ReportId, request: Request, db: Db) -> Any:
    return _call(store.content, db, request.app.state.workspace, report_id)


@router.get("/api/reports/{report_id}/verify")
def verify_report(report_id: ReportId, request: Request, db: Db) -> Any:
    return _call(store.verify, db, request.app.state.workspace, report_id)


@router.get("/api/reports/{report_id}/download")
def download(report_id: ReportId, request: Request, db: Db, format: Format = "html") -> Any:  # noqa: A002
    try:
        rep, data = store.read_artifact(db, request.app.state.workspace, report_id, format)
    except LedgerSpyError as exc:
        return _rejected(exc)
    return Response(
        data,
        media_type=model.FORMATS[format],
        headers={
            "Content-Disposition": f'attachment; filename="{store.download_name(rep, format)}"',
            "X-Content-SHA256": rep["artifacts"][format]["sha256"],
        },
    )
