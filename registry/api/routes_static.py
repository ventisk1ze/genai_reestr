"""Статика и главная страница: /, /index.html (Jinja2-шаблон с токеном), /static/*.

Разбор файлов — как в прежнем сервере: путь только внутри static/, типы из STATIC_TYPES,
неизвестное — 404 {"error": "not found"}. bootstrap.js (если есть) вставляется на место
маркера /*__BOOTSTRAP__*/ в собранный HTML — механизм сохранён для совместимости.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from jinja2 import Environment, FileSystemLoader

from registry import config

router = APIRouter()

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
                ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}

_env = Environment(loader=FileSystemLoader(str(config.TEMPLATES_DIR)), autoescape=False, auto_reload=True)
# auto_reload: как в прежнем сервере, шаблон читается с диска при каждом запросе — правки каркаса
# подхватываются без перезапуска сервера.


def _not_found():
    return JSONResponse({"error": "not found"}, status_code=404)


def render_index() -> Response:
    """Главная страница: шаблон + токен API (+ bootstrap.js, если кто-то положил его в static/)."""
    html = _env.get_template("index.html").render(token=config.TOKEN)
    boot = config.STATIC_DIR / "bootstrap.js"
    if boot.exists():
        script = boot.read_text("utf-8").replace("__TOKEN__", config.TOKEN)
        html = html.replace("/*__BOOTSTRAP__*/", "<script>" + script + "</" + "script>")
    return Response(html, media_type="text/html; charset=utf-8")


@router.get("/")
async def index():
    return render_index()


@router.get("/index.html")
async def index_html():
    return render_index()


@router.get("/static/{path:path}")
async def static_files(path: str):
    target = (config.STATIC_DIR / path).resolve()
    if not str(target).startswith(str(config.STATIC_DIR.resolve())) or not target.is_file():
        return _not_found()
    return Response(target.read_bytes(),
                    media_type=STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream"))


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                  include_in_schema=False)
async def catch_all(request: Request, path: str):
    """Неизвестный путь — 404 {"error": "not found"}: как прежний поиск по таблице роутов."""
    return _not_found()
