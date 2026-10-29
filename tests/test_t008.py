"""T-008: помощник — скиллы по ролям, контекст пишущего, результат и карточка знаний.
Критерии приёмки — docs/backlog.md (T-008); имена полей — от dev-t007 (константы ниже).
Модель — мок HTTP-сервер на localhost (GIGACHAT_API_URL), GigaCode — фейковый скрипт в PATH. Данные выдуманные."""
import json
import os
import re
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, Server, frontend_text# noqa: E402

# ---- контракт (правится только здесь)
ADMIN_ARGS = ["--admin-pin-stdin"]
POSTANOVKA_SKILL = "postanovka"
NEW_SKILL = "rezultat"           # новый скилл «Результат и карточка знаний» (без roles — доступен всем)
SKILL_TEXT_KEYS = ("greeting", "examples")   # в GET /api/llm у каждого скилла — свои приветствие и примеры
EMP_LABEL_BAD = "Тимлид:"        # в stdin gigacode у реплик сотрудника этой метки быть не должно
LABELS = {"employee": "Сотрудник (", "teamlead": "Тимлид (", "deputy": "Заместитель тимлида (",
          "admin": "Администратор реестра"}
ACTION_KEY = "action_code"
HTML = frontend_text()
SKILLS_DIR = ROOT / "skills"

ADMIN_PIN = "63829104"
LEAD = "Наставников Глеб Тестович"
DEP = "Помощникова Вера Тестовна"
EMP = "Работяга Илья Тестович"
EMP2 = "Сменщикова Ольга Тестовна"
LEAD2 = "Посторонний Фёдор Тестович"
EMP3 = "Далёкая Майя Тестовна"
PINS = {LEAD: "12121212", DEP: "23232323", EMP: "3434", EMP2: "45454", LEAD2: "56565656", EMP3: "6767"}
TEAMS = [{"id": "t8-a", "name": "Выдуманная команда Т8", "lead": LEAD, "deputy": DEP},
         {"id": "t8-b", "name": "Выдуманная соседняя команда", "lead": LEAD2, "deputy": ""}]
PEOPLE = [{"name": n, "email": f"p{i}@example.test", "team": tm, "active": True}
          for i, (n, tm) in enumerate(((LEAD, "t8-a"), (DEP, "t8-a"), (EMP, "t8-a"), (EMP2, "t8-a"),
                                       (LEAD2, "t8-b"), (EMP3, "t8-b")))]
RESULT = {"result": "Обзор", "result_desc": "Выдуманный обзор готов", "report_event": "", "report_date": ""}
DECISION = {"block": "KM-01", "level": "ЦК", "bz_save": "да", "bz_reason": "Пригодится команде"}

# задачи: (ключ, кто ставит, исполнитель, замещающий, до какого статуса)
T_EMP_WORK = "Выдуманная задача Т8 исполнителя в работе"
T_EMP_APPR = "Выдуманная задача Т8 исполнителя согласована"
T_EMP2_SUB = "Выдуманная задача Т8 где он замещающий"
T_TEAM_OTHER = "Выдуманная задача Т8 команды без него"
T_FOREIGN = "Выдуманная задача Т8 чужой команды"


class MockModel:
    """Отвечает как /chat/completions и запоминает запросы."""

    def __init__(self):
        self.requests = []
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                mock.requests.append(json.loads(self.rfile.read(n) or b"{}"))
                data = json.dumps({"choices": [{"message": {"content": "Ответ мока Т8"}}]},
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(200)
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
import json, sys
calls = {out!r}
data = sys.stdin.read()
with open(calls, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": data}}, ensure_ascii=False) + "\\n")
print("Фейковый ответ GigaCode Т8")
"""


class Base(unittest.TestCase):
    storage = "json"

    @classmethod
    def setUpClass(cls):
        cls.mock = MockModel()
        cls.gc_dir = tempfile.TemporaryDirectory()
        cls.gc_calls = Path(cls.gc_dir.name) / "calls.jsonl"
        exe = Path(cls.gc_dir.name) / "gigacode"
        exe.write_text(FAKE_GC.format(py=sys.executable, out=str(cls.gc_calls)), "utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        env = {"GIGACHAT_API_URL": cls.mock.url, "REG_GIGACODE_BIN": str(exe),
               "PATH": cls.gc_dir.name + os.pathsep + os.environ.get("PATH", "")}
        try:
            cls.s = Server(cls.storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env=env)
        except Exception:
            cls.mock.stop()
            cls.gc_dir.cleanup()
            raise
        try:
            cls.setup_registry()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def setup_registry(cls):
        s = cls.s
        code, r = s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
        assert code == 200, r
        cls.sid = {"admin": r["session"]}
        cfg = s.call("GET", "/api/state")[1]["config"]
        cfg["teams"], cfg["people"] = [dict(t) for t in TEAMS], [dict(p) for p in PEOPLE]
        code, r = s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=cls.sid["admin"])
        assert code == 200, r
        for name, pin in PINS.items():
            code, r = s.call("POST", "/api/admin/pin", {"name": name, "pin": pin}, session=cls.sid["admin"])
            assert code == 200, (name, r)
            code, r = s.call("POST", "/api/login", {"name": name, "pin": pin})
            assert code == 200, (name, r)
            cls.sid[name] = r["session"]
        cls.tasks = {}
        for title, author, ex, sub, status in (
                (T_EMP_WORK, LEAD, EMP, "", "В работе"), (T_EMP_APPR, DEP, EMP, "", "Согласована"),
                (T_EMP2_SUB, LEAD, EMP2, EMP, "Новая"), (T_TEAM_OTHER, DEP, EMP2, "", "Новая"),
                (T_FOREIGN, LEAD2, EMP3, "", "Новая")):
            cls.tasks[title] = cls.make_task(title, author, ex, sub, status)

    @classmethod
    def call_as(cls, who, task, code):
        body = {"task": task, ACTION_KEY: code, "action": "Тест"}
        if task.get("_v") is not None:
            body["expect_v"] = task["_v"]
        c, r = cls.s.call("POST", "/api/task", body, session=cls.sid[who])
        assert c == 200, (code, who, r)
        return r["task"]

    @classmethod
    def make_task(cls, title, author, ex, sub, status):
        t = cls.call_as(author, {"kind": "once", "id": None, "title": title, "description": "Выдумка",
                                 "executor": ex, "substitute": sub, "deadline": "2030-11-30"}, "create")
        if status in ("В работе", "Согласована"):
            t = cls.call_as(ex, t, "take")
        if status == "Согласована":
            t = cls.call_as(ex, {**t, **RESULT}, "to_review")
            t = cls.call_as(author, {**t, **DECISION}, "approve")
        assert t["status"] == status, t
        return t

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()
        cls.mock.stop()
        cls.gc_dir.cleanup()

    # ---- хелперы
    def skills(self, who):
        code, r = self.s.call("GET", "/api/llm", session=self.sid.get(who))
        self.assertEqual(code, 200, r)
        return r

    def new_skill(self):
        if NEW_SKILL:
            return NEW_SKILL
        names = [s["name"] for s in self.skills(EMP)["skills"] if s["name"] != POSTANOVKA_SKILL]
        self.assertEqual(len(names), 1, names)
        return names[0]

    def ask(self, who, text, **extra):
        return self.s.call("POST", "/api/llm", {"messages": [{"role": "user", "content": text}], **extra},
                           session=self.sid[who])

    def system_of(self, req):
        return "\n".join(m["content"] for m in req["messages"] if m["role"] == "system")

    def gc_calls_list(self):
        if not self.gc_calls.exists():
            return []
        return [json.loads(x) for x in self.gc_calls.read_text("utf-8").splitlines() if x.strip()]


# ================================================================ критерий 1: доступ к скиллам
class SkillAccessTest(Base):
    def test_skill_md_declares_roles(self):
        text = (SKILLS_DIR / POSTANOVKA_SKILL / "SKILL.md").read_text("utf-8")
        front = re.match(r"^---\s*\n(.*?)\n---", text, re.S).group(1)
        m = re.search(r"^roles\s*:\s*(.+)$", front, re.M)
        self.assertTrue(m, "в SKILL.md постановки нет поля roles")
        self.assertNotIn("employee", m.group(1))

    def test_employee_list(self):
        r = self.skills(EMP)
        names = [s["name"] for s in r["skills"]]
        self.assertNotIn(POSTANOVKA_SKILL, names)
        self.assertEqual(r["default"], self.new_skill())
        self.assertIn(r["default"], names)

    def test_lead_deputy_admin_list(self):
        new = self.new_skill()
        for who in (LEAD, DEP, LEAD2, "admin"):
            with self.subTest(who=who):
                r = self.skills(who)
                names = [s["name"] for s in r["skills"]]
                self.assertIn(POSTANOVKA_SKILL, names)
                self.assertIn(new, names)
                self.assertEqual(r["default"], POSTANOVKA_SKILL)

    def test_greeting_and_examples_per_skill(self):
        r = self.skills(LEAD)
        by = {s["name"]: s for s in r["skills"]}
        for k in SKILL_TEXT_KEYS:
            with self.subTest(key=k):
                a, b = by[POSTANOVKA_SKILL].get(k), by[self.new_skill()].get(k)
                self.assertTrue(a and b, (k, a, b))
                self.assertNotEqual(a, b)

    def test_employee_postanovka_api_403(self):
        n = len(self.mock.requests)
        code, r = self.ask(EMP, "Поставь задачу про выдуманный отчёт", skill=POSTANOVKA_SKILL)
        self.assertEqual(code, 403, r)
        self.assertRegex(r.get("error", ""), "[А-Яа-яЁё]")
        self.assertEqual(len(self.mock.requests), n, "запрос дошёл до модели")

    def test_employee_postanovka_gigacode_403(self):
        n = len(self.gc_calls_list())
        code, r = self.ask(EMP, "Поставь задачу", skill=POSTANOVKA_SKILL, engine="gigacode")
        self.assertEqual(code, 403, r)
        self.assertRegex(r.get("error", ""), "[А-Яа-яЁё]")
        self.assertEqual(len(self.gc_calls_list()), n, "gigacode запущен")

    def test_employee_default_is_new_skill(self):
        code, r = self.ask(EMP, "Помоги с результатом")
        self.assertEqual(code, 200, r)
        self.assertEqual(r.get("skill"), self.new_skill())

    def test_unknown_skill_not_fallback_to_postanovka(self):
        """Неизвестный скилл у сотрудника не должен молча превратиться в «Постановку»."""
        n, g = len(self.mock.requests), len(self.gc_calls_list())
        for engine in ("api", "gigacode"):
            with self.subTest(engine=engine):
                code, r = self.ask(EMP, "Привет", skill="nonexistent-skill", engine=engine)
                self.assertEqual(code, 400, r)
                self.assertRegex(r.get("error", ""), "[А-Яа-я]")
        self.assertEqual((len(self.mock.requests), len(self.gc_calls_list())), (n, g))

    def test_without_session_list(self):
        """Без сессии — только скиллы без roles, default — новый скилл."""
        code, r = self.s.call("GET", "/api/llm")
        self.assertEqual(code, 200, r)
        self.assertEqual([x["name"] for x in r["skills"]], [self.new_skill()])
        self.assertEqual(r["default"], self.new_skill())

    def test_lead_default_request_is_postanovka(self):
        for who in (LEAD, DEP, "admin"):
            with self.subTest(who=who):
                code, r = self.ask(who, "Поставь выдуманную задачу")
                self.assertEqual(code, 200, r)
                self.assertEqual(r.get("skill"), POSTANOVKA_SKILL)

    def test_lead_both_skills_work(self):
        for sk in (POSTANOVKA_SKILL, self.new_skill()):
            for who in (LEAD, DEP, "admin"):
                with self.subTest(skill=sk, who=who):
                    code, r = self.ask(who, "Привет", skill=sk)
                    self.assertEqual(code, 200, r)
                    self.assertEqual(r.get("skill"), sk)

    def test_without_session(self):
        """Регрессия T-002: POST /api/llm без сессии — 401, модель не вызывается."""
        n = len(self.mock.requests)
        code, r = self.s.call("POST", "/api/llm", {"messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(code, 401, r)
        self.assertEqual(len(self.mock.requests), n)


class SqliteSkillAccessTest(SkillAccessTest):
    storage = "sqlite"


# ================================================================ критерий 2: контекст пишущего
class ContextTest(Base):
    def system_for(self, who):
        n = len(self.mock.requests)
        code, r = self.ask(who, "Какие у меня задачи?", skill=self.new_skill())
        self.assertEqual(code, 200, r)
        self.assertEqual(len(self.mock.requests), n + 1)
        return self.system_of(self.mock.requests[-1])

    def line_with(self, text, title):
        line = next((x for x in text.splitlines() if title in x), None)
        self.assertIsNotNone(line, f"нет задачи «{title}» в контексте")
        return line

    def test_employee_sees_own_tasks(self):
        sys_text = self.system_for(EMP)
        self.assertIn(EMP, sys_text)
        self.assertRegex(sys_text, "[Сс]отрудник")
        for title in (T_EMP_WORK, T_EMP_APPR, T_EMP2_SUB):
            with self.subTest(title=title):
                t = self.tasks[title]
                line = self.line_with(sys_text, title)
                self.assertRegex(line, rf"\b{t['id']}\b", line)
                self.assertIn(t["status"], line)
        self.assertRegex(self.line_with(sys_text, T_EMP2_SUB), "роль пишущего: [^;]*замещающий")
        self.assertRegex(self.line_with(sys_text, T_EMP_WORK), "роль пишущего: [^;]*исполнитель")
        self.assertIn("сотрудник команды «Выдуманная команда Т8»", sys_text)
        for title in (T_TEAM_OTHER, T_FOREIGN):
            self.assertNotIn(title, sys_text)

    def test_lead_sees_team_tasks(self):
        for who, role in ((LEAD, "[Тт]имлид|[Лл]ид"), (DEP, "[Зз]ам")):
            with self.subTest(who=who):
                sys_text = self.system_for(who)
                self.assertIn(who, sys_text)
                self.assertRegex(sys_text, role)
                self.assertIn("Выдуманная команда Т8", sys_text)
                for title in (T_EMP_WORK, T_EMP_APPR, T_TEAM_OTHER):
                    self.assertIn(title, sys_text)
                self.assertNotIn(T_FOREIGN, sys_text)

    def test_lead_role_in_task(self):
        sys_text = self.system_for(LEAD)
        self.assertRegex(self.line_with(sys_text, T_EMP_WORK), "роль пишущего: [^;]*тимлид")
        self.assertRegex(self.line_with(sys_text, T_TEAM_OTHER), "роль пишущего: [^;]*тимлид команды")
        self.assertRegex(self.system_for(DEP), "заместитель тимлида команды «Выдуманная команда Т8»")

    def test_admin_sees_all(self):
        sys_text = self.system_for("admin")
        self.assertIn("Администратор реестра", sys_text)
        for title in (T_EMP_WORK, T_TEAM_OTHER, T_FOREIGN):
            self.assertIn(title, sys_text)

    def test_old_accepted_hidden(self):
        """Принятые — только с done_date не старше 30 дней."""
        from datetime import date
        for title, done, visible in (("Выдуманная принятая давно Т8", "2020-01-01", False),
                                     ("Выдуманная принятая сегодня Т8", date.today().isoformat(), True)):
            with self.subTest(title=title):
                t = self.make_task(title, LEAD, EMP, "", "Новая")
                body = {"task": {**t, "status": "Принята", "done_date": done}, ACTION_KEY: "edit",
                        "action": "Правка: тест", "expect_v": t["_v"]}
                code, r = self.s.call("POST", "/api/task", body, session=self.sid["admin"])
                self.assertEqual(code, 200, r)
                self.assertEqual((title in self.system_for(EMP)), visible)

    def test_other_lead_does_not_see(self):
        sys_text = self.system_for(LEAD2)
        self.assertIn(T_FOREIGN, sys_text)
        for title in (T_EMP_WORK, T_EMP_APPR, T_EMP2_SUB, T_TEAM_OTHER):
            self.assertNotIn(title, sys_text)

    def test_postanovka_also_knows_who(self):
        n = len(self.mock.requests)
        code, r = self.ask(DEP, "Поставь задачу", skill=POSTANOVKA_SKILL)
        self.assertEqual(code, 200, r)
        self.assertIn(DEP, self.system_of(self.mock.requests[n]))

    def test_no_pin_hash_in_context(self):
        sys_text = self.system_for(LEAD)
        self.assertNotIn('"salt"', sys_text)
        self.assertNotIn('"hash"', sys_text)


# ================================================================ критерий 6: GigaCode — подпись ролью
class GigaCodeLabelTest(Base):
    def last_stdin(self, who, text):
        n = len(self.gc_calls_list())
        code, r = self.ask(who, text, engine="gigacode", skill=self.new_skill())
        self.assertEqual(code, 200, r)
        calls = self.gc_calls_list()
        self.assertEqual(len(calls), n + 1)
        call = calls[-1]
        self.assertNotIn("auto-edit", call["argv"])   # правило 7 CLAUDE.md
        return call["stdin"]

    def label_of(self, stdin, text):
        line = next((x for x in stdin.splitlines() if text in x), None)
        self.assertIsNotNone(line, "реплики нет в stdin")
        return line.split(text)[0]

    def test_employee_not_labeled_teamlead(self):
        text = "Сформулируй результат по выдуманной задаче сотрудника"
        stdin = self.last_stdin(EMP, text)
        label = self.label_of(stdin, text)
        self.assertNotIn(EMP_LABEL_BAD, label, label)
        self.assertNotIn("Тимлид", label)
        self.assertIn(LABELS["employee"] + EMP, label)

    def test_lead_label(self):
        text = "Сформулируй результат по выдуманной задаче лида"
        label = self.label_of(self.last_stdin(LEAD, text), text)
        self.assertNotRegex(label, "[Сс]отрудник", label)
        self.assertIn(LABELS["teamlead"] + LEAD, label)
        text = "Реплика зама для проверки метки"
        self.assertIn(LABELS["deputy"] + DEP, self.label_of(self.last_stdin(DEP, text), text))
        text = "Реплика админа для проверки метки"
        self.assertIn(LABELS["admin"], self.label_of(self.last_stdin("admin", text), text))

    def test_stdin_knows_who(self):
        stdin = self.last_stdin(EMP, "Кто я?")
        self.assertIn(EMP, stdin)
        self.assertIn(T_EMP_WORK, stdin)
        self.assertNotIn(T_FOREIGN, stdin)


# ================================================================ критерий 4: карточка — права T-003
class CardRightsTest(Base):
    def card(self, who, title, q="Как сделать выдуманный обзор?"):
        t = self.tasks[title]
        return self.s.call("POST", "/api/card", {"kind": t["kind"], "id": t["id"], "title": q,
                                                 "body": "Выдуманный текст карточки"}, session=self.sid[who])

    def test_executor_in_approved(self):
        code, r = self.card(EMP, T_EMP_APPR)
        self.assertEqual(code, 200, r)

    def test_teamlead_refused(self):
        for who in (LEAD, DEP, LEAD2, EMP2):
            with self.subTest(who=who):
                code, r = self.card(who, T_EMP_APPR, f"Чужая карточка {who}?")
                self.assertEqual(code, 403, r)
                self.assertRegex(r.get("error", ""), "[А-Яа-я]")

    def test_executor_other_status(self):
        code, r = self.card(EMP, T_EMP_WORK)
        self.assertEqual(code, 403, r)


# ================================================================ критерии 3, 5: интерфейс (статически)
class StaticTest(unittest.TestCase):
    def test_transfer_buttons(self):
        self.assertIn("Перенести результат в задачу", HTML)
        self.assertIn("Прикрепить карточку к задаче", HTML)

    def test_result_and_card_blocks_parsed(self):
        for kind in ("result", "card"):
            with self.subTest(kind=kind):
                self.assertRegex(HTML, r"```\s*" + kind + r"|['\"`]" + kind + r"['\"`]\s*[,)\]]",
                                 f"нет разбора блока ```{kind}```")
        self.assertRegex(HTML, r"```\(?\?:?\s*task|```task|'task'")   # блок task остаётся (регрессия)

    def test_title_without_teamlead(self):
        self.assertNotIn("Помощник тимлида", HTML)
        self.assertRegex(HTML, r"<b>Помощник</b>")

    def test_skill_select_not_truncated(self):
        rules = re.findall(r"\.ai-skill\s*\{([^}]*)\}", HTML)
        self.assertTrue(rules)
        for r in rules:
            m = re.search(r"max-width\s*:\s*(\d+)px", r)
            self.assertFalse(m and int(m.group(1)) <= 200, f".ai-skill обрезается: {r}")

    def test_greeting_from_skill(self):
        for k in SKILL_TEXT_KEYS:
            self.assertRegex(HTML, r"\." + k + r"\b", f"приветствие/примеры не берутся из скилла ({k})")

    def test_blocks_split_by_kind(self):
        for kind in ("result", "card"):
            self.assertRegex(HTML, r"splitBlocks?\([^)]*'" + kind + "'\\)")

    def test_transfer_button_disabled_with_reason(self):
        """Критерий 4: кнопка недоступна с пояснением, если не исполнитель/замещающий или статус не тот."""
        m = re.search(r"function aiBlockBtn\([^)]*\)\{(.*?)\n\}", HTML, re.S)
        self.assertTrue(m, "нет aiBlockBtn")
        b = m.group(1)
        self.assertIn("disabled", b)
        self.assertIn("title", b)
        self.assertRegex(HTML, r"'В работе',\s*'На доработке'")

    def test_take_button_only_for_task_teamlead(self):
        """Критерий 5: «Доработать постановку» — только тимлиду открытой задачи и только в скилле постановки."""
        m = re.search(r"function aiTakeSync\(\)\{(.*?)\n\}", HTML, re.S)
        self.assertTrue(m, "нет aiTakeSync")
        self.assertIn("isTL(", m.group(1))
        self.assertRegex(m.group(1), r"AI_POSTANOVKA|'postanovka'")

    def test_lessons_no_reduced_motion(self):
        """Урок CLAUDE.md: анимации не глушить по prefers-reduced-motion."""
        self.assertNotIn("prefers-reduced-motion", HTML)


if __name__ == "__main__":
    unittest.main()
