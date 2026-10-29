"""T-012: база знаний — одна, в настройках владельца; узлы онтологии; отправка карточки через PR (прямой git+REST).

Каркас — test_t010 (BBBase: сервер реестра + прокси сотрудника + REST-фейк Bitbucket). БЗ — локальный
bare-репозиторий, засеянный ontology.yml; адрес — в REG_KB_REPO_URL у владельца (локальный путь — git
без учётки). Постоянный клон — REG_KB_CLONE у сотрудника: переиспользуется между отправками, при сбое
push/PR — переклонируется. Все данные выдуманные; учётка BB — только в памяти прокси.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import APP, ROOT, Server, clean_env, free_port  # noqa: E402
import test_t010 as t10  # noqa: E402
from fake_bb_rest import FakeBitbucket  # noqa: E402
from test_t010 import (ADMIN_ARGS, ADMIN_PIN, APPROVED, BBBase, BB_BASE, BB_LOGIN, BB_PASS, CARDS, DECISION,  # noqa: E402
                       EXE, LEAD, PEOPLE, PINS, POSTANOVKA, RESULT, SRV_ENV, WORK, git_env)

KB_STATUS, KB_ONTOLOGY = "/api/kb/status", "/api/kb/ontology"
KB_TASK, KB_UPLOAD, KB_DONE = "/api/kb/task", "/api/kb/upload", "/api/kb/done"
KB_PROJECT, KB_REPO = "kbproj", "kb-cards"
KB_URL = None   # адрес БЗ — путь к локальному bare (назначается в setUpClass)
KB_PARTS = f"{KB_PROJECT}/{KB_REPO}"   # проект/репозиторий для REST-фейка
ONTOLOGY_YML = ("Онтология базы знаний:\n"
                "    Общие вопросы:\n"
                "    Аудит моделей:\n"
                "        Метрики качества:\n")
ONTOLOGY_NODES = [{"name": "Онтология базы знаний", "path": "Онтология базы знаний",
                   "children": [{"name": "Общие вопросы", "path": "Онтология базы знаний/Общие вопросы",
                                 "children": []},
                                {"name": "Аудит моделей", "path": "Онтология базы знаний/Аудит моделей",
                                 "children": [{"name": "Метрики качества",
                                               "path": "Онтология базы знаний/Аудит моделей/Метрики качества",
                                               "children": []}]}]}]
HIST_KB = "Карточка отправлена в базу знаний"
NODE = "Онтология базы знаний/Общие вопросы"


def seed_bare(path, files, env, branch="master"):
    """Bare-репозиторий с файлами в ветке — «база знаний» для тестов."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = path.parent / (path.name + "-seed")
    subprocess.run(["git", "init", "--quiet", str(seed)], check=True, env={**os.environ, **env},
                   capture_output=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=seed, check=True,
                   env={**os.environ, **env}, capture_output=True)
    for name, text in files.items():
        (seed / name).write_text(text, "utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True, env={**os.environ, **env}, capture_output=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "Начало выдуманной БЗ"], cwd=seed, check=True,
                   env={**os.environ, **env}, capture_output=True)
    subprocess.run(["git", "init", "--quiet", "--bare", str(path)], check=True,
                   env={**os.environ, **env}, capture_output=True)
    subprocess.run(["git", "--git-dir", str(path), "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
                   check=True, env={**os.environ, **env}, capture_output=True)
    subprocess.run(["git", "push", "--quiet", str(path), f"{branch}:{branch}"], cwd=seed, check=True,
                   env={**os.environ, **env}, capture_output=True)
    shutil.rmtree(seed)


class KbRepo:
    """Обёртка над bare-репозиторием БЗ (для проверок содержимого)."""

    def __init__(self, path, env):
        self.path, self.env = Path(path), env

    def git(self, *args):
        p = subprocess.run(["git", "-c", "core.quotePath=false", "--git-dir", str(self.path), *args],
                           env={**os.environ, **self.env}, capture_output=True, text=True,
                           encoding="utf-8", stdin=subprocess.DEVNULL)
        if p.returncode:
            raise RuntimeError(f"git {args}: {p.stderr}")
        return p.stdout

    def branches(self):
        return set(self.git("for-each-ref", "--format=%(refname:short)", "refs/heads").split())

    def show(self, branch, path):
        return self.git("show", f"{branch}:{path}")

    def merge(self, branch, into):
        """«Pull request влит»: целевая ветка сдвигается на ветку отправки."""
        self.git("update-ref", f"refs/heads/{into}", f"refs/heads/{branch}")


class KbBase(BBBase):
    """Тот же каркас, что T-010, плюс: БЗ у владельца (REG_KB_REPO_URL) и постоянный клон у сотрудника."""

    ontology = ONTOLOGY_YML

    @classmethod
    def server_env(cls):
        return {**SRV_ENV, "REG_KB_REPO_URL": str(cls.work / KB_PROJECT / f"{KB_REPO}.git"),
                "REG_KB_BRANCH": "master"}

    @classmethod
    def proxy_settings(cls, **extra):
        return super().proxy_settings(REG_KB_CLONE=str(cls.work / "kb-clone"), **extra)   # + git_env из BBBase

    @classmethod
    def setUpClass(cls):
        super().setUpClass()   # сервер (REG_KB_REPO_URL из server_env), прокси, REST-фейк BBBase
        cls.kb_repo_path = cls.work / KB_PROJECT / f"{KB_REPO}.git"   # проект/репо — из пути (repo_url_parts)
        files = {"ontology.yml": cls.ontology} if cls.ontology else {"README.md": "# Выдуманная БЗ без онтологии\n"}
        seed_bare(cls.kb_repo_path, files, cls.bare.env)
        cls.rest.repos[KB_PARTS] = cls.kb_repo_path   # PR для БЗ — в её bare
        cls.clone = Path(cls.work) / "kb-clone"

    def kb_repo(self):
        return KbRepo(self.kb_repo_path, self.bare.env)

    def wipe_clone(self):
        import shutil
        shutil.rmtree(self.clone, ignore_errors=True)

    def ok_send(self, t, card, node=NODE, who=EXE, proxy=None):
        p = proxy or self.proxy
        c, r = p.call("POST", KB_UPLOAD, {"kind": t["kind"], "id": t["id"], "path": card["path"],
                                          "node": node}, session=self.sid[who])
        self.assertEqual(c, 200, r)
        return r


class KbFlowTest(KbBase):
    """Полный поток: статус, онтология, отправка карточки в узел, постоянный клон, самооздоровление."""

    def test_status_via_proxy(self):
        """Кр. 1: статус БЗ через прокси — доступно, цель от сервера, ответ без учётки."""
        m = self.rest.mark()
        code, r = self.proxy.call("GET", KB_STATUS, session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("available"), True, r)
        self.assertEqual(r.get("via"), "proxy", r)
        self.assertEqual(r.get("target", {}).get("repo_url"), str(self.kb_repo_path), r)
        self.assertEqual(self.rest.since(m), [], "статус не ходит в BB")
        self.no_secrets(self.proxy.responses[-1], KB_STATUS)

    def test_ontology(self):
        """Кр. 1: онтология единственной БЗ — дерево узлов (парсит MCP-сервер), без параметров."""
        code, r = self.proxy.call("GET", KB_ONTOLOGY, session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        self.assertEqual(r.get("nodes"), ONTOLOGY_NODES, r)
        self.no_secrets(self.proxy.responses[-1], KB_ONTOLOGY)

    def test_send_card_creates_clone_and_pr(self):
        """Кр. 1, 2: карточка — в папку узла, PR; постоянный клон создаётся; ссылка — в модели карточки."""
        self.wipe_clone()
        t, cards = self.task_in(status=APPROVED)
        card = cards[0]
        m = self.rest.mark()
        r = self.ok_send(t, card)
        self.assertTrue(r["pr_url"].startswith(self.rest.base + "/"), r)
        self.assertIn("/browse/", r["file_url"])
        self.assertIn("at=master", r["file_url"])
        self.assertIn(urllib.request.quote(NODE), r["file_url"], "узла нет в ссылке на файл")
        # постоянный клон создан и живёт в REG_KB_CLONE
        self.assertTrue((self.clone / ".git").is_dir(), "клон не создан")
        # репозиторий: файл карточки в папке узла, содержимое — формат реестра
        repo = self.kb_repo()
        fname = Path(card["path"]).name
        pr_branch = next(b for b in repo.branches() if b.startswith(f"kb-card/once-{t['id']}-"))
        self.assertRegex(pr_branch, rf"^kb-card/once-{t['id']}-\d{{8}}-\d{{6}}$")
        self.assertEqual(repo.show(pr_branch, f"{NODE}/{fname}"),
                         f"# {card['title'].strip()}\n\n{card['body'].strip()}\n")
        # задача на сервере: у карточки — bb с узлом и ссылками
        after = self.get(t)
        e = next(x for x in after["cards"] if x["path"] == card["path"])
        bb = e.get("bb") or {}
        self.assertEqual(bb.get("node"), NODE)
        self.assertEqual(bb.get("file_url"), r["file_url"])
        self.assertEqual(bb.get("pr_url"), r["pr_url"])
        self.assertEqual(bb.get("by"), EXE)
        self.assertIn(HIST_KB, after["history"][-1]["action"])
        self.assertIn(str(r["pr_id"]), after["history"][-1]["action"])
        # REST: один PR с правильной учёткой, проектом и ветками
        recs = self.rest.since(m)
        self.assertEqual(len(recs), 1, "один REST-запрос pull request на отправку")
        self.assertTrue(recs[0]["auth_ok"], "REST получил не ту учётку")
        pa = recs[0]["payload"]
        self.assertEqual((pa["fromRef"]["id"], pa["toRef"]["id"]),
                         (f"refs/heads/{pr_branch}", "refs/heads/master"))
        self.assertTrue((self.clone / ".git").is_dir(), "постоянный клон должен жить после отправки")
        self.no_secrets(self.proxy.responses[-1], KB_UPLOAD)

    def test_send_reuses_clone(self):
        """Кр. 2: повторная отправка переиспользует клон — в журнале MCP один clone на две отправки."""
        self.wipe_clone()
        t, cards = self.task_in(status=APPROVED)
        self.ok_send(t, cards[0])
        marker = self.clone / ".git" / "reuse-marker"   # маркер: переклонирование стёрло бы его
        marker.write_text("клон жив", "utf-8")
        self.next_second()
        r2 = self.ok_send(t, cards[0])
        self.assertTrue(marker.exists(), "клон переклонирован — должен переиспользоваться")
        self.assertTrue((self.clone / ".git").is_dir())
        after = self.get(t)
        e = next(x for x in after["cards"] if x["path"] == cards[0]["path"])
        self.assertEqual(e["bb"]["pr_url"], r2["pr_url"], "ссылка в карточке — на новый PR")

    def test_send_broken_clone_selfheals(self):
        """Кр. 2: сломанная папка клона (без .git) — переклонируется, отправка проходит."""
        self.wipe_clone()
        self.clone.mkdir(parents=True)
        (self.clone / "мусор").write_text("не git", "utf-8")
        t, cards = self.task_in(status=APPROVED)
        r = self.ok_send(t, cards[0])
        self.assertTrue((self.clone / ".git").is_dir(), "клон не восстановлен")
        self.assertIn("/browse/", r["file_url"])
        self.assertTrue((self.clone / ".git").is_dir())

    def test_send_after_merge_says_already_there(self):
        """Кр. 2: PR влит, клон протух — PR «без изменений» падает, клон переклонируется,
        свежий master уже содержит карточку — точный ответ «уже лежит»."""
        self.wipe_clone()
        t, cards = self.task_in(status=APPROVED)
        r1 = self.ok_send(t, cards[0])
        repo = self.kb_repo()
        pr_branch = next(b for b in repo.branches() if b.startswith(f"kb-card/once-{t['id']}-"))
        repo.merge(pr_branch, "master")   # «влитие» PR — удалённая ветка ушла вперёд клона
        self.next_second()
        m = self.rest.mark()
        c, r2 = self.proxy.call("POST", KB_UPLOAD, {"kind": t["kind"], "id": t["id"],
                                                    "path": cards[0]["path"], "node": NODE},
                                session=self.sid[EXE])
        self.assertEqual(c, 400, r2)
        self.assertIn("уже лежит", r2.get("error", ""), r2)
        self.assertEqual(self.rest.since(m), [], "«уже лежит» — pull request не создаётся")
        # карточка не переписана — ссылка на первый PR осталась
        after = self.get(t)
        e = next(x for x in after["cards"] if x["path"] == cards[0]["path"])
        self.assertEqual(e["bb"]["pr_url"], r1["pr_url"])

    def test_send_not_executor_forbidden(self):
        """Кр. 3: не исполнитель — 403 от сервера реестра (прокси передаёт как есть)."""
        self.wipe_clone()
        t, cards = self.task_in(status=APPROVED)
        c, r = self.proxy.call("POST", KB_UPLOAD, {"kind": t["kind"], "id": t["id"], "path": cards[0]["path"],
                                                   "node": NODE}, session=self.sid[LEAD])
        self.assertEqual(c, 403, r)

    def test_send_wrong_status_forbidden(self):
        """Кр. 3: не «Согласована» — 403 (как в T-010)."""
        t, _ = self.task_in(status=WORK)
        c, r = self.proxy.call("POST", KB_UPLOAD, {"kind": t["kind"], "id": t["id"], "path": "cards/1_x.md",
                                                   "node": NODE}, session=self.sid[EXE])
        self.assertEqual(c, 403, r)
        self.assertIn("Согласована", r.get("error", ""))

    def test_send_bad_node_rejected(self):
        """Кр. 3: плохой узел — 400 прокси до всякого git."""
        t, cards = self.task_in(status=APPROVED)
        c, r = self.proxy.call("POST", KB_UPLOAD, {"kind": t["kind"], "id": t["id"], "path": cards[0]["path"],
                                                   "node": "../вверх"}, session=self.sid[EXE])
        self.assertEqual(c, 400, r)


class KbNoOntologyTest(KbBase):
    """БЗ без ontology.yml — понятная 400 с именем файла."""

    ontology = ""   # без онтологии: сид — один README

    def test_ontology_missing(self):
        code, r = self.proxy.call("GET", KB_ONTOLOGY, session=self.sid[EXE])
        self.assertEqual(code, 400, r)
        self.assertIn("ontology.yml", r.get("error", ""))


class ServerSideTest(KbBase):
    """Серверная сторона: прямые запросы, права, валидация, запись ссылки, миграция старых карточек."""

    def test_status_and_direct_endpoints(self):
        """Сервер в BB не ходит: онтология и отправка напрямую — 400 с подсказкой; статус — цель из настроек."""
        for method, path, body in (("GET", KB_ONTOLOGY, None), ("POST", KB_UPLOAD, {"kind": "once", "id": 1})):
            code, r = self.s.call(method, path, body, session=self.sid[EXE])
            self.assertEqual(code, 400, (method, path, r))
            self.assertIn("прокси", r.get("error", ""), (method, path))
        code, r = self.s.call("GET", KB_STATUS)
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("configured"), True, r)
        self.assertEqual(r.get("target", {}).get("repo_url"), str(self.kb_repo_path), r)
        self.assertEqual(r.get("target", {}).get("branch"), "master", r)

    def test_kb_task_contract(self):
        """Кр. 3: /api/kb/task — права, карточка, цель и узел; ошибки — понятные 400/403."""
        t, cards = self.task_in(status=APPROVED)
        card = cards[0]
        q = f"kind={t['kind']}&id={t['id']}&path={urllib.request.quote(card['path'])}" \
            f"&node={urllib.request.quote(NODE)}"
        code, r = self.s.call("GET", f"{KB_TASK}?{q}", session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        self.assertEqual(r["card"]["path"], card["path"])
        self.assertEqual(r["card"]["title"], card["title"])
        self.assertEqual(r["target"], {"repo_url": str(self.kb_repo_path), "branch": "master", "node": NODE})
        self.assertEqual(r["by"], EXE)
        # не исполнитель
        code, r = self.s.call("GET", f"{KB_TASK}?{q}", session=self.sid[LEAD])
        self.assertEqual(code, 403, r)
        # плохой узел: путь наверх и слишком глубоко
        for node in ("../вверх", "а/б/в/г/д/е/ж/з/и"):
            b = q.replace("node=" + urllib.request.quote(NODE), "node=" + urllib.request.quote(node))
            code, r = self.s.call("GET", f"{KB_TASK}?{b}", session=self.sid[EXE])
            self.assertEqual(code, 400, (node, r))
        # кириллица и вложенность в узле — разрешены (каждый узел онтологии — папка)
        deep = "Онтология базы знаний/Аудит моделей/Метрики качества"
        b = q.replace("node=" + urllib.request.quote(NODE), "node=" + urllib.request.quote(deep))
        code, r = self.s.call("GET", f"{KB_TASK}?{b}", session=self.sid[EXE])
        self.assertEqual(code, 200, (deep, r))
        # чужой карточки в задаче нет
        b = q.replace(urllib.request.quote(card["path"]), urllib.request.quote("cards/9_no.md"))
        code, r = self.s.call("GET", f"{KB_TASK}?{b}", session=self.sid[EXE])
        self.assertEqual(code, 400, r)

    def test_kb_done_writes_link_into_card(self):
        """Кр. 1, 3: /api/kb/done — ссылка пишется в карточку атомарно, с историей и версией."""
        t, cards = self.task_in(status=APPROVED)
        card = cards[0]
        before = self.get(t)
        body = {"kind": t["kind"], "id": t["id"], "path": card["path"], "node": NODE,
                "file_url": "https://bb.example.test/browse/x?at=master",
                "pr_url": "https://bb.example.test/pull-requests/7", "pr_id": 7}
        code, r = self.s.call("POST", KB_DONE, body, session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        after = self.get(t)
        self.assertEqual(after["_v"], before["_v"] + 1)
        e = next(x for x in after["cards"] if x["path"] == card["path"])
        self.assertEqual(e["bb"]["file_url"], body["file_url"])
        self.assertEqual(e["bb"]["node"], NODE)
        self.assertIn(HIST_KB, after["history"][-1]["action"])
        # служебное поле bb у карточки не подделать через /api/task: cards — служебное, правка игнорирует его
        code, r = self.s.call("POST", "/api/task", {"task": {**after, "cards": [{"path": card["path"],
                                                                                 "bb": {"pr_id": 999}}]},
                                                    "expect_v": after["_v"], "action_code": "edit"},
                              session=self.sid[EXE])
        self.assertEqual(code, 200, r)
        edited = self.get(t)
        e = next(x for x in edited["cards"] if x["path"] == card["path"])
        self.assertEqual(e["bb"]["pr_id"], 7, "подделанный bb через /api/task прошёл")

    def test_kb_done_validation(self):
        """/api/kb/done: плохие ссылки, узел и pr_id — 400; не исполнитель — 403; карточка не меняется."""
        t, cards = self.task_in(status=APPROVED)
        base = {"kind": t["kind"], "id": t["id"], "path": cards[0]["path"], "node": NODE,
                "file_url": "https://bb.example.test/browse/x?at=master",
                "pr_url": "https://bb.example.test/pull-requests/7", "pr_id": 7}
        for k, v in (("pr_url", "javascript:alert(1)"), ("file_url", "not-a-url"), ("pr_id", 0),
                     ("pr_id", "x"), ("node", "../вверх"), ("path", "cards/9_no.md")):
            code, r = self.s.call("POST", KB_DONE, {**base, k: v}, session=self.sid[EXE])
            self.assertEqual(code, 400, (k, v, r))
        code, r = self.s.call("POST", KB_DONE, base, session=self.sid[LEAD])
        self.assertEqual(code, 403, r)

    def test_old_string_cards_normalized(self):
        """Миграция: старые строковые cards[] отдаются объектами {path}, на диске не трогаются."""
        jdir = self.s.dir / "json"
        (jdir / "once").mkdir(parents=True, exist_ok=True)
        old = {"kind": "once", "id": 9, "_v": 3, "status": "Согласована", "title": "Старая задача",
               "cards": ["cards/9_Старая карточка.md"], "history": []}
        (jdir / "once" / "0009.json").write_text(json.dumps(old, ensure_ascii=False), "utf-8")
        tasks = self.s.call("GET", "/api/state")[1]["tasks"]
        t = next(x for x in tasks if x["id"] == 9)
        self.assertEqual(t["cards"], [{"path": "cards/9_Старая карточка.md"}], t["cards"])
        raw = json.loads((jdir / "once" / "0009.json").read_text("utf-8"))
        self.assertEqual(raw["cards"], ["cards/9_Старая карточка.md"])


class NotConfiguredTest(unittest.TestCase):
    """Без REG_KB_REPO_URL статус — «не настроена», задача и отправка — понятные 400."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._work = tempfile.TemporaryDirectory()
        cls.s = Server("json", args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env={**SRV_ENV},
                       tmp=cls._work)

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()
        cls._work.cleanup()

    def test_status_not_configured(self):
        code, r = self.s.call("GET", KB_STATUS)
        self.assertEqual(code, 200, r)
        self.assertIs(r.get("configured"), False, r)
        self.assertIn("REG_KB_REPO_URL", r.get("reason", ""))
        self.assertNotIn("target", r)


class EvilServerKbTest(unittest.TestCase):
    """Прокси не доверяет серверу реестра: подменённые цель/карточка в ответе /api/kb/task — 400."""

    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from test_t010 import Proxy
        cls._work = tempfile.TemporaryDirectory()
        cls.work = Path(cls._work.name).resolve()
        (cls.work / "repos").mkdir()
        cls.token = "evil-kb-token"
        reg = cls

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, code, obj):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith(KB_STATUS):
                    return self._j(200, {"available": False, "via": "proxy", "configured": True,
                                         "target": {"repo_url": str(cls.kb_repo_path), "branch": "master"},
                                         "reason": "x"})
                if self.path.startswith(KB_TASK):
                    return self._j(200, {  # подмена: другой узел и чужая карточка
                        "task": {"kind": "once", "id": 1, "title": "T"},
                        "card": {"path": "cards/1_chuzhaya.md", "title": "Чужая?", "body": "Чужой текст"},
                        "target": {"repo_url": str(cls.kb_repo_path), "branch": "master", "node": "Подменённый узел"},
                        "by": EXE})
                return self._j(404, {"error": "not found"})

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.reg_port = cls.httpd.socket.getsockname()[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.kb_repo_path = cls.work / KB_PROJECT / f"{KB_REPO}.git"
        seed_bare(cls.kb_repo_path, {"ontology.yml": ONTOLOGY_YML}, {})
        cls.proxy = Proxy(f"127.0.0.1:{cls.reg_port}", cls.work, cls.token,
                          env={"REG_BB_BASE_URL": "http://127.0.0.1:1", "REG_BB_TIMEOUT": "30",
                               "REG_KB_CLONE": str(cls.work / "kb-clone"), **git_env(cls.work)})

    @classmethod
    def tearDownClass(cls):
        cls.proxy.stop()
        cls.httpd.shutdown()
        cls._work.cleanup()

    def test_substituted_reply_rejected(self):
        """Подменённый узел/карточка в ответе сервера — прокси отвечает 400, в git не ходит."""
        code, r = self.proxy.call("POST", KB_UPLOAD, {"kind": "once", "id": 1, "path": "cards/1_moya.md",
                                                      "node": NODE}, session="s1")
        self.assertEqual(code, 400, r)
        self.assertIn("Сервер реестра прислал недопустимые данные", r.get("error", ""))


if __name__ == "__main__":
    unittest.main()
