"""HTTP-слой: FastAPI-приложение реестра.

Контракт /api/* сохранён бит-в-бит с прежним сервером на BaseHTTPRequestHandler:
те же пути, заголовки (X-Token, X-Session), коды и тексты ошибок {"error": ...}.
Роуты — async def, операции исполняются в threadpool (run_in_threadpool): хранилища
синхронные с блокировкой LOCK, как раньше в ThreadingHTTPServer.
"""
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from registry import config, runtime
from registry.errors import AuthError, BadRequest, Conflict, Forbidden, Locked
from registry.sessions import session_user


def _err(code, payload):
    return JSONResponse(payload, status_code=code)


def create_app():
    """Собрать приложение: middleware → обработчики ошибок → роуты → статика."""
    from registry.api import routes_ai, routes_auth, routes_bb, routes_kb, routes_static, routes_tasks

    app = FastAPI(title="Реестр задач ЦК", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(routes_auth.router)
    app.include_router(routes_tasks.router)
    app.include_router(routes_ai.router)
    app.include_router(routes_bb.router)
    app.include_router(routes_kb.router)
    app.include_router(routes_static.router)

    # --- порядок проверок как в прежнем _guard(): сначала хост (--local), потом токен /api/*
    @app.middleware("http")
    async def guard_and_nostore(request: Request, call_next):
        if runtime.LOCAL_MODE and config.LOCAL_HOST_CHECK \
                and (request.headers.get("host") or "").split(":")[0] not in ("localhost", "127.0.0.1"):
            # --local — запуск на своём ПК: только localhost (защита от DNS-rebinding, отключается
            # REG_LOCAL_HOST_CHECK=0). На DataLab так и выглядит — с подсказкой
            return _err(403, {"error": "Сервер запущен с --local и принимает запросы только с localhost. "
                                    "На DataLab запускайте без --local: bash start.sh "
                                    "(или REG_LOCAL_HOST_CHECK=0, если --local пробрасывается через прокси)"})
        if request.url.path.startswith("/api/") and request.headers.get("X-Token") != config.TOKEN:
            return _err(403, {"error": "Неверный токен — перезагрузите страницу"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"   # реестр всегда свежий, как и раньше
        return response

    # --- маппинг исключений домена в коды и тексты — один в один с прежним Handler._run
    @app.exception_handler(AuthError)
    async def _auth(request, exc):
        return _err(401, {"error": str(exc), "login": True})

    @app.exception_handler(Forbidden)
    async def _forbidden(request, exc):
        return _err(403, {"error": str(exc)})

    @app.exception_handler(Locked)
    async def _locked(request, exc):
        return _err(429, {"error": str(exc)})

    @app.exception_handler(BadRequest)
    async def _bad(request, exc):
        return _err(400, {"error": str(exc)})

    @app.exception_handler(Conflict)
    async def _conflict(request, exc):
        return _err(409, {"error": str(exc)})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request, exc):
        # 405 «метод не тот» в прежнем сервере был 404 «not found»: роут на путь есть, метод — нет
        if exc.status_code == 405:
            return _err(404, {"error": "not found"})
        return _err(exc.status_code, {"error": exc.detail})

    @app.exception_handler(Exception)
    async def _crash(request, exc):
        return _err(500, {"error": f"{type(exc).__name__}: {exc}"})

    return app


async def read_body(request: Request):
    """Тело запроса как JSON-словарь — семантика прежнего Handler._body: пусто → {}, битый JSON → 400."""
    raw = await request.body()
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        raise BadRequest("Некорректный JSON")


async def drain_body(request: Request, n, cap=64 * 1024 * 1024):
    """Дочитать и выбросить тело (не больше cap, кусками): иначе клиент вместо ответа увидит обрыв связи."""
    left = min(max(0, n), cap)
    if left <= 0:
        return
    try:
        async for chunk in request.stream():
            left -= len(chunk)
            if left <= 0:
                break
    except Exception:
        pass


def sid_of(request: Request) -> str:
    return (request.headers.get("X-Session") or "").strip()


def user_of(request: Request) -> dict:
    """Кто действует — только из сессии (X-User больше не подпись автора)."""
    sid = sid_of(request)
    u = session_user(sid)
    if u is None:
        raise AuthError("Сессия истекла или недействительна — войдите снова" if sid
                        else "Войдите в реестр (ФИО и PIN), чтобы сохранять изменения")
    return u


def run_sync(fn, *args, **kwargs):
    """Операция хранилища — в threadpool: как прежний поток на запрос, сессии и LOCK те же."""
    return run_in_threadpool(fn, *args, **kwargs)
