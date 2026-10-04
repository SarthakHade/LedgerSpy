"""Case endpoints (skeleton: create and fetch). Findings, audit-log viewer and lifecycle are P15."""

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from ledgerspy.api.schemas import CaseCreate, CaseOut
from ledgerspy.cases import store
from ledgerspy.cases.db import connect

router = APIRouter(prefix="/api/cases")


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]


@router.post("", status_code=201, response_model=CaseOut)
def create_case(body: CaseCreate, request: Request, db: Db) -> dict:
    return store.create_case(
        db,
        name=body.name,
        client=body.client,
        period_start=body.period_start,
        period_end=body.period_end,
        actor=request.app.state.actor,
    )


@router.get("/{case_id}", response_model=CaseOut)
def get_case(case_id: Annotated[str, Path(pattern=r"^C-[0-9A-HJKMNP-TV-Z]{26}$")], db: Db) -> dict:
    case = store.get_case(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case
