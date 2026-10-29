"""Роуты базы знаний (T-012): /api/kb/status, /api/kb/task, /api/kb/done.

Онтология и отправка — только через прокси сотрудника: прямо на сервере эти пути отвечают
подсказкой (op_kb_direct), как /api/bb/upload в T-010. База одна — параметров выбора нет.
"""
from fastapi import APIRouter, Request

from registry.api import read_body, run_sync, user_of
from registry.kb import op_kb_direct, op_kb_done, op_kb_status, op_kb_task

router = APIRouter()


@router.get("/api/kb/status")
async def kb_status():
    return await run_sync(op_kb_status)


@router.get("/api/kb/ontology")
async def kb_ontology_direct():
    return await run_sync(op_kb_direct)


@router.get("/api/kb/task")
async def kb_task(request: Request):
    """kind, id, path, node — первые значения параметров, как в прежнем parse_qs(...)[0]."""
    g = lambda k: [request.query_params.get(k) or ""]
    q = {"kind": g("kind"), "id": g("id"), "path": g("path"), "node": g("node")}
    u = user_of(request)
    return await run_sync(op_kb_task, q, u)


@router.post("/api/kb/upload")
async def kb_upload_direct(request: Request):
    await read_body(request)
    return await run_sync(op_kb_direct)


@router.post("/api/kb/done")
async def kb_done(request: Request):
    body = await read_body(request)
    u = user_of(request)
    return await run_sync(op_kb_done, body, u)
