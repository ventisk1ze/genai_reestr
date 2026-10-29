"""Роуты задач и карточек знаний: /api/state, /api/sync, /api/task, /api/card."""
from fastapi import APIRouter, Request

from registry.api import read_body, run_sync, user_of
from registry.cards import op_read_card, op_save_card
from registry.tasks import op_delete_task, op_save_task, op_state

router = APIRouter()


@router.get("/api/state")
async def state():
    return await run_sync(op_state)


@router.post("/api/sync")
async def sync(request: Request):
    await read_body(request)   # кнопка «Обновить» — просто перечитать реестр; тело не используется
    return await run_sync(op_state)


@router.post("/api/task")
async def save_task(request: Request):
    body = await read_body(request)
    u = user_of(request)
    return await run_sync(op_save_task, body, u)


@router.delete("/api/task")
async def delete_task(request: Request):
    body = await read_body(request)
    u = user_of(request)
    return await run_sync(op_delete_task, body, u)


@router.get("/api/card")
async def read_card(request: Request):
    return await run_sync(op_read_card, request.query_params.get("path") or "")


@router.post("/api/card")
async def save_card(request: Request):
    body = await read_body(request)
    u = user_of(request)
    return await run_sync(op_save_card, body, u)
