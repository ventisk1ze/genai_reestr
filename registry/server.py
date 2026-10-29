"""Запуск веб-сервера: баннер, хранилище, uvicorn (FastAPI-приложение).

Синхронные операции хранилища исполняются в threadpool — семантика прежнего
ThreadingHTTPServer: те же блокировки LOCK и сессии в памяти.
"""
import signal
import socket
import sys
import threading
import webbrowser

import uvicorn

from registry import config, runtime
from registry.ai.llm import gigacode_path, llm_configured
from registry.ai.skills import load_skills
from registry.bb import bb_server_target, bb_target_problem
from registry.store import init_store


def _port_free(host, port):
    """Порт занят — то же сообщение, что и раньше (uvicorn сказал бы по-английски)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
    except OSError as e:
        sys.exit(f"Порт {port} занят: {e}")


def serve(a):
    runtime.LOCAL_MODE = a.local
    runtime.SERVE_PORT = a.port
    try:
        init_store(a.storage)
    except Exception as e:
        sys.exit(f"Хранилище не готово: {e}")
    if not (config.TEMPLATES_DIR / "index.html").exists():
        sys.exit(f"Нет {config.TEMPLATES_DIR / 'index.html'}")
    host = "127.0.0.1" if a.local else "0.0.0.0"
    _port_free(host, a.port)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # как раньше: kill из start.sh — выход с кодом 0

    print(f"Хранилище: {runtime.STORE.describe()}")
    if a.local:
        print(f"Интерфейс: http://localhost:{a.port}/")
    else:
        print(f"Интерфейс: в адресе своего Jupyter замените всё после /jserver на /proxy/{a.port}/")
        if a.port in config.TEAM_PORTS:
            print(f"Для команды: http://jlab-<ваш табельный>:{a.port} — коллеги подключаются: bash connect.sh <ваш табельный>")
        else:
            print(f"ВНИМАНИЕ: порт {a.port} вне {config.TEAM_PORTS.start}–{config.TEAM_PORTS.stop - 1} — коллеги до реестра не достучатся")
    skills = load_skills()
    print(f"Скиллы ({config.SKILLS_DIR}): {', '.join(skills) or 'нет'}")
    import os
    print(f"Ассистент: модель {config.LLM_MODEL} через {os.environ['GIGACHAT_API_URL']}" if llm_configured()
          else "Ассистент: выключен — не задан GIGACHAT_API_URL")
    print(f"GigaCode в лава-болле: {gigacode_path()} (режим подтверждений {config.GIGACODE_APPROVAL})" if gigacode_path()
          else f"GigaCode в лава-болле: недоступен — не найден {config.GIGACODE_BIN}")
    bb, bb_missing = bb_server_target()
    bb_bad = "" if bb_missing else bb_target_problem(bb)
    print(f"Выгрузка карточек в BB: выключена — не заданы {', '.join(bb_missing)}" if bb_missing else
          f"Выгрузка карточек в BB: настройки с ошибкой — {bb_bad}" if bb_bad else
          f"Выгрузка карточек в BB: {bb['repo_url']} → {bb['branch']}, папка {bb['dir']}; "
          "выгружают сотрудники через свой прокси (bash connect.sh)")
    print("Остановить: Ctrl+C")
    if a.local and not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{a.port}/")).start()

    from registry.api import create_app
    uvicorn.run(create_app(), host=host, port=a.port, log_level="warning", access_log=False)
