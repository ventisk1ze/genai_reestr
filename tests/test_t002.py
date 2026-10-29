"""T-002: сотрудники, команды, PIN и админ-панель. Критерии приёмки — docs/backlog.md (T-002), контракт — от dev.
Все данные выдуманные."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402
from helpers import Server, run_cli, stored_text, teams_for  # noqa: E402

# ---- контракт (правится только здесь; заголовок сессии — helpers.SESSION_HEADER)
API_LOGIN = "/api/login"            # POST {name, pin} | {admin: true, pin} -> {session, expires, user}
API_LOGOUT = "/api/logout"          # POST, X-Session
API_WHOAMI = "/api/whoami"          # GET -> {user: {...} | null}
API_PIN = "/api/admin/pin"          # POST {name, pin} | {name, reset: true} — только админ
ADMIN_ARGS = ["--admin-pin-stdin"]  # строка stdin — админский PIN (с --creds-stdin — третья строка)
PIN_SET_FLAG = "has_pin"
HISTORY_ADMIN = "Правка администратором"
SECRET_KEYS = {"pin", "salt", "hash", "iter"}

ADMIN_PIN = "90817263"
LEAD = "Тестов Тест Тестович"
EMP = "Примерова Анна Сергеевна"
EMP2 = "Выдумкин Пётр Ильич"
GONE = "Уволенов Иван Петрович"
LEAD_PIN, EMP_PIN, EMP2_PIN, GONE_PIN = "73914682", "4826", "559103", "1111"

PEOPLE = [
    {"name": LEAD, "email": "testov@example.test", "role": "teamlead", "team": LEAD, "active": True},
    {"name": EMP, "email": "primerova@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": EMP2, "email": "vydumkin@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": GONE, "email": "uvolenov@example.test", "role": "employee", "team": LEAD, "active": False},
]
PINS = {LEAD: LEAD_PIN, EMP: EMP_PIN, EMP2: EMP2_PIN, GONE: GONE_PIN}


def secret_keys_in(obj, path="$"):
    """Пути ко всем ключам pin/salt/hash/iter в JSON-ответе (на любой глубине)."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SECRET_KEYS:
                found.append(f"{path}.{k}")
            found += secret_keys_in(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += secret_keys_in(v, f"{path}[{i}]")
    return found


def proc_secrets_visible(pid, secrets):
    """Какие из секретов видны в аргументах и окружении процесса (Linux — /proc, macOS — ps eww)."""
    if Path(f"/proc/{pid}/environ").exists():
        text = (Path(f"/proc/{pid}/environ").read_bytes() + b"\n" + Path(f"/proc/{pid}/cmdline").read_bytes())
        text = text.decode("utf-8", "replace")
    else:
        text = subprocess.run(["ps", "eww", "-p", str(pid), "-o", "command="], capture_output=True,
                              text=True).stdout
        assert "app.py" in text, "ps не показал процесс: " + text
    return [s for s in secrets if s in text]


def admin_server(storage="json", **kw):
    return Server(storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], **kw)


def login(s, name, pin):
    code, r = s.call("POST", API_LOGIN, {"name": name, "pin": pin})
    return code, r.get("session") if code == 200 else r


def admin_login(s, pin=ADMIN_PIN):
    code, r = s.call("POST", API_LOGIN, {"admin": True, "pin": pin})
    return code, r.get("session") if code == 200 else r


def get_config(s):
    code, st = s.call("GET", "/api/state")
    assert code == 200, st
    return st["config"]


def save_config(s, session, mutate):
    cfg = get_config(s)
    mutate(cfg)
    return s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=session)


def setup_people(s):
    """Админ заводит справочник и PIN; возвращает сессию админа."""
    code, adm = admin_login(s)
    assert code == 200 and adm, (code, adm)

    def put(cfg):
        cfg["people"] = [dict(p) for p in PEOPLE]
        cfg["teams"] = teams_for(PEOPLE)
    code, r = save_config(s, adm, put)
    assert code == 200, r
    for name, pin in PINS.items():
        code, r = s.call("POST", API_PIN, {"name": name, "pin": pin}, session=adm)
        assert code == 200, (name, r)
    return adm


def people_of(cfg):
    return {p["name"]: p for p in cfg.get("people", [])}


def task_body(title, teamlead=LEAD, action="Поставлена"):
    # с T-003 для постановки обязательны исполнитель и срок
    return {"task": {"kind": "once", "id": None, "title": title, "teamlead": teamlead, "executor": EMP,
                     "deadline": "2030-12-31"}, "action": action}


class SharedServer(unittest.TestCase):
    """Один сервер с админом и справочником на класс — тесты быстрые."""
    storage = "json"

    @classmethod
    def setUpClass(cls):
        cls.s = admin_server(cls.storage)
        cls.adm = setup_people(cls.s)

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def sess(self, name, pin=None):
        code, sid = login(self.s, name, pin or PINS[name])
        self.assertEqual(code, 200, sid)
        self.assertTrue(sid)
        return sid

    def new_task(self, session, title="Задача T-002", user=""):
        return self.s.call("POST", "/api/task", task_body(title), session=session, user=user)


# ================================================================ критерий 1
class PeopleTest(SharedServer):
    """Роль, команда, «активен»; старый справочник без новых полей читается."""

    def test_fields_roundtrip(self):
        ppl = people_of(get_config(self.s))
        self.assertEqual(ppl[LEAD]["role"], "teamlead")
        self.assertEqual(ppl[EMP]["role"], "employee")
        # T-007: people[].team — id команды, где LEAD — лид
        team = next(t["id"] for t in get_config(self.s)["teams"] if t["lead"] == LEAD)
        self.assertEqual(ppl[EMP]["team"], team)
        self.assertEqual(ppl[LEAD]["team"], team)
        self.assertIs(ppl[GONE]["active"], False)
        self.assertIs(ppl[EMP]["active"], True)
        self.assertEqual(ppl[EMP]["email"], "primerova@example.test")

    def test_old_people_without_new_fields(self):
        """Справочник формата до T-002 (name/email/color) в config.json — сервер стартует, поля по умолчанию."""
        tmp = tempfile.TemporaryDirectory()
        try:
            d = Path(tmp.name) / "json"
            d.mkdir()
            cfg = {"_v": 3, "ck_name": "ЦК GenAI", "people": [
                {"name": "Старов Олег Олегович", "email": "starov@example.test", "color": "#123456"},
                {"name": "Новиков Глеб Ярославич", "email": "novikov@example.test"}],
                "matrix": {"БЗ-01": {"Старов Олег Олегович": "0", "Новиков Глеб Ярославич": "1"}}}
            (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")
            s = Server("json", tmp=tmp)
            try:
                ppl = people_of(get_config(s))
                old, new = ppl["Старов Олег Олегович"], ppl["Новиков Глеб Ярославич"]
                self.assertEqual(old["email"], "starov@example.test")
                self.assertIs(old["active"], True)
                self.assertIs(old[PIN_SET_FLAG], False)
                self.assertEqual(old["role"], "teamlead")   # стоит 0 в matrix
                # T-007: миграция — команда на тимлида, название — его ФИО, people[].team — id команды
                team = next(t for t in get_config(s)["teams"] if t["lead"] == "Старов Олег Олегович")
                self.assertEqual(team["name"], "Старов Олег Олегович")
                self.assertEqual(old["team"], team["id"])
                self.assertEqual(new["role"], "employee")
                self.assertEqual(new["team"], "")
            finally:
                s.stop()
        finally:
            tmp.cleanup()


# ================================================================ критерий 2
class PinStorageTest(unittest.TestCase):
    """PIN — только хеш с солью; в файлах хранилища нет PIN, в ответах API нет pin/salt/hash."""

    def check_storage(self, storage):
        s = admin_server(storage)
        try:
            adm = setup_people(s)
            files = stored_text(s.dir)
            self.assertTrue(files)
            for pin in (LEAD_PIN, EMP2_PIN, ADMIN_PIN):
                self.assertNotIn(pin, files, f"PIN открытым текстом в хранилище {storage}")
            self.assertIn("salt", files, "в хранилище не видно соли — где хеш?")
            log = s.output()
            for pin in (LEAD_PIN, EMP2_PIN, ADMIN_PIN):
                self.assertNotIn(pin, log, "PIN в выводе сервера")
            self.assertIs(people_of(get_config(s))[LEAD][PIN_SET_FLAG], True)
        finally:
            s.stop()

    def test_json_storage(self):
        self.check_storage("json")

    def test_sqlite_storage(self):
        self.check_storage("sqlite")

    def test_hash_is_pbkdf2_with_per_person_salt(self):
        s = admin_server()
        try:
            setup_people(s)
            cfg = json.loads((s.dir / "json" / "config.json").read_text("utf-8"))
            recs = {p["name"]: p["pin"] for p in cfg["people"] if p.get("pin")}
            self.assertEqual(set(recs), set(PINS))
            self.assertEqual(len({r["salt"] for r in recs.values()}), len(recs), "соль должна быть у каждого своя")
            import hashlib
            r = recs[LEAD]
            salt = bytes.fromhex(r["salt"]) if all(c in "0123456789abcdef" for c in r["salt"]) else None
            if salt is None:
                import base64
                salt = base64.b64decode(r["salt"])
            want = hashlib.pbkdf2_hmac("sha256", LEAD_PIN.encode(), salt, int(r["iter"]))
            self.assertIn(r["hash"], (want.hex(), __import__("base64").b64encode(want).decode()))
        finally:
            s.stop()

    def test_pin_format(self):
        s = admin_server()
        try:
            adm = setup_people(s)
            for bad in ("123", "123456789", "12a4", " 1234", "１２３４", "12 34", 1234, None):
                code, r = s.call("POST", API_PIN, {"name": EMP, "pin": bad}, session=adm)
                self.assertEqual(code, 400, (bad, r))
            code, r = s.call("POST", API_PIN, {"name": "Нет Такого Человека", "pin": "1234"}, session=adm)
            self.assertEqual(code, 400, r)
            for ok in ("1234", "12345678"):
                code, r = s.call("POST", API_PIN, {"name": EMP, "pin": ok}, session=adm)
                self.assertEqual(code, 200, (ok, r))
                self.assertEqual(r, {"name": EMP, "has_pin": True})
            code, r = s.call("POST", API_LOGIN, {"name": EMP, "pin": "12"})
            self.assertEqual(code, 400, r)
        finally:
            s.stop()


class NoSecretsInResponsesTest(SharedServer):
    """Уточнение (а): pin/salt/hash нет в /api/state, /api/sync, ответах /api/config, /api/login, app.py ctx."""

    def assertClean(self, where, obj):
        self.assertEqual(secret_keys_in(obj), [], where)
        text = json.dumps(obj, ensure_ascii=False)
        for pin in (LEAD_PIN, EMP2_PIN, ADMIN_PIN):
            self.assertNotIn(pin, text, where)

    def test_state_and_sync(self):
        self.assertClean("GET /api/state", self.s.call("GET", "/api/state")[1])
        self.assertClean("GET /api/state (админ)", self.s.call("GET", "/api/state", session=self.adm)[1])
        code, r = self.s.call("POST", "/api/sync", {})
        self.assertEqual(code, 200, r)
        self.assertClean("POST /api/sync", r)

    def test_config_response(self):
        code, r = save_config(self.s, self.adm, lambda c: c.update(ck_name="ЦК GenAI"))
        self.assertEqual(code, 200, r)
        self.assertClean("POST /api/config", r)

    def test_login_whoami_pin_responses(self):
        code, r = self.s.call("POST", API_LOGIN, {"name": EMP, "pin": EMP_PIN})
        self.assertEqual(code, 200, r)
        self.assertClean("POST /api/login", r)
        self.assertEqual(r["user"]["name"], EMP)
        self.assertIs(r["user"]["admin"], False)
        self.assertClean("GET /api/whoami", self.s.call("GET", API_WHOAMI, session=r["session"])[1])
        code, r = self.s.call("POST", API_PIN, {"name": EMP, "pin": EMP_PIN}, session=self.adm)
        self.assertClean("POST /api/admin/pin", r)

    def test_cli_ctx(self):
        r = run_cli(self.s, "ctx")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertClean("app.py ctx", json.loads(r.stdout))
        for word in ('"salt"', '"hash"', '"pin"'):
            self.assertNotIn(word, r.stdout)


class ConfigKeepsPinsTest(SharedServer):
    """Уточнение (б): /api/config без pin или с поддельным pin не стирает и не подменяет PIN."""

    def test_config_without_pin_keeps_pins(self):
        def strip(cfg):
            for p in cfg["people"]:
                p.pop(PIN_SET_FLAG, None)
                p.pop("pin", None)
        code, r = save_config(self.s, self.adm, strip)
        self.assertEqual(code, 200, r)
        self.assertIs(people_of(get_config(self.s))[EMP][PIN_SET_FLAG], True)
        self.sess(EMP)

    def test_config_with_forged_pin_ignored(self):
        import hashlib
        salt = b"\x00" * 16
        forged = {"salt": salt.hex(), "hash": hashlib.pbkdf2_hmac("sha256", b"0000", salt, 1000).hex(), "iter": 1000}

        def forge(cfg):
            for p in cfg["people"]:
                if p["name"] in (EMP, EMP2):
                    p["pin"] = forged
                    p[PIN_SET_FLAG] = True
        code, r = save_config(self.s, self.adm, forge)
        self.assertEqual(code, 200, r)
        code, sid = login(self.s, EMP, "0000")
        self.assertNotEqual(code, 200, "поддельный PIN из /api/config принят")
        self.sess(EMP)
        self.sess(EMP2)

    def test_config_has_pin_flag_cannot_reset(self):
        def unset(cfg):
            for p in cfg["people"]:
                if p["name"] == EMP2:
                    p[PIN_SET_FLAG] = False
        code, r = save_config(self.s, self.adm, unset)
        self.assertEqual(code, 200, r)
        self.sess(EMP2)


class RenameDeactivateTest(unittest.TestCase):
    """Уточнение (в): переименование/деактивация — сессия перестаёт работать на запись; переименование сбрасывает PIN."""

    def setUp(self):
        self.s = admin_server()
        self.adm = setup_people(self.s)

    def tearDown(self):
        self.s.stop()

    def test_deactivate_drops_session(self):
        code, sid = login(self.s, EMP2, EMP2_PIN)
        self.assertEqual(code, 200)
        # с T-003 сотрудник не ставит задачи: тимлид ставит задачу на EMP2, тот берёт её в работу — 200
        body = task_body("До деактивации")
        body["task"]["executor"] = EMP2
        code, r = self.s.call("POST", "/api/task", body, session=login(self.s, LEAD, LEAD_PIN)[1])
        self.assertEqual(code, 200, r)
        t = r["task"]
        code, r = self.s.call("POST", "/api/task", {"task": t, "expect_v": t["_v"], "action_code": "take"},
                              session=sid)
        self.assertEqual(code, 200, r)
        t = r["task"]

        def off(cfg):
            people_of(cfg)[EMP2]["active"] = False
        code, r = save_config(self.s, self.adm, off)
        self.assertEqual(code, 200, r)
        code, r = self.s.call("POST", "/api/task", {"task": {**t, "result": "После деактивации"},
                                                    "expect_v": t["_v"], "action_code": "edit"}, session=sid)
        self.assertEqual(code, 401, r)
        code, _ = login(self.s, EMP2, EMP2_PIN)
        self.assertNotEqual(code, 200)

    def test_rename_drops_session_and_pin(self):
        new_name = "Выдумкин-Новый Пётр Ильич"
        code, sid = login(self.s, EMP2, EMP2_PIN)
        self.assertEqual(code, 200)

        def rename(cfg):
            people_of(cfg)[EMP2]["name"] = new_name
        code, r = save_config(self.s, self.adm, rename)
        self.assertEqual(code, 200, r)
        code, r = self.s.call("POST", "/api/task", task_body("После переименования"), session=sid)
        self.assertEqual(code, 401, r)
        ppl = people_of(get_config(self.s))
        self.assertIs(ppl[new_name][PIN_SET_FLAG], False)
        self.assertNotEqual(login(self.s, new_name, EMP2_PIN)[0], 200)

    def test_remove_person_drops_session(self):
        code, sid = login(self.s, EMP, EMP_PIN)
        self.assertEqual(code, 200)
        code, r = save_config(self.s, self.adm,
                              lambda cfg: cfg.update(people=[p for p in cfg["people"] if p["name"] != EMP]))
        self.assertEqual(code, 200, r)
        self.assertEqual(self.s.call("POST", "/api/task", task_body("Удалён"), session=sid)[0], 401)

    def test_other_sessions_survive_config_save(self):
        sid = login(self.s, LEAD, LEAD_PIN)[1]
        code, r = save_config(self.s, self.adm, lambda c: c.update(ck_name="ЦК GenAI"))
        self.assertEqual(code, 200, r)
        self.assertEqual(self.s.call("POST", "/api/task", task_body("Сессия жива"), session=sid)[0], 200)


# ================================================================ критерий 3
class LoginTest(SharedServer):
    """Вход ФИО+PIN, отказ, блокировка 5 минут именно этого ФИО, выход, неактивный."""

    def test_login_ok_and_whoami(self):
        sid = self.sess(EMP)
        code, r = self.s.call("GET", API_WHOAMI, session=sid)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["user"]["name"], EMP)
        self.assertEqual(r["user"]["role"], "employee")
        self.assertIsNone(self.s.call("GET", API_WHOAMI)[1]["user"])

    def test_wrong_pin(self):
        code, r = login(self.s, LEAD, "0000")
        self.assertEqual(code, 401, r)
        self.sess(LEAD)   # успешный вход сбрасывает счётчик

    def test_unknown_person(self):
        self.assertEqual(login(self.s, "Несуществующий Некто", "1234")[0], 401)

    def test_lockout_after_five_failures_is_per_name(self):
        for i in range(4):
            code, r = login(self.s, EMP2, "0000")
            self.assertEqual(code, 401, (i, r))
        code, r = login(self.s, EMP2, "0000")
        self.assertEqual(code, 429, r)
        code, r = login(self.s, EMP2, EMP2_PIN)
        self.assertEqual(code, 429, "после 5 неверных PIN верный должен отклоняться 5 минут")
        self.assertIn(EMP2, r.get("error", ""))
        self.sess(EMP)   # другой человек входит
        self.sess(LEAD)
        # админ снимает блокировку, задав PIN заново
        code, r = self.s.call("POST", API_PIN, {"name": EMP2, "pin": EMP2_PIN}, session=self.adm)
        self.assertEqual(code, 200, r)
        self.sess(EMP2)

    def test_logout_invalidates_session(self):
        sid = self.sess(LEAD)
        self.assertEqual(self.new_task(sid, "До выхода")[0], 200)
        code, r = self.s.call("POST", API_LOGOUT, {}, session=sid)
        self.assertEqual(code, 200, r)
        code, r = self.new_task(sid, "После выхода")
        self.assertEqual(code, 401, r)
        self.assertIs(r.get("login"), True)
        self.assertIsNone(self.s.call("GET", API_WHOAMI, session=sid)[1]["user"])

    def test_inactive_cannot_login(self):
        code, r = login(self.s, GONE, GONE_PIN)
        self.assertEqual(code, 401, r)

    def test_no_pin_cannot_login(self):
        def add(cfg):
            cfg["people"].append({"name": "Беспинов Лев Львович", "email": "bespinov@example.test",
                                  "role": "employee", "team": LEAD, "active": True})
        self.assertEqual(save_config(self.s, self.adm, add)[0], 200)
        self.assertEqual(login(self.s, "Беспинов Лев Львович", "1234")[0], 401)

    def test_garbage_session(self):
        code, r = self.new_task("not-a-real-session")
        self.assertEqual(code, 401, r)


class SqliteLoginTest(LoginTest):
    storage = "sqlite"


# ================================================================ критерий 4
class WriteRequiresSessionTest(SharedServer):
    """Запись только с сессией; автор — из сессии, X-User игнорируется; чтение без сессии."""

    def test_writes_without_session_401(self):
        code, r = self.s.call("POST", "/api/task", task_body("Без сессии"), user=LEAD)
        self.assertEqual(code, 401, r)
        cfg = get_config(self.s)
        for path, body in (("/api/config", {"config": cfg, "expect_v": cfg["_v"]}),
                           ("/api/card", {"kind": "once", "id": "1", "title": "Карточка", "body": "Текст"}),
                           ("/api/llm", {"messages": [{"role": "user", "content": "привет"}]}),
                           (API_PIN, {"name": EMP, "pin": "4321"})):
            code, r = self.s.call("POST", path, body, user=LEAD)
            self.assertEqual(code, 401, (path, r))
        code, r = self.s.call("DELETE", "/api/task", {"kind": "once", "id": "1"}, user=LEAD)
        self.assertEqual(code, 401, r)
        self.assertNotIn("Без сессии", str(self.s.call("GET", "/api/state")[1]["tasks"]))

    def test_reads_without_session(self):
        self.assertEqual(self.s.call("GET", "/api/state")[0], 200)
        self.assertEqual(self.s.call("GET", "/api/llm")[0], 200)
        self.assertNotIn(self.s.call("GET", "/api/card?path=nothing.md")[0], (401, 403))

    def test_author_from_session_not_x_user(self):
        sid = self.sess(LEAD)   # с T-003 ставит задачи и правит постановку тимлид
        code, r = self.new_task(sid, "Автор из сессии", user=EMP)
        self.assertEqual(code, 200, r)
        t = r["task"]
        self.assertEqual(t["updated_by"], LEAD)
        self.assertEqual(t["history"][-1]["by"], LEAD)
        code, r = self.s.call("POST", "/api/task", {"task": {**t, "title": "Правка"}, "expect_v": t["_v"],
                                                    "action": "Правка: название"}, session=sid, user=EMP)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["task"]["updated_by"], LEAD)
        self.assertEqual(r["task"]["history"][-1]["by"], LEAD)

    def test_card_and_delete_with_session(self):
        # с T-003 карточку в «Новая» и удаление может только админ (права по этапам — test_t003)
        t = self.new_task(self.sess(LEAD), "Карточка и удаление")[1]["task"]
        sid = self.adm
        code, r = self.s.call("POST", "/api/card", {"kind": t["kind"], "id": t["id"], "title": "Карточка БЗ",
                                                    "body": "Выдуманный текст карточки"}, session=sid)
        self.assertEqual(code, 200, r)
        v = next(x["_v"] for x in self.s.call("GET", "/api/state")[1]["tasks"] if x["id"] == t["id"])
        code, r = self.s.call("DELETE", "/api/task", {"kind": t["kind"], "id": t["id"], "expect_v": v}, session=sid)
        self.assertEqual(code, 200, r)
        self.assertNotIn("Карточка и удаление", str(self.s.call("GET", "/api/state")[1]["tasks"]))

    def test_llm_with_session_passes_auth(self):
        code, r = self.s.call("POST", "/api/llm", {"messages": [{"role": "user", "content": "привет"}]},
                              session=self.sess(EMP))
        self.assertNotIn(code, (401, 403), r)   # ассистент выключен — ошибка возможна, но не авторизации


# ================================================================ критерий 5
class AdminTest(SharedServer):
    """Справочник/PIN не-админом — 403; админ правит любое поле задачи с записью в историю."""

    def test_admin_login_user(self):
        code, r = self.s.call("POST", API_LOGIN, {"admin": True, "pin": ADMIN_PIN})
        self.assertEqual(code, 200, r)
        self.assertIs(r["user"]["admin"], True)
        self.assertEqual(r["user"]["role"], "admin")
        self.assertIs(get_config(self.s) and self.s.call("GET", "/api/state")[1]["meta"]["admin_login"], True)

    def test_admin_wrong_pin(self):
        self.assertEqual(admin_login(self.s, "00000000")[0], 401)
        self.assertEqual(admin_login(self.s)[0], 200)

    def test_non_admin_forbidden(self):
        sid = self.sess(LEAD)
        code, r = save_config(self.s, sid, lambda c: c.update(ck_name="Захват"))
        self.assertEqual(code, 403, r)
        code, r = self.s.call("POST", API_PIN, {"name": EMP, "pin": "4321"}, session=sid)
        self.assertEqual(code, 403, r)
        code, r = self.s.call("POST", API_PIN, {"name": EMP, "reset": True}, session=self.sess(EMP))
        self.assertEqual(code, 403, r)
        self.assertNotEqual(get_config(self.s)["ck_name"], "Захват")
        self.sess(EMP)   # PIN не изменился

    def test_admin_reset_pin(self):
        name = "Сбросов Марк Андреевич"
        self.assertEqual(save_config(self.s, self.adm, lambda c: c["people"].append(
            {"name": name, "email": "sbrosov@example.test", "role": "employee", "team": LEAD, "active": True}))[0], 200)
        code, r = self.s.call("POST", API_PIN, {"name": name, "pin": "246810"}, session=self.adm)
        self.assertEqual(code, 200, r)
        sid = self.sess(name, "246810")
        code, r = self.s.call("POST", API_PIN, {"name": name, "reset": True}, session=self.adm)
        self.assertEqual(code, 200, r)
        self.assertEqual(r, {"name": name, "has_pin": False})
        self.assertIs(people_of(get_config(self.s))[name][PIN_SET_FLAG], False)
        self.assertEqual(login(self.s, name, "246810")[0], 401)
        self.assertEqual(self.new_task(sid, "После сброса PIN")[0], 401, "сброс PIN должен закрыть сессии")

    def test_new_pin_drops_old_sessions(self):
        name = "Сменов Ян Олегович"
        self.assertEqual(save_config(self.s, self.adm, lambda c: c["people"].append(
            {"name": name, "email": "smenov@example.test", "role": "employee", "team": LEAD, "active": True}))[0], 200)
        self.s.call("POST", API_PIN, {"name": name, "pin": "1357"}, session=self.adm)
        sid = self.sess(name, "1357")
        self.assertEqual(self.s.call("POST", API_PIN, {"name": name, "pin": "2468"}, session=self.adm)[0], 200)
        self.assertEqual(self.new_task(sid, "Старая сессия")[0], 401)
        self.assertEqual(login(self.s, name, "1357")[0], 401)
        self.sess(name, "2468")

    def test_admin_edits_any_field_with_history(self):
        sid = self.sess(LEAD)
        t = self.new_task(sid, "Правит админ")[1]["task"]
        code, r = self.s.call("POST", "/api/task", {"task": {**t, "status": "Завершена", "title": "Поправлено",
                                                             "executor": EMP}, "expect_v": t["_v"],
                                                    "action": "Правка: статус"}, session=self.adm, user=LEAD)
        self.assertEqual(code, 200, r)
        t2 = r["task"]
        self.assertEqual((t2["status"], t2["title"], t2["executor"]), ("Завершена", "Поправлено", EMP))
        self.assertTrue(t2["history"][-1]["action"].startswith(HISTORY_ADMIN), t2["history"][-1])
        self.assertNotIn("Правка: ", t2["history"][-1]["action"])
        self.assertNotEqual(t2["history"][-1]["by"], LEAD)   # не из X-User


class NoAdminPinTest(unittest.TestCase):
    def test_admin_login_unavailable_without_stdin_pin(self):
        s = Server("json")
        try:
            self.assertIs(s.call("GET", "/api/state")[1]["meta"]["admin_login"], False)
            for pin in (ADMIN_PIN, "0000", "12345678"):
                code, r = admin_login(s, pin)
                self.assertEqual(code, 403, (pin, r))
        finally:
            s.stop()

    def test_bad_admin_pin_on_stdin_fails_start(self):
        for bad in ("", "12", "abcd"):
            with self.assertRaises(RuntimeError, msg=repr(bad)):
                Server("json", args=ADMIN_ARGS, stdin_lines=[bad]).stop()


# ================================================================ критерий 6
class SecretsNotInProcessTest(unittest.TestCase):
    """Админский PIN и пароль GP — только через stdin, не в окружении и не в аргументах процесса."""

    def test_admin_pin_alone(self):
        s = admin_server()
        try:
            self.assertEqual(admin_login(s)[0], 200)
            self.assertEqual(proc_secrets_visible(s.proc.pid, [ADMIN_PIN]), [])
        finally:
            s.stop()

    def test_admin_pin_and_gp_password(self):
        gp_user, gp_pass = "test_gp_user_x9", "Gp-Pass-Секрет-417"
        s = Server("sqlite", args=["--creds-stdin", *ADMIN_ARGS], stdin_lines=[gp_user, gp_pass, ADMIN_PIN])
        try:
            self.assertEqual(admin_login(s)[0], 200, "PIN третьей строкой после логина и пароля GP")
            self.assertEqual(proc_secrets_visible(s.proc.pid, [gp_pass, ADMIN_PIN]), [])
        finally:
            s.stop()

    def test_start_sh_passes_secrets_via_stdin(self):
        sh = (helpers.ROOT / "start.sh").read_text("utf-8")
        self.assertIn("--admin-pin-stdin", sh)
        for bad in ("export GP_PASS", "export GP_PASSWORD", "export REG_ADMIN_PIN", "export ADMIN_PIN",
                    "--admin-pin=", "--admin-pin "):
            self.assertNotIn(bad, sh)


# ================================================================ критерий 7
class GigaCodeCommandsTest(SharedServer):
    """ctx/tasks/task без PIN; create-task без терминала — понятный отказ, задача не создаётся."""

    def test_ctx_tasks_task_without_pin(self):
        t = self.new_task(self.sess(LEAD), "Видна в tasks")[1]["task"]
        r = run_cli(self.s, "ctx")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(EMP, r.stdout)
        r = run_cli(self.s, "tasks")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Видна в tasks", r.stdout)
        r = run_cli(self.s, "task", t["kind"], str(t["id"]))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Видна в tasks", r.stdout)

    def check_create_refused(self, title, **kw):
        before = len(self.s.call("GET", "/api/state")[1]["tasks"])
        r = run_cli(self.s, "create-task", "-", "--by", LEAD,
                    input_text=json.dumps({"title": title, "teamlead": LEAD}, ensure_ascii=False), **kw)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("веб-форм", r.stderr + r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        tasks = self.s.call("GET", "/api/state")[1]["tasks"]
        self.assertEqual(len(tasks), before)
        self.assertNotIn(title, str(tasks))

    def test_create_task_without_tty(self):
        self.check_create_refused("Из GigaCode без терминала", detach=True)

    def test_create_task_no_tty_flag(self):
        self.check_create_refused("Из GigaCode с REG_NO_TTY", env={"REG_NO_TTY": "1"})


# ================================================================ фронтенд (статически; браузерных тестов нет)
class FrontendSessionTest(unittest.TestCase):
    """Сессия в браузере — в памяти и в sessionStorage (только ключ ck.session, T-005); запись идёт с X-Session."""
    html = helpers.frontend_text()
    app = (helpers.ROOT / "app.py").read_text("utf-8")

    def test_session_header_sent(self):
        self.assertIn("'X-Session'", self.html)

    def test_session_and_pin_not_persisted(self):
        import re
        # localStorage — ни сессии, ни PIN, ни токена
        bad = re.compile(r"(store\.set|localStorage\.setItem)\(\s*['\"](ck\.)?(session|auth|pin|sid|token)", re.I)
        self.assertIsNone(bad.search(self.html))
        # sessionStorage — только ключ ck.session (решение пользователя, T-005); PIN — нигде
        keys = re.findall(r"sessionStorage\.(?:setItem|getItem|removeItem)\(\s*([^,)]*)", self.html)
        self.assertTrue(keys)
        self.assertEqual({k.strip() for k in keys}, {"'ck.session'"})
        self.assertIsNone(re.search(r"(sessionStorage|localStorage)\.setItem\([^)]*\bpin", self.html, re.I))
        self.assertNotIn("document.cookie", self.html)
        # сервер сам куки не ставит (сессия — не в куке); слово в комментариях (прокси их отбрасывает) не в счёт
        self.assertIsNone(re.search(r"send_header\(\s*['\"]set-cookie", self.app, re.I))
        self.assertIsNone(re.search(r"\bSimpleCookie\b|http\.cookies", self.app))
        self.assertIsNone(re.search(r"store\.set\([^)]*AUTH", self.html))

if __name__ == "__main__":
    unittest.main()
