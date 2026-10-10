"""P15 endpoints: investigation cases, evidence links, notes, decisions, OCR corrections, activity, audit.

Token-protected like every /api route. Lists are paginated (`size` ≤ 500) in a deterministic order;
identifiers are validated by pattern; request bodies forbid unknown fields; no endpoint takes a file path. An
investigation lives inside an engagement case (`/api/cases`), which scopes every record it may reference.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import _rejected
from ledgerspy.cases import audit
from ledgerspy.errors import LedgerSpyError
from ledgerspy.investigations import model, service

router = APIRouter()

InvId = Annotated[str, PathParam(pattern=r"^IV-[0-9A-HJKMNP-TV-Z]{26}$")]
LinkId = Annotated[str, PathParam(pattern=r"^LK-[0-9A-HJKMNP-TV-Z]{26}$")]
Page = Annotated[int, Query(ge=1)]
Size = Annotated[int, Query(ge=1, le=500)]
CASE = r"^C-[0-9A-HJKMNP-TV-Z]{26}$"
LINK = r"^LK-[0-9A-HJKMNP-TV-Z]{26}$"
LinkType = Literal[tuple(model.LINK_TYPES)]  # type: ignore[valid-type]
Action = Literal[tuple(sorted({a for v in model.ACTIONS_BY_KIND.values() for a in v}))]  # type: ignore[valid-type]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinkIn(_Body):
    link_type: LinkType
    dataset_id: str | None = Field(default=None, pattern=r"^D-[0-9A-HJKMNP-TV-Z]{26}$")
    run_id: str | None = Field(default=None, pattern=r"^RR-[0-9a-f]{16}$")
    ref_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=3, max_length=2000)
    annotation: str | None = Field(default=None, max_length=2000)


class InvestigationIn(_Body):
    case_id: str = Field(pattern=CASE)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    priority: Literal[model.PRIORITIES] = "medium"  # type: ignore[valid-type]
    links: list[LinkIn] = Field(default_factory=list, max_length=50)


class InvestigationPatch(_Body):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    priority: Literal[model.PRIORITIES] | None = None  # type: ignore[valid-type]


class TransitionIn(_Body):
    to_status: Literal[model.STATUSES]  # type: ignore[valid-type]
    reason: str | None = Field(default=None, max_length=10_000)
    outcome: Literal[model.OUTCOMES] | None = None  # type: ignore[valid-type]
    conclusion: str | None = Field(default=None, max_length=10_000)


class UnlinkIn(_Body):
    reason: str = Field(min_length=3, max_length=2000)


class NoteIn(_Body):
    body: str = Field(min_length=1, max_length=10_000)
    link_id: str | None = Field(default=None, pattern=LINK)


class DecisionIn(_Body):
    link_id: str | None = Field(default=None, pattern=LINK)
    action: Action
    rationale: str = Field(min_length=3, max_length=10_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    supporting: list[str] = Field(default_factory=list, max_length=100)
    contradicting: list[str] = Field(default_factory=list, max_length=100)
    unresolved: bool | None = None


class OcrCorrectionIn(_Body):
    dataset_id: str = Field(pattern=r"^D-[0-9A-HJKMNP-TV-Z]{26}$")
    item_id: str = Field(pattern=r"^PX-[0-9a-f]{16}$")
    field: Literal[
        "date", "value_date", "description", "reference", "debit", "credit", "amount", "drcr", "balance"
    ]
    value: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=3, max_length=2000)


def _call(fn, *args, **kw) -> Any:
    try:
        return fn(*args, **kw)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/investigations/vocabulary")
def vocabulary() -> Any:
    return {
        "statuses": model.STATUSES,
        "transitions": model.TRANSITIONS,
        "priorities": model.PRIORITIES,
        "outcomes": model.OUTCOMES,
        "link_types": model.LINK_TYPES,
        "actions_by_kind": model.ACTIONS_BY_KIND,
        "action_text": model.ACTION_TEXT,
        "note": service.NOTE,
    }


@router.post("/api/investigations", status_code=201)
def create(body: InvestigationIn, request: Request, db: Db) -> Any:
    return _call(
        service.create,
        db,
        request.app.state.config,
        body.case_id,
        body.title,
        body.description,
        body.priority,
        request.app.state.actor,
        [x.model_dump() for x in body.links],
    )


@router.get("/api/investigations")
def list_investigations(
    db: Db,
    case_id: Annotated[str | None, Query(pattern=CASE)] = None,
    status: Literal[model.STATUSES] | None = None,  # type: ignore[valid-type]
    priority: Literal[model.PRIORITIES] | None = None,  # type: ignore[valid-type]
    page: Page = 1,
    size: Size = 50,
) -> Any:
    return service.list_page(db, case_id, status, priority, page, size)


@router.get("/api/investigations/{inv_id}")
def get_investigation(inv_id: InvId, db: Db) -> Any:
    return _call(service.get, db, inv_id)


@router.patch("/api/investigations/{inv_id}")
def update_investigation(inv_id: InvId, body: InvestigationPatch, request: Request, db: Db) -> Any:
    return _call(service.update, db, inv_id, body.model_dump(), request.app.state.actor)


@router.post("/api/investigations/{inv_id}/transitions")
def transition(inv_id: InvId, body: TransitionIn, request: Request, db: Db) -> Any:
    return _call(
        service.transition,
        db,
        inv_id,
        body.to_status,
        request.app.state.actor,
        body.reason,
        body.outcome,
        body.conclusion,
    )


@router.get("/api/investigations/{inv_id}/links")
def links(
    inv_id: InvId,
    db: Db,
    active: bool | None = None,
    link_type: LinkType | None = None,
    page: Page = 1,
    size: Size = 100,
) -> Any:
    return _call(service.links_page, db, inv_id, active, link_type, page, size)


@router.post("/api/investigations/{inv_id}/links", status_code=201)
def add_link(inv_id: InvId, body: LinkIn, request: Request, db: Db) -> Any:
    return _call(
        service.link, db, request.app.state.config, inv_id, body.model_dump(), request.app.state.actor
    )


@router.get("/api/investigations/{inv_id}/links/{link_id}")
def link_detail(inv_id: InvId, link_id: LinkId, request: Request, db: Db, verify: bool = True) -> Any:
    return _call(service.link_detail, db, request.app.state.config, inv_id, link_id, verify)


@router.post("/api/investigations/{inv_id}/links/{link_id}/unlink")
def unlink(inv_id: InvId, link_id: LinkId, body: UnlinkIn, request: Request, db: Db) -> Any:
    return _call(service.unlink, db, inv_id, link_id, body.reason, request.app.state.actor)


@router.get("/api/investigations/{inv_id}/notes")
def notes(inv_id: InvId, db: Db, page: Page = 1, size: Size = 100) -> Any:
    return _call(service.notes_page, db, inv_id, page, size)


@router.post("/api/investigations/{inv_id}/notes", status_code=201)
def add_note(inv_id: InvId, body: NoteIn, request: Request, db: Db) -> Any:
    return _call(service.add_note, db, inv_id, body.body, body.link_id, request.app.state.actor)


@router.get("/api/investigations/{inv_id}/decisions")
def decisions(
    inv_id: InvId,
    db: Db,
    link_id: Annotated[str | None, Query(pattern=LINK)] = None,
    page: Page = 1,
    size: Size = 100,
) -> Any:
    return _call(service.decisions_page, db, inv_id, link_id, page, size)


@router.post("/api/investigations/{inv_id}/decisions", status_code=201)
def add_decision(inv_id: InvId, body: DecisionIn, request: Request, db: Db) -> Any:
    return _call(
        service.decide,
        db,
        request.app.state.config,
        inv_id,
        body.link_id,
        body.action,
        body.rationale,
        request.app.state.actor,
        body.payload,
        body.supporting,
        body.contradicting,
        body.unresolved,
    )


@router.post("/api/investigations/{inv_id}/ocr-corrections", status_code=201)
def ocr_correction(inv_id: InvId, body: OcrCorrectionIn, request: Request, db: Db) -> Any:
    return _call(
        service.correct_ocr,
        db,
        request.app.state.config,
        inv_id,
        body.dataset_id,
        body.item_id,
        body.field,
        body.value,
        body.rationale,
        request.app.state.actor,
    )


@router.get("/api/investigations/{inv_id}/activity")
def activity(inv_id: InvId, db: Db, page: Page = 1, size: Size = 100) -> Any:
    return _call(service.activity_page, db, inv_id, page, size)


@router.get("/api/investigations/{inv_id}/summary")
def summary(inv_id: InvId, db: Db) -> Any:
    return _call(service.summary, db, inv_id)


@router.get("/api/investigations/{inv_id}/manifest")
def manifest(inv_id: InvId, request: Request, db: Db) -> Any:
    return _call(service.manifest, db, request.app.state.workspace, inv_id)


@router.get("/api/audit/verify")
def audit_verify(db: Db) -> Any:
    """Recompute the hash chain (see docs/investigations.md §Audit for what this does and does not prove)."""
    r = audit.verify(db)
    return {
        "ok": r.ok,
        "entries": r.entries,
        "head_hash": r.head_hash,
        "message": r.message,
        "failed_seq": r.failed_seq,
        "limitation": "detects edited, inserted, reordered or deleted entries and missing triggers; it "
        "cannot detect truncation of the newest entries or a rewrite of the whole chain by someone who "
        "controls the database file — record the head hash outside the database to detect those.",
    }
