"""Роуты выгрузки в BB: /api/bb/status, /api/bb/task, /api/bb/done, /api/bb/upload."""
from fastapi import APIRouter, Request

from registry.api import read_body, run_sync, user_of
from registry.bb.ops import op_bb_done, op_bb_status, op_bb_task, op_bb_upload_direct

router = APIRouter()


@router.get("/api/bb/status")
async def bb_status():
    return await run_sync(op_bb_status)


@router.get("/api/bb/task")
async def bb_task(request: Request):
    """kind и id — первые значения параметров, как в прежнем parse_qs(...)[0]."""
    q = {"kind": [request.query_params.get("kind") or ""], "id": [request.query_params.get("id") or ""]}
    u = user_of(request)
    return await run_sync(op_bb_task, q, u)


@router.post("/api/bb/done")
async def bb_done(request: Request):
    body = await read_body(request)
    u = user_of(request)
    return await run_sync(op_bb_done, body, u)


@router.post("/api/bb/upload")
async def bb_upload(request: Request):
    """POST /api/bb/upload пришёл прямо на сервер: сервер реестра в BB не ходит."""
    await read_body(request)
    return await run_sync(op_bb_upload_direct)
