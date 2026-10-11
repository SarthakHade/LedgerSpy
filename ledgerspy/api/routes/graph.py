"""P13 endpoints: the dataset's relationship graph (read-only, token-protected).

Every list is paginated with `size` ≤ `graph.limits.max_page_size`; the visualization payload is bounded by
`graph.limits.visualization_max_*` and reports truncation. Optional `from` / `to` (ISO dates) restrict the
graph to transactions in that window. Identifiers are validated by pattern; no endpoint takes a file path.
"""

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam

from ledgerspy.api.limits import Page
from ledgerspy.api.routes.cases import Db
from ledgerspy.api.routes.datasets import DatasetId, _rejected
from ledgerspy.errors import LedgerSpyError, require
from ledgerspy.graph import model, service

router = APIRouter()

NodeId = Annotated[str, PathParam(pattern=r"^CE-[0-9a-f]{16}$")]
EdgeId = Annotated[str, PathParam(pattern=r"^G-[0-9a-f]{16}$")]
EdgeType = Literal[
    "paid",
    "shares_bank_account",
    "shares_tax_id",
    "shares_contact",
    "shares_address",
    "possible_same_entity",
    "potential_conflict_of_interest",
]
NodeType = Literal[
    "group_company",
    "employee",
    "vendor",
    "customer",
    "vendor_customer",
    "payer",
    "payee",
    "payer_payee",
    "account",
    "unknown",
]
Sort = Literal["exposure", "pagerank", "betweenness", "degree", "label"]
CycleStatus = Literal["review", "ordered_low_retention", "insufficient_data", "not_time_ordered"]
Size = Annotated[int, Query(ge=1, le=5000)]
From = Annotated[date | None, Query(alias="from")]
To = Annotated[date | None, Query(alias="to")]
require(set(EdgeType.__args__) == set(model.EDGE_TYPES), "the API edge vocabulary differs from the model's")


def _limits(request: Request):
    return request.app.state.config.settings.graph.limits


def _size(request: Request, size: int) -> int:
    lim = _limits(request).max_page_size
    if size > lim:
        raise LedgerSpyError("page_too_large", f"size must be at most {lim}", {"max_page_size": lim})
    return size


def _run(request: Request, db, dataset_id: str, frm: date | None, to: date | None):
    return service.run(db, request.app.state.config, dataset_id, frm, to)


@router.get("/api/datasets/{dataset_id}/graph")
def graph_summary(dataset_id: DatasetId, request: Request, db: Db, frm: From = None, to: To = None) -> Any:
    try:
        return service.summary(_run(request, db, dataset_id, frm, to))
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/nodes")
def graph_nodes(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Page = 1,
    size: Size = 50,
    type: Annotated[NodeType | None, Query()] = None,  # noqa: A002 — public query parameter name
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    sort: Sort = "exposure",
    frm: From = None,
    to: To = None,
) -> Any:
    try:
        r = _run(request, db, dataset_id, frm, to)
        return service.nodes_page(r, page, _size(request, size), type, q, sort)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/nodes/{node_id}")
def graph_node(
    dataset_id: DatasetId, node_id: NodeId, request: Request, db: Db, frm: From = None, to: To = None
) -> Any:
    try:
        r = _run(request, db, dataset_id, frm, to)
        return service.node_detail(r, node_id, _limits(request).neighbourhood_max_edges)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/edges")
def graph_edges(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Page = 1,
    size: Size = 50,
    type: Annotated[EdgeType | None, Query()] = None,  # noqa: A002
    node: Annotated[str | None, Query(pattern=r"^CE-[0-9a-f]{16}$")] = None,
    frm: From = None,
    to: To = None,
) -> Any:
    try:
        r = _run(request, db, dataset_id, frm, to)
        return service.edges_page(r, page, _size(request, size), type, node)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/edges/{edge_id}")
def graph_edge(
    dataset_id: DatasetId, edge_id: EdgeId, request: Request, db: Db, frm: From = None, to: To = None
) -> Any:
    try:
        r = _run(request, db, dataset_id, frm, to)
        return service.edge_detail(r, edge_id, _limits(request).edge_max_transactions)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/cycles")
def graph_cycles(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Page = 1,
    size: Size = 50,
    status: Annotated[CycleStatus | None, Query()] = None,
    frm: From = None,
    to: To = None,
) -> Any:
    try:
        r = _run(request, db, dataset_id, frm, to)
        return service.cycles_page(r, page, _size(request, size), status)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/metrics")
def graph_metrics(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Page = 1,
    size: Size = 50,
    sort: Sort = "pagerank",
    frm: From = None,
    to: To = None,
) -> Any:
    try:
        r = _run(request, db, dataset_id, frm, to)
        return service.metrics_page(r, page, _size(request, size), sort)
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/employee-vendor")
def graph_employee_vendor(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Page = 1,
    size: Size = 50,
    frm: From = None,
    to: To = None,
) -> Any:
    try:
        r = _run(request, db, dataset_id, frm, to)
        return service._page(r.employee_vendor, page, _size(request, size))
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/ghost-vendors")
def graph_ghost(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Page = 1,
    size: Size = 50,
    frm: From = None,
    to: To = None,
) -> Any:
    try:
        r = _run(request, db, dataset_id, frm, to)
        return service._page(
            r.ghost["items"], page, _size(request, size), {"master_data": r.ghost["master_data"]}
        )
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/signals")
def graph_signals(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    page: Page = 1,
    size: Size = 50,
    frm: From = None,
    to: To = None,
) -> Any:
    try:
        r = _run(request, db, dataset_id, frm, to)
        return service._page(r.signals, page, _size(request, size), {"fused_in_p9": False})
    except LedgerSpyError as exc:
        return _rejected(exc)


@router.get("/api/datasets/{dataset_id}/graph/visualization")
def graph_visualization(
    dataset_id: DatasetId,
    request: Request,
    db: Db,
    max_nodes: Annotated[int | None, Query(ge=1)] = None,
    edge_types: Annotated[list[EdgeType] | None, Query()] = None,
    frm: From = None,
    to: To = None,
) -> Any:
    lim = _limits(request)
    try:
        if max_nodes is not None and max_nodes > lim.visualization_max_nodes:
            raise LedgerSpyError("too_many_nodes", f"max_nodes must be at most {lim.visualization_max_nodes}")
        r = _run(request, db, dataset_id, frm, to)
        return service.visualization(
            r, max_nodes or lim.visualization_max_nodes, lim.visualization_max_edges, edge_types
        )
    except LedgerSpyError as exc:
        return _rejected(exc)
