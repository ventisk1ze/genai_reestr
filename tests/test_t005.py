"""T-005: вход переживает F5. Сессия — только идентификатор в sessionStorage (ключ ck.session), при загрузке
проверяется через GET /api/whoami; «Выйти» и 401 стирают ключ; в localStorage сессии нет; PIN — нигде.
Фронтенд проверяется статически (playwright и node не используем), сервер — по контракту /api/whoami.
Критерии — docs/backlog.md (T-005). Все данные выдуманные."""
import re
import sys
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, Server, frontend_text, teams_for# noqa: E402

HTML = frontend_text()
SESSION_KEY = "ck.session"
API_WHOAMI = "/api/whoami"

ADMIN_ARGS = ["--admin-pin-stdin"]
ADMIN_PIN = "52637481"
LEAD = "Вершинин Лев Тестович"
EMP = "Ступина Нина Тестовна"
GONE = "Уходящий Ян Тестович"
PINS = {LEAD: "48151623", EMP: "2718", GONE: "90210"}
PEOPLE = [
    {"name": LEAD, "email": "vershinin@example.test", "role": "teamlead", "active": True},
    {"name": EMP, "email": "stupina@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": GONE, "email": "uhodyashiy@example.test", "role": "employee", "team": LEAD, "active": True},
]


def body(name):
    """Тело JS-функции `function name(...){...}` (в т.ч. async) или `const name = (...) => {...}`."""
    m = re.search(r"function\s+" + name + r"\s*\([^)]*\)\s*\{", HTML) \
        or re.search(r"const\s+" + name + r"\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*\{", HTML)
    if not m:
        return None
    i, depth = m.end(), 1
    while depth and i < len(HTML):
        depth += {"{": 1, "}": -1}.get(HTML[i], 0)
        i += 1
    return HTML[m.end():i - 1]


def session_wrappers():
    """Имена объектов-обёрток над sessionStorage (`const SESS = {get(){… sessionStorage …}}`)."""
    names = []
    for m in re.finditer(r"const\s+(\w+)\s*=\s*\{", HTML):
        i, depth = m.end(), 1
        while depth and i < len(HTML):
            depth += {"{": 1, "}": -1}.get(HTML[i], 0)
            i += 1
        if "sessionStorage" in HTML[m.end():i]:
            names.append(m.group(1))
    return names


def clears_session(code):
    """Код стирает сессию из хранилища вкладки — напрямую или через обёртку (`SESS.set('')`, `.del()`…)."""
    if re.search(r"sessionStorage\.(removeItem\(\s*['\"]" + re.escape(SESSION_KEY) + r"['\"]|clear\(\))", code):
        return True
    for w in session_wrappers():
        if re.search(re.escape(w) + r"\.(set\(\s*(''|\"\"|null|undefined)?\s*\)|del\(|remove\(|clear\(|drop\()", code):
            return True
    return False


# ================================================================ фронтенд, статически
class StaticTest(unittest.TestCase):
    def test_session_in_session_storage(self):
        """Решение пользователя: идентификатор сессии — в sessionStorage под ключом ck.session."""
        self.assertRegex(HTML, r"sessionStorage\.setItem\(\s*['\"]" + re.escape(SESSION_KEY) + r"['\"]")
        self.assertRegex(HTML, r"sessionStorage\.getItem\(\s*['\"]" + re.escape(SESSION_KEY) + r"['\"]")

    def test_session_storage_in_try_catch(self):
        """sessionStorage может бросать (приватный режим, запрет хранилища) — каждое обращение в try/catch."""
        hits = [m.start() for m in re.finditer(r"sessionStorage\.", HTML)]
        self.assertTrue(hits)
        for pos in hits:
            before = HTML[max(0, pos - 300):pos]
            t = before.rfind("try")
            self.assertNotEqual(t, -1, f"sessionStorage без try: …{HTML[pos-80:pos+60]}…")
            self.assertNotIn("catch", before[t:], f"sessionStorage вне try: …{HTML[pos-80:pos+60]}…")

    def test_no_session_in_local_storage(self):
        """Критерий 4: в localStorage сессии нет — ни через store.set, ни напрямую."""
        self.assertIsNone(re.search(r"store\.set\(\s*['\"](session|sid|auth|user|token)", HTML, re.I))
        for m in re.finditer(r"localStorage\.setItem\(([^;]*)\)", HTML):
            self.assertNotRegex(m.group(1), r"(?i)session|sid\b|pin", m.group(0))
        # единственная прямая запись в localStorage — внутри store
        self.assertEqual(len(re.findall(r"localStorage\.setItem\(", HTML)), 1)

    def test_pin_never_stored(self):
        """Критерий 4: PIN не пишется ни в какое хранилище браузера."""
        writes = re.findall(r"(?:store\.set|sessionStorage\.setItem|localStorage\.setItem)\(([^\n]*)", HTML)
        for w in session_wrappers():
            writes += re.findall(re.escape(w) + r"\.set\(([^\n]*)", HTML)
        self.assertTrue(writes)
        for arg in writes:
            self.assertNotRegex(arg.split(")")[0], r"(?i)\bpin\b|pinEl|\.pin\b", arg)
        self.assertNotIn("document.cookie", HTML)
        self.assertNotIn("indexedDB", HTML)

    def test_whoami_on_start(self):
        """Критерий 2: при загрузке сохранённая сессия проверяется на сервере через /api/whoami."""
        holders = [n for n in re.findall(r"function\s+(\w+)\s*\(", HTML) if API_WHOAMI in (body(n) or "")]
        self.assertTrue(holders, "нигде не вызывается /api/whoami")
        started = [n for n in holders if re.search(r"^\s*(?:await\s+)?" + n + r"\(", HTML, re.M)]
        self.assertTrue(started, f"{holders} не вызывается при загрузке страницы")
        b = body(started[0])
        self.assertTrue(clears_session(b), "недействительная сессия не стирается из хранилища")
        reads = ["sessionStorage.getItem"] + [w + ".get(" for w in session_wrappers()]
        self.assertTrue(any(x in b for x in reads), "сессия не читается из хранилища вкладки")

    def test_stored_session_goes_to_header(self):
        """Восстановленная сессия уходит в X-Session (через общий AUTH.session)."""
        b = body("api")
        self.assertIsNotNone(b)
        self.assertRegex(b, r"'X-Session'\s*:\s*AUTH\.session")

    def test_login_saves_session(self):
        """Вход сохраняет идентификатор сессии (r.session), а не PIN."""
        b = body("login")
        self.assertIsNotNone(b)
        writes = [x for w in session_wrappers() for x in re.findall(re.escape(w) + r"\.set\(([^)]*)\)", b)]
        writes += re.findall(r"sessionStorage\.setItem\(([^)]*)\)", b)
        self.assertTrue(writes, "login() не сохраняет сессию")
        self.assertTrue(any("session" in x for x in writes), writes)

    def test_logout_clears(self):
        """Критерий 3: «Выйти» стирает сессию из хранилища вкладки."""
        b = body("logout")
        self.assertIsNotNone(b)
        self.assertTrue(clears_session(b), "logout() не стирает ck.session")

    def test_401_clears(self):
        """Критерий 3: ответ 401 стирает сессию."""
        b = body("api")
        m = re.search(r"401[^\n]*", b)
        self.assertTrue(m, "в api() нет обработки 401")
        self.assertTrue(clears_session(m.group(0)), "обработка 401 не стирает ck.session")


# ================================================================ сервер: /api/whoami
class WhoamiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server("json", args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN])
        try:
            code, r = cls.s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
            assert code == 200, r
            cls.adm = r["session"]
            cfg = cls.s.call("GET", "/api/state")[1]["config"]
            cfg["people"] = [dict(p) for p in PEOPLE]
            cfg["teams"] = teams_for(PEOPLE)
            code, r = cls.s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=cls.adm)
            assert code == 200, r
            for name, pin in PINS.items():
                code, r = cls.s.call("POST", "/api/admin/pin", {"name": name, "pin": pin}, session=cls.adm)
                assert code == 200, (name, r)
        except Exception:
            cls.s.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def login(self, name):
        code, r = self.s.call("POST", "/api/login", {"name": name, "pin": PINS[name]})
        self.assertEqual(code, 200, r)
        return r["session"]

    def whoami(self, sid):
        code, r = self.s.call("GET", API_WHOAMI, session=sid)
        self.assertEqual(code, 200, r)
        self.assertIn("user", r)
        return r["user"]

    def test_valid_session_employee(self):
        """Критерий 1: после F5 — та же роль и команда, что при входе."""
        sid = self.login(EMP)
        u = self.whoami(sid)
        # T-007: people[].team — id команды, где LEAD — лид
        team = next(t["id"] for t in self.s.call("GET", "/api/state")[1]["config"]["teams"] if t["lead"] == LEAD)
        self.assertEqual((u["name"], u["role"], u["team"], u["admin"]), (EMP, "employee", team, False))

    def test_valid_session_teamlead(self):
        """Критерий 1: охват «Мой реестр» опирается на роль teamlead из whoami."""
        u = self.whoami(self.login(LEAD))
        self.assertEqual((u["name"], u["role"], u["admin"]), (LEAD, "teamlead", False))

    def test_whoami_same_as_login(self):
        code, r = self.s.call("POST", "/api/login", {"name": EMP, "pin": PINS[EMP]})
        self.assertEqual(code, 200, r)
        self.assertEqual(self.whoami(r["session"]), r["user"])

    def test_no_secrets_in_whoami(self):
        sid = self.login(LEAD)
        code, raw = self.s.raw("GET", API_WHOAMI, session=sid)
        self.assertEqual(code, 200)
        for bad in ("salt", "hash", '"pin"', PINS[LEAD], ADMIN_PIN):
            self.assertNotIn(bad, raw)

    def test_after_logout_null(self):
        """Критерий 3: после «Выйти» сохранённая сессия недействительна."""
        sid = self.login(EMP)
        self.assertEqual(self.s.call("POST", "/api/logout", {}, session=sid)[0], 200)
        self.assertIsNone(self.whoami(sid))

    def test_garbage_or_missing_session_null(self):
        """Критерий 2: подложенный/битый идентификатор — user null (не 500)."""
        for sid in ("мусор-сессия", "x" * 43, "' OR 1=1 --"):
            code, r = self.s.call("GET", API_WHOAMI, headers={"X-Session": urllib.parse.quote(sid)})
            self.assertEqual((code, r.get("user", "нет поля")), (200, None), sid)
        self.assertIsNone(self.whoami(None))

    def test_whoami_requires_token(self):
        sid = self.login(EMP)
        code, _ = self.s.call("GET", API_WHOAMI, session=sid, token=False)
        self.assertEqual(code, 403)

    def test_deactivated_null(self):
        """Критерий 2: сотрудника деактивировали — его сохранённая сессия больше не действует."""
        sid = self.login(GONE)
        self.assertEqual(self.whoami(sid)["name"], GONE)
        cfg = self.s.call("GET", "/api/state")[1]["config"]
        cfg["people"] = [dict(p, active=False) if p["name"] == GONE else p for p in cfg["people"]]
        for p in cfg["people"]:
            p.pop("has_pin", None)
        code, r = self.s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=self.adm)
        self.assertEqual(code, 200, r)
        try:
            self.assertIsNone(self.whoami(sid))
        finally:
            cfg = self.s.call("GET", "/api/state")[1]["config"]
            cfg["people"] = [dict(p, active=True) if p["name"] == GONE else p for p in cfg["people"]]
            for p in cfg["people"]:
                p.pop("has_pin", None)
            self.s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=self.adm)

    def test_admin(self):
        """Критерий 5: админ тоже переживает F5."""
        u = self.whoami(self.adm)
        self.assertIs(u["admin"], True)
        self.assertEqual(u["role"], "admin")

    def test_session_survives_many_reloads(self):
        """Несколько F5 подряд — whoami не расходует и не продлевает сессию сверх срока."""
        sid = self.login(LEAD)
        for _ in range(5):
            self.assertEqual(self.whoami(sid)["name"], LEAD)


class SessionLifetimeTest(unittest.TestCase):
    def test_expired_null(self):
        """Критерий 2: истёкшая сессия — user null (срок как у сессии на сервере, тут ~1 с)."""
        s = Server("json", args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env={"REG_SESSION_HOURS": "0.0003"})
        try:
            code, r = s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
            self.assertEqual(code, 200, r)
            self.assertIsNotNone(s.call("GET", API_WHOAMI, session=r["session"])[1]["user"])
            time.sleep(1.6)
            self.assertIsNone(s.call("GET", API_WHOAMI, session=r["session"])[1]["user"])
        finally:
            s.stop()

    def test_server_restart_null(self):
        """Критерий 2: после перезапуска сервера сохранённая во вкладке сессия недействительна."""
        s = Server("json", args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN])
        try:
            sid = s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})[1]["session"]
            tmp = s.tmp
            s._own_tmp = False
            s.stop()
            s = Server("json", args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], tmp=tmp)
            s._own_tmp = True
            self.assertEqual(s.call("GET", API_WHOAMI, session=sid), (200, {"user": None}))
        finally:
            s.stop()


if __name__ == "__main__":
    unittest.main()
