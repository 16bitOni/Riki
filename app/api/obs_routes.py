"""Read-only endpoints behind /dashboard. Protected by the same X-API-Key as the rest of the API."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.routes import require_api_key
from app.obs import health, logs, queries

router = APIRouter(prefix="/obs", dependencies=[Depends(require_api_key)])

Hours = Annotated[float, Query(ge=0.05, le=24 * 90)]


@router.get("/overview")
async def overview(hours: Hours = 24):
    return await queries.overview(hours)


@router.get("/alerts")
async def alerts():
    return await health.alerts()


@router.get("/traces")
async def traces(
    hours: Hours = 24, kind: str = "", status: str = "", source: str = "", q: Annotated[str, Query(max_length=200)] = "",
    min_ms: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
):
    return await queries.traces(kind, status, source, q, min_ms, hours, limit, offset)


@router.get("/traces/{trace_id}")
async def trace_detail(trace_id: UUID):
    found = await queries.trace_detail(trace_id)
    if found is None:
        raise HTTPException(404, "No such trace")
    return found


@router.get("/llm")
async def llm(hours: Hours = 24):
    return await queries.llm(hours)


@router.get("/tools")
async def tools(hours: Hours = 24):
    return await queries.tools(hours)


@router.get("/retrieval")
async def retrieval(hours: Hours = 24):
    return await queries.retrieval(hours)


@router.get("/memory")
async def memory():
    return await queries.memory()


@router.get("/db")
async def db_tables():
    return await queries.db_tables()


@router.get("/db/{table}")
async def db_rows(
    table: str, limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str, Query(max_length=200)] = "", sort: str = "", desc: bool = True,
):
    found = await queries.db_rows(table, limit, offset, q, sort, desc)
    if found is None:
        raise HTTPException(404, "That table cannot be browsed")
    return found


@router.get("/logs")
async def log_lines(
    level: str = "INFO", q: Annotated[str, Query(max_length=200)] = "", after: int = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 300,
):
    return {"lines": logs.handler.query(level, q, after, limit), "counts": logs.handler.counts()}


@router.get("/system")
async def system():
    return await health.system()
