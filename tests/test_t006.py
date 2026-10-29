"""T-006: история лава-болла у сотрудника на сервере и история запросов.
Контракт (от тимлида):
  GET  /api/ai/history                       -> {"msgs": [{"role", "content"}], "queries": [...]}; без сессии — 401
  POST /api/ai/history {"action": "reset"}         — очищает msgs, queries остаются; ответ как у GET
  POST /api/ai/history {"action": "clear_queries"} — очищает queries; другое action -> 400
  POST /api/llm при успехе сохраняет msgs (user/assistant из запроса + ответ, последние 40) и последний
  user-текст в начало queries (strip, без повторов, максимум 30); при ошибке модели — ничего.
Модель — мок HTTP-сервер на localhost (GIGACHAT_API_URL), GigaCode — фейковый скрипт. Данные выдуманные."""
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import APP, ROOT, Server, backend_text, clean_env, frontend_text, teams_for# noqa: E402

API_HIST = "/api/ai/history"
MAX_MSGS, MAX_QUERIES = 40, 30
HTML = frontend_text()
APP_SRC = backend_text()

ADMIN_ARGS = ["--admin-pin-stdin"]
ADMIN_PIN = "71625348"
LEAD = "Ведущий Арсений Тестович"
EMP = "Опросова Вера Тестовна"
EMP2 = "Соседов Илья Тестович"
PINS = {LEAD: "36925814", EMP: "4321", EMP2: "86753"}
PEOPLE = [
    {"name": LEAD, "email": "vedushiy@example.test", "role": "teamlead", "active": True},
    {"name": EMP, "email": "oprosova@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": EMP2, "email": "sosedov@example.test", "role": "employee", "team": LEAD, "active": True},
]


# ---------------------------------------------------------------- мок шлюза модели
class MockModel:
    """Отвечает как /chat/completions: «Ответ модели №N»; fail=True — HTTP 400 (без повторов у сервера)."""

    def __init__(self):
        self.fail = False
        self.n = 0
        self.requests = []
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                mock.requests.append(json.loads(self.rfile.read(n) or b"{}"))
                if mock.fail:
                    data, code = b'{"error":"bad request (mock)"}', 400
                else:
                    mock.n += 1
                    data = json.dumps({"choices": [{"message": {"content": f"Ответ модели №{mock.n}"}}]},
                                      ensure_ascii=False).encode("utf-8")
                    code = 200
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


FAKE_GC = """#!{py}
import sys
sys.stdin.read()
print("Фейковый ответ GigaCode")
"""


def setup_people(s):
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
    return adm


def login_all(s):
    sid = {"admin": s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})[1]["session"]}
    for name, pin in PINS.items():
        code, r = s.call("POST", "/api/login", {"name": name, "pin": pin})
        assert code == 200, (name, r)
        sid[name] = r["session"]
    return sid


def ask(text, before=()):
    return {"messages": [*before, {"role": "user", "content": text}]}


class Base(unittest.TestCase):
    storage = "json"

    @classmethod
    def setUpClass(cls):
        cls.mock = MockModel()
        cls.gc_dir = tempfile.TemporaryDirectory()
        exe = Path(cls.gc_dir.name) / "gigacode"
        exe.write_text(FAKE_GC.format(py=sys.executable), "utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        cls.env = {"GIGACHAT_API_URL": cls.mock.url, "REG_GIGACODE_BIN": str(exe),
                   "PATH": cls.gc_dir.name + os.pathsep + os.environ.get("PATH", "")}
        try:
            cls.s = Server(cls.storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env=cls.env)
        except Exception:
            cls.mock.stop()
            raise
        try:
            setup_people(cls.s)
            cls.sid = login_all(cls.s)
        except Exception:
            cls.s.stop()
            cls.mock.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()
        cls.mock.stop()
        cls.gc_dir.cleanup()

    def setUp(self):
        self.mock.fail = False
        for who in (EMP, EMP2, LEAD, "admin"):   # каждый тест — с чистой историей
            self.post_hist(who, "reset")
            self.post_hist(who, "clear_queries")

    def hist(self, who):
        code, r = self.s.call("GET", API_HIST, session=self.sid[who])
        self.assertEqual(code, 200, r)
        return r

    def post_hist(self, who, action):
        code, r = self.s.call("POST", API_HIST, {"action": action}, session=self.sid[who])
        self.assertEqual(code, 200, r)
        return r

    def llm(self, who, body, ok=True):
        code, r = self.s.call("POST", "/api/llm", body, session=self.sid[who])
        if ok:
            self.assertEqual(code, 200, r)
        return code, r


class HistoryTest(Base):
    def test_shape_and_empty(self):
        r = self.hist(EMP)
        self.assertEqual(r, {"msgs": [], "queries": []})

    def test_requires_session(self):
        """Критерий 6: без сессии и с недействительной сессией — 401, и на чтение, и на запись."""
        n = len(self.mock.requests)
        for sid in (None, "garbage-not-a-session"):
            self.assertEqual(self.s.call("GET", API_HIST, session=sid)[0], 401, sid)
            self.assertEqual(self.s.call("POST", API_HIST, {"action": "reset"}, session=sid)[0], 401, sid)
            self.assertEqual(self.s.call("POST", "/api/llm", ask("Без входа"), session=sid)[0], 401, sid)
        self.assertEqual(len(self.mock.requests), n, "без входа запрос ушёл в модель")

    def test_saved_after_llm(self):
        """Ответ /api/llm прежний; msgs = запрос (без error) + ответ; запрос — в queries."""
        before = [{"role": "user", "content": "Первый вопрос про выдуманный отчёт"},
                  {"role": "assistant", "content": "Первый ответ"},
                  {"role": "error", "content": "Модель недоступна (тест)"}]
        code, r = self.llm(EMP, ask("  Второй вопрос про выдуманный отчёт  ", before))
        self.assertTrue({"reply", "model", "skill"} <= set(r), r)
        self.assertTrue(r["reply"].startswith("Ответ модели №"))
        h = self.hist(EMP)
        got = [(m["role"], m["content"].strip()) for m in h["msgs"]]
        self.assertEqual(got, [("user", "Первый вопрос про выдуманный отчёт"), ("assistant", "Первый ответ"),
                               ("user", "Второй вопрос про выдуманный отчёт"), ("assistant", r["reply"])])
        self.assertNotIn("error", [m["role"] for m in h["msgs"]])
        self.assertEqual(h["queries"], ["Второй вопрос про выдуманный отчёт"])
        for m in h["msgs"]:
            self.assertEqual(set(m), {"role", "content"})

    def test_isolation(self):
        """Критерий 1: диалог и запросы сотрудника не видны другому — ни в истории, ни в /api/state."""
        self.llm(EMP, ask("Секретный вопрос Опросовой"))
        self.llm(EMP2, ask("Вопрос Соседова"))
        h1, h2 = self.hist(EMP), self.hist(EMP2)
        self.assertEqual(h1["queries"], ["Секретный вопрос Опросовой"])
        self.assertEqual(h2["queries"], ["Вопрос Соседова"])
        self.assertNotIn("Опросовой", json.dumps(h2, ensure_ascii=False))
        self.assertNotIn("Соседова", json.dumps(h1, ensure_ascii=False))
        # подмена через параметры, тело и X-User не помогает
        for path in (API_HIST + "?person=" + urllib.parse.quote(EMP), API_HIST + "?name=" + urllib.parse.quote(EMP)):
            code, r = self.s.call("GET", path, session=self.sid[EMP2], user=EMP)
            self.assertEqual(code, 200, r)
            self.assertNotIn("Опросовой", json.dumps(r, ensure_ascii=False))
        code, r = self.s.call("POST", API_HIST, {"action": "reset", "person": EMP, "name": EMP},
                              session=self.sid[EMP2], user=EMP)
        self.assertEqual(code, 200, r)
        self.assertEqual(self.hist(EMP)["queries"], ["Секретный вопрос Опросовой"])
        self.assertTrue(self.hist(EMP)["msgs"], "reset от соседа стёр чужой диалог")
        _, raw = self.s.raw("GET", "/api/state", session=self.sid[EMP2])
        self.assertNotIn("Секретный вопрос Опросовой", raw)
        _, raw = self.s.raw("GET", "/api/state")
        self.assertNotIn("Секретный вопрос Опросовой", raw)

    def test_admin_own_history(self):
        """У админа — своя история (ключ — служебное имя), чужие не видит."""
        self.llm(EMP, ask("Вопрос сотрудника для изоляции от админа"))
        self.llm("admin", ask("Вопрос администратора"))
        self.assertEqual(self.hist("admin")["queries"], ["Вопрос администратора"])
        self.assertEqual(self.hist(EMP)["queries"], ["Вопрос сотрудника для изоляции от админа"])

    def test_new_login_sees_same_history(self):
        """Критерий 1: после входа «на другом компьютере» (новая сессия) — тот же диалог."""
        self.llm(LEAD, ask("Вопрос тимлида с первого компьютера"))
        code, r = self.s.call("POST", "/api/login", {"name": LEAD, "pin": PINS[LEAD]})
        self.assertEqual(code, 200, r)
        h = self.s.call("GET", API_HIST, session=r["session"])[1]
        self.assertEqual(h, self.hist(LEAD))
        self.assertEqual(h["queries"], ["Вопрос тимлида с первого компьютера"])

    def test_reset_keeps_queries(self):
        """Критерий 4: «Новый диалог» очищает диалог, история запросов остаётся."""
        self.llm(EMP, ask("Запрос до сброса"))
        r = self.post_hist(EMP, "reset")
        self.assertEqual(r, {"msgs": [], "queries": ["Запрос до сброса"]})
        self.assertEqual(self.hist(EMP), r)

    def test_clear_queries_keeps_msgs(self):
        """Критерий 4: историю запросов можно очистить отдельно — диалог не трогается."""
        self.llm(EMP, ask("Запрос для очистки"))
        msgs = self.hist(EMP)["msgs"]
        r = self.post_hist(EMP, "clear_queries")
        self.assertEqual(r, {"msgs": msgs, "queries": []})
        self.assertEqual(self.hist(EMP), r)

    def test_unknown_action_400(self):
        for body in ({"action": "drop_all"}, {"action": ""}, {}, {"action": None}, {"action": ["reset"]}):
            code, r = self.s.call("POST", API_HIST, body, session=self.sid[EMP])
            self.assertEqual(code, 400, (body, r))

    def test_queries_dedup_and_order(self):
        """Критерий 3: новые сверху, без повторов — повтор поднимается наверх."""
        for q in ("Запрос А", "Запрос Б", "Запрос В", "  Запрос А  "):
            self.llm(EMP, ask(q))
        self.assertEqual(self.hist(EMP)["queries"], ["Запрос А", "Запрос В", "Запрос Б"])

    def test_queries_take_last_user_text(self):
        """В queries — только последний user-текст запроса, не ранние сообщения диалога."""
        before = [{"role": "user", "content": "Ранний вопрос"}, {"role": "assistant", "content": "Ранний ответ"}]
        self.llm(EMP, ask("Поздний вопрос", before))
        self.assertEqual(self.hist(EMP)["queries"], ["Поздний вопрос"])

    def test_queries_limit_30(self):
        for i in range(MAX_QUERIES + 5):
            self.llm(EMP2, ask(f"Выдуманный запрос {i:02d}"))
        q = self.hist(EMP2)["queries"]
        self.assertEqual(len(q), MAX_QUERIES)
        self.assertEqual(q[0], f"Выдуманный запрос {MAX_QUERIES + 4:02d}")
        self.assertEqual(q[-1], "Выдуманный запрос 05")

    def test_msgs_limit_40(self):
        """Допущение: хранятся последние 40 сообщений диалога (запрос + ответ)."""
        before = []
        for i in range(25):
            before += [{"role": "user", "content": f"Вопрос {i}"}, {"role": "assistant", "content": f"Ответ {i}"}]
        code, r = self.llm(EMP, ask("Вопрос последний", before))
        full = before + [{"role": "user", "content": "Вопрос последний"}, {"role": "assistant", "content": r["reply"]}]
        msgs = self.hist(EMP)["msgs"]
        self.assertEqual(len(msgs), MAX_MSGS)
        self.assertEqual(msgs, full[-MAX_MSGS:])

    def test_nothing_saved_on_model_error(self):
        """Ошибки не сохраняются: при сбое модели история и запросы не меняются, /api/llm — ошибка."""
        self.llm(EMP, ask("Удачный запрос"))
        before = self.hist(EMP)
        self.mock.fail = True
        code, r = self.llm(EMP, ask("Неудачный запрос", before["msgs"]), ok=False)
        self.assertEqual(code, 400, r)
        self.assertIn("error", r)
        self.assertEqual(self.hist(EMP), before)

    def test_bad_request_saves_nothing(self):
        """Пустой запрос (400 до модели) тоже ничего не сохраняет."""
        before = self.hist(EMP)
        code, _ = self.llm(EMP, {"messages": [{"role": "user", "content": "   "}]}, ok=False)
        self.assertEqual(code, 400)
        self.assertEqual(self.hist(EMP), before)

    def test_gigacode_engine_saves_too(self):
        """Движок GigaCode (фейковый скрипт) — история сохраняется так же."""
        code, r = self.llm(LEAD, {**ask("Вопрос через GigaCode"), "engine": "gigacode"})
        self.assertEqual(r["reply"], "Фейковый ответ GigaCode")
        h = self.hist(LEAD)
        self.assertEqual(h["queries"], ["Вопрос через GigaCode"])
        self.assertEqual(h["msgs"][-1], {"role": "assistant", "content": "Фейковый ответ GigaCode"})

    def test_cyrillic_and_special_text_roundtrip(self):
        text = "Запрос с «кавычками», 'апострофом', \"двойными\", ; DROP TABLE x; -- и эмодзи-нет — тире"
        self.llm(EMP, ask(text))
        self.assertEqual(self.hist(EMP)["queries"], [text])


class SqliteHistoryTest(HistoryTest):
    storage = "sqlite"


# ---------------------------------------------------------------- переживает перезапуск
class PersistTest(unittest.TestCase):
    def check(self, storage):
        mock = MockModel()
        env = {"GIGACHAT_API_URL": mock.url}
        tmp = tempfile.TemporaryDirectory()
        s = None
        try:
            s = Server(storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env=env, tmp=tmp)
            setup_people(s)
            sid = login_all(s)
            for who, q in ((EMP, "Вопрос до перезапуска"), (EMP2, "Вопрос соседа до перезапуска")):
                code, r = s.call("POST", "/api/llm", ask(q), session=sid[who])
                self.assertEqual(code, 200, r)
            want = {w: s.call("GET", API_HIST, session=sid[w])[1] for w in (EMP, EMP2)}
            s.stop()
            s = Server(storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env=env, tmp=tmp)
            sid = login_all(s)
            for w in (EMP, EMP2):
                self.assertEqual(s.call("GET", API_HIST, session=sid[w]), (200, want[w]), (storage, w))
            self.assertEqual(want[EMP]["queries"], ["Вопрос до перезапуска"])
            return Path(tmp.name)
        finally:
            if s:
                s.stop()
            mock.stop()
            self.addCleanup(tmp.cleanup)

    def test_json(self):
        self.check("json")

    def test_sqlite(self):
        d = self.check("sqlite")
        con = sqlite3.connect(d / "registry.db")
        try:
            names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        finally:
            con.close()
        self.assertTrue(any(n.endswith("ai_history") for n in names), names)


# ---------------------------------------------------------------- Greenplum — статически
class GpStaticTest(unittest.TestCase):
    def test_gp_ddl(self):
        """Критерий 5: в GP — таблица ai_history с DISTRIBUTED BY (person), PK совместим с распределением."""
        p = subprocess.run([sys.executable, "-c", "import json; from registry.store.gp_store import gp_ddl; print(json.dumps(gp_ddl()))"],
                           cwd=ROOT, env=clean_env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        ddl = [x for x in json.loads(p.stdout.strip().splitlines()[-1]) if "ai_history" in x]
        self.assertEqual(len(ddl), 1, ddl)
        self.assertRegex(ddl[0], r"DISTRIBUTED BY \(\s*person\s*\)\s*$")
        pk = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", ddl[0])
        if pk:
            self.assertIn("person", [c.strip() for c in pk.group(1).split(",")])

    def test_no_on_conflict(self):
        """GP6 не знает ON CONFLICT — ни в SQL истории, ни где-либо ещё в app.py."""
        self.assertNotIn("ON CONFLICT", APP_SRC.upper())
        self.assertNotRegex(APP_SRC.upper(), r"INSERT\s+OR\s+REPLACE|UPSERT")
        self.assertIn("ai_history", APP_SRC)


# ---------------------------------------------------------------- фронтенд — статически
class StaticTest(unittest.TestCase):
    def test_no_dialog_in_local_storage(self):
        """Критерий 2: диалог не в localStorage."""
        self.assertNotRegex(HTML, r"store\.(set|get)\(\s*['\"]ai\.msgs")
        self.assertNotRegex(HTML, r"localStorage\.setItem\(\s*['\"]ck\.ai\.msgs")

    def test_old_key_removed(self):
        """Критерий 2: старый ключ ck.ai.msgs удаляется (в try/catch)."""
        m = re.search(r"localStorage\.removeItem\(\s*['\"]ck\.ai\.msgs['\"]\s*\)|store\.del\(\s*['\"]ai\.msgs['\"]",
                      HTML)
        self.assertTrue(m, "нет удаления ck.ai.msgs")
        if m.group(0).startswith("localStorage"):
            before = HTML[max(0, m.start() - 200):m.start()]
            self.assertIn("try", before)
            self.assertNotIn("catch", before[before.rfind("try"):])

    def test_texts(self):
        """Критерии 3 и 4: подписи интерфейса."""
        for s in ("История запросов", "Очистить историю запросов", "Запросов пока нет"):
            self.assertIn(s, HTML)

    def test_history_api_used(self):
        self.assertRegex(HTML, r"['\"`]/?api/ai/history['\"`]")
        self.assertRegex(HTML, r"api\/ai\/history['\"`]\s*,\s*\{\s*action\s*:\s*['\"]reset['\"]")
        self.assertRegex(HTML, r"api\/ai\/history['\"`]\s*,\s*\{\s*action\s*:\s*['\"]clear_queries['\"]")

    def test_reset_calls_server(self):
        """Критерий 4: «Новый диалог» (ai-reset) очищает диалог на сервере."""
        m = re.search(r"'ai-reset'\s*:\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{(.*?)\n\s*'?[\w-]+'?\s*:", HTML, re.S)
        self.assertTrue(m, "нет действия ai-reset")
        code = m.group(1)
        called = re.findall(r"\b(\w+)\(", code)
        bodies = code + "".join(_fn_body(n) for n in called)
        self.assertRegex(bodies, r"ai\/history['\"`]\s*,\s*\{\s*action\s*:\s*['\"]reset['\"]")

    def test_query_click_fills_input_without_send(self):
        """Критерий 3: клик по запросу подставляет текст в поле ввода, не отправляя."""
        acts = re.findall(r"\n\s*'?([\w-]+)'?\s*:\s*(?:async\s*)?\(?\w*\)?\s*=>\s*(\{.*?\}|[^\n]*),?\s*(?=\n)", HTML, re.S)
        acts = [a for a in acts if "aiIn" in a[1] and re.search(r"quer", a[1], re.I) and a[0] != "ai-ex"]
        self.assertTrue(acts, "нет действия, подставляющего запрос из истории в #aiIn")
        for name, code in acts:
            self.assertNotIn("aiSend(", code, name)

    def test_logout_hides_dialog(self):
        """Критерий 2: при выходе диалог исчезает с экрана."""
        b = _fn_body("logout")
        self.assertRegex(b, r"AI\.msgs\s*=\s*\[\]|aiClear\(|aiForget\(|AI\.queries\s*=\s*\[\]|aiResetLocal\(",
                         "logout() не очищает диалог на экране")


    def test_history_loaded_after_login_and_restore(self):
        """Критерий 1: после входа и после восстановления сессии (F5) диалог берётся с сервера."""
        loaders = [n for n in re.findall(r"function\s+(\w+)\s*\(", HTML)
                   if re.search(r"api\(\s*['\"`]/?api/ai/history['\"`]\s*\)", _fn_body(n))]
        self.assertTrue(loaders, "нет функции с GET /api/ai/history")
        for name in ("login", "restoreSession"):
            self.assertTrue(any(l + "(" in _fn_body(name) for l in loaders), f"{name}() не загружает историю")

    def test_401_hides_dialog(self):
        """Критерий 2: при 401 диалог тоже исчезает с экрана."""
        m = re.search(r"401[^\n]*", _fn_body("api"))
        self.assertTrue(m)
        self.assertRegex(m.group(0), r"AI\.msgs\s*=\s*\[\]|aiForget\(|aiClear\(")

    def test_new_css_classes_no_conflict(self):
        """Урок: новые классы не совпадают с существующими правилами (как .busy у заставки)."""
        self.assertNotRegex(HTML, r"(?m)^\s*\.show\s*\{[^}]*display\s*:\s*none", "глобальный .show скрывает элементы")
        rules = re.findall(r"(?m)^([^{}\n]*\.ai-qlist[^{}\n]*)\{", HTML)
        self.assertTrue(rules, "нет стилей .ai-qlist")


def _fn_body(name):
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", HTML) \
        or re.search(r"const\s+" + re.escape(name) + r"\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*\{", HTML)
    if not m:
        return ""
    i, depth = m.end(), 1
    while depth and i < len(HTML):
        depth += {"{": 1, "}": -1}.get(HTML[i], 0)
        i += 1
    return HTML[m.end():i - 1]


if __name__ == "__main__":
    unittest.main()
