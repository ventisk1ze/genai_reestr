"""T-007: команды — тимлид и заместитель, права по команде задачи.
Критерии приёмки — docs/backlog.md (T-007); контракт — от тимлида, имена полей — от dev-t007 (константы ниже).
Все данные выдуманные."""
import json
import os
import pty
import select
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import APP, ROOT, Server, clean_env, frontend_text, run_cli# noqa: E402

# ---- контракт (правится только здесь)
ADMIN_ARGS = ["--admin-pin-stdin"]
LEAD_TEAMS = "lead_teams"       # в user из /api/login и /api/whoami: команды, где он лид или зам
CTX_TEAMS = "teams"             # ключ команд в выводе `app.py ctx`
TEAM_FLAG = "--team"            # create-task: выбор команды
ACTION_KEY = "action_code"
TABLE_PREFIX = "ck_registry_"

NEW, WORK, REVIEW, REWORK, APPROVED, DONE, ACCEPTED = (
    "Новая", "В работе", "На согласовании", "На доработке", "Согласована", "Завершена", "Принята")

ADMIN_PIN = "52738164"
LEAD_A = "Командиров Антон Тестович"
DEP_A = "Замов Денис Тестович"
EXE_A = "Делова Нина Тестовна"
SUB_A = "Подменова Ирина Тестовна"
LEAD_B = "Соседкин Борис Тестович"
OUT_B = "Чужой Виктор Тестович"
MULTI = "Двоякий Михаил Тестович"      # лид двух команд — В и Г
PINS = {LEAD_A: "11223344", DEP_A: "22334455", EXE_A: "3344", SUB_A: "44556", LEAD_B: "55667788",
        OUT_B: "6677", MULTI: "77889900"}
TA, TB, TC, TD = "team-a", "team-b", "team-c", "team-d"
TEAMS = [
    {"id": TA, "name": "Выдуманная команда А", "lead": LEAD_A, "deputy": DEP_A},
    {"id": TB, "name": "Выдуманная команда Б", "lead": LEAD_B, "deputy": ""},
    {"id": TC, "name": "Выдуманная команда В", "lead": MULTI, "deputy": ""},
    {"id": TD, "name": "Выдуманная команда Г", "lead": MULTI, "deputy": ""},
]
PEOPLE = [
    {"name": LEAD_A, "email": "komandirov@example.test", "team": TA, "active": True},
    {"name": DEP_A, "email": "zamov@example.test", "team": TA, "active": True},
    {"name": EXE_A, "email": "delova@example.test", "team": TA, "active": True},
    {"name": SUB_A, "email": "podmenova@example.test", "team": TA, "active": True},
    {"name": LEAD_B, "email": "sosedkin@example.test", "team": TB, "active": True},
    {"name": OUT_B, "email": "chuzhoy@example.test", "team": TB, "active": True},
    {"name": MULTI, "email": "dvoyakiy@example.test", "team": TC, "active": True},
]
POSTANOVKA = {"title": "Выдуманная задача T-007", "description": "Подготовить тестовый обзор",
              "executor": EXE_A, "substitute": SUB_A, "deadline": "2030-12-31"}
RESULT = {"result": "Обзор", "result_desc": "Выдуманный обзор готов", "report_event": "", "report_date": ""}
DECISION = {"block": "KM-01", "level": "ЦК", "bz_save": "да", "bz_reason": "Пригодится команде"}


def team_ids(lst):
    """Список команд пользователя → множество id (элемент — id или объект с id)."""
    return {x["id"] if isinstance(x, dict) else x for x in (lst or [])}


class Base(unittest.TestCase):
    """Сервер с админом, четырьмя командами и семью людьми на класс."""
    storage = "json"

    @classmethod
    def setUpClass(cls):
        cls.s = Server(cls.storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN])
        try:
            code, r = cls.s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
            assert code == 200, r
            cls.adm = r["session"]
            cfg = cls.s.call("GET", "/api/state")[1]["config"]
            cfg["teams"] = [dict(t) for t in TEAMS]
            cfg["people"] = [dict(p) for p in PEOPLE]
            code, r = cls.s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=cls.adm)
            assert code == 200, r
            cls.sid, cls.users = {"admin": cls.adm}, {}
            for name, pin in PINS.items():
                code, r = cls.s.call("POST", "/api/admin/pin", {"name": name, "pin": pin}, session=cls.adm)
                assert code == 200, (name, r)
                code, r = cls.s.call("POST", "/api/login", {"name": name, "pin": pin})
                assert code == 200, (name, r)
                cls.sid[name], cls.users[name] = r["session"], r["user"]
        except Exception:
            cls.s.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    # ---- хелперы
    def config(self):
        return self.s.call("GET", "/api/state")[1]["config"]

    def save_config(self, cfg):
        return self.s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=self.adm)

    def whoami(self, who):
        code, r = self.s.call("GET", "/api/whoami", session=self.sid[who])
        self.assertEqual(code, 200, r)
        return r["user"]

    def get(self, t):
        tasks = self.s.call("GET", "/api/state")[1]["tasks"]
        return next(x for x in tasks if x["kind"] == t["kind"] and x["id"] == t["id"])

    def post(self, who, task, code=None, expect_v="cur", **extra):
        body = {"task": task, "action": "Тест", **extra}
        if code:
            body[ACTION_KEY] = code
        if expect_v == "cur":
            expect_v = task.get("_v")
        if expect_v is not None:
            body["expect_v"] = expect_v
        return self.s.call("POST", "/api/task", body, session=self.sid[who])

    def create_raw(self, who, kind="once", **fields):
        task = {"kind": kind, "id": None, **POSTANOVKA, **fields}
        if kind == "regular":
            task.setdefault("regularity", "еженедельно")
        return self.post(who, task, "create", expect_v=None)

    def create(self, who=LEAD_A, kind="once", **fields):
        code, r = self.create_raw(who, kind, **fields)
        self.assertEqual(code, 200, r)
        return r["task"]

    def act(self, who, t, code, ok=True, params=None, **changes):
        cur = self.get(t)
        c, r = self.post(who, {**cur, **changes}, code, **({"params": params} if params else {}))
        if ok:
            self.assertEqual(c, 200, (code, who, r))
            return r["task"]
        return c, r

    def refused(self, who, t, code, expected=403, params=None, **changes):
        before = self.get(t)
        c, r = self.act(who, t, code, ok=False, params=params, **changes)
        self.assertEqual(c, expected, (code, who, r))
        self.assertRegex(r.get("error", ""), "[А-Яа-яЁё]", r)
        self.assertEqual(self.get(t)["_v"], before["_v"])
        return r

    def to(self, status, lead=LEAD_A, kind="once", **fields):
        """Создать задачу от имени lead (лид или зам) и довести до статуса; тимлидские шаги — тоже он."""
        t = self.create(lead, kind, **fields)
        steps = {NEW: [], WORK: ["take"], REVIEW: ["take", "review"],
                 APPROVED: ["take", "review", "approve"], DONE: ["take", "review", "approve", "finish"],
                 ACCEPTED: ["take", "review", "approve", "finish", "accept"]}[status]
        for s in steps:
            if s == "take":
                t = self.act(EXE_A, t, "take")
            elif s == "review":
                t = self.act(EXE_A, t, "to_review", **RESULT)
            elif s == "approve":
                t = self.act(lead, t, "approve", **DECISION)
            elif s == "finish":
                t = self.act(EXE_A, t, "finish", bz_card="Размещена в АС Ретривер: выдуманная ссылка")
            elif s == "accept":
                t = self.act(lead, t, "accept")
        self.assertEqual(t["status"], status)
        return t


# ================================================================ критерий 7: login / whoami
class WhoamiTest(Base):
    def test_login_lists_lead_teams(self):
        exp = {LEAD_A: {TA}, DEP_A: {TA}, LEAD_B: {TB}, MULTI: {TC, TD}, EXE_A: set(), SUB_A: set(), OUT_B: set()}
        for who, teams in exp.items():
            with self.subTest(who=who):
                u = self.users[who]
                self.assertIn(LEAD_TEAMS, u, u)
                self.assertEqual(team_ids(u[LEAD_TEAMS]), teams)
                self.assertEqual(u["role"], "teamlead" if teams else "employee")

    def test_whoami_same_as_login(self):
        for who in (LEAD_A, DEP_A, EXE_A, MULTI):
            with self.subTest(who=who):
                u = self.whoami(who)
                self.assertEqual(team_ids(u[LEAD_TEAMS]), team_ids(self.users[who][LEAD_TEAMS]))
                self.assertEqual(u["role"], self.users[who]["role"])
                self.assertFalse(u["admin"])

    def test_admin_whoami(self):
        u = self.whoami("admin")
        self.assertEqual(u["role"], "admin")
        self.assertTrue(u["admin"])

    def test_people_team_is_id(self):
        ppl = {p["name"]: p for p in self.config()["people"]}
        self.assertEqual(ppl[EXE_A]["team"], TA)
        self.assertEqual(ppl[OUT_B]["team"], TB)
        teams = {t["id"]: t for t in self.config()["teams"]}
        self.assertEqual((teams[TA]["lead"], teams[TA]["deputy"]), (LEAD_A, DEP_A))
        self.assertEqual(teams[TA]["name"], "Выдуманная команда А")

    def test_no_pin_hashes_leak(self):
        """Регрессия T-002: в справочнике для клиента нет соли и хеша."""
        txt = json.dumps(self.config(), ensure_ascii=False)
        self.assertNotIn('"salt"', txt)
        self.assertNotIn('"hash"', txt)


# ================================================================ критерий 3: создание задачи
class CreateTest(Base):
    def test_lead_creates_in_own_team(self):
        t = self.create(LEAD_A)
        self.assertEqual((t["team"], t["teamlead"], t["status"]), (TA, LEAD_A, NEW))

    def test_deputy_creates_and_is_teamlead(self):
        t = self.create(DEP_A)
        self.assertEqual((t["team"], t["teamlead"]), (TA, DEP_A))

    def test_forged_team_and_teamlead(self):
        """Лид команды А не может поставить задачу в команду Б или подписать её чужим тимлидом."""
        code, r = self.create_raw(LEAD_A, team=TB, teamlead=LEAD_B)
        if code == 200:
            self.assertEqual((r["task"]["team"], r["task"]["teamlead"]), (TA, LEAD_A))
        else:
            self.assertIn(code, (400, 403), r)
            self.assertRegex(r.get("error", ""), "[А-Яа-я]")
        code, r = self.create_raw(LEAD_A, teamlead=LEAD_B)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["task"]["teamlead"], LEAD_A)

    def test_several_teams_needs_choice(self):
        before = len(self.s.call("GET", "/api/state")[1]["tasks"])
        code, r = self.create_raw(MULTI)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), "[Кк]оманд", r)
        self.assertEqual(len(self.s.call("GET", "/api/state")[1]["tasks"]), before)
        for team in (TC, TD):
            with self.subTest(team=team):
                t = self.create(MULTI, team=team)
                self.assertEqual((t["team"], t["teamlead"]), (team, MULTI))

    def test_several_teams_foreign_choice(self):
        code, r = self.create_raw(MULTI, team=TA)
        self.assertIn(code, (400, 403), r)
        self.assertRegex(r.get("error", ""), "[А-Яа-я]")

    def test_employee_cannot_create(self):
        for who in (EXE_A, SUB_A, OUT_B):
            with self.subTest(who=who):
                code, r = self.create_raw(who)
                self.assertEqual(code, 403, r)
                self.assertRegex(r.get("error", ""), "[А-Яа-я]")
                code, r = self.create_raw(who, team=TA)
                self.assertEqual(code, 403, r)

    def test_admin_creates_in_any_team(self):
        t = self.create("admin", team=TB, teamlead=LEAD_B)
        self.assertEqual((t["team"], t["teamlead"]), (TB, LEAD_B))
        t2 = self.act(LEAD_B, t, "edit", description="Лид Б правит задачу своей команды")
        self.assertEqual(t2["description"], "Лид Б правит задачу своей команды")


# ================================================================ критерий 4: права лида и зама
class RightsTest(Base):
    OTHERS = (LEAD_B, MULTI)

    def pairs(self):
        """(кто поставил, кто действует как тимлид): зам действует по задаче лида и наоборот."""
        return ((LEAD_A, DEP_A), (DEP_A, LEAD_A), (LEAD_A, LEAD_A), (DEP_A, DEP_A))

    def test_edit_new(self):
        for author, actor in self.pairs():
            with self.subTest(author=author, actor=actor):
                t = self.create(author)
                for who in self.OTHERS + (EXE_A, OUT_B):
                    self.refused(who, t, "edit", description="Чужая правка постановки")
                t = self.act(actor, t, "edit", description=f"Правит {actor}")
                self.assertEqual(t["description"], f"Правит {actor}")
                self.assertEqual(t["teamlead"], author)   # тимлид задачи — кто поставил

    def test_approve(self):
        for author, actor in self.pairs():
            with self.subTest(author=author, actor=actor):
                t = self.to(REVIEW, author)
                for who in self.OTHERS + (EXE_A, OUT_B):
                    self.refused(who, t, "approve", **DECISION)
                self.assertEqual(self.act(actor, t, "approve", **DECISION)["status"], APPROVED)

    def test_rework(self):
        p = {"comment": "Добавить выводы", "deadline": "2031-01-15"}
        for author, actor in self.pairs():
            with self.subTest(author=author, actor=actor):
                t = self.to(REVIEW, author)
                for who in self.OTHERS:
                    self.refused(who, t, "rework", params=p)
                self.assertEqual(self.act(actor, t, "rework", params=p)["status"], REWORK)

    def test_accept_and_return(self):
        for author, actor in self.pairs():
            with self.subTest(author=author, actor=actor):
                t = self.to(DONE, author)
                for who in self.OTHERS + (EXE_A,):
                    self.refused(who, t, "accept")
                    self.refused(who, t, "return", params={"to": "approved", "comment": "Чужой возврат"})
                t = self.act(actor, t, "return", params={"to": "approved", "comment": "Карточка неполная"})
                self.assertEqual(t["status"], APPROVED)
                t = self.act(EXE_A, t, "finish")
                self.assertEqual(self.act(actor, t, "accept")["status"], ACCEPTED)

    def test_new_cycle_and_feedback(self):
        for author, actor in self.pairs():
            with self.subTest(author=author, actor=actor):
                t = self.to(WORK, author, kind="regular")
                for who in self.OTHERS + (EXE_A,):
                    self.refused(who, t, "feedback", params={"text": "Чужая оценка"})
                t = self.act(actor, t, "feedback", params={"text": f"Оценка от {actor}"})
                self.assertEqual(t["feedback"][-1]["by"], actor)
                t = self.act(EXE_A, t, "to_review", **RESULT)
                t = self.act(actor, t, "approve", **DECISION)
                t = self.act(EXE_A, t, "finish", bz_card="Размещена в АС Ретривер")
                t = self.act(actor, t, "accept")
                for who in self.OTHERS + (EXE_A,):
                    self.refused(who, t, "new_cycle", params={"deadline": "2031-06-30"})
                t = self.act(actor, t, "new_cycle", params={"deadline": "2031-06-30"})
                self.assertEqual((t["status"], t["team"]), (NEW, TA))

    def test_executor_rights_unchanged(self):
        """Исполнитель из другой команды — права исполнителя по полю executor, как в T-003."""
        t = self.create(LEAD_A, executor=OUT_B, substitute="")
        self.refused(EXE_A, t, "take")
        t = self.act(OUT_B, t, "take")
        self.assertEqual(t["status"], WORK)
        self.refused(LEAD_B, t, "edit", result="Лид чужой команды правит результат")

    def test_team_not_forged_on_edit(self):
        t = self.create(LEAD_A)
        before = self.get(t)
        c, r = self.act(DEP_A, t, "edit", ok=False, team=TB)
        after = self.get(t)
        self.assertEqual(after["team"], TA, "команду задачи клиент подменить не может")
        if c != 200:
            self.assertEqual(c, 403, r)
            self.assertEqual(after["_v"], before["_v"])
        c, r = self.act(EXE_A, self.act(EXE_A, t, "take"), "edit", ok=False, team=TB, result="Черновик")
        self.assertEqual(self.get(t)["team"], TA)

    def test_admin_can_move_task(self):
        t = self.create(LEAD_A)
        t = self.act("admin", t, "edit", team=TB)
        self.assertEqual(t["team"], TB)


class SqliteRightsTest(RightsTest):
    storage = "sqlite"


class SqliteCreateTest(CreateTest):
    storage = "sqlite"


# ================================================================ критерии 1–2: справочник команд
class ConfigTest(Base):
    def team(self, cfg, tid):
        return next(t for t in cfg["teams"] if t["id"] == tid)

    def test_rename_keeps_tasks(self):
        t = self.to(REVIEW, LEAD_A)
        cfg = self.config()
        self.team(cfg, TA)["name"] = "Переименованная команда А"
        code, r = self.save_config(cfg)
        self.assertEqual(code, 200, r)
        self.assertEqual(self.get(t)["team"], TA)
        self.assertEqual(self.team(self.config(), TA)["name"], "Переименованная команда А")
        self.assertEqual(self.act(DEP_A, t, "approve", **DECISION)["status"], APPROVED)
        cfg = self.config()
        self.team(cfg, TA)["name"] = "Выдуманная команда А"
        self.assertEqual(self.save_config(cfg)[0], 200)

    def test_delete_team_with_tasks_refused(self):
        t = self.create(LEAD_B)
        self.assertEqual(t["team"], TB)
        cfg = self.config()
        cfg["teams"] = [x for x in cfg["teams"] if x["id"] != TB]
        code, r = self.save_config(cfg)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), "[Зз]адач", r)
        self.assertIn(TB, {x["id"] for x in self.config()["teams"]})

    def test_create_and_delete_empty_team(self):
        cfg = self.config()
        cfg["teams"].append({"id": "team-new", "name": "Выдуманная пустая команда", "lead": OUT_B, "deputy": ""})
        code, r = self.save_config(cfg)
        self.assertEqual(code, 200, r)
        ids = {x["id"] for x in r["config"]["teams"]}
        new_id = "team-new" if "team-new" in ids else next(
            x["id"] for x in r["config"]["teams"] if x["name"] == "Выдуманная пустая команда")
        self.assertTrue(new_id)
        self.assertIn(new_id, team_ids(self.whoami(OUT_B)[LEAD_TEAMS]))
        self.assertEqual(self.whoami(OUT_B)["role"], "teamlead")
        cfg = self.config()
        cfg["teams"] = [x for x in cfg["teams"] if x["id"] != new_id]
        code, r = self.save_config(cfg)
        self.assertEqual(code, 200, r)
        self.assertEqual(self.whoami(OUT_B)["role"], "employee")

    def test_lead_equals_deputy_refused(self):
        cfg = self.config()
        self.team(cfg, TB)["deputy"] = LEAD_B
        code, r = self.save_config(cfg)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), "[А-Яа-я]")
        self.assertEqual(self.team(self.config(), TB)["deputy"], "")

    def test_unknown_lead_refused(self):
        cfg = self.config()
        self.team(cfg, TB)["deputy"] = "Несуществующий Тест Тестович"
        code, r = self.save_config(cfg)
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), "[А-Яа-я]")

    def test_deputy_change_applies_to_live_session(self):
        """Сняли зама — его живая сессия теряет права; назначили обратно — права вернулись."""
        t = self.to(REVIEW, LEAD_A)
        cfg = self.config()
        self.team(cfg, TA)["deputy"] = ""
        self.assertEqual(self.save_config(cfg)[0], 200)
        try:
            sid = self.sid[DEP_A]
            code, r = self.s.call("GET", "/api/whoami", session=sid)
            if r.get("user") is None:   # сервер мог закрыть сессию — войти заново
                self.sid[DEP_A] = self.s.call("POST", "/api/login", {"name": DEP_A, "pin": PINS[DEP_A]})[1]["session"]
            u = self.whoami(DEP_A)
            self.assertEqual((u["role"], team_ids(u[LEAD_TEAMS])), ("employee", set()))
            self.refused(DEP_A, t, "approve", **DECISION)
            code, r = self.create_raw(DEP_A)
            self.assertEqual(code, 403, r)
        finally:
            cfg = self.config()
            self.team(cfg, TA)["deputy"] = DEP_A
            self.assertEqual(self.save_config(cfg)[0], 200)
        if self.s.call("GET", "/api/whoami", session=self.sid[DEP_A])[1].get("user") is None:
            self.sid[DEP_A] = self.s.call("POST", "/api/login", {"name": DEP_A, "pin": PINS[DEP_A]})[1]["session"]
        self.assertEqual(self.act(DEP_A, t, "approve", **DECISION)["status"], APPROVED)

    def test_manual_role_ignored(self):
        """Роль в карточке человека больше не задаётся вручную: role:'teamlead' без команды — сотрудник."""
        cfg = self.config()
        for p in cfg["people"]:
            if p["name"] == OUT_B:
                p["role"] = "teamlead"
        self.assertEqual(self.save_config(cfg)[0], 200)
        if self.s.call("GET", "/api/whoami", session=self.sid[OUT_B])[1].get("user") is None:
            self.sid[OUT_B] = self.s.call("POST", "/api/login", {"name": OUT_B, "pin": PINS[OUT_B]})[1]["session"]
        self.assertEqual(self.whoami(OUT_B)["role"], "employee")
        code, r = self.create_raw(OUT_B)
        self.assertEqual(code, 403, r)


class SqliteConfigTest(ConfigTest):
    storage = "sqlite"


# ================================================================ критерий 6: миграция
OLD_LEAD, OLD_LEAD2 = "Староверов Пётр Тестович", "Прежнева Анна Тестовна"
OLD_EMP, OLD_EMP2 = "Давнишний Олег Тестович", "Бывалова Ольга Тестовна"
OLD_PINS = {OLD_LEAD: "10203040", OLD_LEAD2: "20304050", OLD_EMP: "3040", OLD_EMP2: "40506"}
OLD_CONFIG = {
    "_v": 5, "ck_name": "ЦК GenAI",
    "blocks": [{"code": "KM-01", "name": "Выдуманный блок"}],
    "people": [
        {"name": OLD_LEAD, "email": "staroverov@example.test", "role": "teamlead", "team": OLD_LEAD, "active": True},
        {"name": OLD_LEAD2, "email": "prezhneva@example.test", "role": "teamlead", "team": OLD_LEAD2, "active": True},
        {"name": OLD_EMP, "email": "davnishniy@example.test", "role": "employee", "team": OLD_LEAD, "active": True},
        {"name": OLD_EMP2, "email": "byvalova@example.test", "role": "employee", "team": OLD_LEAD2, "active": True},
    ],
}


def old_task(tid, teamlead, status, executor):
    return {"id": tid, "_v": 3, "kind": "once", "title": f"Старая выдуманная задача {tid}",
            "description": "Задача до T-007, без поля team", "teamlead": teamlead, "executor": executor,
            "substitute": "", "deadline": "2030-06-30", "deadline_initial": "2030-06-30", "status": status,
            "created": "2026-09-01", "history": [{"ts": "2026-09-01 10:00", "by": teamlead, "action": "Поставлена"}],
            "result": "Старый результат" if status != NEW else "", "result_desc": "Описание" if status != NEW else "",
            "iterations": [], "cycles": [], "feedback": [], "cards": []}


OLD_TASKS = [old_task(1, OLD_LEAD, REVIEW, OLD_EMP), old_task(2, OLD_LEAD2, NEW, OLD_EMP2),
             old_task(3, OLD_LEAD, DONE, OLD_EMP)]


def seed_old(storage, root):
    """Реестр формата до T-007 прямо в хранилище: справочник без teams, задачи без team."""
    if storage == "json":
        d = root / "json"
        (d / "once").mkdir(parents=True)
        (d / "regular").mkdir()
        (d / "cards").mkdir()
        (d / "config.json").write_text(json.dumps(OLD_CONFIG, ensure_ascii=False), "utf-8")
        for t in OLD_TASKS:
            (d / "once" / f"{t['id']:04d}.json").write_text(json.dumps(t, ensure_ascii=False), "utf-8")
        return
    con = sqlite3.connect(root / "registry.db")
    p = TABLE_PREFIX
    con.execute(f"CREATE TABLE {p}tasks (kind TEXT NOT NULL, id INTEGER NOT NULL, v INTEGER NOT NULL, "
                f"data TEXT NOT NULL, PRIMARY KEY (kind, id))")
    con.execute(f"CREATE TABLE {p}config (id INTEGER PRIMARY KEY, v INTEGER NOT NULL, data TEXT NOT NULL)")
    cfg = {k: v for k, v in OLD_CONFIG.items() if k != "_v"}
    con.execute(f"INSERT INTO {p}config (id, v, data) VALUES (1, 5, ?)", (json.dumps(cfg, ensure_ascii=False),))
    for t in OLD_TASKS:
        data = {k: v for k, v in t.items() if k not in ("kind", "id", "_v")}
        con.execute(f"INSERT INTO {p}tasks (kind, id, v, data) VALUES ('once', ?, 3, ?)",
                    (t["id"], json.dumps(data, ensure_ascii=False)))
    con.commit()
    con.close()


class MigrationTest(unittest.TestCase):
    storage = "json"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        seed_old(cls.storage, Path(cls.tmp.name))
        cls.s = Server(cls.storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], tmp=cls.tmp)
        cls.cfg_before_pins = cls.s.call("GET", "/api/state")[1]["config"]   # до любых записей — путь чтения
        cls.adm = cls.s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})[1]["session"]
        cls.sid, cls.users = {}, {}
        for name, pin in OLD_PINS.items():
            code, r = cls.s.call("POST", "/api/admin/pin", {"name": name, "pin": pin}, session=cls.adm)
            assert code == 200, (name, r)
            code, r = cls.s.call("POST", "/api/login", {"name": name, "pin": pin})
            assert code == 200, (name, r)
            cls.sid[name], cls.users[name] = r["session"], r["user"]

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()
        cls.tmp.cleanup()

    def team_of(self, cfg, lead):
        teams = [t for t in cfg.get("teams") or [] if t.get("lead") == lead]
        self.assertEqual(len(teams), 1, (lead, cfg.get("teams")))
        return teams[0]

    def tasks(self):
        return {t["id"]: t for t in self.s.call("GET", "/api/state")[1]["tasks"]}

    def test_teams_by_leads_on_read(self):
        cfg = self.cfg_before_pins
        for lead in (OLD_LEAD, OLD_LEAD2):
            with self.subTest(lead=lead):
                t = self.team_of(cfg, lead)
                self.assertEqual(t["name"], lead)   # название — ФИО лида
                self.assertFalse(t.get("deputy"))
                self.assertTrue(t["id"])

    def test_people_keep_membership(self):
        for cfg in (self.cfg_before_pins, self.s.call("GET", "/api/state")[1]["config"]):
            ppl = {p["name"]: p for p in cfg["people"]}
            ta, tb = self.team_of(cfg, OLD_LEAD)["id"], self.team_of(cfg, OLD_LEAD2)["id"]
            self.assertEqual({ppl[OLD_LEAD]["team"], ppl[OLD_EMP]["team"]}, {ta})
            self.assertEqual({ppl[OLD_LEAD2]["team"], ppl[OLD_EMP2]["team"]}, {tb})
            self.assertEqual(ppl[OLD_EMP]["email"], "davnishniy@example.test")

    def test_ids_stable_after_save(self):
        cfg = self.s.call("GET", "/api/state")[1]["config"]
        self.assertEqual(self.team_of(cfg, OLD_LEAD)["id"], self.team_of(self.cfg_before_pins, OLD_LEAD)["id"])

    def test_roles_from_teams(self):
        self.assertEqual(self.users[OLD_LEAD]["role"], "teamlead")
        self.assertEqual(len(team_ids(self.users[OLD_LEAD][LEAD_TEAMS])), 1)
        self.assertEqual(self.users[OLD_EMP]["role"], "employee")
        self.assertEqual(team_ids(self.users[OLD_EMP][LEAD_TEAMS]), set())

    def test_no_task_loses_teamlead(self):
        tasks = self.tasks()
        self.assertEqual({i: tasks[i]["teamlead"] for i in (1, 2, 3)}, {1: OLD_LEAD, 2: OLD_LEAD2, 3: OLD_LEAD})
        self.assertEqual(tasks[1]["history"][0]["by"], OLD_LEAD)
        cfg = self.s.call("GET", "/api/state")[1]["config"]
        # у старых задач команда вычисляется сервером по полю «Тимлид»
        self.assertEqual(tasks[2]["team"], self.team_of(cfg, OLD_LEAD2)["id"])
        self.assertEqual(tasks[3]["team"], self.team_of(cfg, OLD_LEAD)["id"])

    def post(self, who, t, code, **kw):
        cur = self.tasks()[t]
        body = {"task": {**cur, **kw.pop("fields", {})}, ACTION_KEY: code, "expect_v": cur["_v"], **kw}
        return self.s.call("POST", "/api/task", body, session=self.sid[who])

    def test_old_task_rights_by_teamlead(self):
        code, r = self.post(OLD_LEAD2, 1, "approve", fields=DECISION)
        self.assertEqual(code, 403, r)
        code, r = self.post(OLD_EMP, 1, "approve", fields=DECISION)
        self.assertEqual(code, 403, r)
        code, r = self.post(OLD_LEAD, 1, "approve", fields=DECISION)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["task"]["teamlead"], OLD_LEAD)
        code, r = self.post(OLD_LEAD, 3, "accept")
        self.assertEqual(code, 200, r)
        code, r = self.post(OLD_LEAD, 2, "edit", fields={"description": "Чужая старая задача"})
        self.assertEqual(code, 403, r)
        code, r = self.post(OLD_LEAD2, 2, "edit", fields={"description": "Своя старая задача"})
        self.assertEqual(code, 200, r)

    def test_new_task_gets_migrated_team(self):
        task = {"kind": "once", "id": None, "title": "Новая выдуманная задача после миграции",
                "executor": OLD_EMP, "deadline": "2031-01-01"}
        code, r = self.s.call("POST", "/api/task", {"task": task, ACTION_KEY: "create"}, session=self.sid[OLD_LEAD])
        self.assertEqual(code, 200, r)
        cfg = self.s.call("GET", "/api/state")[1]["config"]
        self.assertEqual(r["task"]["team"], self.team_of(cfg, OLD_LEAD)["id"])

    def test_delete_migrated_team_with_old_tasks_refused(self):
        """Старые задачи без поля team тоже держат команду (по полю «Тимлид»)."""
        cfg = self.s.call("GET", "/api/state")[1]["config"]
        tid = self.team_of(cfg, OLD_LEAD2)["id"]
        cfg["teams"] = [t for t in cfg["teams"] if t["id"] != tid]
        code, r = self.s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg["_v"]}, session=self.adm)
        self.assertEqual(code, 400, r)


class SqliteMigrationTest(MigrationTest):
    storage = "sqlite"


class SaveWithoutTeamsTest(Base):
    """Клиент до T-007 шлёт справочник без teams — команды остаются прежние, состав по ФИО лида — в id."""

    def test_save_without_teams_keeps_teams(self):
        cfg = self.config()
        cfg.pop("teams", None)
        for p in cfg["people"]:
            if p["name"] == OUT_B:
                p["team"] = LEAD_A        # старый клиент: команда = ФИО лида
        code, r = self.save_config(cfg)
        self.assertEqual(code, 200, r)
        cfg = self.config()
        self.assertEqual({t["id"] for t in cfg["teams"]}, {TA, TB, TC, TD})
        ppl = {p["name"]: p for p in cfg["people"]}
        self.assertEqual(ppl[OUT_B]["team"], TA)
        self.assertEqual(self.whoami(DEP_A)["role"], "teamlead")


# ================================================================ критерий 7: GigaCode
def run_cli_tty(server, *args, pin, timeout=30):
    """Команда app.py с настоящим терминалом (pty): PIN вводится в приглашение getpass. -> (код, вывод)."""
    env = clean_env(REG_SERVER=server.base)
    pid, fd = pty.fork()
    if pid == 0:   # дочерний процесс: терминал — slave-сторона pty
        try:
            os.chdir(ROOT)
            os.execve(sys.executable, [sys.executable, str(APP), *args], env)
        finally:
            os._exit(127)
    out, sent, end = b"", False, time.time() + timeout
    try:
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:   # EIO: процесс закрыл терминал
                break
            if not chunk:
                break
            out += chunk
            if not sent and b"PIN" in out:
                os.write(fd, (pin + "\n").encode())
                sent = True
    finally:
        os.close(fd)
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        status = 0
    return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace")


class GigaCodeTest(Base):
    def task_file(self, title, **extra):
        f = Path(self.s.dir) / f"task-{abs(hash(title))}.json"
        f.write_text(json.dumps({"title": title, "executor": EXE_A, "deadline": "2031-03-01", **extra},
                                ensure_ascii=False), "utf-8")
        return str(f)

    def find(self, title):
        return [t for t in self.s.call("GET", "/api/state")[1]["tasks"] if t["title"] == title]

    def test_ctx_shows_teams(self):
        r = run_cli(self.s, "ctx")
        self.assertEqual(r.returncode, 0, r.stderr)
        ctx = json.loads(r.stdout)
        self.assertIn(CTX_TEAMS, ctx, ctx)
        txt = json.dumps(ctx[CTX_TEAMS], ensure_ascii=False)
        for x in ("Выдуманная команда А", LEAD_A, DEP_A, LEAD_B):
            self.assertIn(x, txt)
        self.assertNotIn('"hash"', r.stdout)

    def test_create_task_by_deputy(self):
        title = "Из GigaCode от зама"
        code, out = run_cli_tty(self.s, "create-task", self.task_file(title), "--by", DEP_A, pin=PINS[DEP_A])
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        t = self.find(title)
        self.assertEqual(len(t), 1, out)
        self.assertEqual((t[0]["team"], t[0]["teamlead"]), (TA, DEP_A))

    def test_create_task_several_teams_needs_flag(self):
        title = "Из GigaCode без выбора команды"
        code, out = run_cli_tty(self.s, "create-task", self.task_file(title), "--by", MULTI, pin=PINS[MULTI])
        self.assertNotEqual(code, 0, out)
        self.assertIn(TEAM_FLAG, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.find(title), [])
        title = "Из GigaCode с выбором команды"
        code, out = run_cli_tty(self.s, "create-task", self.task_file(title), "--by", MULTI, TEAM_FLAG, TD,
                                pin=PINS[MULTI])
        self.assertEqual(code, 0, out)
        t = self.find(title)
        self.assertEqual((t[0]["team"], t[0]["teamlead"]), (TD, MULTI))

    def test_create_task_by_employee_refused(self):
        title = "Из GigaCode от сотрудника"
        code, out = run_cli_tty(self.s, "create-task", self.task_file(title), "--by", EXE_A, pin=PINS[EXE_A])
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.find(title), [])

    def test_no_tty_still_refused(self):
        """Регрессия T-002: без терминала — отказ с подсказкой про веб-форму."""
        title = "Из GigaCode без терминала T-007"
        r = run_cli(self.s, "create-task", self.task_file(title), "--by", DEP_A, detach=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("веб-форм", r.stderr + r.stdout)
        self.assertEqual(self.find(title), [])


# ================================================================ критерии 2, 3, 5: интерфейс (статически)
class StaticTest(unittest.TestCase):
    html = frontend_text()

    def test_uses_lead_teams(self):
        self.assertIn(LEAD_TEAMS, self.html)

    def test_admin_teams_section(self):
        self.assertIn("teams", self.html)
        self.assertRegex(self.html, "[Зз]ам(еститель)?")

    def test_task_team_field(self):
        self.assertRegex(self.html, r"\.team\b")

    def test_my_registry_by_team(self):
        """Критерий 5: «Мой реестр» лида и зама — задачи команды (t.team среди его команд) плюс свои как исполнителя."""
        import re
        m = re.search(r"const isTL\s*=\s*t\s*=>([^;]+);", self.html)
        self.assertTrue(m, "нет isTL")
        self.assertIn("t.team", m.group(1))
        self.assertRegex(m.group(1), r"myTeams\(\)|" + LEAD_TEAMS)


if __name__ == "__main__":
    unittest.main()
