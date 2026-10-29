"""T-004: фильтр по умолчанию — «Мои» у сотрудника, «Мой реестр» у тимлида, у админа всё.
Фича только во фронтенде: проверяем index.html статически (playwright и node не используем) и
регрессию контракта /api/state и /api/login, на который опирается охват. Все данные выдуманные."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, Server, frontend_text, teams_for# noqa: E402

HTML = frontend_text()


def body(name):
    """Тело JS-функции `function name(...){...}` или `const name = (...) => {...}` / `=> выражение;`."""
    m = re.search(r"function\s+" + name + r"\s*\([^)]*\)\s*\{", HTML) \
        or re.search(r"const\s+" + name + r"\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*\{", HTML)
    if m:
        i, depth = m.end(), 1
        while depth and i < len(HTML):
            depth += {"{": 1, "}": -1}.get(HTML[i], 0)
            i += 1
        return HTML[m.end():i - 1]
    m = re.search(r"const\s+" + name + r"\s*=\s*(?:\([^)]*\)|\w+)\s*=>\s*(.+?);\n", HTML)
    return m.group(1) if m else None


def fn(test, *names):
    for n in names:
        b = body(n)
        if b is not None:
            return b
    test.fail(f"нет функции {' / '.join(names)}")


# ================================================================ критерии 1–7: интерфейс
class StaticTest(unittest.TestCase):
    def test_mine_checkbox_removed(self):
        """Критерий 7: флажок «Мои» и поле mine удалены."""
        self.assertNotIn('data-f="mine"', HTML)
        self.assertIsNone(re.search(r"\bmine\s*:\s*(false|true)", HTML))
        self.assertIsNone(re.search(r"(?<!delete S\.)\bf\.mine\b", HTML))   # чистка старого ck.f — можно

    def test_texts(self):
        """Критерии 1 и 6: подписи переключателя и пустого охвата."""
        for s in ("Мой реестр", "Ваших задач нет", "Показать все"):
            self.assertIn(s, HTML)

    def test_scope_values(self):
        """Охват — 'mine' | 'all' в S.f.scope."""
        self.assertRegex(HTML, r"scope\s*(===?|:|=)\s*'mine'")
        self.assertRegex(HTML, r"scope\s*(===?|:|=)\s*'all'")

    def test_switcher_in_filters_bar_not_for_admin(self):
        """Критерии 1 и 4: переключатель — в filtersBar(), с числами, админу не выводится.
        me() у админа пуст, поэтому роль проверяется через me() или isAdmin()."""
        b = fn(self, "filtersBar")
        self.assertIn("Мой реестр", b)
        self.assertRegex(b, r"data-act=\"fscope\"")
        self.assertRegex(b, r"(me\(\)|isAdmin\(\))\s*\?[^;]*'Мой реестр'", "переключатель зависит от роли")
        self.assertRegex(b, r"'Мой реестр'\s*:\s*'Мои'", "у сотрудника «Мои», у тимлида «Мой реестр»")
        self.assertRegex(b, r"\.length\]", "у каждого варианта — число задач")

    def test_scope_action(self):
        """Кнопки охвата меняют только S.f.scope и не пишут в localStorage."""
        m = re.search(r"\n\s*fscope\s*:\s*el\s*=>\s*\{([^}]*)\}", HTML)
        self.assertTrue(m, "нет действия fscope")
        self.assertIn("S.f.scope", m.group(1))
        self.assertNotIn("saveF", m.group(1))
        self.assertNotIn("store.set", m.group(1))

    def test_scope_applied_in_base_filtered(self):
        """Критерий 7: охват применяется в baseFiltered() — чипы статусов его учитывают."""
        b = fn(self, "baseFiltered")
        self.assertRegex(b, r"inScope\b|\.scope\b")
        self.assertNotIn("isMine", b)

    def test_in_scope_rules(self):
        """Критерии 2–4: сотрудник — executor/substitute; тимлид — ещё и teamlead; админ — всё."""
        b = fn(self, "inScope")
        self.assertIn("scope", b)
        self.assertRegex(b, r"isAdmin\(\)|!me\(\)", "у админа — все задачи")
        mine, seen, todo = "", set(), re.findall(r"\b([a-zA-Z]+)\(t\)", b)
        while todo:   # T-007: myScope(t) -> isTL(t) — разворачиваем вложенные хелперы
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            nb = body(n) or ""
            mine += nb
            todo += re.findall(r"\b([a-zA-Z]+)\(t\)", nb)
        for k in ("executor", "substitute", "teamlead"):
            self.assertIn("t." + k, b + mine)
        self.assertRegex(b + mine, r"role\s*===\s*'teamlead'[^;]*t\.teamlead|isTL\(", "teamlead — только у тимлида")

    def test_default_scope_by_role(self):
        """Критерии 2–5: по умолчанию у админа 'all', у остальных 'mine'."""
        b = fn(self, "defaultScope")
        self.assertRegex(b, r"isAdmin\(\)|me\(\)")
        self.assertIn("'mine'", b)
        self.assertIn("'all'", b)

    def test_scope_not_saved(self):
        """Критерий 5: scope не попадает в localStorage — ни через saveF, ни через поиск."""
        sets = re.findall(r"store\.set\(\s*'f'\s*,\s*([^)]*)\)", HTML)
        self.assertTrue(sets, "фильтры по-прежнему запоминаются")
        for arg in sets:
            arg = arg.strip()
            self.assertNotEqual(arg, "S.f", "S.f целиком уходит в localStorage вместе со scope")
            if re.fullmatch(r"\w+", arg):   # const {scope, ...f} = S.f; store.set('f', f)
                self.assertRegex(HTML, r"\{\s*scope\b[^}]*\.\.\." + arg + r"\s*\}\s*=\s*S\.f\b")

    def test_scope_reset_on_login_and_logout(self):
        """Критерий 5: охват сбрасывается при каждом входе и выходе."""
        for name in ("login", "logout"):
            b = fn(self, name)
            self.assertRegex(b, r"scope|defaultScope\(|resetScope\(", f"{name}() не сбрасывает охват")

    def test_stored_scope_ignored_on_start(self):
        """Старое или подложенное ck.f.scope в localStorage не задаёт охват при загрузке."""
        m = re.search(r"\bf\s*:\s*(.+?),\n\s*sort\s*:", HTML, re.S)
        self.assertTrue(m, "инициализация S.f не найдена")
        init = m.group(1)
        if "store.get('f'" in init and "scope" in init:   # scope в init — должен идти после store.get
            self.assertGreater(init.rfind("scope"), init.find("store.get('f'"))

    def test_empty_scope_only_when_no_own_tasks(self):
        """Решение тимлида: «Ваших задач нет» — только если своих задач нет совсем, без учёта прочих фильтров."""
        b = fn(self, "scopeEmpty")
        self.assertRegex(b, r"S\.tasks\.(filter|some)\(\s*myScope\s*\)", "пустота охвата — по всем задачам")
        self.assertNotRegex(b, r"\b(filtered|baseFiltered|unscoped)\(\)", "чипы, поиск и вид не делают охват пустым")

    def test_filtered_out_message_keeps_show_all(self):
        """Свои задачи есть, но скрыты фильтрами — «Задач по этим фильтрам нет.», при scope='mine' — «Показать все»."""
        for view in ("listView", "boardView"):
            self.assertRegex(fn(self, view), r"scopeEmpty\(|emptyHTML\(|scopeMine\(", view)
        i = HTML.find("Задач по этим фильтрам нет")
        self.assertGreater(i, 0)
        tail = HTML[i:HTML.find("\n", i)]           # та же строка шаблона
        show = re.search(r"const\s+(\w+)\s*=\s*'[^'\n]*Показать все", HTML)
        self.assertTrue("Показать все" in tail or (show and show.group(1) in tail),
                        "под «Задач по этим фильтрам нет» нет кнопки «Показать все»")
        self.assertRegex(tail, r"scope", "«Показать все» — только при охвате «Мои»")

    def test_is_mine_removed(self):
        """Решение тимлида: мёртвый isMine удалён."""
        self.assertNotRegex(HTML, r"\bisMine\b")

    def test_empty_scope_has_show_all(self):
        """Критерий 6: пустой охват — кнопка «Показать все» переключает на 'all'."""
        m = re.search(r"Показать все", HTML)
        tail = HTML[m.start() - 300:m.start() + 50]
        self.assertRegex(tail, r"data-act=\"[\w-]+\"", "«Показать все» — кнопка с действием")


# ================================================================ регрессия контракта
ADMIN_PIN = "40404040"
LEAD = "Тестов Тест Тестович"
EMP = "Пробова Анна Тестовна"
SUB = "Запасов Иван Тестович"
PINS = {LEAD: "11223344", EMP: "5566", SUB: "7788"}
PEOPLE = [
    {"name": LEAD, "email": "testov@example.test", "role": "teamlead", "team": LEAD, "active": True},
    {"name": EMP, "email": "probova@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": SUB, "email": "zapasov@example.test", "role": "employee", "team": LEAD, "active": True},
]


class ContractTest(unittest.TestCase):
    """Охват считается на клиенте по полям задачи и роли из /api/login — контракт не должен меняться."""

    @classmethod
    def setUpClass(cls):
        cls.s = Server("json", args=["--admin-pin-stdin"], stdin_lines=[ADMIN_PIN])
        try:
            code, r = cls.s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
            assert code == 200, r
            cls.admin_user = r["user"]
            adm = r["session"]
            cfg = cls.s.call("GET", "/api/state")[1]["config"]
            cfg["people"] = PEOPLE
            cfg["teams"] = teams_for(PEOPLE)
            code, r = cls.s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=adm)
            assert code == 200, r
            cls.users, cls.sid = {}, {}
            for name, pin in PINS.items():
                code, r = cls.s.call("POST", "/api/admin/pin", {"name": name, "pin": pin}, session=adm)
                assert code == 200, r
                code, r = cls.s.call("POST", "/api/login", {"name": name, "pin": pin})
                assert code == 200, r
                cls.users[name], cls.sid[name] = r["user"], r["session"]
            task = {"kind": "once", "id": None, "title": "Выдуманная задача T-004", "teamlead": LEAD,
                    "executor": EMP, "substitute": SUB, "deadline": "2030-12-31"}
            code, r = cls.s.call("POST", "/api/task", {"task": task, "action": "Поставлена", "action_code": "create"},
                                 session=cls.sid[LEAD])
            assert code == 200, r
            cls.task = r["task"]
        except Exception:
            cls.s.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def test_state_task_fields(self):
        code, st = self.s.call("GET", "/api/state")
        self.assertEqual(code, 200, st)
        t = next(x for x in st["tasks"] if x["id"] == self.task["id"] and x["kind"] == "once")
        self.assertEqual((t["teamlead"], t["executor"], t["substitute"]), (LEAD, EMP, SUB))

    def test_login_roles(self):
        self.assertTrue(self.admin_user.get("admin"))
        self.assertEqual(self.users[LEAD]["name"], LEAD)
        self.assertEqual(self.users[LEAD]["role"], "teamlead")
        self.assertFalse(self.users[LEAD].get("admin"))
        self.assertEqual(self.users[EMP]["role"], "employee")
        self.assertEqual(self.users[SUB]["name"], SUB)

    def test_all_tasks_visible_to_everyone(self):
        """Охват — только вид: сервер по-прежнему отдаёт все задачи, сотрудник может нажать «Все»."""
        code, st = self.s.call("GET", "/api/state", session=self.sid[EMP])
        self.assertEqual(code, 200, st)
        self.assertTrue(any(x["id"] == self.task["id"] for x in st["tasks"]))


if __name__ == "__main__":
    unittest.main()
