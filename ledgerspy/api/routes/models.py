"""P11 endpoints: supervised model artefacts (read-only) and supervised inference.

All token-protected (LocalSecurityMiddleware). Models are referenced by model ID only (M- + 16 hex digits),
never by a path; request bodies are closed schemas, so a `path` or any other field is refused. Lifecycle
changes (promote, approve, activate, retire) are CLI-only (`ledgerspy model …`), recorded in the registry and
the audit log. Supervised output is an uncalibrated ranking score, served separately from P9 review risk,
which it never changes.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import DatasetId, _rejected
from ledgerspy.errors import LedgerSpyError
from ledgerspy.supervised import inference, registry

router = APIRouter()

ModelId = Annotated[str, PathParam(pattern=r"^M-[0-9a-f]{16}$")]
InferenceId = Annotated[str, PathParam(pattern=r"^I-[0-9a-f]{16}$")]


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model_id: str | None = Field(default=None, pattern=r"^M-[0-9a-f]{16}$")  # default: the active model
    experimental: bool = False  # must be true to run a model that is not the active approved one
    apply_threshold: bool = True
    page: int = Field(default=1, ge=1)
    size: int = Field(default=50, ge=1, le=500)


@router.get("/api/models")
def list_models(request: Request, db: Db) -> Any:
    try:
        return registry.list_models(db, request.app.state.workspace)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/models/active")
def active_model(request: Request, db: Db) -> Any:
    try:
        return registry.active_status(db, request.app.state.workspace)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/models/{model_id}")
def inspect_model(model_id: ModelId, request: Request, db: Db) -> Any:
    try:
        return registry.describe(db, request.app.state.workspace, model_id, full=True)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.post("/api/datasets/{dataset_id}/supervised/inference")
def run_inference(dataset_id: DatasetId, body: InferenceRequest, request: Request, db: Db) -> Any:
    """Score the dataset with the active approved model, or an explicitly requested experimental one."""
    try:
        result = inference.infer(
            db,
            request.app.state.config,
            request.app.state.workspace,
            dataset_id,
            body.model_id,
            body.experimental,
            body.apply_threshold,
        )
    except LedgerSpyError as exc:
        return _rejected(exc)
    return inference.page(result, body.page, body.size)


@router.get("/api/supervised/inference/{inference_id}")
def inference_metadata(inference_id: InferenceId, request: Request) -> Any:
    try:
        return inference.read_metadata(request.app.state.workspace, inference_id)
    except LedgerSpyError as exc:
        return _rejected(exc)
