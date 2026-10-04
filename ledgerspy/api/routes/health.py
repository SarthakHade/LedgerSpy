from fastapi import APIRouter, Request

from ledgerspy import __version__
from ledgerspy.api.schemas import Health
from ledgerspy.cases.db import SCHEMA_VERSION

router = APIRouter()


@router.get("/api/health", response_model=Health)
def health(request: Request) -> Health:
    """Public (no token): liveness plus the versions needed to identify this build."""
    return Health(
        status="ok",
        version=__version__,
        schema_version=SCHEMA_VERSION,
        config_sha256=request.app.state.config.sha256,
    )
