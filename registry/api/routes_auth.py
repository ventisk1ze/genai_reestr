"""Роуты входа и справочников: /api/login, /api/logout, /api/whoami, /api/config, /api/admin/pin."""
from fastapi import APIRouter, Request

from registry.api import read_body, run_sync, sid_of, user_of
from registry.sessions import session_user
from registry.auth import op_admin_pin, op_login, op_logout, require_admin
from registry.settings import op_save_config

router = APIRouter()


@router.post("/api/login")
async def login(request: Request):
    body = await read_body(request)
    return await run_sync(op_login, body)


@router.post("/api/logout")
async def logout(request: Request):
    return await run_sync(op_logout, sid_of(request))


@router.get("/api/whoami")
async def whoami(request: Request):
    return {"user": session_user(sid_of(request))}


@router.post("/api/config")
async def save_config(request: Request):
    body = await read_body(request)
    u = user_of(request)
    require_admin(u)
    return await run_sync(op_save_config, body, u["name"])


@router.post("/api/admin/pin")
async def admin_pin(request: Request):
    body = await read_body(request)
    require_admin(user_of(request))
    return await run_sync(op_admin_pin, body)
