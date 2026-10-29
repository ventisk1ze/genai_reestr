"""T-010: выгрузка карточек знаний в BB (Bitbucket) напрямую (git + REST, T-013) — из контейнера сотрудника.
Критерии приёмки — docs/backlog.md (T-010); контракт — от тимлида, уточнения — от dev (константы ниже).

Как устроено в тесте:
  сервер реестра — helpers.Server (json и sqlite) с REG_BB_* владельца; в BB не ходит, учётки BB не видит;
      REG_BB_REPO_URL — путь к локальному bare-репозиторию (git ходит на локальные пути без учётки);
  прокси сотрудника — `app.py proxy 127.0.0.1:<порт> --port N --bb-creds-stdin` подпроцессом: логин и пароль BB —
      двумя строками в stdin; свои REG_BB_BASE_URL (адрес фейкового REST), REG_BB_CLONE (постоянный клон),
      git-идентичность через GIT_AUTHOR_*/GIT_COMMITTER_*;
  REST Bitbucket — tests/fake_bb_rest.py (stdlib): pull request с Basic-авторизацией, ветки проверяет в bare,
      журнал запросов с хешем учётки;
  «злой» сервер реестра — FakeRegistry (http.server в тесте): проверки стороны сотрудника.
Хост Bitbucket — 127.0.0.1 (фейковый REST; настоящий Bitbucket не трогаем). Процессы — строго по PID.
Все данные выдуманные (правило 3 CLAUDE.md)."""
import ast
import base64
import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import APP, ROOT, Server, backend_text, clean_env, employee_side_files, free_port, frontend_css, frontend_text, teams_for  # noqa: E402
from fake_bb_rest import FakeBitbucket  # noqa: E402

# ---------------------------------------------------------------- контракт (правится только здесь)
API_STATUS, API_TASK, API_DONE, API_UPLOAD = "/api/bb/status", "/api/bb/task", "/api/bb/done", "/api/bb/upload"
CREDS_FLAG = "--bb-creds-stdin"
SRV_KEYS = ("REG_BB_REPO_URL", "REG_BB_PROJECT", "REG_BB_REPO", "REG_BB_BRANCH", "REG_BB_DIR")
EMP_KEYS = ("REG_BB_BASE_URL", "REG_BB_TIMEOUT", "REG_BB_CLONE")
DEF_BRANCH, DEF_DIR = "master", "KB"
BUTTON = "Выгрузить карточки в BB"
HIST_DONE = "Карточки выгружены в BB"
NO_CHANGES = re.compile(r"без изменений|изменений нет", re.I)
VIA_PROXY = re.compile(r"через (свой )?прокси", re.I)
RU = "[А-Яа-яЁё]"
APPROVED, WORK, DONE = "Согласована", "В работе", "Завершена"

# ---------------------------------------------------------------- выдуманные люди, задача, учётка BB
ADMIN_ARGS = ["--admin-pin-stdin"]
ADMIN_PIN = "73920518"
LEAD = "Выгрузкин Борис Тестович"
LEAD2 = "Соседов Аркадий Тестович"
EXE = "Карточкина Вера Тестовна"
SUB = "Заменин Игорь Тестович"
OUT = "Посторонний Глеб Тестович"
PINS = {LEAD: "48151623", LEAD2: "56473829", EXE: "4242", SUB: "73737", OUT: "1029"}
PEOPLE = [
    {"name": LEAD, "email": "vygruzkin@example.test", "role": "teamlead", "active": True},
    {"name": LEAD2, "email": "sosedov@example.test", "role": "teamlead", "active": True},
    {"name": EXE, "email": "kartochkina@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": SUB, "email": "zamenin@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": OUT, "email": "postoronniy@example.test", "role": "employee", "team": LEAD2, "active": True},
]
POSTANOVKA = {"title": "Выдуманная задача T-010: обзор выгрузки", "description": "Подготовить выдуманный обзор",
              "teamlead": LEAD, "executor": EXE, "substitute": SUB, "deadline": "2030-12-31"}
RESULT = {"result": "Обзор", "result_desc": "Выдуманный обзор готов"}
DECISION = {"block": "KM-01", "level": "ЦК", "bz_save": "да", "bz_reason": "Пригодится команде"}
CARDS = [  # первая — кириллица и знаки в вопросе (имя файла реестр чистит, заголовок в файле — как есть)
    ("Как выгрузить «карточку» в BB: шаг 1/2?",
     "Выдуманный ответ: нажать кнопку «Выгрузить».\nВторая строка — «кавычки» & <теги>, 100% выдумка."),
    ("Plain question about the knowledge base", "Fictional answer, line one.\nLine two."),
]
BB_BASE = "https://bb.example.test"   # выдуманный хост для ссылок в /api/bb/done (синтакис http(s))
REST_BASE = None   # адрес фейкового REST — определяется в каркасе (KbBase/BBBase)
PROJECT, REPO = "KBPROJ", "kb-repo"
BB_LOGIN = "t010-bb-login-" + secrets.token_hex(3)
BB_PASS = "Vy#dum@n:Pa55%/" + secrets.token_hex(6)       # спецсимволы URL — для проверки маскировки
BB_PASS_RU = "Пароль#Т010@" + secrets.token_hex(6)       # кириллица — канал stdin -> прокси -> лаунчер без потерь
SRV_ENV = {"REG_BB_PROJECT": PROJECT, "REG_BB_REPO": REPO}   # URL добавляет каркас (путь к bare)

HTML = frontend_text()
APP_SRC = backend_text()
CONNECT = (ROOT / "connect.sh").read_text("utf-8")
README = (ROOT / "README.md").read_text("utf-8")
ENV_EXAMPLE = (ROOT / "registry.env.example").read_text("utf-8")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sha(v):
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def secret_forms(v):
    """Как секрет может проступить в тексте: как есть, в URL (quote), в JSON с \\u-экранированием."""
    return {v, quote(v), quote(v, safe=""), json.dumps(v)[1:-1]}


# ---------------------------------------------------------------- процессы (строго по PID)
def proc_raw(pid):
    """Исходные argv и окружение процесса глазами ОС (аналог /proc/<pid>/environ) байтами; None — недоступно."""
    try:
        if sys.platform.startswith("linux"):
            return Path(f"/proc/{pid}/cmdline").read_bytes() + b"\0" + Path(f"/proc/{pid}/environ").read_bytes()
        if sys.platform == "darwin":
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            mib = (ctypes.c_int * 3)(1, 49, pid)   # CTL_KERN, KERN_PROCARGS2
            n = ctypes.c_size_t(0)
            if libc.sysctl(mib, 3, None, ctypes.byref(n), None, 0) != 0:
                return None
            buf = ctypes.create_string_buffer(n.value)
            if libc.sysctl(mib, 3, buf, ctypes.byref(n), None, 0) != 0:
                return None
            return buf.raw[:n.value]
    except Exception:   # noqa
        return None
    return None


def ps_field(pid, field):
    return subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    st = ps_field(pid, "stat")
    return bool(st) and not st.startswith("Z")   # зомби — уже не работает


def wait_dead(pid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


def stop_proc(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


def files_with(dirs, needles):
    """Файлы в папках, где встречается хотя бы одна строка из needles (в имени или содержимом)."""
    found = []
    nb = [n.encode("utf-8") for n in needles if n]
    for d in dirs:
        for p in Path(d).rglob("*"):
            if p.is_file():
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                if any(x in data or x in str(p).encode("utf-8", "replace") for x in nb):
                    found.append(str(p))
    return found


# ---------------------------------------------------------------- git: bare-репозиторий БЗ
def git_env(work):
    gcfg = Path(work) / "gitconfig-empty"
    gcfg.touch()
    return {"GIT_AUTHOR_NAME": "Тестовый Автор", "GIT_AUTHOR_EMAIL": "author@example.test",
            "GIT_COMMITTER_NAME": "Тестовый Автор", "GIT_COMMITTER_EMAIL": "author@example.test",
            "GIT_CONFIG_GLOBAL": str(gcfg), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


class BareRepo:
    """Локальный «репозиторий БЗ»: ветки, в каждой README.md и чужая карточка KB/Чужая карточка.md;
    extra(seed) — своё содержимое (например, символические ссылки) до первого коммита."""

    def __init__(self, work, branches=(DEF_BRANCH,), extra=None, name="kb-repo.git"):
        self.env = git_env(work)
        self.path = Path(work) / name
        seed = Path(work) / f"seed-{name}"
        self.git("init", "--quiet", str(seed))
        self.git("symbolic-ref", "HEAD", f"refs/heads/{branches[0]}", cwd=seed)
        (seed / "README.md").write_text("# Выдуманная база знаний\n", "utf-8")
        if extra:
            extra(seed)
        else:
            (seed / "KB").mkdir()
            (seed / "KB" / "Чужая карточка.md").write_text("# Чужой вопрос?\n\nЧужой выдуманный ответ\n", "utf-8")
        self.git("add", "-A", cwd=seed)
        self.git("commit", "--quiet", "-m", "Начало выдуманной БЗ", cwd=seed)
        for b in branches[1:]:
            self.git("branch", b, cwd=seed)
        self.git("init", "--bare", "--quiet", str(self.path))
        self.git("--git-dir", str(self.path), "symbolic-ref", "HEAD", f"refs/heads/{branches[0]}")
        self.git("push", "--quiet", str(self.path), *[f"{b}:{b}" for b in branches], cwd=seed)
        shutil.rmtree(seed)

    def git(self, *args, cwd=None):
        p = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, env={**os.environ, **self.env},
                           capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL)
        if p.returncode:
            raise RuntimeError(f"git {args}: {p.stderr}")
        return p.stdout

    def bgit(self, *args):
        return self.git("--git-dir", str(self.path), *args)

    def branches(self):
        return set(self.bgit("for-each-ref", "--format=%(refname:short)", "refs/heads").split())

    def changed(self, base, branch):
        return set(self.bgit("diff", "--name-only", base, branch).splitlines())

    def show(self, branch, path):
        return self.bgit("show", f"{branch}:{path}")

    def merge(self, branch, into):
        """«Pull request влит»: целевая ветка сдвигается на ветку выгрузки."""
        self.bgit("update-ref", f"refs/heads/{into}", f"refs/heads/{branch}")


# ---------------------------------------------------------------- прокси сотрудника (app.py proxy)
class Proxy:
    def __init__(self, target, work, token, env=None, creds=True, login=BB_LOGIN, password=BB_PASS, stdin_text=None,
                 extra_args=()):
        self.port = free_port()
        self.tmp = Path(work) / f"proxy-{self.port}-tmp"
        self.cwd = Path(work) / f"proxy-{self.port}-cwd"
        self.tmp.mkdir()
        self.cwd.mkdir()
        self.log_path = Path(work) / f"proxy-{self.port}.log"
        self._log = open(self.log_path, "w+b")
        self.argv = [sys.executable, str(APP), "proxy", target, "--port", str(self.port),
                     *([CREDS_FLAG] if creds else []), *extra_args]
        self.env = clean_env(TMPDIR=str(self.tmp), BITBUCKET_USERNAME=None, BITBUCKET_PASSWORD=None,
                             BITBUCKET_BASE_URL=None, **(env or {}))
        self.proc = subprocess.Popen(self.argv, env=self.env, cwd=self.cwd,
                                     stdin=subprocess.PIPE if creds else subprocess.DEVNULL,
                                     stdout=self._log, stderr=subprocess.STDOUT)
        if creds:
            try:
                self.proc.stdin.write((stdin_text if stdin_text is not None else f"{login}\n{password}\n").encode("utf-8"))
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
        self.base, self.token, self.responses = f"http://127.0.0.1:{self.port}", token, []
        for _ in range(100):
            if self.proc.poll() is not None:
                break
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        out = self.output()
        self.stop()
        raise RuntimeError("прокси не поднялся:\n" + out)

    def call(self, method, path, body=None, session=None, token=True, timeout=90, headers=None):
        h = {"Content-Type": "application/json"}
        if token and self.token:
            h["X-Token"] = self.token
        if session:
            h["X-Session"] = session
        h.update(headers or {})
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=h)
        try:
            with NO_PROXY.open(req, timeout=timeout) as r:
                code, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        text = raw.decode("utf-8", "replace")
        self.responses.append(text)
        try:
            return code, (json.loads(text) if text else {})
        except ValueError:
            return code, {"raw": text}

    def output(self):
        if not self._log.closed:
            self._log.flush()
        return self.log_path.read_text("utf-8", "replace")

    def stop(self):
        stop_proc(self.proc)
        if not self._log.closed:
            self._log.close()


# ---------------------------------------------------------------- «злой» сервер реестра (http.server в тесте)
class FakeRegistry:
    """Отдаёт /api/bb/status и /api/bb/task как скажет тест, записывает все запросы прокси (заголовки, тела)."""
    TOKEN = "fake-registry-token-t010"

    def __init__(self):
        self.reqs, self.task_code, self.task_body, self.done_code, self.status_code = [], 200, None, 200, 200
        self.status_body = {"available": False, "server_configured": True, "reason": "Выгрузка — через свой прокси"}
        reg = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj, ctype="application/json; charset=utf-8"):
                data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _rec(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                reg.reqs.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()),
                                 "body": body})
                return body

            def do_GET(self):
                self._rec()
                p = urlparse(self.path).path
                if p in ("/", "/index.html"):
                    return self._reply(200, f"<script>const TOKEN = '{reg.TOKEN}';</script>".encode(), "text/html")
                if p == API_STATUS:
                    return self._reply(reg.status_code, reg.status_body if reg.status_code == 200 else {"error": "not found"})
                if p == API_TASK:
                    return self._reply(reg.task_code, reg.task_body)
                return self._reply(404, {"error": "not found"})

            def do_POST(self):
                body = self._rec()
                if urlparse(self.path).path == API_DONE:
                    if reg.done_code != 200:
                        return self._reply(reg.done_code, {"error": "Выдуманный сбой сервера реестра"})
                    d = json.loads(body or b"{}")
                    return self._reply(200, {"task": {"kind": d.get("kind"), "id": d.get("id"), "title": "Выдумка",
                                                      "bb": [d], "_v": 2}})
                return self._reply(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def done_reqs(self, since=0):
        return [r for r in self.reqs[since:] if r["method"] == "POST" and urlparse(r["path"]).path == API_DONE]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------- база: сервер + прокси + REST-фейк + bare
class BBBase(unittest.TestCase):
    storage = "json"
    srv_env = SRV_ENV
    proxy_env = {}
    target_branch, bb_dir = DEF_BRANCH, DEF_DIR
    branches = (DEF_BRANCH,)
    login, password = BB_LOGIN, BB_PASS
    timeout = "60"

    @classmethod
    def setUpClass(cls):
        cls._work = tempfile.TemporaryDirectory()
        cls.work = Path(cls._work.name).resolve()
        cls.s, cls.proxy, cls.extra_proxies = None, None, []
        try:
            cls.bare = BareRepo(cls.work, cls.branches)
            cls.rest = FakeBitbucket(cls.work / "rest", {f"{PROJECT}/{REPO}": cls.bare.path},
                                     cls.login, cls.password)
            cls.s = Server(cls.storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env=cls.server_env())
            cls.proxy = cls.start_proxy()
            cls.sid, cls.admin_name = cls.setup_people()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for p in [getattr(cls, "proxy", None), *getattr(cls, "extra_proxies", [])]:
            if p is not None:
                p.stop()
        if getattr(cls, "s", None) is not None:
            cls.s.stop()
        if getattr(cls, "rest", None) is not None:
            cls.rest.stop()
        cls._work.cleanup()

    @classmethod
    def server_env(cls):
        return {"REG_BB_REPO_URL": str(cls.bare.path), **cls.srv_env}   # локальный bare; srv_env может переопределить

    @classmethod
    def proxy_settings(cls, **extra):
        return {"REG_BB_BASE_URL": cls.rest.base, "REG_BB_TIMEOUT": cls.timeout,
                "REG_BB_CLONE": str(cls.work / "bb-clone"), **git_env(cls.work),
                **cls.proxy_env, **extra}

    @classmethod
    def start_proxy(cls, target=None, env=None, extra=True, **kw):
        p = Proxy(target or f"127.0.0.1:{cls.s.port}", cls.work, cls.s.token,
                  env=cls.proxy_settings(**(env or {})) if extra else env,
                  **{"login": cls.login, "password": cls.password, **kw})
        if getattr(cls, "proxy", None) is not None:
            cls.extra_proxies.append(p)
        return p

    @classmethod
    def setup_people(cls):
        """Админ (напрямую на сервере) заводит людей и PIN; все входят через прокси сотрудника."""
        s = cls.s
        code, r = s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
        assert code == 200, r
        adm = r["session"]
        cfg = s.call("GET", "/api/state")[1]["config"]
        cfg["people"] = [dict(p) for p in PEOPLE]
        cfg["teams"] = teams_for(PEOPLE)
        code, r = s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=adm)
        assert code == 200, r
        for name, pin in PINS.items():
            code, r = s.call("POST", "/api/admin/pin", {"name": name, "pin": pin}, session=adm)
            assert code == 200, (name, r)
        code, r = cls.proxy.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
        assert code == 200, r
        sid, admin_name = {"admin": r["session"]}, r["user"]["name"]
        for name, pin in PINS.items():
            code, r = cls.proxy.call("POST", "/api/login", {"name": name, "pin": pin})
            assert code == 200, (name, r)
            sid[name] = r["session"]
        return sid, admin_name

    # ---- задачи (через сервер владельца, сессии — те же, что у браузера через прокси)
    def post_task(self, who, task, code, params=None, expect_v="cur"):
        body = {"task": task, "action_code": code, "action": "Тест T-010"}
        if params:
            body["params"] = params
        if expect_v == "cur":
            expect_v = task.get("_v")
        if expect_v is not None:
            body["expect_v"] = expect_v
        return self.s.call("POST", "/api/task", body, session=self.sid[who])

    def get(self, t):
        tasks = self.s.call("GET", "/api/state")[1]["tasks"]
        return next(x for x in tasks if x["kind"] == t["kind"] and x["id"] == t["id"])

    def act(self, who, t, code, params=None, **changes):
        c, r = self.post_task(who, {**self.get(t), **changes}, code, params)
        self.assertEqual(c, 200, (code, who, r))
        return r["task"]

    def add_card(self, who, t, title, body, path=""):
        c, r = self.s.call("POST", "/api/card", {"kind": t["kind"], "id": t["id"], "title": title, "body": body,
                                                 "path": path}, session=self.sid[who])
        self.assertEqual(c, 200, r)
        return {"path": r["path"], "title": title, "body": body}

    def task_in(self, status=APPROVED, kind="once", cards=CARDS, who=EXE):
        """Задача в нужном статусе (bz_save = да); cards — карточки исполнителя в «Согласована»."""
        task = {"kind": kind, "id": None, **POSTANOVKA}
        if kind == "regular":
            task["regularity"] = "еженедельно"
        c, r = self.post_task(LEAD, task, "create", expect_v=None)
        self.assertEqual(c, 200, r)
        t = self.act(EXE, r["task"], "take")
        if status == WORK:
            return self.get(t), []
        t = self.act(EXE, t, "to_review", **RESULT)
        t = self.act(LEAD, t, "approve", **DECISION)
        self.assertEqual(t["status"], APPROVED)
        added = [self.add_card(who, t, title, body) for title, body in cards]
        if status == DONE:
            t = self.act(EXE, t, "finish")
        return self.get(t), added

    def expected(self, cards):
        """Файлы в BB: <папка>/<имя файла карточки в реестре>, содержимое — «# вопрос» + ответ (формат реестра)."""
        return {f"{self.bb_dir}/{Path(c['path']).name}": f"# {c['title'].strip()}\n\n{c['body'].strip()}\n" for c in cards}

    @staticmethod
    def snap(t):
        return {k: t.get(k) for k in ("_v", "status", "bb", "bz_card", "history", "cards")}

    def admin_server(self, env=None):
        """Ещё один сервер без людей: админ ставит задачу сразу в «Согласована» (bz_save = да) и добавляет карточку."""
        srv = Server("json", args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env=env or self.server_env())
        self.addCleanup(srv.stop)
        code, a = srv.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
        self.assertEqual(code, 200, a)
        adm = a["session"]
        code, r = srv.call("POST", "/api/task", {"task": {"kind": "once", "id": None, **POSTANOVKA, **RESULT, **DECISION,
                                                          "status": APPROVED}, "action_code": "create"}, session=adm)
        self.assertEqual(code, 200, r)
        t = r["task"]
        self.assertEqual(t["status"], APPROVED, "админ не поставил задачу сразу в «Согласована»")
        code, r = srv.call("POST", "/api/card", {"kind": "once", "id": t["id"], "title": "Карточка админа?",
                                                 "body": "Выдуманный ответ админа"}, session=adm)
        self.assertEqual(code, 200, r)
        return srv, adm, t

    # ---- выгрузка через прокси
    def upload(self, t, who=EXE, proxy=None, timeout=90, body=None):
        p = proxy or self.proxy
        code, r = p.call("POST", API_UPLOAD, body if body is not None else {"kind": t["kind"], "id": t["id"]},
                         session=self.sid.get(who) if who else None, timeout=timeout)
        self.no_secrets(p.responses[-1], "ответ /api/bb/upload")
        return code, r

    def ok_upload(self, t, who=EXE, proxy=None):
        code, r = self.upload(t, who, proxy)
        self.assertEqual(code, 200, r)
        for k in ("pr_url", "pr_id", "branch", "files", "task"):
            self.assertIn(k, r)
        return r

    def refused_upload(self, t, codes, who=EXE, proxy=None, body=None):
        """Отказ: нужный код, текст по-русски, в Bitbucket не ходили (REST-журнал пуст), задача не изменилась."""
        before, m = self.snap(self.get(t)), self.rest.mark()
        code, r = self.upload(t, who, proxy, body=body)
        self.assertIn(code, codes, r)
        self.assertRegex(r.get("error", ""), RU, r)
        self.assertEqual(self.rest.since(m), [], "REST Bitbucket получил запрос, хотя выгрузка отклонена")
        self.assertEqual(self.snap(self.get(t)), before, "при отказе в задаче ничего не должно меняться")
        return r

    def no_secrets(self, text, where):
        for v in (self.password, self.login):
            for f in secret_forms(v):
                self.assertNotIn(f, text, f"учётка BB в {where}")

    def no_secret_bytes(self, data, where):
        for v in (self.password, self.login):
            for f in secret_forms(v):
                self.assertNotIn(f.encode("utf-8"), data, f"учётка BB в {where}")

    def assert_clone_kept(self):
        """Постоянный клон живёт в REG_BB_CLONE между выгрузками (кэш, не удаляется)."""
        self.assertTrue((Path(self.work) / "bb-clone" / ".git").is_dir(), "постоянный клон не создан")

    @staticmethod
    def next_second():
        """Ветка — kb/<вид>-<id>-<ГГГГММДД-ЧЧММСС>: следующая выгрузка — в другую секунду."""
        time.sleep(1.05 - time.time() % 1)


# ================================================================ критерии 1–4: сквозной путь через прокси
class UploadFlowTest(BBBase):
    """Хранилище json; целевая ветка и папка — по умолчанию (master, KB)."""

    def test_status_via_proxy_available(self):
        """Кр. 2, 6: прокси с учёткой, сервер настроен — выгрузка доступна, ответ — от прокси."""
        code, r = self.proxy.call("GET", API_STATUS, session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("available"), True, r)
        self.assertEqual(r.get("via"), "proxy", r)
        self.no_secrets(self.proxy.responses[-1], "/api/bb/status")

    def test_upload_creates_branch_files_and_pr(self):
        """Кр. 1, 2: новая ветка kb/once-<id>-<время> с карточками в KB/, pull request в целевую ветку
        через REST (Basic-учётка, правильные проект/ветки), ссылка на PR — в задаче (bb, история, bz_card)."""
        t, cards = self.task_in()
        m = self.rest.mark()
        r = self.ok_upload(t)
        recs = self.rest.since(m)
        self.assertEqual(len(recs), 1, "один REST-запрос pull request на выгрузку")
        br = r["branch"]
        self.assertRegex(br, rf"^kb/once-{t['id']}-\d{{8}}-\d{{6}}$")
        pr = recs[0]["payload"]
        self.assertEqual((pr["fromRef"]["id"], pr["toRef"]["id"]),
                         (f"refs/heads/{br}", f"refs/heads/{self.target_branch}"))
        self.assertTrue((pr.get("title") or "").strip(), "у pull request нет заголовка")
        self.assertTrue(recs[0]["auth_ok"], "REST получил не ту учётку")
        self.assertIn("/rest/api/1.0/projects/", recs[0]["path"])
        self.assertIn(f"/repos/{REPO}/", recs[0]["path"])
        # репозиторий: ветка, файлы карточек только в папке БЗ, содержимое — в формате реестра
        want = self.expected(cards)
        self.assertIn(br, self.bare.branches())
        self.assertEqual(self.bare.changed(self.target_branch, br), set(want), "в ветке изменено не только своё")
        for path, content in want.items():
            self.assertEqual(self.bare.show(br, path), content, path)
        self.assertEqual(self.bare.show(br, f"{DEF_DIR}/Чужая карточка.md"),
                         "# Чужой вопрос?\n\nЧужой выдуманный ответ\n", "чужая карточка в BB изменилась")
        self.assert_clone_kept()
        # ответ браузеру
        self.assertTrue(r["pr_url"].startswith(self.rest.base + "/"), r)
        self.assertTrue(r["pr_url"].endswith(f"/pull-requests/{r['pr_id']}"), r)
        self.assertEqual(sorted(Path(f).name for f in r["files"]), sorted(Path(f).name for f in want))
        # задача на сервере
        after = self.get(t)
        self.assertEqual(after["status"], APPROVED)
        self.assertEqual(len(after.get("bb") or []), 1, after.get("bb"))
        e = after["bb"][0]
        self.assertEqual((e.get("by"), e.get("pr_url"), str(e.get("pr_id")), e.get("branch")),
                         (EXE, r["pr_url"], str(r["pr_id"]), br))
        self.assertTrue(e.get("ts"))
        self.assertEqual(sorted(Path(f).name for f in e.get("files") or []), sorted(Path(f).name for f in want))
        self.assertIn(r["pr_url"], after["bz_card"], "пустой «Факт размещения карточки» должен заполниться ссылкой")
        h = after["history"][-1]
        self.assertIn(HIST_DONE, h["action"])
        self.assertIn(str(r["pr_id"]), h["action"])
        self.assertEqual(h["by"], EXE)
        self.assertEqual(r["task"].get("_v"), after["_v"], "в ответе — задача как сохранена")
        self.assertFalse((ROOT / DEF_DIR).exists(), "файлы карточек записаны в папку проекта")

    def test_repeat_upload_new_branch_and_pr(self):
        """Кр. 1: повторная выгрузка (первый PR ещё не влит) — новая ветка и новый PR, ссылка дописывается."""
        t, _cards = self.task_in()
        r1 = self.ok_upload(t)
        self.next_second()
        r2 = self.ok_upload(t)
        self.assertNotEqual(r1["branch"], r2["branch"])
        self.assertNotEqual(str(r1["pr_id"]), str(r2["pr_id"]))
        after = self.get(t)
        self.assertEqual([e["pr_url"] for e in after["bb"]], [r1["pr_url"], r2["pr_url"]])
        self.assertIn(r1["pr_url"], after["bz_card"])
        self.assertIn(r2["pr_url"], after["bz_card"])
        self.assertLess(after["bz_card"].index(r1["pr_url"]), after["bz_card"].index(r2["pr_url"]), "ссылка не дописана в конец")
        self.assertEqual(sum(HIST_DONE in h["action"] for h in after["history"]), 2)

    def test_no_changes_then_edited_card(self):
        """Кр. 1: в BB карточки уже такие же — 400 «без изменений», commit/push/PR не вызываются, задача не меняется;
        после правки карточки — новая ветка и PR, bz_card дописан, а не перезаписан."""
        t, cards = self.task_in()
        r1 = self.ok_upload(t)
        self.bare.merge(r1["branch"], self.target_branch)
        self.next_second()
        before, m = self.snap(self.get(t)), self.rest.mark()
        code, r = self.upload(t)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), NO_CHANGES)
        self.assertEqual(self.rest.since(m), [], "«без изменений» — pull request не создаётся")
        self.assertEqual(self.snap(self.get(t)), before, "«без изменений» — в задаче ничего не пишется")
        edited = self.add_card(EXE, t, cards[0]["title"], "Исправленный выдуманный ответ.", path=cards[0]["path"])
        self.assertEqual(edited["path"], cards[0]["path"])
        self.next_second()
        r3 = self.ok_upload(t)
        self.assertNotEqual(r3["branch"], r1["branch"])
        want = self.expected([edited])
        self.assertEqual(self.bare.changed(self.target_branch, r3["branch"]), set(want), "в PR — только изменённая карточка")
        self.assertEqual(self.bare.show(r3["branch"], next(iter(want))), next(iter(want.values())))
        after = self.get(t)
        self.assertIn(r1["pr_url"], after["bz_card"])
        self.assertIn(r3["pr_url"], after["bz_card"])
        self.assertEqual(len(after["bb"]), 2)

    def test_bz_card_text_kept(self):
        """Кр. 1: «Факт размещения карточки» уже заполнен — ссылка дописывается, прежний текст остаётся."""
        t, _ = self.task_in()
        old = "Размещена в АС Ретривер: выдуманная ссылка"
        self.act(EXE, t, "edit", bz_card=old)
        r = self.ok_upload(t)
        bz = self.get(t)["bz_card"]
        self.assertTrue(bz.startswith(old), bz)
        self.assertIn(r["pr_url"], bz)

    def test_substitute_and_admin_upload(self):
        """Кр. 1, 5: выгружают и замещающий, и админ; в списке выгрузок — кто выгружал."""
        t, _ = self.task_in()
        r1 = self.ok_upload(t, who=SUB)
        self.next_second()
        r2 = self.ok_upload(t, who="admin")
        bb = self.get(t)["bb"]
        self.assertEqual([(e["by"], e["pr_url"]) for e in bb], [(SUB, r1["pr_url"]), (self.admin_name, r2["pr_url"])])

    def test_regular_task_upload(self):
        """Кр. 1: регулярная задача — ветка kb/regular-<id>-…, файл карточки «Рег<id>_…» в папке БЗ."""
        t, cards = self.task_in(kind="regular", cards=CARDS[:1])
        r = self.ok_upload(t)
        self.assertRegex(r["branch"], rf"^kb/regular-{t['id']}-\d{{8}}-\d{{6}}$")
        want = self.expected(cards)
        self.assertTrue(all(Path(p).name.startswith(f"Рег{t['id']}_") for p in want), want)
        self.assertEqual(self.bare.changed(self.target_branch, r["branch"]), set(want))

    def test_regular_new_cycle_keeps_uploads_in_cycle(self):
        """Регулярная задача: после «Принята» новый цикл начинается без выгрузок, прошлые — в истории цикла."""
        t, _ = self.task_in(kind="regular", cards=CARDS[:1])
        r = self.ok_upload(t)
        t = self.act(EXE, t, "finish")
        t = self.act(LEAD, t, "accept")
        t = self.act(LEAD, t, "new_cycle", params={"deadline": "2031-06-30"})
        self.assertEqual(t.get("bb") or [], [], "у нового цикла не должно быть чужих выгрузок")
        self.assertEqual([e["pr_url"] for e in t["cycles"][-1].get("bb") or []], [r["pr_url"]])

    def test_credentials_reach_rest_only_in_memory(self):
        """Кр. 3: REST получил правильную учётку (Basic) — и она нигде больше: ни в аргументах/окружении
        процессов, ни в файлах (askpass-помощник без секретов, канал — в памяти)."""
        t, _ = self.task_in(cards=CARDS[:1])
        m = self.rest.mark()
        self.ok_upload(t)
        recs = self.rest.since(m)
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["auth_ok"], "REST-запрос не с той учёткой")
        self.no_secret_bytes(json.dumps(recs[0]).encode("utf-8"), "журнале REST-фейка")
        raw = proc_raw(self.proxy.proc.pid)
        if raw is not None:
            self.no_secret_bytes(raw, "аргументах/окружении прокси")
        self.assertEqual(files_with([self.work, self.proxy.tmp, self.proxy.cwd], [self.password]), [],
                         "файлы с паролем BB")

    def test_proxy_process_has_no_credentials(self):
        """Кр. 3: учётки нет в аргументах и исходном окружении процесса прокси (только stdin)."""
        raw = proc_raw(self.proxy.proc.pid)
        if raw is None:
            self.skipTest("исходное окружение процесса недоступно на этой ОС — проверить на DataLab")
        self.no_secret_bytes(raw, "аргументах/окружении прокси")
        self.no_secrets(ps_field(self.proxy.proc.pid, "command"), "ps -o command прокси")

    def test_owner_side_never_sees_credentials(self):
        """Кр. 3: после выгрузки и отказа учётки нет на сервере владельца — ни в журнале, ни в хранилище, ни в /api/state;
        в журнале прокси и в его временных файлах — тоже нет."""
        t, _ = self.task_in(cards=CARDS[:1])
        self.ok_upload(t)
        self.rest.set_fail("500")
        try:
            self.next_second()
            code, r = self.upload(t)
            self.assertEqual(code, 400, r)
        finally:
            self.rest.set_fail("")
        self.no_secrets(self.s.output(), "журнале сервера")
        for path, data in ((p, p.read_bytes()) for p in self.s.dir.rglob("*") if p.is_file()):
            self.no_secret_bytes(data, f"файле сервера {path.name}")
        self.no_secrets(json.dumps(self.s.call("GET", "/api/state")[1], ensure_ascii=False), "/api/state")
        self.no_secrets(self.proxy.output(), "журнале прокси")
        self.assertEqual(files_with([self.work, self.proxy.tmp, self.proxy.cwd], [self.password]), [],
                         "файлы с паролем BB")


class SqliteUploadFlowTest(UploadFlowTest):
    """Хранилище sqlite; своя целевая ветка (REG_BB_BRANCH) и папка (REG_BB_DIR); пароль с кириллицей."""
    storage = "sqlite"
    target_branch, bb_dir = "develop", "kb-cards"
    branches = (DEF_BRANCH, "develop")
    srv_env = {**SRV_ENV, "REG_BB_BRANCH": "develop", "REG_BB_DIR": "kb-cards"}
    password = BB_PASS_RU


# ================================================================ критерий 5: права проверяет сервер
class RightsTest(BBBase):
    def test_task_endpoint_shape(self):
        """GET /api/bb/task: задача, карточки (file, title, body), куда выгружать (по умолчанию master и KB)."""
        t, cards = self.task_in()
        code, r = self.s.call("GET", f"{API_TASK}?kind={t['kind']}&id={t['id']}", session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        self.assertEqual((r["task"]["kind"], r["task"]["id"], r["task"]["title"]), (t["kind"], t["id"], t["title"]))
        self.assertEqual(sorted(c["file"] for c in r["cards"]), sorted(Path(c["path"]).name for c in cards))
        got = {c["file"]: (c["title"], c["body"]) for c in r["cards"]}
        for c in cards:
            self.assertEqual(got[Path(c["path"]).name], (c["title"].strip(), c["body"].strip()))
        want = {"repo_url": str(self.bare.path), "project": PROJECT, "repo": REPO,
                "branch": DEF_BRANCH, "dir": DEF_DIR}
        self.assertEqual({k: r["target"].get(k) for k in want}, want)
        self.no_secrets(json.dumps(r, ensure_ascii=False), "/api/bb/task")

    def test_task_endpoint_needs_session(self):
        t, _ = self.task_in(cards=CARDS[:1])
        code, r = self.s.call("GET", f"{API_TASK}?kind={t['kind']}&id={t['id']}")
        self.assertEqual(code, 401, r)

    def test_task_endpoint_bad_params(self):
        for q in ("kind=evil&id=1", "kind=once&id=abc", "kind=once", "id=1", "kind=once&id=99999"):
            with self.subTest(q=q):
                code, r = self.s.call("GET", f"{API_TASK}?{q}", session=self.sid[EXE])
                self.assertIn(code, (400, 404), r)
                self.assertRegex(r.get("error", ""), RU)

    def test_task_endpoint_status_and_cards(self):
        """Не «Согласована» — 403/400; без карточек — 400 про карточки."""
        w, _ = self.task_in(status=WORK)
        self.add_card("admin", w, "Карточка админа в работе?", "Выдуманный ответ")
        code, r = self.s.call("GET", f"{API_TASK}?kind=once&id={w['id']}", session=self.sid[EXE])
        self.assertIn(code, (400, 403), r)
        e, _ = self.task_in(cards=[])
        code, r = self.s.call("GET", f"{API_TASK}?kind=once&id={e['id']}", session=self.sid[EXE])
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), r"(?i)карточ")

    def test_task_endpoint_rights(self):
        """Исполнитель, замещающий, админ — да; тимлид задачи, чужой тимлид, посторонний — 403 по-русски."""
        t, _ = self.task_in(cards=CARDS[:1])
        for who in (EXE, SUB, "admin"):
            with self.subTest(who=who):
                code, r = self.s.call("GET", f"{API_TASK}?kind={t['kind']}&id={t['id']}", session=self.sid[who])
                self.assertEqual(code, 200, r)
        for who in (LEAD, LEAD2, OUT):
            with self.subTest(who=who):
                code, r = self.s.call("GET", f"{API_TASK}?kind={t['kind']}&id={t['id']}", session=self.sid[who])
                self.assertEqual(code, 403, r)
                self.assertRegex(r.get("error", ""), RU)

    def test_upload_without_session_401(self):
        t, _ = self.task_in(cards=CARDS[:1])
        self.refused_upload(t, (401,), who=None)

    def test_upload_not_executor_403(self):
        """Посторонний сотрудник, тимлид задачи (не исполнитель), чужой тимлид — 403, в BB не ходили."""
        t, _ = self.task_in(cards=CARDS[:1])
        for who in (OUT, LEAD, LEAD2):
            with self.subTest(who=who):
                self.refused_upload(t, (403,), who=who)

    def test_upload_not_approved(self):
        """Не «Согласована» («В работе» с карточкой админа, «Завершена») — 403/400, в BB не ходили."""
        t, _ = self.task_in(status=WORK)
        self.add_card("admin", t, "Карточка админа в работе?", "Выдуманный ответ админа")
        self.refused_upload(t, (400, 403))
        t, _ = self.task_in(status=DONE, cards=CARDS[:1])
        self.assertEqual(t["status"], DONE)
        self.refused_upload(t, (400, 403))

    def test_upload_without_cards_400(self):
        t, _ = self.task_in(cards=[])
        r = self.refused_upload(t, (400,))
        self.assertRegex(r["error"], r"(?i)карточ")

    def test_upload_unknown_task(self):
        t, _ = self.task_in(cards=CARDS[:1])
        for body in ({"kind": "once", "id": 99999}, {"kind": "evil", "id": t["id"]}, {"kind": "once", "id": "1/../2"}):
            with self.subTest(body=body):
                m = self.rest.mark()
                code, r = self.upload(t, body=body)
                self.assertIn(code, (400, 403, 404), r)
                self.assertRegex(r.get("error", ""), RU)
                self.assertEqual(self.rest.since(m), [])

    # ---- POST /api/bb/done напрямую на сервер
    def done(self, t, who=EXE, **over):
        body = {"kind": t["kind"], "id": t["id"], "pr_url": f"{BB_BASE}/projects/{PROJECT}/repos/{REPO}/pull-requests/901",
                "pr_id": 901, "branch": f"kb/{t['kind']}-{t['id']}-20301231-235959",
                "files": [f"{DEF_DIR}/{Path(p['path']).name}" for p in t.get("cards") or []], **over}
        return self.s.call("POST", API_DONE, body, session=self.sid.get(who) if who else None)

    def test_done_records_upload(self):
        """POST /api/bb/done: запись в bb (ts и by — от сервера), история с номером PR, ссылка в bz_card."""
        t, _ = self.task_in(cards=CARDS[:1])
        code, r = self.done(t, by="Подделкин Пётр Тестович", ts="2000-01-01T00:00:00")
        self.assertEqual(code, 200, r)
        after = self.get(t)
        e = after["bb"][-1]
        self.assertEqual(e["by"], EXE, "автор выгрузки — из сессии, а не из тела запроса")
        self.assertNotEqual(e.get("ts"), "2000-01-01T00:00:00")
        self.assertEqual((e["pr_url"], str(e["pr_id"])), (f"{BB_BASE}/projects/{PROJECT}/repos/{REPO}/pull-requests/901", "901"))
        self.assertIn("901", after["history"][-1]["action"])
        self.assertIn(HIST_DONE, after["history"][-1]["action"])
        self.assertIn(e["pr_url"], after["bz_card"])
        self.assertEqual(r["task"]["_v"], after["_v"])

    def test_done_bz_card_appends(self):
        """bz_card: пусто — заполняется; заполнено — ссылка дописывается; вторая ссылка — тоже дописывается."""
        t, _ = self.task_in(cards=CARDS[:1])
        old = "Размещена в АС Ретривер: выдуманная ссылка"
        self.act(EXE, t, "edit", bz_card=old)
        u1, u2 = (f"{BB_BASE}/projects/{PROJECT}/repos/{REPO}/pull-requests/{n}" for n in (911, 912))
        self.assertEqual(self.done(t, pr_url=u1, pr_id=911)[0], 200)
        self.assertEqual(self.done(t, pr_url=u2, pr_id=912)[0], 200)
        bz = self.get(t)["bz_card"]
        self.assertTrue(bz.startswith(old), bz)
        self.assertLess(bz.index(u1), bz.index(u2), bz)

    def test_done_only_http_links(self):
        t, _ = self.task_in(cards=CARDS[:1])
        before = self.snap(self.get(t))
        for bad in ("javascript:alert(1)", "JavaScript:alert(1)", " javascript:alert(1)", "data:text/html,<b>x</b>",
                    "//evil.example.test/pr/1", "ftp://bb.example.test/pr/1", ""):
            with self.subTest(pr_url=bad):
                code, r = self.done(t, pr_url=bad)
                self.assertEqual(code, 400, r)
                self.assertRegex(r.get("error", ""), RU)
        self.assertEqual(self.snap(self.get(t)), before)

    def test_done_validates_fields(self):
        """Номер PR — число, ветка — допустимое имя, файлы — «<папка>/<имя>.md»; иначе 400, задача не меняется."""
        t, _ = self.task_in(cards=CARDS[:1])
        before = self.snap(self.get(t))
        for over in ({"pr_id": "abc"}, {"pr_id": None}, {"pr_id": -1}, {"branch": "-f"}, {"branch": "../evil"},
                     {"files": ["../../etc/passwd"]}, {"files": "KB/x.md"}, {"files": []}, {"pr_url": None}):
            with self.subTest(over=over):
                code, r = self.done(t, **over)
                self.assertEqual(code, 400, r)
                self.assertRegex(r.get("error", ""), RU)
        self.assertEqual(self.snap(self.get(t)), before)

    def test_bb_list_not_editable_by_client(self):
        """Список выгрузок bb пишет только сервер: правкой задачи его не подделать (исполнитель — 403/игнор)."""
        t, _ = self.task_in(cards=CARDS[:1])
        fake = [{"ts": "2030-01-01T00:00:00", "by": OUT, "pr_url": "javascript:alert(1)", "pr_id": 1,
                 "branch": "kb/x", "files": ["KB/x.md"]}]
        code, r = self.post_task(EXE, {**self.get(t), "bb": fake}, "edit")
        self.assertIn(code, (200, 403), r)
        self.assertNotEqual(self.get(t).get("bb"), fake, "клиент подделал список выгрузок")

    def test_done_rights(self):
        """Чужие права — 403, без сессии — 401, не «Согласована» — 403/400; задача не меняется."""
        t, _ = self.task_in(cards=CARDS[:1])
        before = self.snap(self.get(t))
        for who in (OUT, LEAD, LEAD2):
            with self.subTest(who=who):
                code, r = self.done(t, who=who)
                self.assertEqual(code, 403, r)
                self.assertRegex(r.get("error", ""), RU)
        self.assertEqual(self.done(t, who=None)[0], 401)
        self.assertEqual(self.snap(self.get(t)), before)
        w, _ = self.task_in(status=WORK)
        self.assertIn(self.done(w)[0], (400, 403))


# ================================================================ критерии 2, 6: не настроено — недоступно с пояснением
class NotConfiguredTest(BBBase):
    """Сервер владельца — с настройками репозитория и, как у владельца-исполнителя, с настройками сотрудника
    в своём окружении: выгружать сам он всё равно не должен. Ещё серверы — без REG_BB_*."""

    def own_proxy(self, **kw):
        p = self.start_proxy(**kw)
        self.addCleanup(p.stop)
        return p

    def own_server(self, env=None):
        return self.admin_server(env or SRV_ENV)   # по умолчанию — без REG_BB_REPO_URL: «не настроено»

    def test_direct_server_status(self):
        """Кр. 2: сервер напрямую — выгрузки нет (available false), но видно, что репозиторий настроен."""
        code, r = self.s.call("GET", API_STATUS, session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("available"), False, r)
        self.assertIs(r.get("server_configured"), True, r)
        self.assertRegex(r.get("reason", ""), RU)

    def test_direct_server_upload_refused(self):
        """Кр. 2: POST /api/bb/upload на сервер владельца — 400 «через свой прокси» (сервер в BB не ходит)."""
        t, _ = self.task_in(cards=CARDS[:1])
        before, m = self.snap(self.get(t)), self.rest.mark()
        for who in (EXE, "admin"):
            with self.subTest(who=who):
                code, r = self.s.call("POST", API_UPLOAD, {"kind": t["kind"], "id": t["id"]}, session=self.sid[who])
                self.assertEqual(code, 400, r)
                self.assertRegex(r.get("error", ""), VIA_PROXY)
        self.assertEqual(self.rest.since(m), [])
        self.assertEqual(self.snap(self.get(t)), before)

    def test_server_without_settings(self):
        """Кр. 6: у владельца нет REG_BB_* — сервер: server_configured false, /api/bb/task — 400; прокси сотрудника
        с учёткой: недоступно с пояснением про настройки владельца, выгрузка — 400, в BB не ходили."""
        srv, adm, t = self.own_server()
        code, r = srv.call("GET", API_STATUS, session=adm)
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("available"), False, r)
        self.assertIs(r.get("server_configured"), False, r)
        self.assertRegex(r.get("reason", ""), r"REG_BB_REPO_URL|владел")
        code, r = srv.call("GET", f"{API_TASK}?kind=once&id={t['id']}", session=adm)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), RU)
        p = self.own_proxy(target=f"127.0.0.1:{srv.port}")
        p.token = srv.token
        code, r = p.call("GET", API_STATUS, session=adm)
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("available"), False, r)
        self.assertRegex(r.get("reason", ""), r"REG_BB_REPO_URL|владел")
        m = self.rest.mark()
        code, r = p.call("POST", API_UPLOAD, {"kind": "once", "id": t["id"]}, session=adm)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), RU)
        self.assertEqual(self.rest.since(m), [])

    def test_server_partial_settings(self):
        """Нет части настроек владельца — server_configured false, в причине — чего не хватает."""
        srv, adm, _ = self.own_server(env={"REG_BB_REPO_URL": str(self.bare.path)})
        code, r = srv.call("GET", API_STATUS, session=adm)
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("server_configured"), False, r)
        text = r.get("reason", "") + " " + " ".join(r.get("missing") or [])
        self.assertIn("REG_BB_PROJECT", text)
        self.assertIn("REG_BB_REPO", text.replace("REG_BB_REPO_URL", ""))

    def check_unavailable(self, p, reason_re):
        code, r = p.call("GET", API_STATUS, session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("available"), False, r)
        self.assertRegex(r.get("reason", ""), reason_re)
        t, _ = self.task_in(cards=CARDS[:1])
        r = self.refused_upload(t, (400,), proxy=p)
        self.assertRegex(r["error"], reason_re)

    def test_empty_login(self):
        """Кр. 3, 6: в connect.sh нажали Enter (пустой логин) — выгрузка выключена, причина — про connect.sh."""
        self.check_unavailable(self.own_proxy(stdin_text="\n\n"), r"connect\.sh")

    def test_no_creds_flag(self):
        """Прокси старым способом (без --bb-creds-stdin) — работает как прокси, выгрузка выключена с причиной."""
        self.check_unavailable(self.own_proxy(creds=False), r"connect\.sh")

    def test_no_git_in_path(self):
        """Кр. 6: в контейнере сотрудника нет git — недоступно с причиной про git."""
        p = self.own_proxy(env={"PATH": str(self.work / "empty-bin")})
        (self.work / "empty-bin").mkdir(exist_ok=True)
        self.check_unavailable(p, r"git")

    def test_base_url_with_login_or_invalid(self):
        """Кр. 3, 4: REG_BB_BASE_URL с логином или не адрес http(s) — недоступно с причиной."""
        for base in ("https://someone@bb.example.test", "https://someone:secret@bb.example.test", "bb.example.test",
                     "ftp://bb.example.test"):
            with self.subTest(base=base):
                p = self.own_proxy(env={"REG_BB_BASE_URL": base})
                self.check_unavailable(p, r"REG_BB_BASE_URL")
                self.assertNotIn("secret", p.output())

    def test_settings_from_registry_env_creds_not(self):
        """Кр. 3, 7: настройки сотрудника — из registry.env (несекретные); BITBUCKET_USERNAME/PASSWORD в файле
        игнорируются — учётка только из stdin."""
        env_file = self.work / "registry-employee.env"
        file_pass = "Fake-file-password-" + secrets.token_hex(4)
        env_file.write_text("\n".join([f"REG_BB_BASE_URL={self.rest.base}", "REG_BB_TIMEOUT=60",
                                       f"REG_BB_CLONE={self.work / 'bb-clone-env'}",
                                       "BITBUCKET_USERNAME=file-login", f"BITBUCKET_PASSWORD={file_pass}"]) + "\n",
                            "utf-8")
        p = self.own_proxy(env={"REG_ENV_FILE": str(env_file), **{k: None for k in EMP_KEYS},
                                **git_env(self.work)}, extra=False)
        code, r = p.call("GET", API_STATUS, session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("available"), True, r)
        t, _ = self.task_in(cards=CARDS[:1])
        m = self.rest.mark()
        self.ok_upload(t, proxy=p)
        recs = self.rest.since(m)
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["auth_ok"], "пароль BB — только из stdin, не из registry.env")
        p2 = self.own_proxy(env={"REG_ENV_FILE": str(env_file), **{k: None for k in EMP_KEYS},
                                 **git_env(self.work)}, extra=False, stdin_text="\n\n")
        code, r = p2.call("GET", API_STATUS, session=self.sid[EXE])
        self.assertIs(r.get("available"), False, "учётка BB из registry.env подхватилась")
        self.assertNotIn(file_pass, p.output() + p2.output())


# ================================================================ критерии 3, 6: ошибки Bitbucket (REST/git)
class ErrorsTest(BBBase):
    timeout = "4"   # REG_BB_TIMEOUT прокси — для зависаний

    def tearDown(self):
        self.rest.set_fail("")

    def failed_upload(self, mode, step_re=None):
        """Отказ BB (REST-режим фейка): 400 по-русски с шагом, без учётки; в задаче ничего; журнал прокси чист."""
        t, _ = self.task_in(cards=CARDS[:1])
        self.rest.set_fail(mode)
        before = self.snap(self.get(t))
        t0 = time.time()
        code, r = self.upload(t)
        took = time.time() - t0
        self.rest.set_fail("")
        self.assertEqual(code, 400, r)
        err = r.get("error", "")
        self.assertRegex(err, RU)
        if step_re:
            self.assertRegex(err, step_re)
        self.assertEqual(self.snap(self.get(t)), before, "при ошибке в задачу ничего не пишется")
        self.no_secrets(self.proxy.output(), "журнале прокси")
        self.no_secrets(json.dumps(r, ensure_ascii=False), "ответе браузеру")
        return r, took

    def test_rest_401_bad_credentials(self):
        """Кр. 6: Bitbucket не принял учётку (401) — ошибка с подсказкой перезапустить connect.sh, без учётки."""
        r, _ = self.failed_upload("401", r"(?i)pull request")
        self.assertRegex(r["error"], r"не принял логин или пароль")

    def test_rest_403_no_rights(self):
        """Кр. 6: нет прав на репозиторий (403) — подсказка попросить доступ."""
        r, _ = self.failed_upload("403", r"(?i)pull request")
        self.assertRegex(r["error"], r"нет прав")

    def test_rest_404_repo_or_branch(self):
        """Кр. 6: репозиторий или ветка не найдены (404) — подсказка про адрес и ветку."""
        r, _ = self.failed_upload("404", r"(?i)pull request")
        self.assertRegex(r["error"], r"не найден|ветк")

    def test_rest_500(self):
        """Кр. 6: общий отказ REST (500) — понятная ошибка с кодом, без учётки."""
        r, _ = self.failed_upload("500", r"(?i)pull request")
        self.assertRegex(r["error"], r"HTTP 500")

    def test_hang_timeout(self):
        """Кр. 6: REST зависает при REG_BB_TIMEOUT=4 — ошибка по таймауту (с переклонированием и повтором — вдвое)."""
        _, took = self.failed_upload("slow", r"(?i)pull request|таймаут|Timeout")
        self.assertLess(took, 2 * 4 + 15, "таймаут REG_BB_TIMEOUT не сработал")

    def test_clone_failure(self):
        """Кр. 6: адрес — существующая папка без git — ошибка шага «clone», в BB (REST) не ходили."""
        empty = self.work / "not-a-repo"
        empty.mkdir()
        shutil.rmtree(self.work / "bb-clone", ignore_errors=True)   # общий клон указывает на другой bare
        srv, adm, t = self.admin_server(env={**SRV_ENV, "REG_BB_REPO_URL": str(empty)})
        p = self.start_proxy(target=f"127.0.0.1:{srv.port}")
        self.addCleanup(p.stop)
        p.token = srv.token
        m = self.rest.mark()
        code, r = p.call("POST", API_UPLOAD, {"kind": t["kind"], "id": t["id"]}, session=adm)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), r"(?i)clone|клонир")
        self.assertEqual(self.rest.since(m), [], "при отказе клона в REST не ходили")
        self.no_secrets(p.output(), "журнале прокси")

    def test_proxy_stopped_mid_upload(self):
        """Кр. 4: прокси останавливают (SIGTERM) посреди выгрузки — задача не тронута, учётки в журнале нет;
        постоянный клон — кэш, может остаться (переклонируется при следующей попытке)."""
        p = self.start_proxy(env={"REG_BB_TIMEOUT": "120"})
        t, _ = self.task_in(cards=CARDS[:1])
        before = self.snap(self.get(t))
        self.rest.set_fail("slow")
        th = threading.Thread(target=lambda: p.call("POST", API_UPLOAD, {"kind": t["kind"], "id": t["id"]},
                                                    session=self.sid[EXE], timeout=60), daemon=True)
        th.start()
        end = time.time() + 15
        while time.time() < end and not self.rest.records():
            time.sleep(0.05)
        self.assertTrue(self.rest.records(), "выгрузка не дошла до REST-запроса")
        p.proc.terminate()
        try:
            p.proc.wait(20)
        except subprocess.TimeoutExpired:
            p.proc.kill()
            p.proc.wait(5)
            self.fail("прокси не остановился по SIGTERM за 20 с")
        th.join(10)
        self.assertEqual(self.snap(self.get(t)), before)
        self.no_secrets(p.output(), "журнале прокси")


# ================================================================ критерий 4: сторона сотрудника не доверяет серверу
class ServerForeignRepoTest(BBBase):
    """Кр. 4 на настоящем сервере: у владельца REG_BB_REPO_URL с чужим хостом (или с логином) — прокси сотрудника
    отказывает (400), в BB не ходит, в задаче ничего."""
    srv_env = {**SRV_ENV, "REG_BB_REPO_URL": "https://evil.example.test/scm/kbproj/kb-repo.git"}

    def test_foreign_host(self):
        t, _ = self.task_in(cards=CARDS[:1])
        r = self.refused_upload(t, (400,))
        self.assertRegex(r["error"], r"evil\.example\.test|чужой хост|REG_BB_BASE_URL|Bitbucket")

    def test_login_in_repo_url(self):
        srv, adm, t = self.admin_server({**SRV_ENV, "REG_BB_REPO_URL": "https://someone@bb.example.test/scm/kbproj/kb-repo.git"})
        p = self.start_proxy(target=f"127.0.0.1:{srv.port}")
        self.addCleanup(p.stop)
        p.token = srv.token
        before = srv.call("GET", "/api/state")[1]["tasks"]
        m = self.rest.mark()
        code, r = p.call("POST", API_UPLOAD, {"kind": "once", "id": t["id"]}, session=adm)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), RU)
        self.assertEqual(self.rest.since(m), [])
        self.assertEqual(srv.call("GET", "/api/state")[1]["tasks"], before)


class EvilBase(BBBase):
    """Прокси смотрит на поддельный сервер реестра (FakeRegistry) — он присылает что угодно в /api/bb/task."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.reg = FakeRegistry()
            cls.evil = cls.start_proxy(target=f"127.0.0.1:{cls.reg.port}")
            cls.evil.token = FakeRegistry.TOKEN
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "reg", None) is not None:
            cls.reg.stop()
        super().tearDownClass()

    def payload(self, **over):
        target = {"repo_url": str(self.bare.path), "project": PROJECT, "repo": REPO,
                  "branch": DEF_BRANCH, "dir": DEF_DIR}
        target.update(over.pop("target", {}))
        cards = over.pop("cards", [{"file": "7_Выдуманный вопрос.md", "title": "Выдуманный вопрос?", "body": "Ответ"}])
        return {"task": {"kind": "once", "id": 7, "title": "Выдуманная задача"}, "cards": cards, "target": target,
                "by": EXE, **over}

    def evil_upload(self, payload, fresh=False):
        """fresh — выгрузка дойдёт до ветки: следующая секунда, чтобы имя ветки не совпало с прошлой."""
        self.reg.task_code, self.reg.task_body = 200, payload
        if fresh:
            self.next_second()
        n, m = len(self.reg.reqs), self.rest.mark()
        code, r = self.evil.call("POST", API_UPLOAD, {"kind": "once", "id": 7}, session="fake-session-t010")
        return code, r, n, m

    def assert_refused(self, payload):
        code, r, n, m = self.evil_upload(payload)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), RU)
        self.assertEqual(self.rest.since(m), [], "в BB ходили с недопустимыми настройками")
        self.assertEqual(self.reg.done_reqs(n), [], "в задачу записано")
        self.assertEqual([p for p in self.work.rglob("escape-t010*")], [], "файл записан вне клона")
        return r


class EmployeeSideChecksTest(EvilBase):
    def test_happy_path_sends_only_result(self):
        """Кр. 2, 3: серверу уходят только kind, id, pr_url, pr_id, branch, files; учётки нет ни в одном запросе
        к серверу (заголовки, адрес, тело); сессия и токен браузера передаются как есть."""
        code, r, n, m = self.evil_upload(self.payload(), fresh=True)
        self.assertEqual(code, 200, r)
        done = self.reg.done_reqs(n)
        self.assertEqual(len(done), 1)
        body = json.loads(done[0]["body"])
        self.assertLessEqual(set(body), {"kind", "id", "pr_url", "pr_id", "branch", "files"}, body)
        self.assertEqual(Path(body["files"][0]).name, "7_Выдуманный вопрос.md")
        for q in self.reg.reqs[n:]:
            blob = json.dumps({**q, "body": q["body"].decode("utf-8", "replace")}, ensure_ascii=False)
            self.no_secrets(blob, f"запросе к серверу реестра {q['method']} {q['path']}")
            hdr = {k.lower(): v for k, v in q["headers"].items()}
            self.assertFalse({"authorization", "proxy-authorization"} & set(hdr), hdr)
            if urlparse(q["path"]).path in (API_TASK, API_DONE):
                self.assertEqual(hdr.get("x-session"), "fake-session-t010")
                self.assertEqual(hdr.get("x-token"), FakeRegistry.TOKEN)
        task_q = [q for q in self.reg.reqs[n:] if urlparse(q["path"]).path == API_TASK]
        self.assertEqual(parse_qs(urlparse(task_q[0]["path"]).query), {"kind": ["once"], "id": ["7"]})
        self.no_secrets(json.dumps(r, ensure_ascii=False), "ответе браузеру")

    def test_foreign_repo_host_refused(self):
        """Кр. 4: репозиторий не на корпоративном Bitbucket сотрудника (REG_BB_BASE_URL) или с логином в URL — 400."""
        for url in ("https://evil.example.test/scm/kbproj/kb-repo.git",
                    "https://bb.example.test.evil.test/scm/kbproj/kb-repo.git",
                    "https://evil.test/bb.example.test/scm/kbproj/kb-repo.git",
                    "https://bb.example.test@evil.test/scm/kbproj/kb-repo.git",
                    "https://someone@bb.example.test/scm/kbproj/kb-repo.git",
                    "https://someone:secret@bb.example.test/scm/kbproj/kb-repo.git",
                    "ssh://git@bb.example.test:7999/kbproj/kb-repo.git",
                    "file:///srv/kb-repo.git", "/srv/kb-repo.git", "ext::sh -c true", ""):
            with self.subTest(url=url):
                self.assert_refused(self.payload(target={"repo_url": url}))

    def test_scheme_downgrade_refused(self):
        """Кр. 4 (сверх буквы: хост тот же, схема http при https у сотрудника) — пароль ушёл бы открытым текстом."""
        self.assert_refused(self.payload(target={"repo_url": "http://bb.example.test/scm/kbproj/kb-repo.git"}))

    def test_evil_dir_refused(self):
        """Кр. 4: папка с выходом из клона или в .git — 400, в BB не ходили."""
        for d in ("..", "../KB", "/tmp/escape-t010", "KB/../..", ".git", ".git/hooks", "KB/../.git", "KB\x00x"):
            with self.subTest(dir=d):
                self.assert_refused(self.payload(target={"dir": d}))

    def test_evil_file_names_refused(self):
        """Кр. 4: имена файлов с путём или выходом из папки — 400, файлы вне клона не пишутся."""
        for f in ("../escape-t010.md", "../../escape-t010.md", "../.git/hooks/pre-commit", ".git/config",
                  "/tmp/escape-t010.md", "KB/../../escape-t010.md", "sub/escape-t010.md", "..", ".", "",
                  "escape-t010\x00.md"):
            with self.subTest(file=f):
                self.assert_refused(self.payload(cards=[{"file": f, "title": "Злой вопрос?", "body": "Злой ответ"}]))

    def test_evil_branch_refused(self):
        """Кр. 4: целевая ветка — опция git или недопустимое имя — 400."""
        for b in ("-f", "--upload-pack=true", "master..evil", "../evil", "master\nevil", "master evil", "master~1",
                  "evil:master", "refs/heads/../evil", ".evil", "evil.lock", "evil@{1}", "evil\\x"):
            with self.subTest(branch=b):
                self.assert_refused(self.payload(target={"branch": b}))

    def test_other_task_in_reply_refused(self):
        """Сервер прислал карточки другой задачи — 400, в BB не ходили."""
        self.assert_refused(self.payload(task={"kind": "once", "id": 8, "title": "Чужая выдуманная задача"}))
        self.assert_refused(self.payload(task={"kind": "regular", "id": 7, "title": "Чужая выдуманная задача"}))

    def test_bad_card_content_refused(self):
        """Карточка без вопроса или с телом не строкой — 400, в BB не ходили."""
        for card in ({"file": "7_x.md", "title": "", "body": "Ответ"}, {"file": "7_x.md", "title": "Вопрос?", "body": None},
                     {"file": "7_x.md", "title": ["Вопрос?"], "body": "Ответ"}, "7_x.md"):
            with self.subTest(card=card):
                self.assert_refused(self.payload(cards=[card]))
        self.assert_refused(self.payload(cards=[]))
        self.assert_refused(self.payload(cards=[{"file": "7_x.md", "title": "В?", "body": "О"}] * 2))

    def test_old_server_without_bb(self):
        """Сервер реестра ещё без T-010 (404 на /api/bb/status) — у сотрудника «недоступно» с пояснением."""
        self.reg.status_code = 404
        try:
            code, r = self.evil.call("GET", API_STATUS, session="fake-session-t010")
        finally:
            self.reg.status_code = 200
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("available"), False, r)
        self.assertRegex(r.get("reason", ""), RU)

    def test_server_refusal_passed_through(self):
        """Отказ сервера в /api/bb/task (права) — браузеру как есть, в BB не ходили."""
        self.reg.task_code, self.reg.task_body = 403, {"error": "Выгружает исполнитель или замещающий (выдумка)"}
        m = self.rest.mark()
        code, r = self.evil.call("POST", API_UPLOAD, {"kind": "once", "id": 7}, session="fake-session-t010")
        self.assertEqual(code, 403, r)
        self.assertEqual(r.get("error"), "Выгружает исполнитель или замещающий (выдумка)")
        self.assertEqual(self.rest.since(m), [])

    def test_done_failure_reports_pr(self):
        """PR создан, а сервер не записал выгрузку — ошибка со ссылкой на PR (чтобы вписать вручную), клон удалён."""
        self.reg.done_code = 500
        try:
            code, r, n, m = self.evil_upload(self.payload(), fresh=True)
        finally:
            self.reg.done_code = 200
        self.assertNotEqual(code, 200, r)
        self.assertTrue(r.get("pr_url"), r)
        self.assertIn(r["pr_url"], r.get("error", ""))


class RepoContentTest(EvilBase):
    """Кр. 4: файлы — только внутри временного клона, даже если в репозитории БЗ папка или файл карточки —
    символическая ссылка наружу (репозиторий наполняют другие люди); репозиторий без папки KB или без ветки."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.outside = cls.work / "outside-dir"
            cls.outside.mkdir()
            cls.outside_file = cls.work / "outside-file.md"
            cls.outside_file.write_text("Чужой файл вне клона\n", "utf-8")

            def dir_link(seed):
                os.symlink(str(cls.outside), seed / "KB")

            def file_link(seed):
                (seed / "KB").mkdir()
                os.symlink(str(cls.outside_file), seed / "KB" / "7_Выдуманный вопрос.md")

            cls.bare_dir = BareRepo(cls.work, extra=dir_link, name="kb-dirlink.git")
            cls.bare_empty = BareRepo(cls.work, extra=lambda seed: None, name="kb-nokb.git")
            cls.bare_file = BareRepo(cls.work, extra=file_link, name="kb-filelink.git")
            for b, slug in ((cls.bare_dir, "kb-dirlink"), (cls.bare_empty, "kb-nokb"),
                            (cls.bare_file, "kb-filelink")):
                cls.rest.repos[f"{PROJECT}/{slug}"] = b.path
        except Exception:
            cls.tearDownClass()
            raise

    def check(self, bare, slug):
        """Символическая ссылка в репозитории (папка или файл карточки) — отказ до коммита, вне клона ничего."""
        self.clone_wipe()
        code, r, n, m = self.evil_upload(self.payload(target={"repo_url": str(bare.path), "repo": slug}), fresh=True)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), RU)
        self.assertEqual(r.get("step", "запись файлов"), "запись файлов", r)
        self.assertEqual(self.reg.done_reqs(n), [])
        self.assertEqual(list(self.outside.iterdir()), [], "карточка записана вне клона (по ссылке-папке)")
        self.assertEqual(self.outside_file.read_text("utf-8"), "Чужой файл вне клона\n", "файл вне клона перезаписан")
        self.assertEqual(self.rest.since(m), [], "pull request не создавался")

    def clone_wipe(self):
        import shutil as _s
        _s.rmtree(self.work / "bb-clone", ignore_errors=True)   # клон с чужим репозиторием — переклонировать

    def test_kb_dir_missing_is_created(self):
        """В целевой ветке ещё нет папки KB — прокси её создаёт, выгрузка проходит."""
        self.clone_wipe()
        code, r, n, m = self.evil_upload(self.payload(target={"repo_url": str(self.bare_empty.path),
                                                              "repo": "kb-nokb"}), fresh=True)
        self.assertEqual(code, 200, r)
        self.assertEqual(self.bare_empty.changed(DEF_BRANCH, r["branch"]), {"KB/7_Выдуманный вопрос.md"})
        self.assertEqual(self.bare_empty.show(r["branch"], "KB/7_Выдуманный вопрос.md"), "# Выдуманный вопрос?\n\nОтвет\n")

    def test_target_branch_missing(self):
        """Целевой ветки нет в репозитории — 400, шаг «fetch», подсказка про ветку, в задаче ничего."""
        self.clone_wipe()
        code, r, n, m = self.evil_upload(self.payload(target={"branch": "release/no-such"}), fresh=True)
        self.assertEqual(code, 400, r)
        self.assertEqual(r.get("step", "fetch"), "fetch", r)
        self.assertRegex(r.get("error", ""), r"ветк")
        self.assertEqual(self.reg.done_reqs(n), [])

    def test_dir_is_symlink(self):
        self.check(self.bare_dir, "kb-dirlink")

    def test_card_file_is_symlink(self):
        self.check(self.bare_file, "kb-filelink")


# ================================================================ регрессия: прокси как прокси
class ProxyRegressionTest(BBBase):
    def test_page_state_login_task_through_proxy(self):
        """Обычные запросы через прокси с выгрузкой: страница, состояние, вход, постановка и действия, карточка."""
        page = NO_PROXY.open(self.proxy.base + "/", timeout=10).read().decode("utf-8")
        self.assertIn(self.s.token, page)
        code, st = self.proxy.call("GET", "/api/state")
        self.assertEqual(code, 200, st)
        self.assertTrue(st["config"]["people"])
        code, r = self.proxy.call("POST", "/api/login", {"name": EXE, "pin": PINS[EXE]})
        self.assertEqual(code, 200, r)
        sid = r["session"]
        code, r = self.proxy.call("POST", "/api/task", {"task": {"kind": "once", "id": None, **POSTANOVKA},
                                                        "action_code": "create", "action": "Через прокси"},
                                  session=self.sid[LEAD])
        self.assertEqual(code, 200, r)
        t = r["task"]
        code, r = self.proxy.call("POST", "/api/task", {"task": t, "action_code": "take", "expect_v": t["_v"]}, session=sid)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["task"]["status"], WORK)
        code, r = self.proxy.call("POST", "/api/task", {"task": t, "action_code": "take", "expect_v": t["_v"]}, session=sid)
        self.assertEqual(code, 409, r)   # ошибки сервера — как есть
        code, r = self.proxy.call("POST", "/api/task", {"task": t, "action_code": "take"}, session=None)
        self.assertEqual(code, 401, r)
        code, r = self.proxy.call("DELETE", "/api/task", {"kind": "once", "id": t["id"], "expect_v": t["_v"] + 1},
                                  session=self.sid["admin"])
        self.assertEqual(code, 200, r)


    def test_bb_without_token_refused(self):
        """Чужая страница без токена реестра (X-Token) не запустит выгрузку: 403 от сервера, в BB не ходили."""
        t, _ = self.task_in(cards=CARDS[:1])
        before, m = self.snap(self.get(t)), self.rest.mark()
        code, r = self.proxy.call("POST", API_UPLOAD, {"kind": t["kind"], "id": t["id"]}, session=self.sid[EXE], token=False)
        self.assertEqual(code, 403, r)
        code, r = self.proxy.call("GET", API_STATUS, session=self.sid[EXE], token=False)
        self.assertEqual(code, 403, r)
        self.assertEqual(self.rest.since(m), [])
        self.assertEqual(self.snap(self.get(t)), before)

    def test_server_down(self):
        """Сервер реестра не отвечает — статус и выгрузка: понятная ошибка по-русски, в BB не ходили."""
        dead = free_port()
        p = self.start_proxy(target=f"127.0.0.1:{dead}")
        self.addCleanup(p.stop)
        m = self.rest.mark()
        for method, path, body in (("GET", API_STATUS, None), ("POST", API_UPLOAD, {"kind": "once", "id": 1})):
            with self.subTest(path=path):
                code, r = p.call(method, path, body, session=self.sid[EXE], timeout=90)
                self.assertGreaterEqual(code, 400, r)
                self.assertRegex(r.get("error", "") + r.get("reason", ""), RU)
        self.assertEqual(self.rest.since(m), [])


# ================================================================ критерии 3, 7: connect.sh (статически)
def _connect_lines():
    return CONNECT.splitlines()


class ConnectShStaticTest(unittest.TestCase):
    def test_bash_syntax(self):
        p = subprocess.run(["bash", "-n", str(ROOT / "connect.sh")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_reads_registry_env_with_secret_check(self):
        """Настройки сотрудника (REG_BB_MCP и др.) — из registry.env, с отказом, если там логин/пароль/токен."""
        self.assertIn("registry.env", CONNECT)
        lines = _connect_lines()
        checks = [i for i, ln in enumerate(lines) if "grep" in ln and re.search(r"PASS", ln) and re.search(r"TOKEN|SECRET", ln)]
        self.assertTrue(checks, "нет проверки registry.env на логины/пароли/токены")
        self.assertTrue(any("die" in " ".join(lines[i:i + 3]) or "exit" in " ".join(lines[i:i + 3]) for i in checks),
                        "проверка на секреты не останавливает connect.sh")

    def silent_read(self):
        """Скрытый ввод (read -s …) — пароль BB; возвращает match, group(2) — имя переменной."""
        for m in re.finditer(r"\bread((?:\s+-\w+)+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s+(\w+)", CONNECT):
            if "s" in m.group(1).replace("-", "").replace(" ", ""):
                return m
        self.fail("нет скрытого ввода пароля BB (read -s)")

    def test_asks_bb_creds_always(self):
        """Логин BB спрашивается при каждом запуске — без условий (пароль — под проверкой пустого логина)."""
        m = re.search(r'^\s*read .*Логин BB', CONNECT, re.M)
        self.assertTrue(m, "нет запроса логина BB")
        before = CONNECT[:m.start()].splitlines()[-6:]
        self.assertFalse(any(re.search(r"\bif\b|\[\[", ln) for ln in before),
                         "запрос логина BB не должен быть под условием")
        self.assertIn("Логин BB", CONNECT)

    def test_creds_via_pipe_to_proxy(self):
        """Учётка — в прокси через канал на stdin (--bb-creds-stdin), не аргументом и не окружением."""
        self.assertIn(CREDS_FLAG, CONNECT)
        self.assertRegex(CONNECT, r"printf\s+['\"]%s\\n%s\\n['\"]")
        var = self.silent_read().group(2)
        for ln in _connect_lines():
            if ln.lstrip().startswith("#"):
                continue
            self.assertNotRegex(ln, rf"\bexport\s+(\w+\s+)*{var}\b", "пароль BB экспортируется")
            self.assertNotRegex(ln, rf"\b{var}=\S*\s+(setsid\s+)?python", "пароль BB — в окружении команды")
            if "app.py" in ln and "proxy" in ln:
                self.assertNotIn("$" + var, ln, "пароль BB — в аргументах прокси")
                self.assertNotIn("${" + var, ln, "пароль BB — в аргументах прокси")


# ================================================================ критерии 1, 6: интерфейс (статически)
def _fn_containing(text):
    """Тела функций (function f(){…} или const f = (…) => {…}), в которых встречается text."""
    out = []
    for m in re.finditer(r"(?:function\s+(\w+)\s*\([^)]*\)|const\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>)\s*\{", HTML):
        i, depth = m.end(), 1
        while depth and i < len(HTML):
            depth += {"{": 1, "}": -1}.get(HTML[i], 0)
            i += 1
        body = HTML[m.end():i - 1]
        if text in body:
            out.append((m.group(1) or m.group(2), body))
    return out


def _hiding_classes():
    css = re.sub(r"/\*.*?\*/", "", frontend_css(), flags=re.S)
    out = set()
    for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"display\s*:\s*none", decl):
            out |= {m.group(1) for m in (re.fullmatch(r"\s*\.([\w-]+)\s*", s) for s in sel.split(",")) if m}
    return out


class StaticUiTest(unittest.TestCase):
    """Без браузера (playwright не установлен) — проверки index.html; поведение — вручную в браузере."""

    def test_button_text(self):
        self.assertIn(BUTTON, HTML)

    def test_api_calls_relative(self):
        """Запросы к /api/bb/status и /api/bb/upload — через api() (относительный путь от BASE, урок про …/proxy/39246)."""
        for path in (API_STATUS, API_UPLOAD):
            with self.subTest(path=path):
                self.assertRegex(HTML, r"api\(\s*['\"`]/?" + re.escape(path.lstrip("/")))

    def test_confirmation_before_upload(self):
        """Нажатие — с подтверждением."""
        fns = _fn_containing("api/bb/upload")
        self.assertTrue(fns, "нет функции выгрузки")
        self.assertTrue(any("confirm(" in body for _, body in fns), "выгрузка без подтверждения")

    def test_button_conditions(self):
        """Кнопка — в «Согласована», исполнителю/замещающему/админу (canCard), если есть карточки; недоступно — с пояснением."""
        i = HTML.find(BUTTON)
        self.assertGreater(i, -1)
        near = HTML[max(0, i - 1500):i + 500]
        self.assertRegex(near, r"canCard\(|'Согласована'")
        self.assertRegex(near, r"cards")
        self.assertRegex(near, r"disabled")
        self.assertRegex(near, r"reason|title=")

    def test_pr_links_escaped_noopener(self):
        """Ссылки на PR: адрес через esc(), с rel="noopener" (прямо в разметке или во вспомогательной функции)."""
        tags = re.findall(r"<a\b[^>]*pr_url[^>]*>", HTML)
        helpers = {n for n in re.findall(r"\b(\w+)\(\s*[\w.?]*\bpr_url\b", HTML) if n not in ("esc", "String", "if")}
        for n in helpers:
            for _, body in [(x, b) for x, b in _fn_containing("<a") if x == n]:
                tags += re.findall(r"<a\b[^>]*>", body)
        self.assertTrue(tags, "нет ссылки на PR (pr_url) в разметке")
        for tag in tags:
            self.assertIn("esc(", tag, tag)
            self.assertRegex(tag, r"rel=\\?['\"][^'\"]*noopener", tag)

    def test_new_classes_no_conflict(self):
        """Урок CLAUDE.md: классы кнопки и списка выгрузок не совпадают с занятыми (.busy и прочие с display:none)."""
        classes = set()
        for _, body in _fn_containing("api/bb/") + _fn_containing("pr_url"):
            for attr in re.findall(r"class=\\?\"([^\"$\\]+)", body):
                classes |= set(attr.split())
        i = HTML.find(BUTTON)
        for attr in re.findall(r"class=\\?\"([^\"$\\]+)", HTML[max(0, i - 300):i]):
            classes |= set(attr.split())
        self.assertFalse(classes & ({"busy", "show", "dragging"} | _hiding_classes()), classes)


def _js_const(name):
    m = re.search(rf"^(?:export )?const {re.escape(name)} = .*$", HTML, re.M)
    if not m:
        raise AssertionError(f"нет const {name} во фронтенде")
    return m.group(0).removeprefix("export ")   # ES-модули: префикс export в node-пробнике не нужен


def _js_function(name):
    m = re.search(rf"function {re.escape(name)}\([^)]*\)\s*\{{", HTML)
    if not m:
        raise AssertionError(f"нет function {name} в index.html")
    i, depth = m.end(), 1
    while depth and i < len(HTML):
        depth += {"{": 1, "}": -1}.get(HTML[i], 0)
        i += 1
    return HTML[m.start():i]


NODE_HARNESS = r"""
const AUTH = {session:'', user:null};
%(consts)s
%(funcs)s
const T = o => Object.assign({kind:'once', id:7, status:'Согласована', executor:'Карточкина Вера Тестовна',
  substitute:'Заменин Игорь Тестович', cards:['cards/7_x.md'], bb:[]}, o);
const out = {};
AUTH.user = {name:'Карточкина Вера Тестовна', admin:false};
BB.status = {available:true};
out.exec_ok = bbHTML(T({}));
AUTH.user = {name:'Заменин Игорь Тестович', admin:false};
out.sub_ok = bbHTML(T({}));
AUTH.user = {name:'Посторонний Глеб Тестович', admin:false};
out.other = bbHTML(T({}));
AUTH.user = {name:'Админ', admin:true};
out.admin = bbHTML(T({executor:'Другая Выдумка Тестовна', substitute:''}));
AUTH.user = {name:'Карточкина Вера Тестовна', admin:false};
out.not_approved = bbHTML(T({status:'В работе'}));
out.done = bbHTML(T({status:'Завершена'}));
out.no_cards = bbHTML(T({cards:[]}));
BB.status = {available:false, reason:'Причина <b>недоступности</b> «выдумка»'};
out.unavailable = bbHTML(T({}));
BB.status = null;
out.loading = bbHTML(T({}));
BB.status = {available:true};
out.links = bbHTML(T({status:'Принята', bb:[
  {ts:'2030-01-01T10:00:00', by:'Карточкина Вера Тестовна', pr_url:'javascript:alert(1)', pr_id:1, branch:'kb/x'},
  {ts:'2030-01-01T11:00:00', by:'Икс <b>Тестович</b>', pr_url:'https://bb.example.test/p/2"onmouseover="alert(1)', pr_id:'2<img src=x>', branch:'kb/<b>'},
  {ts:'2030-01-01T12:00:00', by:'Икс Тестович', pr_url:'https://bb.example.test/projects/P/repos/R/pull-requests/3', pr_id:3, branch:'kb/once-7'}]}));
out.http = ['javascript:alert(1)', 'JavaScript:alert(1)', 'https://x.test/y', 'http://x.test', ' https://x.test',
  'https://x.test/"onclick=1', 'https://x.test/<b>', '//x.test/y', 'data:text/html,1', ''].map(u => [u, httpUrl(u)]);
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "нет node — проверка разметки выгрузки в BB только статически")
class NodeUiTest(unittest.TestCase):
    """Кр. 1, 6: функции интерфейса выгрузки (bbHTML и др.) выполняются в node на выдуманных задачах."""

    @classmethod
    def setUpClass(cls):
        consts = "\n".join(_js_const(n) for n in ("esc", "fmt", "fmtTs", "short", "me", "isAdmin", "isEx", "canCard",
                                                   "httpUrl", "BB", "bbKey", "bbCan"))
        src = NODE_HARNESS % {"consts": consts, "funcs": _js_function("bbHTML")}
        p = subprocess.run(["node", "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=60)
        if p.returncode:
            raise AssertionError("node: " + p.stderr[-2000:])
        cls.out = json.loads(p.stdout)

    def test_button_only_for_executors_in_approved_with_cards(self):
        for k in ("exec_ok", "sub_ok", "admin"):
            with self.subTest(who=k):
                self.assertIn(BUTTON, self.out[k])
                self.assertNotRegex(self.out[k], r"<button[^>]*disabled")
        for k in ("other", "not_approved", "done", "no_cards"):
            with self.subTest(case=k):
                self.assertNotIn(BUTTON, self.out[k])

    def test_unavailable_disabled_with_reason(self):
        html = self.out["unavailable"]
        self.assertRegex(html, r"<button[^>]*disabled")
        self.assertIn("Причина &lt;b&gt;недоступности&lt;/b&gt;", html, "причина не показана или не экранирована")
        self.assertNotIn("<b>недоступности", html)
        self.assertRegex(self.out["loading"], r"<button[^>]*disabled")

    def test_upload_list_links(self):
        """Список выгрузок: ссылка — только на http(s), с rel=noopener; номер, ветка и автор экранированы."""
        html = self.out["links"]
        self.assertNotIn("javascript:", html)
        hrefs = re.findall(r"<a\b[^>]*href=\"([^\"]*)\"[^>]*>", html)
        self.assertIn("https://bb.example.test/projects/P/repos/R/pull-requests/3", hrefs)
        for tag in re.findall(r"<a\b[^>]*>", html):
            self.assertIn('rel="noopener', tag)
        self.assertNotIn("onmouseover=\"", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<b>", html)

    def test_http_url_check(self):
        got = dict(map(tuple, self.out["http"]))
        self.assertEqual({u for u, ok in got.items() if ok}, {"https://x.test/y", "http://x.test"}, got)


# ================================================================ критерий 3: connect.sh целиком в песочнице
STUB_APP = r'''import base64, hashlib, json, os, socket, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, %(tests)r)
from fake_bb_mcp import raw_procargs
if len(sys.argv) > 1 and sys.argv[1] == "proxy":
    port = int(sys.argv[sys.argv.index("--port") + 1])
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", port)); s.listen(5)
    got = {}
    def rd():
        got["stdin"] = sys.stdin.buffer.read().decode("utf-8", "replace") if sys.stdin else ""
    th = threading.Thread(target=rd, daemon=True); th.start(); th.join(5)
    kind, argv, env = raw_procargs()
    lines = (got.get("stdin") or "").split("\n")   # в журнал — только sha256 строк: пароль в файл не пишем
    rec = {"argv": sys.argv, "stdin_sha": [hashlib.sha256(x.encode("utf-8")).hexdigest() for x in lines],
           "pid": os.getpid(),
           "raw": base64.b64encode(b"\0".join((argv or []) + (env or []))).decode() if kind else None}
    with open(os.path.join(HERE, "stub-proxy.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("Выгрузка карточек в BB: из этого контейнера (заглушка теста)", flush=True)
    end = time.time() + 60
    s.settimeout(0.5)
    while time.time() < end:
        try:
            c, _ = s.accept(); c.close()
        except OSError:
            pass
'''


@unittest.skipUnless(shutil.which("bash"), "нет bash")
class ConnectShSandboxTest(unittest.TestCase):
    """connect.sh целиком: копия скрипта, где /tmp/ заменён на временную папку; рядом — заглушка app.py (пишет
    свои argv, stdin и исходное окружение глазами ОС); setsid — заглушка (на macOS его нет); HOME — временная папка.
    Учётка BB вводится в stdin connect.sh, как с клавиатуры."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name).resolve()
        (self.dir / "bin").mkdir()
        (self.dir / "home").mkdir()
        (self.dir / "systmp").mkdir()
        setsid = self.dir / "bin" / "setsid"
        setsid.write_text('#!/bin/sh\nexec "$@"\n', "utf-8")
        setsid.chmod(0o755)
        text = CONNECT.replace("/tmp/", f"{self.dir}/systmp/")
        self.assertNotIn("/tmp/", text.replace(f"{self.dir}/systmp/", ""))
        (self.dir / "connect.sh").write_text(text, "utf-8")
        (self.dir / "app.py").write_text(STUB_APP % {"tests": str(Path(__file__).resolve().parent)}, "utf-8")
        self.reg = FakeRegistry()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for rec in self.records():
            if alive(rec["pid"]) and str(self.dir) in ps_field(rec["pid"], "command"):
                os.kill(rec["pid"], signal.SIGKILL)
        self.reg.stop()
        self._tmp.cleanup()

    def records(self):
        f = self.dir / "stub-proxy.jsonl"
        return [json.loads(x) for x in f.read_text("utf-8").splitlines()] if f.exists() else []

    def run_connect(self, stdin, env_file=None):
        if env_file is not None:
            (self.dir / "registry.env").write_text(env_file, "utf-8")
        env = {"PATH": f"{self.dir / 'bin'}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}/usr/bin:/bin",
               "HOME": str(self.dir / "home"), "PROXY_PORT": str(free_port()), "LANG": "C.UTF-8", "TERM": "dumb"}
        p = subprocess.run(["bash", str(self.dir / "connect.sh"), f"127.0.0.1:{self.reg.port}"], input=stdin,
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=90)
        return p

    def test_creds_reach_proxy_only_via_stdin(self):
        """REG_BB_MCP задан — логин и пароль спрошены и ушли прокси двумя строками в stdin, с --bb-creds-stdin;
        в аргументах и исходном окружении прокси их нет; в выводе connect.sh пароля нет."""
        mcp = self.dir / "mcp" / "server.py"
        mcp.parent.mkdir()
        mcp.write_text("# выдуманный server.py\n", "utf-8")
        p = self.run_connect(f"{BB_LOGIN}\n{BB_PASS}\n", f"REG_BB_BASE_URL={BB_BASE}\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        recs = self.records()
        self.assertEqual(len(recs), 1, p.stdout + p.stderr)
        rec = recs[0]
        self.assertIn(CREDS_FLAG, rec["argv"])
        self.assertEqual(rec["stdin_sha"][:2], [sha(BB_LOGIN), sha(BB_PASS)], "в stdin прокси — не логин и пароль")
        for v in secret_forms(BB_PASS) | secret_forms(BB_LOGIN):
            self.assertNotIn(v, " ".join(rec["argv"]))
            if rec["raw"] is not None:
                self.assertNotIn(v.encode("utf-8"), base64.b64decode(rec["raw"]), "учётка в окружении/аргументах прокси")
        for v in secret_forms(BB_PASS):
            self.assertNotIn(v, p.stdout + p.stderr)
            self.assertEqual(files_with([self.dir], [v]), [], "пароль BB записан в файл")

    def test_empty_login_disables(self):
        """Enter вместо логина — пароль не спрашивается, прокси получает пустой логин (выгрузка выключена)."""
        mcp = self.dir / "server.py"
        mcp.write_text("# выдуманный\n", "utf-8")
        p = self.run_connect(f"\n{BB_PASS}\n", f"REG_BB_MCP={mcp}\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rec = self.records()[0]
        self.assertNotIn(sha(BB_PASS), rec["stdin_sha"], "пароль ушёл в прокси, хотя логин пустой")
        self.assertEqual(rec["stdin_sha"][0], sha(""))

    def test_empty_login_disables_bb(self):
        """Enter вместо логина — пароль не спрашивается, прокси получает пустой логин (BB выключен)."""
        p = self.run_connect("\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rec = self.records()[0]
        self.assertEqual(rec["stdin_sha"][0], sha(""), "пустой логин должен уйти в прокси")
        self.assertNotIn(sha(BB_PASS), rec["stdin_sha"], "пароль спросили, хотя логин пустой")

    def test_secret_in_registry_env_stops(self):
        """В registry.env есть BITBUCKET_PASSWORD — connect.sh останавливается, прокси не запускается."""
        p = self.run_connect(f"{BB_LOGIN}\n{BB_PASS}\n", "REG_BB_MCP=/x/server.py\nBITBUCKET_PASSWORD=Fake-1\n")
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertEqual(self.records(), [])
        self.assertRegex(p.stdout + p.stderr, RU)


# ================================================================ критерии 7, 8: без зависимостей, документация
class NoDepsTest(unittest.TestCase):
    def test_app_imports_stdlib_only(self):
        """Кр. 7: сторона сотрудника (прокси, команды GigaCode, BB, хранилища) — на stdlib;
        asyncpg — только лениво (в режиме gp); fastapi нужна только серверу владельца."""
        bad = []
        for f in employee_side_files():
            tree = ast.parse(f.read_text("utf-8"))
            for node in ast.walk(tree):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else []
                for n in names:
                    root = n.split(".")[0]
                    if root not in sys.stdlib_module_names and root not in ("asyncpg", "registry"):
                        bad.append(f"{f.name}: {n}")
            top = [a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names]
            self.assertNotIn("asyncpg", top, f.name)
        self.assertEqual(bad, [])

    def test_frontend_without_cdn(self):
        self.assertIsNone(re.search(r"<script[^>]+src=['\"]https?:", HTML))
        self.assertIsNone(re.search(r"<link[^>]+href=['\"]https?:", HTML))


class DocsTest(unittest.TestCase):
    def test_readme_contract(self):
        """Кр. 8: контракт API, флаг прокси и все настройки — в README."""
        missing = [v for v in (API_STATUS, API_TASK, API_DONE, API_UPLOAD, CREDS_FLAG, *SRV_KEYS, *EMP_KEYS)
                   if v not in README]
        self.assertEqual(missing, [], "нет в README.md")

    def test_env_example(self):
        """Кр. 7: настройки — в registry.env.example; учётки BB там нет."""
        missing = [v for v in (*SRV_KEYS, *EMP_KEYS) if v not in ENV_EXAMPLE]
        self.assertEqual(missing, [], "нет в registry.env.example")
        self.assertIsNone(re.search(r"^\s*#?\s*BITBUCKET_(USERNAME|PASSWORD)\s*=", ENV_EXAMPLE, re.M))

    def test_settings_not_secret_names(self):
        """Имена настроек не похожи на секретные — иначе load_env_file их пропустит (SECRET_KEY в app.py)."""
        pat = re.search(r"SECRET_KEY\s*=\s*re\.compile\(r\"([^\"]+)\"", APP_SRC).group(1)
        self.assertEqual([k for k in (*SRV_KEYS, *EMP_KEYS) if re.search(pat, k, re.I)], [])


if __name__ == "__main__":
    unittest.main()
