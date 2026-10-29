"""Роуты помощника: /api/llm (статус и запрос), /api/ai/file, /api/ai/history."""
import sys

from fastapi import APIRouter, Request

from registry import config
from registry.ai.files import _mb, op_ai_file
from registry.ai.llm import ai_history, op_ai_history, op_llm, op_llm_status, remember_llm
from registry.api import drain_body, read_body, run_sync, sid_of, user_of
from registry.errors import BadRequest
from registry.sessions import session_user

router = APIRouter()


@router.get("/api/llm")
async def llm_status(request: Request):
    return await run_sync(op_llm_status, session_user(sid_of(request)))


@router.post("/api/llm")
async def llm(request: Request):
    body = await read_body(request)
    u = user_of(request)   # лава-болл запускает модель — только для вошедших
    r = await run_sync(op_llm, body, u)
    try:   # ошибка модели — исключение выше, ничего не сохраняем; сбой хранилища не отнимает ответ
        r["msgs"] = await run_sync(remember_llm, u["name"], body, r["reply"])   # диалог как сохранён (T-009: файлы)
    except Exception as e:
        print(f"История лава-болла не сохранена: {type(e).__name__}: {e}", file=sys.stderr)
    return r


@router.post("/api/ai/file")
async def ai_file(request: Request):
    """Файл помощника: без сессии (401) и сверх лимита размера (400) тело в память не читается."""
    try:
        n = max(0, int(request.headers.get("content-length") or 0))
    except ValueError:
        n = 0
    try:
        user_of(request)
        if n > config.AI_FILE_BODY_MAX:
            raise BadRequest(f"Файл больше {_mb(config.AI_FILE_MAX_MB)} МБ — приложите файл поменьше "
                             "или только нужную часть")
    except Exception:
        await drain_body(request, n)   # как раньше: дочитать и выбросить, иначе клиент увидит обрыв связи
        raise
    body = await read_body(request)
    return await run_sync(op_ai_file, body)


@router.get("/api/ai/history")
async def get_history(request: Request):
    u = user_of(request)
    return await run_sync(ai_history, u["name"])


@router.post("/api/ai/history")
async def post_history(request: Request):
    body = await read_body(request)
    u = user_of(request)
    return await run_sync(op_ai_history, body, u["name"])
