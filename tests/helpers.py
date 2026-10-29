"""Общий каркас тестов: сервер реестра подпроцессом на свободном порту, реестр во временной папке.
Окружение очищено от REG_*/GIGACHAT_*/GP_* и задано явно; секреты (админский PIN, логин/пароль GP) — только через stdin.
Процессы останавливаются строго по PID (terminate/kill), без pkill."""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"

# Контракт T-002 (уточняется по ответу разработчика) — держим в одном месте
SESSION_HEADER = "X-Session"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_env(**extra):
    """Окружение без REG_*/GIGACHAT_*/GP_*: тест не зависит от настроек пользователя и не видит registry.env."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REG_", "GIGACHAT_", "GP_"))}
    env.update(GIGACHAT_API_URL="", REG_ENV_FILE="", PYTHONIOENCODING="utf-8")
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    for k in [k for k, v in extra.items() if v is None]:   # None — убрать переменную совсем
        env.pop(k, None)
    return env


class Server:
    """storage: json | sqlite. stdin_lines — строки для канала stdin (админский PIN и т.п.), после записи канал закрывается."""

    def __init__(self, storage="json", args=(), stdin_lines=None, env=None, tmp=None):
        self._own_tmp = tmp is None
        self.tmp = tempfile.TemporaryDirectory() if tmp is None else tmp
        self.dir = Path(self.tmp.name)
        self.port = free_port()
        self.storage = storage
        self.env = clean_env(REG_STORAGE=storage, REG_JSON_DIR=self.dir / "json",
                             REG_DB_PATH=self.dir / "registry.db", **(env or {}))
        self.log = open(self.dir / f"server-{self.port}.log", "w+b")
        self.argv = [sys.executable, str(APP), "--local", "--no-browser", "--port", str(self.port),
                     "--storage", storage, *args]
        self.proc = subprocess.Popen(self.argv, env=self.env, cwd=ROOT,
                                     stdin=subprocess.PIPE if stdin_lines is not None else subprocess.DEVNULL,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        if stdin_lines is not None:
            self.proc.stdin.write("".join(f"{x}\n" for x in stdin_lines).encode("utf-8"))
            self.proc.stdin.close()
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = None
        for _ in range(100):
            if self.proc.poll() is not None:
                break
            try:
                page = urllib.request.urlopen(self.base + "/", timeout=1).read().decode("utf-8")
                self.token = re.search(r"TOKEN = '([^']+)'", page).group(1)
                return
            except Exception:
                time.sleep(0.1)
        out = self.output()
        self.stop()
        raise RuntimeError("сервер не поднялся:\n" + out)

    def output(self):
        self.log.flush()
        return (self.dir / f"server-{self.port}.log").read_text("utf-8", "replace")

    def call(self, method, path, body=None, user="", session=None, token=True, headers=None):
        h = {"Content-Type": "application/json", "X-User": urllib.parse.quote(user)}
        if token:
            h["X-Token"] = self.token
        if session:
            h[SESSION_HEADER] = session
        h.update(headers or {})
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw.decode("utf-8", "replace")}

    def raw(self, method, path, body=None, session=None):
        """Ответ как текст — для поиска хешей/солей в любом месте ответа."""
        h = {"Content-Type": "application/json", "X-Token": self.token}
        if session:
            h[SESSION_HEADER] = session
        req = urllib.request.Request(self.base + path, method=method, headers=h,
                                     data=None if body is None else json.dumps(body).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.log.close()
        if self._own_tmp:
            self.tmp.cleanup()


def run_cli(server, *args, input_text=None, env=None, detach=False, timeout=30):
    """Команда GigaCode (`app.py ctx|tasks|create-task`) против тестового сервера; stdin — не терминал.
    detach=True — новая сессия без управляющего терминала (нет /dev/tty), как у агента в вебе."""
    return subprocess.run([sys.executable, str(APP), *args], env=clean_env(REG_SERVER=server.base, **(env or {})),
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          input=input_text if input_text is not None else "", timeout=timeout,
                          start_new_session=detach)


def frontend_text():
    """Весь фронтенд одним текстом: шаблон + css + js — для статических проверок из времён одного index.html."""
    parts = [(ROOT / "templates" / "index.html").read_text("utf-8")]
    for p in sorted((ROOT / "static").rglob("*")):
        if p.is_file():
            parts.append(p.read_text("utf-8"))
    return "\n".join(parts)


def frontend_css():
    """Все стили фронтенда одним текстом (static/css/*.css в порядке загрузки)."""
    return "\n".join(p.read_text("utf-8") for p in sorted((ROOT / "static" / "css").glob("*.css")))


def backend_text():
    """Весь бэкенд одним текстом: app.py + пакет registry/ — для статических проверок."""
    parts = [(ROOT / "app.py").read_text("utf-8")]
    for p in sorted((ROOT / "registry").rglob("*.py")):
        parts.append(p.read_text("utf-8"))
    return "\n".join(parts)


def employee_side_files():
    """Файлы, которые работают в контейнере сотрудника без fastapi: прокси, команды GigaCode, BB, хранилища."""
    names = ["app.py", "registry/__init__.py", "registry/main.py", "registry/config.py", "registry/util.py",
             "registry/errors.py", "registry/runtime.py", "registry/org.py", "registry/sessions.py",
             "registry/auth.py", "registry/creds.py", "registry/cli.py", "registry/proxy.py",
             "registry/store/__init__.py", "registry/store/json_store.py", "registry/store/sqlite_store.py",
             "registry/store/gp_store.py", "registry/bb/__init__.py", "registry/bb/direct.py", "registry/bb/ontology.py",
             "registry/bb/upload.py", "registry/bb/kb.py"]
    return [ROOT / n for n in names]


def stored_text(directory):
    """Всё содержимое файлов хранилища (json-папка, sqlite) как текст — для поиска PIN открытым текстом."""
    chunks = []
    for p in Path(directory).rglob("*"):
        if p.is_file() and not p.name.startswith("server-"):
            chunks.append(p.read_bytes().decode("utf-8", "replace"))
    return "\n".join(chunks)


def teams_for(people):
    """С T-007 роль тимлида следует из команд: на каждого человека с role 'teamlead' — команда, где он лид
    (название — его ФИО, как при миграции). people[].team = ФИО лида сервер переводит в id команды."""
    return [{"id": f"team-{i}", "name": p["name"], "lead": p["name"], "deputy": ""}
            for i, p in enumerate((p for p in people if p.get("role") == "teamlead"), 1)]
